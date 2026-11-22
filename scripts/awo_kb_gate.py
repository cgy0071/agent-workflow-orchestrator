"""Optional AWO hooks. The controller admits the request and the reviewer approves exact bytes."""
import hashlib
import json
from pathlib import Path


def _reader(con, request, worker):
    from kb_host_bridge import _config, deployment
    db = next(row[2] for row in con.execute("PRAGMA database_list") if row[1] == "main")
    state = Path(db).parent
    project = request["kb_gate"]["task"]["project"]
    def binding():
        cfg = _config(state)
        spec = cfg.get("projects", {}).get(project, {})
        rule = spec.get("tasks", {}).get(request["task_id"], {}).get("acceptance", {})
        if (cfg.get("enabled") is not True or "kb.check" not in cfg.get("read_operations", [])
                or rule.get("nonce") != request["nonce"]
                or rule.get("gate_sha256") != fingerprint(request["kb_gate"])
                or rule.get("worker_instance") != worker):
            raise PermissionError("KB acceptance binding unavailable or changed")
        return spec, cfg["artifact_digest"]
    deployed = deployment(state)
    if not deployed or "kb.check" not in deployed[1]:
        raise PermissionError("KB acceptance deployment unavailable")
    from awo_kb.service import Reader
    from awo_kb.handoff import fingerprint
    spec, digest = binding()
    if deployed[2] != digest:
        raise PermissionError("KB acceptance deployment unavailable")
    vault = Path(spec["vault"])
    if not vault.is_absolute():
        raise ValueError("absolute host vault required")
    def current_policy():
        current, current_digest = binding()
        if current_digest != digest or Path(current["vault"]).resolve() != vault.resolve():
            raise PermissionError("KB acceptance vault or deployment changed")
        return current["policy"]
    return Reader(vault, current_policy)


def _artifact(request, name):
    base = Path(request["cwd"]).resolve()
    path = (base / name).resolve()
    path.relative_to(base)
    return path


def admit(con, request, worker):
    if "kb_gate" not in request:
        from kb_host_bridge import _config
        db = next(row[2] for row in con.execute("PRAGMA database_list") if row[1] == "main")
        for project in _config(Path(db).parent).get("projects", {}).values():
            if "acceptance" in project.get("tasks", {}).get(request["task_id"], {}):
                raise PermissionError("protected task requires its frozen KB gate")
        return
    gate = request["kb_gate"]
    if not isinstance(gate, dict) or set(gate) != {"task", "claim_slots", "producer", "artifact"}:
        raise ValueError("invalid frozen KB gate")
    names = [item if isinstance(item, str) else item["path"] for item in request["completion"]["artifacts"]]
    if gate["artifact"] not in names:
        raise ValueError("KB delivery must be a declared completion artifact")
    _artifact(request, gate["artifact"])
    producer = gate["producer"]
    if not isinstance(producer, dict) or set(producer) != {"runtime", "model", "prompt_rev"} or not all(
            isinstance(v, str) and v.strip() for v in producer.values()):
        raise ValueError("invalid controller producer declaration")
    slots = gate["claim_slots"]
    if not isinstance(slots, list) or not all(isinstance(s, str) and s.strip() for s in slots) or len(slots) != len(set(slots)):
        raise ValueError("invalid frozen claim slots")
    _reader(con, request, worker["instance_id"]).packet(worker["instance_id"], gate["task"])


class RevisionNeeded(ValueError):
    """Report known categories only; field paths may contain untrusted text."""


def check(con, data, event):
    request = data["request"]
    gate = request["kb_gate"]
    receipt = event.get("receipt")
    if (not receipt or (receipt.get("task_id"), receipt.get("nonce")) !=
            (request["task_id"], request["nonce"]) or event["report"]["status"] != "review_ready"):
        raise ValueError("missing KB completion receipt")
    expected = request["completion"]["artifacts"] + [request["completion"]["binding_artifact"]]
    observed = receipt["artifacts"] + [receipt["binding_artifact"]]
    if len(expected) != len(observed):
        raise ValueError("incomplete KB receipt")
    artifact = _artifact(request, gate["artifact"])
    evidence = {}
    for declaration, item in zip(expected, observed):
        path = _artifact(request, declaration if isinstance(declaration, str) else declaration["path"])
        if Path(item["path"]).resolve() != path or str(path) in evidence:
            raise ValueError("KB receipt path differs from frozen task")
        raw = path.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        if sha != item["sha256"]:
            raise ValueError("KB evidence changed")
        evidence[str(path)] = sha
        if path == artifact:
            delivery = json.loads(raw)
    worker = data["worker_binding"]
    reader = _reader(con, request, worker["instance_id"])
    from awo_kb.handoff import fingerprint, pre_review
    checked = pre_review(reader, worker["instance_id"], gate["task"], delivery,
                         gate["producer"], claim_slots=gate["claim_slots"])
    if not checked["eligible_for_review"] or [row["claim"] for row in delivery["judgments"]] != gate["claim_slots"]:
        kinds = {f.get("category") for f in checked["check"]["findings"]}
        causes = sorted(k for k in kinds if k in {"unsourced", "dangling", "violation"})
        raise RevisionNeeded("KB checking blocked delivery: " + (", ".join(causes) or "frozen claim slots differ"))
    result = {"event_id": event["event_id"], "request_sha256": event["request_sha256"],
              "worker": [worker["instance_id"], worker["version"]], "evidence": evidence, "check": checked}
    return {"sha256": fingerprint(result), "result": result}


