"""Read-only task phases, next actors, and observation age; never native heartbeats."""
from datetime import datetime, timezone
import json
import math


def summarize(state, task=None, stale_after=900, now=None):
    from workflow_scheduler import snapshot
    if type(stale_after) not in (int, float) or not math.isfinite(stale_after) or stale_after <= 0:
        raise ValueError("stale_after must be positive")
    now = datetime.now(timezone.utc) if now is None else now
    raw = snapshot(state)
    rows = {}
    for item in raw["tasks"]:
        dispatch = (item.get("dispatch") or {}).get("status")
        observation = item.get("last_observation") or {}
        reports = [event["data"]["report"]["status"] for event in item["events"]]
        decisions = [review["verdict"] for review in item["reviews"]]
        warnings = []
        age = None
        if observation.get("observed_at"):
            try:
                at = datetime.fromisoformat(observation["observed_at"].replace("Z", "+00:00"))
                if at.tzinfo is None: at = at.replace(tzinfo=timezone.utc)
                age = max(0, (now - at).total_seconds())
                if age > stale_after: warnings.append("observation_stale")
            except (TypeError, ValueError):
                warnings.append("invalid_observation_timestamp")
        else:
            warnings.append("no_monitor_observation")
        controller, reviewer = item["controller_role"], item["reviewer_role"]
        worker = item["worker"]["role"]
        if item["acceptance_valid"]:
            phase, role, action = "accepted", controller, "release_claim_or_advance"
        elif "accept" in decisions:
            phase, role, action = "evidence_changed", controller, "inspect_changed_evidence"
        elif any(value in ("reject", "repair") for value in decisions):
            phase, role, action = "review_blocked", controller, "plan_new_scoped_task"
        elif item["execution"] == "resolved":
            phase, role, action = "closed", controller, "retain_evidence"
        elif dispatch in ("reserved", "uncertain", "submitted"):
            phase, role, action = "execution_unconfirmed", controller, "inspect_execution_no_resend"
        elif "review_ready" in reports:
            phase, role, action = "awaiting_review", reviewer, "acknowledge_and_review"
        elif observation.get("status") in ("failed", "exited") or any(value in ("failed", "exited") for value in reports):
            phase, role, action = "failed", controller, "inspect_termination"
        elif observation.get("status") in ("adapter_error", "monitor_timeout", "incomplete", "identity_changed", "unknown_status", "monitoring_stopped"):
            phase, role, action = "needs_inspection", controller, "inspect_monitor_and_worker"
        else:
            phase, role, action = "executing", worker, "await_delivery"
        uncertain = [note for note in item["notifications"] if note.get("status") in ("reserved_no_blind_retry", "delivery_uncertain")]
        if uncertain:
            warnings.append("notification_uncertain_no_resend")
            if phase == "awaiting_review": role, action = controller, "inspect_notification_and_inbox"
        spec = item["worker"]["spec"]
        rows[item["task_id"]] = {"task_id": item["task_id"], "phase": phase, "worker_role": worker,
                                "harness": spec.get("harness", "unknown"), "adapter": spec["adapter"], "identity": spec["expected_identity"],
                                "next_role": role, "next_action": action, "observation_age_seconds": age,
                                "worker_heartbeat": "unavailable", "warnings": warnings,
                                "claim_held": item.get("claim_held", False), "dispatch": dispatch,
                                "review_verdicts": decisions}
    for run, dag in raw["dags"].items():
        for node, value in dag["nodes"].items():
            plan = dag["tasks"][node]
            task_id = plan["task_id"]
            if task_id not in rows:
                block = raw["scheduling_blocks"].get(task_id)
                phase = "scheduling_blocked" if block else value
                action = "resolve_scheduling_block" if block else ("tick" if value == "ready" else "inspect_dependencies")
                rows[task_id] = {"task_id": task_id, "phase": phase, "worker_role": plan["worker_role"],
                                 "harness": plan["harness"], "adapter": plan["adapter"], "identity": plan["identity"],
                                 "next_role": plan["controller_role"], "next_action": action,
                                 "observation_age_seconds": None, "worker_heartbeat": "unavailable", "warnings": [],
                                 "claim_held": False, "dispatch": None, "review_verdicts": []}
            rows[task_id].update(run=run, node=node, dag_state=value, dependencies=dag["dependencies"][node])
            if task_id in raw["scheduling_blocks"]:
                rows[task_id]["blocking_reason"] = raw["scheduling_blocks"][task_id]["reason"]
    if task and task not in rows:
        raise ValueError("unknown task")
    selected = [row for key, row in sorted(rows.items()) if task is None or key == task]
    return {"read_only": True, "tasks": selected, "resource_capacities": raw["resources"],
            "claims": raw["claims"], "alerts": [{"task_id": row["task_id"], "warnings": row["warnings"]}
                                               for row in selected if row["warnings"]]}


