"""Attempt-scoped interruption diagnosis and writer transfer; local controller only."""
import hashlib
import json
import re
from pathlib import Path

import project_entry as e
import task_session


def _root(plan):
    return Path(plan["derived"]["paths"]["root"])


def _marker(plan):
    return _root(plan) / "handoff" / "reservation.json"


def check_writer(plan):
    if _marker(plan).exists():
        raise e.EntryError("handoff", "Previous writer retired; open the handoff receipt.", "writer_retired")


def _job(plan):
    d = plan["derived"]
    return Path(d["paths"]["worker"]) / "jobs" / e.task_key(
        {"task_id": d["attempt_id"], "nonce": d["nonce"]})


def _view(plan, held=False):
    d = plan["derived"]
    root = _root(plan)
    paths = e._paths(plan)
    j = e.load_journal(root / "run.json") or {}
    a, w, aid, wid = e._workspace_identity(plan, j)
    intent = e._one(paths["state"], "SELECT send_state FROM v2_scheduler_intent WHERE assignment_id=?",
                    (aid,)) if aid else None
    sent = intent and intent["send_state"]
    work = paths["workspace"]
    own = bool(w and w["status"] in ("ready", "active") and Path(w["path"]).resolve() == work.resolve()
               and work.is_dir() and e.git_opt(work, "rev-parse", "--show-toplevel")
               and Path(e.git_text(work, "rev-parse", "--show-toplevel")).resolve() == work.resolve())
    head = e.git_text(work, "rev-parse", "HEAD") if own else None
    changes = e.git(work, "--no-optional-locks", "status", "--porcelain=v1", "-z",
                    "--untracked-files=all")["stdout"] if own else b""
    dirty = [p for p in e._fields(changes) if p]
    within = True
    files = {}
    if own:
        for rel in d["allowed_paths"]:
            f = work / rel
            if f.resolve() != work.resolve() / rel or f.is_symlink() or (f.exists() and not f.is_file()):
                within = False
            else:
                files[rel] = e.sha_file(f) if f.is_file() else None
        names = {v for _, ps in e.status_paths(work) for v in ps}
        if head != d["base"]:
            names.update(p for change in e.name_status(work, d["base"], head) for p in change[1:])
        within = within and names <= set(files)
    job = _job(plan)
    identity = dict(project=d["project_id"], task=d["task_id"], attempt=d["attempt_id"],
                    nonce=d["nonce"], instance=d["worker"]["instance_id"], role="executor",
                    workspace=str(work), assignment=aid)
    worker = paths["worker"]
    untouched = sent in (None, "ready") and not any(
        p.exists() for p in (worker / "active.json", job, worker / "send.lock"))
    isolation = "not_dispatched" if untouched else "unverified"
    if not untouched and sent and not (worker / "send.lock").exists():
        # A process exit, a dead pane or a missing receipt is not a fence.
        # Only a trusted local host can attest a *durable* write fence for
        # this exact attempt, including descendants and session re-entry.
        try:
            reply = task_session.host("inspect-writer-fence", identity)
            if (reply.get("binding") == identity and reply.get("permanently_fenced") is True
                    and reply.get("descendants_fenced") is True):
                isolation = "host_fenced"
        except (OSError, ValueError, e.subprocess.SubprocessError):
            pass
    receipt = job / "receipt" / "status.json"
    last = "workspace_ready" if own else "assignment_claimed" if aid else "prepared"
    if receipt.is_file():
        r = e.read_json(receipt)
        if (r.get("task_id"), r.get("nonce"), r.get("identity")) == (
                d["attempt_id"], d["nonce"], d["worker_spec"]["expected_identity"]):
            last = "worker_" + str(r.get("status"))
    terminal = bool(a and (a["state"] == "completed" or a["review_status"] in
                           ("accepted", "rejected", "repair")) or j.get("event_id"))
    lock = root / "run.lock"
    busy = not held and lock.is_file()
    if busy:
        owner = e._lock_value(lock) or {}
        pid = owner.get("pid")
        busy = not (type(pid) is int and pid > 0 and e.pid_alive(pid) is False)
    permitted = not terminal and within and (own or w is None) and not _marker(plan).exists()
    actions = []
    if permitted and not busy and isolation != "unverified":
        if j and (untouched or e._worker_attempt_state(plan, j)[0] == "recoverable"):
            actions.append("continue")
        if j and not (d.get("route") or plan["spec"].get("evidence_route")
                or "kb_context" in e.read_json(worker / "config.json")):
            actions.extend(["manual", "reassign"])
    if _marker(plan).exists():
        isolation = "retired"
    result = dict(**identity, send_state=sent, last_receipted_step=last, workspace_available=own,
                  head=head, base=d["base"], committed_changes=(
                      e.name_status(work, d["base"], head) if own else []),
                  uncommitted_changes=dirty, writer_isolation=isolation,
                  session=task_session.access(plan), allowed_actions=actions)
    result["state_version"] = e.digest(dict(binding=identity, plan=plan["plan_sha256"],
        journal=e.digest(j), revision=a and a["revision"], workspace_id=wid, sent=sent,
        head=head, dirty=dirty, files=files, isolation=isolation, receipt=(
            e.sha_file(receipt) if receipt.is_file() else None), retired=_marker(plan).exists()))
    return result, files