def screen(con, request, report, receipt):
    """Classify a proposed handoff; never insert a review, advance or release work."""
    if report["status"] != "review_ready" or "kb_gate" not in request:
        return report
    from workflow_control import digest
    row = con.execute("SELECT data,request_hash FROM tasks WHERE id=? AND nonce=?",
                      (request["task_id"], request["nonce"])).fetchone()
    frozen = digest(request)
    if not row or row[1] != frozen:
        raise ValueError("KB screening requires a registered frozen request")
    event = {"request_sha256": frozen, "event_id": digest({"request": frozen, "status": "review_ready"}),
             "receipt": receipt, "report": report}
    try:
        checked = check(con, json.loads(row[0]), event)
    except (OSError, ValueError, TypeError, KeyError, IndexError, ImportError, RuntimeError) as exc:
        # Keep the repair inbox actionable without copying private KB text or paths.
        if isinstance(exc, RevisionNeeded):
            reason = str(exc)
        elif isinstance(exc, PermissionError):
            reason = "KB grant or deployment unavailable"
        elif isinstance(exc, FileNotFoundError) or not receipt:
            reason = "missing KB completion receipt or evidence"
        else:
            reason = "KB checking blocked delivery: revise claims, references or receipt evidence"
        return dict(report, status="kb_revision_required", anomaly_summary=reason,
                    kb_screen={"outcome": "revise_or_supply_evidence", "reason": reason})
    return dict(report, kb_screen={"outcome": "ready_for_independent_review", "sha256": checked["sha256"]})


def seal(con, task_data, event, text, result):
    if result["verdict"] != "accept" or "kb_gate" not in task_data["request"]:
        return
    checked = check(con, task_data, event)
    approvals = [line for line in text.splitlines() if line.startswith("KB-Check-SHA256:")]
    if approvals != ["KB-Check-SHA256: " + checked["sha256"]]:
        raise ValueError("review must approve this KB check digest")
    result["kb_check"] = checked


def _approval_binding(result):
    # Findings are observations at check time. check() rejects a blocked result
    # before it can be returned for comparison. Retain all other signed fields.
    checked = result["check"]
    report = dict(checked["check"])
    del report["findings"]
    return dict(result, check=dict(checked, check=report))


def approved(con, task, event, review):
    data = json.loads(con.execute("SELECT data FROM tasks WHERE id=?", (task,)).fetchone()[0])
    if "kb_gate" not in data["request"]:
        return
    current = check(con, data, event)
    from awo_kb.handoff import fingerprint
    signed = review.get("kb_check")
    if not isinstance(signed, dict) or not isinstance(signed.get("result"), dict):
        raise ValueError("missing KB review approval")
    if signed.get("sha256") != fingerprint(signed["result"]):
        raise ValueError("invalid signed KB report")
    raw = Path(review["path"]).read_bytes()
    approvals = [line for line in raw.decode("utf-8-sig").splitlines() if line.startswith("KB-Check-SHA256:")]
    if (hashlib.sha256(raw).hexdigest() != review["sha256"]
            or approvals != ["KB-Check-SHA256: " + signed["sha256"]]):
        raise ValueError("KB report differs from reviewer approval")
    if _approval_binding(signed["result"]) != _approval_binding(current["result"]):
        raise ValueError("missing or outdated KB review approval")


def preview(state, event_id, actor, version, context):
    from workflow_control import database, guard, check_self_review
    with database(state) as con:
        who = guard(con, actor, version, context, "review")
        event = con.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if not event or event["role"] != who["role"]:
            raise ValueError("event belongs to another reviewer role")
        check_self_review(con, who, event["task"])
        data = json.loads(con.execute("SELECT data FROM tasks WHERE id=?", (event["task"],)).fetchone()[0])
        delivered = json.loads(event["data"])
        if delivered["report"]["status"] == "kb_revision_required":
            raise ValueError(delivered["report"]["kb_screen"]["reason"])
        return check(con, data, delivered)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Preview a registered KB delivery for its bound reviewer")
    for name in ("state", "event", "actor"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--version", required=True, type=int)
    parser.add_argument("--context", required=True, choices=["main", "side", "unknown"])
    args = parser.parse_args()
    print(json.dumps(preview(args.state, args.event, args.actor, args.version, args.context),
                     ensure_ascii=True, indent=2))