def dashboard_details(state, summary=None):
    """Additional ledger details for the local UI, without writes or adapter calls."""
    from workflow_scheduler import _read_database, has_schema, snapshot
    summary = summarize(state) if summary is None else summary
    raw = snapshot(state)
    phases = {row["task_id"]: row["phase"] for row in summary["tasks"]}
    with _read_database(state) as con:
        config = json.loads(con.execute("SELECT value FROM meta WHERE key='config'").fetchone()[0])
        roles = {role: {"role": role, "bindings": [], "tasks": []} for role in config["roles"]}
        for row in con.execute("SELECT role,status,data FROM bindings WHERE status IN ('active','pending') "
                               "ORDER BY role,version"):
            data = json.loads(row["data"])
            spec = data["spec"]
            roles.setdefault(row["role"], {"role": row["role"], "bindings": [], "tasks": []})["bindings"].append({
                "status": row["status"], "mode": data["mode"], "harness": spec.get("harness", "unknown"),
                "adapter": spec["adapter"], "version": data["version"],
                "context_policy": data["context_policy"], "instance_id": data["instance_id"][:8],
                "bound_at": data.get("created_at"),
            })
        inbox = []
        for row in con.execute("SELECT id,task,role,status,data FROM events ORDER BY rowid DESC"):
            data = json.loads(row["data"])
            inbox.append({"event_id": row["id"][:12], "event": row["id"], "role": row["role"],
                          "task_id": row["task"], "status": row["status"],
                          "created_at": data.get("created_at"),
                          "report_status": (data.get("report") or {}).get("status")})
        for row in con.execute("SELECT id,worker,status,data FROM tasks WHERE status!='resolved' ORDER BY id"):
            data = json.loads(row["data"])
            for role in {row["worker"], data["controller"]["role"], data["request"]["reviewer"]["role"]}:
                if role in roles:
                    roles[role]["tasks"].append({"task_id": row["id"], "phase": phases.get(row["id"], row["status"])})
        cwd_by_task = {row["id"]: json.loads(row["data"])["request"]["cwd"]
                       for row in con.execute("SELECT id,data FROM tasks")}
        if has_schema(con):
            for row in con.execute("SELECT data FROM dag_runs"):
                for node in json.loads(row["data"])["graph"]["nodes"]:
                    cwd_by_task.setdefault(node["request"]["task_id"], node["request"]["cwd"])
    task_details = {}
    for item in raw["tasks"]:
        task_details[item["task_id"]] = {"cwd": item["cwd"], "journal": item.get("journal", [])[-10:]}
    for dag in raw["dags"].values():
        for name, plan in dag["tasks"].items():
            task_details.setdefault(plan["task_id"], {"cwd": cwd_by_task.get(plan["task_id"]), "journal": []})
    return {"goal": config.get("goal", ""), "roles": list(roles.values()), "inbox": inbox,
            "verdicts": sorted(config.get("workflow", {}).get("transitions", {})),
            "dags": raw["dags"], "scheduling_blocks": raw["scheduling_blocks"], "task_details": task_details}


def format_text(summary):
    lines = ["TASK | PHASE | HARNESS/ADAPTER/SESSION | NEXT ROLE/ACTION | OBSERVATION AGE"]
    for row in summary["tasks"]:
        identity = row["identity"]
        session = next((identity[key] for key in ("terminal", "session_id", "instance_id", "job_id") if identity.get(key)), "unknown")
        age = "unavailable" if row["observation_age_seconds"] is None else str(round(row["observation_age_seconds"])) + "s"
        lines.append(" | ".join((row["task_id"], row["phase"], row["harness"] + "/" + row["adapter"] + "/" + session,
                                  row["next_role"] + "/" + row["next_action"], age)))
        if row.get("run"):
            lines.append("  dag: " + row["run"] + "/" + row["node"] + " needs: " + (", ".join(row["dependencies"]) or "none"))
        lines.append("  dispatch: " + (row["dispatch"] or "none") + "; claim: " + ("held" if row["claim_held"] else "none") +
                     "; reviews: " + (", ".join(row["review_verdicts"]) or "none"))
        if row.get("blocking_reason"): lines.append("  reason: " + row["blocking_reason"])
        if row["warnings"]: lines.append("  warnings: " + ", ".join(row["warnings"]))
    capacities = summary["resource_capacities"]
    if capacities:
        used = {key: sum(claim["resources"].get(key, 0) for claim in summary["claims"]) for key in capacities}
        lines.append("Resources: " + ", ".join(key + " " + str(used[key]) + "/" + str(capacities[key]) for key in sorted(capacities)))
    for claim in summary["claims"]:
        held = ", ".join(key + "=" + str(value) for key, value in sorted(claim["resources"].items())) or "no resources"
        lines.append("Claim: " + claim["task_id"] + " (" + held + "; " + str(len(claim["workspaces"])) + " workspace paths)")
    lines.append("Worker heartbeat: unavailable. Observation age does not establish worker failure.")
    return "\n".join(lines)
