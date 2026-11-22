"""Recover B03 durable writes using the host's existing v2_command_log row."""
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

import awo_service as service
from awo_kb import editor, store, views


QUEUE_VIEWS = frozenset(("kb.candidate.list", "kb.candidate.todo"))
OPERATIONS = QUEUE_VIEWS | frozenset(("kb.candidate.propose", "kb.candidate.approve",
                        "kb.candidate.reject", "kb.maintenance.decide"))
FIELDS = {"kb.candidate.propose": {"record"},
          "kb.candidate.approve": {"candidate_id", "approved_sha256"},
          "kb.candidate.reject": {"candidate_id", "approved_sha256", "reason"},
          "kb.maintenance.decide": {"record_id", "action", "reason"}}
FIELDS.update({op: set() for op in QUEUE_VIEWS})


@dataclass
class Binding:
    project: str
    actor: str
    task: dict
    reader: object
    plugin: object
    deployment_digest: str


def _authorized(con, env, bind):
    binding = bind(con, env)
    state = service._state_row(con)
    if (state["controller_epoch"] != env["expected_epoch"] or state["frozen"]
            or not state["v2_enabled"]):
        raise ValueError("KB write epoch, freeze or enable check failed")
    if (state["project_id"] != binding.project or binding.task["project"] != binding.project
            or not binding.plugin.health_check()):
        raise PermissionError("KB project or deployment is unavailable")
    binding.reader._args(binding.actor, binding.task, None)
    if env["tool"] != "kb.maintenance.decide":
        kind = editor._writer(binding.reader.current_policy(), binding.actor, binding.project)
        if env["tool"] in {"kb.candidate.approve", "kb.candidate.reject"} and kind != "human":
            raise PermissionError("candidate decision requires human write authority")
    else:
        fields = env["payload"]["fields"]
        records, projects, profiles, _ = binding.reader._args(binding.actor, binding.task, None)
        target = views._visible(records, projects, profiles).get(fields["record_id"])
        if not target or editor._writer(binding.reader.current_policy(), binding.actor,
                                        target["project"]) != "human":
            raise PermissionError("maintenance decision requires current human write authority")
    return binding


def _scope(b):
    return service._sha([b.project, b.actor, b.task, str(Path(b.reader.root).resolve()),
                         b.deployment_digest])


def _effect(b, op, fields):
    """Inspect an immutable decision or re-enter an idempotent candidate mutation."""
    if op != "kb.maintenance.decide":
        return b.plugin.invoke(op, b.reader, b.actor, b.task, **fields)
    before, projects, profiles, _ = b.reader._args(b.actor, b.task, None)
    target = views._visible(before, projects, profiles).get(fields["record_id"])
    if not target or editor._writer(b.reader.current_policy(), b.actor, target["project"]) != "human":
        raise PermissionError("maintenance decision requires current human write authority")
    parent = b.task["kb_revision"]
    history = store.history(b.reader.root)
    if len(history) == len(before):
        return b.plugin.invoke(op, b.reader, b.actor, b.task, **fields)
    first = history[len(before)]
    if (first["type"] != "DECISION" or first["target"] != fields["record_id"]
            or first["action"] != fields["action"] or first["reason"] != fields["reason"]
            or first["actor"] != b.actor or first["project"] != target["project"]
            or first["profiles"] != target["profiles"]):
        raise ValueError("a different revision occupies the expected decision position")
    content = {k: v for k, v in first.items() if k not in ("amendment", "id")}
    if first["id"] != "DEC-" + editor.digest([parent, content]):
        raise ValueError("decision identity mismatch")
    return hashlib.sha256(parent.encode("ascii") + b"\n" + store._markdown(first)).hexdigest()


def execute(state, envelope, bind, *, checkpoint=None):
    """bind(con, envelope) reauthenticates and returns a host-owned Binding each time.

    envelope.payload = {task_id, fields}. The caller validates operation fields,
    and bind enforces tool, task and principal scope on every transaction.
    """
    env = service._command_envelope(envelope, OPERATIONS)
    if set(env["payload"]) != {"task_id", "fields"} or not isinstance(env["payload"]["fields"], dict):
        raise ValueError("expected task_id and fields")
    sha = service._request_sha(env)
    op = env["tool"]
    if set(env["payload"]["fields"]) != FIELDS[op]:
        raise ValueError("invalid KB command fields")
    with closing(service._connect(service._db_path(state))) as con:
        con.execute("BEGIN IMMEDIATE")
        b = _authorized(con, env, bind)
        key = (b.project, b.actor, env["command_id"])
        scope = _scope(b)
        row = service._replay(con, *key[:2], env, sha)
        if row is None:
            if op == "kb.maintenance.decide" and store.head(b.reader.root) != b.task["kb_revision"]:
                raise ValueError("stale expected head")
            intent = {"kb_write_state": "intent", "scope_sha256": scope}
            service._record(con, *key[:2], env, sha, intent)
            service._event(con, b.project, "command", key[2], "kb.write.intent",
                           b.actor, "kb-host", None, {"operation": op, "request_sha256": sha})
        elif row.get("scope_sha256") != scope:
            raise PermissionError("KB binding changed")
        con.commit()
        if checkpoint:
            checkpoint("intent")
        con.execute("BEGIN IMMEDIATE")
        b = _authorized(con, env, bind)
        if _scope(b) != scope:
            raise PermissionError("KB binding changed")
        row = service._replay(con, *key[:2], env, sha)
        if row is None or row.get("scope_sha256") != scope:
            raise ValueError("KB intent unavailable")
        if row["kb_write_state"] == "done":
            if op in QUEUE_VIEWS:
                # A fresh authorized view avoids replaying stale or withdrawn queue contents.
                result = _effect(b, op, env["payload"]["fields"])
                return {"result": result, "replayed": True}
            return {"result": row["result"], "replayed": True}
        if row["kb_write_state"] != "intent":
            raise ValueError("unknown KB command state")
        track_creation = False
        if op == "kb.candidate.propose" and b.reader.candidate_sessions is not None:
            from awo_kb import candidates
            candidate = editor.digest({"record": env["payload"]["fields"]["record"], "proposer": b.actor})
            track_creation = not (candidates._folder(b.reader.root) / (candidate + ".json")).exists()
        result = _effect(b, op, env["payload"]["fields"])
        if checkpoint:
            checkpoint("effect")
        receipt = {"kb_write_state": "done", "scope_sha256": scope}
        if op not in QUEUE_VIEWS:
            receipt["result"] = result
        con.execute("UPDATE v2_command_log SET response_json=? "
                    "WHERE project_id=? AND principal=? AND command_id=?",
                    (json.dumps(receipt), *key))
        service._event(con, b.project, "command", key[2], "kb.write.confirmed",
                       b.actor, "kb-host", None, {"operation": op, "request_sha256": sha})
        if track_creation and candidate == result:
            service._event(con, b.project, "candidate", result, "kb.candidate.created", b.actor,
                           "kb-host", None, {"vault": str(Path(b.reader.root).resolve())})
        con.commit()
        return {"result": result, "replayed": False}
