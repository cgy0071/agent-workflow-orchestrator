"""List and resolve a delivery's KB proposals in the existing review ledger."""
import argparse
import hashlib
import json
from pathlib import Path

import awo_kb_gate as gate
import workflow_control as control


def apply(state, event_id, actor, version, context, *, action_id=None, outcome=None, reason=None):
    """An independent, authorized human can resolve a proposal once, without editing the KB."""
    from awo_kb.editor import _writer
    from awo_kb.handoff import fingerprint

    with control.database(state) as con:
        who = control.guard(con, actor, version, context, "review")
        event = con.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if not event or event["role"] != who["role"]:
            raise PermissionError("not this reviewer's event")
        control.check_self_review(con, who, event["task"])
        data = json.loads(con.execute("SELECT data FROM tasks WHERE id=?", (event["task"],)).fetchone()[0])
        request = data["request"]
        worker = data["worker_binding"]["instance_id"]
        task = request["kb_gate"]["task"]
        reader = gate._reader(con, request, worker)
        reader._args(actor, task, None)
        report = gate.check(con, data, json.loads(event["data"]))
        actions = report["result"]["check"]["check"].get("actions", [])
        # Repeat the protected check as this reviewer. The producer's broader
        # access cannot be used to disclose evidence to another principal.
        artifact = gate._artifact(request, request["kb_gate"]["artifact"])
        raw = artifact.read_bytes()
        evidence_sha = hashlib.sha256(raw).hexdigest()
        if report["result"]["evidence"].get(str(artifact)) != evidence_sha:
            raise ValueError("delivery changed during adjudication")
        delivery = json.loads(raw)
        reviewer_check = reader.check(actor, task, delivery)
        if reviewer_check["blocked"]:
            raise PermissionError("reviewer cannot verify the complete delivery")
        reviewer_actions = reviewer_check.get("actions", [])
        allowed = {a["id"] for a in reviewer_actions}
        visible = {a["id"]: a for a in actions if a["id"] in allowed}
        decisions = {}
        for row in con.execute("SELECT data FROM audit WHERE action=? ORDER BY seq", ("kb_action_decided",)):
            entry = json.loads(row[0])
            if entry["event_id"] == event_id and entry["action_id"] in visible:
                decisions[entry["action_id"]] = entry
        if action_id is None:
            if outcome is not None or reason is not None:
                raise ValueError("a decision requires an action ID")
            return [{"action": value, "decision": decisions.get(key)} for key, value in visible.items()]
        if _writer(reader.current_policy(), actor, task["project"]) != "human":
            raise PermissionError("adjudication requires human write authority")
        if action_id not in visible or outcome not in ("uphold", "dismiss") or not isinstance(reason, str) or not reason.strip():
            raise ValueError("expected a visible proposal, an outcome and a reason")
        proposal = visible[action_id]
        if fingerprint({k: v for k, v in proposal.items() if k != "id"}) != action_id:
            raise ValueError("invalid proposal identity")
        entry = dict(event_id=event_id, action_id=action_id, actor=actor, version=version,
                     outcome=outcome, reason=reason, proposal=proposal, delivery_file_sha256=evidence_sha)
        if action_id in decisions:
            if entry != decisions[action_id]:
                raise ValueError("proposal was already resolved")
        else:
            control.audit(con, "kb_action_decided", entry)
        return entry


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    for key in ("event", "actor", "context"):
        parser.add_argument("--" + key, required=True)
    parser.add_argument("--version", type=int, required=True)
    for key in ("action-id", "outcome", "reason"):
        parser.add_argument("--" + key)
    args = parser.parse_args()
    print(json.dumps(apply(args.state, args.event, args.actor, args.version, args.context,
                           action_id=args.action_id, outcome=args.outcome, reason=args.reason),
                     ensure_ascii=True, indent=2))