def inspect(plan):
    return _view(plan)[0]


def _clone(plan, view, files, dest):
    d = plan["derived"]
    source = Path(view["workspace"]) if view["workspace_available"] else Path(d["paths"]["repo"])
    head = view["head"] or d["base"]
    result = e.run_argv(["git", "clone", "--no-hardlinks", "--no-checkout", "--", str(source),
                         str(dest)], dest.parent)
    if result["returncode"]:
        raise ValueError("Unable to clone retained worktree.")
    e.git(dest, "config", "core.autocrlf", "false")
    e.git(dest, "config", "core.hooksPath", e.os.devnull)
    e.git(dest, "checkout", "--detach", head)
    for rel, expected in files.items():
        target = dest / rel
        if target.resolve() != dest.resolve() / rel:
            raise ValueError("Unsafe recovery destination")
        if expected is None:
            target.unlink(missing_ok=True)
        else:
            content = (source / rel).read_bytes()
            if hashlib.sha256(content).hexdigest() != expected:
                raise ValueError("Workspace changed; inspect the retained attempt.")
            target.parent.mkdir(exist_ok=True, parents=True)
            target.write_bytes(content)
    if e.status_paths(dest):
        e.git(dest, "add", "-A")
        e.git(dest, "-c", "user.name=AWO Handoff", "-c", "user.email=local@invalid",
              "commit", "-m", "Preserve interrupted work before handoff")
    return e.git_text(dest, "rev-parse", "HEAD")


