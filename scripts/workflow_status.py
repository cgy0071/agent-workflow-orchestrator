"""Read-only task phases, next actors, and observation age; never native heartbeats."""
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re

SUMMARY_READ = 65536  # bytes of a taskbook read for its title and opening lines
# Routing lines repeat what the ledger already shows; skip them when summarizing.
ROUTING = re.compile(r"(task id|nonce|cwd|assigned directory|completion marker|status|owner authorization|roles?|"
                     r"date|\u65e5\u671f)[^:\uff1a]{0,60}[:\uff1a]", re.I)
# Identifiers that mean nothing to a reader: digests/nonces (also shortened "b61826b0..."), and absolute paths.
NOISE = [re.compile(p) for p in (r"`?(?:\b[a-z]+_)?(?<![0-9A-Za-z])(?=[0-9a-fA-F]*\d)(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{7,}"
                                 r"(?![0-9A-Za-z])(?:\u2026|\.\.\.)?`?",
                                 r"`?[A-Za-z]:[\\/][^\s,;`'\")\uff0c\uff1b]*`?",
                                 r"`?(?<![\w.])/(?:[\w.-]+/){2,}[\w.-]*`?")]
FILLER = re.compile(r"(?:,?\s*(?:nonce|worktree|assigned worktree|working directory|cwd)\s*(?=[,.;]|$))+", re.I)


def _plain(text, task_id):
    """Drop the task ID, digests, paths and markup from a sentence; keep what the task is about."""
    text = re.sub(r"\b" + re.escape(task_id) + r"\b", "", text).replace("**", "").replace("`", "")
    for pattern in NOISE:
        text = pattern.sub("", text)
    text = FILLER.sub("", text)
    text = re.sub(r"\(\s*\)|\[\s*\]|\uff08\s*\uff09", "", text)
    text = re.sub(r"\s*/\s*(?=[/,;\uff0c\uff1b\u3001]|$)|(?<=[/\u3001])\s*/", "", text)  # separators left by removals
    text = re.sub(r"\s+([,.;:\uff0c\uff1b])", r"\1", re.sub(r"\s{2,}", " ", text))
    text = re.sub(r"([,;\uff0c\uff1b\u3001])\s*[,;\uff0c\uff1b\u3001]+", r"\1", text)
    return re.sub(r"^[\s,.;:/\uff0c\uff1b\uff1a\u3001\u2014\u2013-]+|[\s,;:/\uff0c\uff1b\uff1a\u3001\u2014\u2013-]+$", "", text)


def _delivery_folder(request):
    # A task spec often sits beside its deliverables, e.g. history/TASK-ID/TASK.md next to HANDOFF.md.
    binding = request.get("completion", {}).get("binding_artifact")
    binding = binding if isinstance(binding, str) else (binding or {}).get("path")
    if not binding:
        return None
    path = Path(binding)
    return str((path if path.is_absolute() else Path(request["cwd"]) / path).parent)


def _journal_entry(item):
    """Time, event and outcome only; producer hashes and raw payloads stay in the ledger."""
    data = item.get("data") or {}
    return {"at": item.get("at"), "kind": item.get("kind"), "outcome": data.get("outcome") if isinstance(data, dict) else None}


def task_summary(task_id, cwd, messages, delivery_folder=None):
    """Title and a short excerpt of what a task asks for; never raises."""
    candidates = [Path(m) for m in messages[:1] if m]
    for folder in (delivery_folder, cwd):
        if folder:
            candidates += [Path(folder) / "TASK.md", Path(folder) / "task.md"]
    texts = []
    for path in candidates:
        try:
            if path.is_file():
                with path.open("rb") as stream:
                    texts.append(stream.read(SUMMARY_READ).decode("utf-8", errors="replace"))
                break
        except OSError:
            continue
    texts += [m for m in messages[1:] if isinstance(m, str)]
    for text in texts:
        if text.lstrip().startswith(("{", "[")):
            continue  # machine-readable message; not a human description
        lines = [line.strip().lstrip("#").strip() for line in text.splitlines()]
        lines = [line for line in lines if line and not ROUTING.match(line) and not set(line) <= set("-=|*`")]
        if len(lines) == 1:
            # A one-paragraph dispatch message: its first sentence is the title.
            lines = [part for part in re.split(r"(?<=[.!?\u3002\uff01\uff1f])\s+", lines[0]) if part]
        title, body = "", []
        for line in lines:
            line = _plain(line, task_id)
            if not line:
                continue
            if not title:
                title = line
            else:
                body.append(line)
            if sum(map(len, body)) > 240:
                break
        if title:
            excerpt = " ".join(body)
            return {"title": title[:120], "excerpt": excerpt[:240] + ("..." if len(excerpt) > 240 else "")}
    return {"title": "", "excerpt": ""}


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
        elif observation.get("status") == "incomplete":
            phase, role, action = "needs_inspection", controller, "continue_or_close"
        elif observation.get("status") in ("adapter_error", "monitor_timeout", "blocked", "identity_changed", "unknown_status", "monitoring_stopped"):
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
                "pane": spec.get("pane") if spec["adapter"] == "herdr" else None,
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
        cwd_by_task, sources, parents, beside = {}, {}, {}, {}
        for row in con.execute("SELECT id,data FROM tasks"):
            request = json.loads(row["data"])["request"]
            cwd_by_task[row["id"]] = request["cwd"]
            sources[row["id"]] = [request.get("taskbook_path")]
            parents[row["id"]] = request.get("parent_task")
            beside[row["id"]] = _delivery_folder(request)
        if has_schema(con):
            for row in con.execute("SELECT task,data FROM dispatches"):
                sources.setdefault(row["task"], []).append(json.loads(row["data"]).get("message"))
            for row in con.execute("SELECT data FROM dag_runs"):
                for node in json.loads(row["data"])["graph"]["nodes"]:
                    cwd_by_task.setdefault(node["request"]["task_id"], node["request"]["cwd"])
                    sources.setdefault(node["request"]["task_id"], []).append(node.get("message"))
    task_details = {}
    for item in raw["tasks"]:
        task_details[item["task_id"]] = {"cwd": item["cwd"], "journal": [_journal_entry(j) for j in item.get("journal", [])[-10:]],
                                         "parent_task": parents.get(item["task_id"])}
    for dag in raw["dags"].values():
        for name, plan in dag["tasks"].items():
            task_details.setdefault(plan["task_id"], {"cwd": cwd_by_task.get(plan["task_id"]), "journal": []})
    for task_id, detail in task_details.items():
        detail.update(task_summary(task_id, detail.get("cwd"), sources.get(task_id, []), beside.get(task_id)))
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
