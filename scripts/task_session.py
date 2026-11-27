"""Local executor session lookup. No prompt, terminal input or workflow actions."""
from pathlib import Path
import json
import os
import sqlite3
import subprocess
import uuid

import native_worker
from workflow_evidence import task_key


def binding(plan):
    """Only a finished persistent job, in the current executor binding, owns a conversation."""
    import project_entry as entry
    d = plan["derived"]
    spec = plan["spec"]["worker"]
    if spec.get("session_mode") != "session" or spec["harness"] != "codex" or "evidence_route" in plan["spec"]:
        raise ValueError("one_shot")
    root = Path(d["paths"]["worker"])
    cfg = native_worker.read_json(root / "config.json")
    active = native_worker.read_json(root / "active.json")
    jobdir = root / "jobs" / task_key(dict(task_id=d["attempt_id"], nonce=d["nonce"]))
    job = native_worker.read_json(jobdir / "job.json")
    outcome = native_worker.read_json(jobdir / "outcome.json")
    receipt = native_worker.read_json(jobdir / "receipt" / "status.json")
    session = str(uuid.UUID(outcome["native_session_id"]))
    row = entry._one(Path(d["paths"]["state"]),
                     "SELECT id,version FROM bindings WHERE role='executor' AND status='active'")
    expected = d["worker_spec"]["expected_identity"]
    if not (row == {"id": d["worker"]["instance_id"], "version": d["worker"]["version"]}
            and cfg["identity"] == job["identity"] == expected
            and cfg["mode"] == outcome["execution_semantics"] == "session"
            and active["task_id"] == job["task_id"] == d["attempt_id"]
            and active["nonce"] == job["nonce"] == d["nonce"]
            and Path(active["directory"]).resolve() == jobdir.resolve()
            and session == cfg["native_session_id"] == receipt["native_turn"]["native_session_id"]
            and receipt["status"] == outcome["status"] == "done"
            and Path(receipt["execution_cwd"]).resolve() == Path(d["paths"]["workspace"]).resolve()):
        raise ValueError("binding_mismatch")
    return dict(project=d["project_id"], task=d["task_id"], attempt=d["attempt_id"],
                nonce=d["nonce"], role="executor", instance=row["id"], session=session)


def host(action, identity):
    """Optional trusted local UI broker. Compare and act in one host operation."""
    binary = Path(os.environ.get("AWO_SESSION_HOST", ""))
    if not binary.is_absolute() or not binary.is_file():
        return {}
    p = subprocess.run([str(binary), action], input=json.dumps(identity).encode("ascii"),
                       capture_output=True, timeout=5, check=True)
    reply = json.loads(p.stdout)
    return reply if isinstance(reply, dict) and reply.get("binding") == identity else {}


def access(plan, action="details", adapter=None):
    if action not in ("details", "records", "focus", "resume"):
        raise ValueError("Unknown session operation")
    d = plan["derived"]
    info = dict(records=True, focus=False, resume=False, adapter=plan["spec"]["worker"]["harness"])
    if action == "records":
        return dict(info, delivery=d["paths"]["delivery"], workspace=d["paths"]["workspace"])
    call = adapter or host
    try:
        identity = binding(plan)
        reply = call("inspect", identity)
        if reply.get("binding") == identity:
            info.update(focus=reply.get("focus") is True, resume=reply.get("resume") is True)
        if action != "details":
            if not info[action]:
                raise ValueError("unavailable")
            # Broker must conditionally act on the *current* occupant, not a cached pane ID.
            result = call(action, identity)
            if result.get("binding") != identity or result.get("opened") is not True:
                raise ValueError("occupant_changed")
            info["opened"] = True
    except (OSError, KeyError, TypeError, ValueError, sqlite3.Error, subprocess.SubprocessError):
        info.update(focus=False, resume=False, reason="records_only")
        if action != "details":
            raise ValueError("Session unavailable or binding changed; view delivery and workspace instead.")
    return info