def apply(plan, action, version, recipient):
    if action not in ("continue", "manual", "reassign") or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,95}", recipient or ""):
        raise ValueError("Choose an action and a local recipient identifier.")
    d = plan["derived"]
    root = _root(plan)
    area = root / "handoff"
    key = e.digest([plan["plan_sha256"], version, action, recipient])
    receipt = area / (key + ".json")
    lock, token = e.acquire_lock(plan)
    try:
        if receipt.is_file():
            return e.read_json(receipt), True
        view, files = _view(plan, held=True)
        if version != view["state_version"]:
            raise ValueError("State changed; refresh recovery before choosing again.")
        if action not in view["allowed_actions"]:
            raise ValueError("Writer isolation, scope or grants unverified; inspect records only.")
        area.mkdir(exist_ok=True)
        if action == "continue":
            if recipient != d["worker"]["instance_id"]:
                raise ValueError("Continue requires the original executor instance.")
            # Locked controller recovery resumes only previously receipted work or
            # a ledger send that has never been attempted.
            result = e.execute_plan(root / "plan.json", resume=True, locked=True)
            out = dict(action=action, recipient=recipient, thread=str(root), result_status=result.get("status"))
        else:
            # Serialize a direct native submission with retirement; the marker
            # permanently disables future submissions and native session resume.
            sl = Path(d["paths"]["worker"]) / "send.lock"
            with sl.open("x", encoding="ascii") as f:
                json.dump({"pid": e.os.getpid()}, f)
            try:
                if view["writer_isolation"] == "not_dispatched" and (
                        _job(plan).exists() or (sl.parent / "active.json").exists()):
                    raise ValueError("Worker changed during handoff; refresh.")
                e.write_new(_marker(plan), json.dumps(dict(key=key, state_version=version,
                            action=action, recipient=recipient, binding=plan["plan_sha256"])))
            finally:
                sl.unlink(missing_ok=True)
            state = Path(d["paths"]["state"])
            agent = e.service.agent_get(state, *e._actor(plan), d["worker"]["agent_id"])["agent"]
            e.service.revoke_agent(state, *e._actor(plan), {
                "tool": "agent.revoke", "expected_epoch": d["epoch"],
                "command_id": "handoff-" + key[:24], "payload": {"agent_id": agent["agent_id"],
                "expected_revision": agent["revision"]}})
            dest = area / "checkpoint"
            commit = _clone(plan, view, files, dest)
            out = dict(action=action, recipient=recipient, old_instance=d["worker"]["instance_id"],
                       old_writer="revoked", workspace=str(dest), checkpoint_commit=commit,
                       previous_head=view["head"], files=files, dispatched=False, accepted=False)
            if action == "reassign":
                from awo_app_operations import handle
                child = area / "run"
                spec = e.read_json(root / "spec.json")
                spec.pop("rework_origin", None)
                spec.update(repository=str(dest), work_root=str(child),
                            handoff_origin={"action_id": key, "parent_plan": plan["plan_sha256"]},
                            target_ref="refs/heads/handoff-" + key[:16])
                spec["worker"]["session_mode"] = "one-shot"
                e.save_json(area / "spec.json", spec)
                handle("prepare", ["--thread", str(child), "--spec", str(area / "spec.json")])
                fresh = e.load_plan(child / "plan.json")
                if fresh["derived"]["base"] != commit:
                    raise ValueError("Replacement does not match checkpoint")
                out.update(thread=str(child), new_instance=fresh["derived"]["worker"]["instance_id"],
                           child_plan=fresh["plan_sha256"], nonce=fresh["derived"]["nonce"])
        out.update(project=d["project_id"], task=d["task_id"], attempt=d["attempt_id"],
                   state_version=version, action_id=key)
        e.write_new(receipt, json.dumps(out, sort_keys=True))
        return out, False
    finally:
        e.release_lock(lock, token)


def validate_successor(plan):
    """A replacement can start only from the exact committed transfer."""
    root = _root(plan)
    origin = plan["spec"]["handoff_origin"]
    if root.name != "run" or root.parent.name != "handoff":
        raise ValueError("Invalid handoff attempt location.")
    parent = e.load_plan(root.parent.parent / "plan.json")
    marker = e.read_json(_marker(parent))
    saved = e.read_json(root.parent / (origin["action_id"] + ".json"))
    if not (marker["key"] == saved["action_id"] == origin["action_id"] and
            marker["binding"] == origin["parent_plan"] == parent["plan_sha256"] and
            saved["action"] == "reassign" and saved["child_plan"] == plan["plan_sha256"] and
            saved["nonce"] == plan["derived"]["nonce"] and
            saved["new_instance"] == plan["derived"]["worker"]["instance_id"] and
            saved["old_instance"] == parent["derived"]["worker"]["instance_id"] and
            saved["checkpoint_commit"] == plan["derived"]["base"]):
        raise ValueError("Replacement no longer matches the handoff receipt.")

def linked_child(plan):
    """Resolve a recorded replacement without accepting an arbitrary thread path."""
    root = _root(plan)
    marker = e.read_json(_marker(plan))
    key = marker.get("key", "")
    if marker.get("action") != "reassign" or not re.fullmatch("[0-9a-f]{64}", key):
        raise ValueError("No prepared replacement for this task.")
    receipt = e.read_json(root / "handoff" / (key + ".json"))
    path = root / "handoff" / "run" / "plan.json"
    child = e.load_plan(path)
    validate_successor(child)
    if (child["spec"]["handoff_origin"]["action_id"] != key or
            Path(receipt["thread"]).resolve() != path.parent.resolve()):
        raise ValueError("Replacement thread changed.")
    return path, receipt
