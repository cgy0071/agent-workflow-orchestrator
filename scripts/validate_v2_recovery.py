"""V2 recovery acceptance: real process death, durable restart, and reconciliation.

This opt-in validator reuses the accepted stage-1 scaffolding in
``validate_v2_native_loop`` (fixture project/state, scoped access tokens, the V2
scheduler send gate, native_worker -> run_job, managed directory workspaces) and
adds disposable child processes that are killed only after their PID, OS start
time, argv, script and cwd are verified. It writes a new directory and never
touches an existing ledger, user project, Herdr pane or model session.

Scenarios (all state is a fresh fixture SQLite database):

1. Kill a dispatch driver after the durable ``attempted`` commit but before the
   post-send commit (real native send), restart dispatch -> ALREADY_ATTEMPTED,
   and recover the original native result by observation (reconcile).
2. Kill and restart an observer process against the same SQLite while the native
   job runs; reconcile once to REVIEW_READY, repeat to the same event id.
3. Two independent claim processes: at most one Assignment, one intent and one
   held capacity; command identity replay and conflict.
4. Stale revision, wrong generation, wrong nonce, replayed completion and a
   tampered artifact: intended rejections with no review/unlock/release.
5. Budget admission refusal, a lowered cap fencing a reserved run, a real
   actual-usage overrun (post-hoc detection only) and unknown usage blocking a
   later admission.

Negative inputs synthesised in-process are labelled as such in the trace and
the payload; they are never presented as model output. Offline evidence uses a
local fake executable and is reported as ``PROTOCOL_FIXTURE_RECOVERY_PASS``.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

import awo_service as service
import budget
import monitor_task
import native_worker
import priority_scheduler as scheduler
import validate_v2_native_loop as loop
from workflow_evidence import allocate, task_key

EVIDENCE_CLASSES = loop.EVIDENCE_CLASSES
SOURCE = Path(__file__).resolve().parent
FAILED = {"native": "V2_RECOVERY_FAILED", "protocol-fixture": "PROTOCOL_FIXTURE_RECOVERY_FAILED"}
PASSED = {"native": "V2_RECOVERY_PASS", "protocol-fixture": "PROTOCOL_FIXTURE_RECOVERY_PASS"}

CHILD = r'''"""Disposable fault driver for a V2 recovery fixture (not production code)."""
import json
import os
import sys
import time
from pathlib import Path

SCRIPTS, COMMAND, LABEL_DIR = sys.argv[1], sys.argv[2], Path(sys.argv[3])
sys.path.insert(0, SCRIPTS)

import monitor_task
import priority_scheduler as scheduler
import awo_service as service


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def process_start(pid):
    if sys.platform != "win32":
        try:
            raw = Path("/proc/%d/stat" % pid).read_text()
            return int(raw.split(") ", 1)[1].split()[19])
        except Exception:
            return None
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(0x0400, False, int(pid))
    if not handle:
        return None
    try:
        creation, exited = wintypes.FILETIME(), wintypes.FILETIME()
        kernel, user = wintypes.FILETIME(), wintypes.FILETIME()
        if not kernel32.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exited),
                                        ctypes.byref(kernel), ctypes.byref(user)):
            return None
        return (creation.dwHighDateTime << 32) | creation.dwLowDateTime
    finally:
        kernel32.CloseHandle(handle)


def describe(payload):
    return {"pid": os.getpid(), "argv": sys.argv, "cwd": str(Path.cwd()),
            "script": str(Path(__file__).resolve()), "command": COMMAND,
            "assignment_id": payload.get("assignment_id"), "label": payload.get("label"),
            "started_at": monitor_task.utc_now(),
            "process_start": process_start(os.getpid())}


def wait_for(path, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if Path(path).exists():
            return "released"
        time.sleep(0.05)
    return "timeout"


class FaultTransport:
    """Wraps the real bridge; blocks inside send() so the parent can kill this process."""

    def __init__(self, request, payload):
        self.real = monitor_task.Transport(request)
        self.payload = payload
        self.marker = Path(payload["marker"])
        self.fault = payload["fault"]

    def status(self, spec):
        return self.real.status(spec)

    def output(self, spec):
        return self.real.output(spec)

    def send(self, spec, message):
        if self.fault == "before_send":
            write_json(self.marker, {"stage": "before_send", "pid": os.getpid(),
                                     "at": monitor_task.utc_now(), "sent": False})
            wait_for(self.payload["unblock"], self.payload["block_seconds"])
            raise RuntimeError("pre-send fault window ended without the expected kill")
        response = self.real.send(spec, message)
        write_json(self.marker, {"stage": "after_send", "pid": os.getpid(),
                                 "at": monitor_task.utc_now(), "sent": True,
                                 "response": str(response)[:2000]})
        wait_for(self.payload["unblock"], self.payload["block_seconds"])
        raise RuntimeError("post-send fault window ended without the expected kill")


def contract_request(state, assignment_id):
    con = service._connect(service._db_path(state), readonly=True)
    try:
        row = con.execute("SELECT contract_json FROM v2_scheduler_intent WHERE assignment_id=?",
                          (assignment_id,)).fetchone()
        return json.loads(row[0])["request"]
    finally:
        con.close()


def main():
    payload = json.loads((LABEL_DIR / "payload.json").read_text(encoding="utf-8"))
    write_json(LABEL_DIR / "identity.json", describe(payload))
    state, actor = payload["state"], payload["actor"]
    result = LABEL_DIR / "result.json"
    if COMMAND in ("dispatch", "dispatch_once"):
        transport = None
        if COMMAND == "dispatch":
            transport = FaultTransport(contract_request(state, payload["assignment_id"]), payload)
        try:
            value = scheduler.dispatch_assignment(state, payload["assignment_id"], *actor,
                                                  expected_epoch=payload["epoch"], transport=transport)
        except BaseException as exc:
            write_json(result, {"outcome": "exception", "type": type(exc).__name__,
                                "message": str(exc)})
            raise
        write_json(result, {"outcome": "returned", "value": value})
    elif COMMAND == "reconcile":
        path = LABEL_DIR / "observations.jsonl"
        count = 0
        while True:
            try:
                value = scheduler.reconcile_assignment(state, payload["assignment_id"], *actor,
                                                       expected_epoch=payload["epoch"])
                item = {"at": monitor_task.utc_now(), "result": value}
            except BaseException as exc:
                item = {"at": monitor_task.utc_now(),
                        "error": {"type": type(exc).__name__, "message": str(exc)}}
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(item, sort_keys=True, ensure_ascii=True) + "\n")
                raise
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(item, sort_keys=True, ensure_ascii=True) + "\n")
            count += 1
            if not payload.get("loop") or (payload.get("max_observations")
                                           and count >= payload["max_observations"]):
                break
            time.sleep(payload.get("interval", 0.5))
        write_json(result, {"outcome": "loop_done", "observations": count})
    elif COMMAND == "execute":
        if payload.get("start_gate"):
            wait_for(payload["start_gate"], payload.get("gate_seconds", 60))
        try:
            value = service.execute_command(state, *actor, payload["envelope"])
        except BaseException as exc:
            write_json(result, {"outcome": "exception", "type": type(exc).__name__,
                                "code": getattr(exc, "code", None), "message": str(exc)})
            raise
        write_json(result, {"outcome": "returned", "value": value})
    else:
        raise SystemExit("unknown driver command: " + COMMAND)
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
                    encoding="utf-8")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def q(state, sql, args=()):
    """Read through a normal connection so a killed writer's WAL is recovered."""
    con = service._connect(service._db_path(state))
    try:
        return [dict(row) for row in con.execute(sql, args)]
    finally:
        con.close()


def q1(state, sql, args=()):
    found = q(state, sql, args)
    return found[0] if found else None


def process_start(pid):
    if sys.platform != "win32":
        try:
            raw = Path("/proc/%d/stat" % pid).read_text()
            return int(raw.split(") ", 1)[1].split()[19])
        except Exception:
            return None
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(0x0400, False, int(pid))
    if not handle:
        return None
    try:
        creation, exited = wintypes.FILETIME(), wintypes.FILETIME()
        kernel, user = wintypes.FILETIME(), wintypes.FILETIME()
        if not kernel32.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exited),
                                        ctypes.byref(kernel), ctypes.byref(user)):
            return None
        return (creation.dwHighDateTime << 32) | creation.dwLowDateTime
    finally:
        kernel32.CloseHandle(handle)


def job_list(directory):
    jobs = Path(directory) / "jobs"
    if not jobs.is_dir():
        return []
    return sorted(path.name for path in jobs.iterdir() if path.is_dir())


def job_usage(directory, key):
    path = Path(directory) / "jobs" / key / "receipt" / "output.log"
    totals = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") == "turn.completed":
                found = event.get("usage")
                if isinstance(found, dict):
                    for name, value in found.items():
                        if type(value) is int:
                            totals[name] = totals.get(name, 0) + value
    return {"path": str(path), "output_sha256": sha(path) if path.is_file() else None,
            "status": "actual" if totals else "unknown", "totals": totals or "unknown",
            "cost": {"status": "unknown", "reason": "no provider pricing applied by this fixture"}}


def snapshot(state, assignment_id):
    assignment = q1(state, "SELECT assignment_id,task_id,agent_id,generation,slot_id,holds_slot,"
                           "state,review_status,revision,workspace_id,legacy_task_id,"
                           "legacy_dispatch_status FROM v2_assignment WHERE assignment_id=?",
                   (assignment_id,))
    intent = q1(state, "SELECT run_id,attempt_id,send_state,attempted_at,submitted_at,"
                       "controller_id,controller_version,controller_epoch,response_json,closure_json "
                       "FROM v2_scheduler_intent WHERE assignment_id=?", (assignment_id,))
    attempt = intent["attempt_id"] if intent else None
    dispatch = q1(state, "SELECT status,data FROM dispatches WHERE task=?", (attempt,)) if attempt else None
    journal = [row["kind"] for row in q(state, "SELECT kind FROM task_journal WHERE task=? "
                                                      "ORDER BY seq", (attempt,))] if attempt else []
    events = q(state, "SELECT id,role,status FROM events WHERE task=? ORDER BY id",
               (attempt,)) if attempt else []
    task = q1(state, "SELECT task_id,task_state,revision,accepted_evidence_id FROM v2_task "
                     "WHERE task_id=?", (assignment["task_id"],)) if assignment else None
    workspace = q1(state, "SELECT workspace_id,kind,status,path,revision FROM v2_workspace "
                          "WHERE assignment_id=?", (assignment_id,))
    return {
        "assignment": assignment, "intent": intent, "dispatch": dispatch,
        "journal": journal, "events": events, "task": task, "workspace": workspace,
        "budget_reservations": q(state, "SELECT b.scope_type,b.scope_id,r.dimension,r.amount,r.state,"
                                        "r.settled_amount FROM v2_budget_reservation r JOIN v2_budget b "
                                        "ON b.budget_id=r.budget_id WHERE r.assignment_id=? "
                                        "ORDER BY b.scope_type,r.dimension", (assignment_id,)),
        "evidence": q(state, "SELECT evidence_id,path,sealed_sha256,sealed,state FROM v2_evidence "
                              "WHERE assignment_id=? ORDER BY path", (assignment_id,)),
        "reviews": q(state, "SELECT review_id,verdict,state FROM v2_review WHERE assignment_id=? "
                            "ORDER BY review_id", (assignment_id,)),
        "workspace_reviews": q(state, "SELECT workspace_id,inventory_sha256,verdict FROM "
                                      "v2_workspace_review WHERE assignment_id=?", (assignment_id,)),
    }


def process_exit_code(pid):
    """Exit code of the OS process object for ``pid`` (259 means still active)."""
    if sys.platform != "win32":
        return None
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(0x0400, False, int(pid))
    if not handle:
        return None
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        return code.value
    finally:
        kernel32.CloseHandle(handle)


def release_handle(proc):
    """Drop the parent's Popen handle so a killed PID no longer answers OpenProcess.

    Windows keeps a process object (and its PID) alive while any handle to it is
    open, so a terminated child can still report its creation time. Closing the
    handle is what makes the post-kill check a real death check rather than a
    liveness check that can never fail.
    """
    handle = getattr(proc, "_handle", None)
    if handle is None:
        return
    try:
        handle.Close()
    except Exception:
        pass
    try:
        proc._handle = None
    except Exception:
        pass


class Driver:
    """Spawn, verify, and stop disposable child processes attributable to this fixture."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.directories = self.root / "drivers"
        self.directories.mkdir(parents=True, exist_ok=False)
        self.child = self.directories / "recovery_child.py"
        loop.write_new(self.child, CHILD)
        self.child_sha256 = sha(self.child)
        self.procs = {}
        self.records = []

    def spawn(self, label, command, payload, deadline=30.0):
        directory = self.directories / label
        directory.mkdir(parents=True, exist_ok=False)
        payload = dict(payload, label=label, root=str(self.root))
        save(directory / "payload.json", payload)
        args = [sys.executable, "-B", str(self.child), str(SOURCE), command, str(directory)]
        out = (directory / "stdout.log").open("ab")
        err = (directory / "stderr.log").open("ab")
        try:
            proc = subprocess.Popen(args, cwd=str(self.root), stdout=out, stderr=err,
                                    stdin=subprocess.DEVNULL, env=dict(os.environ))
        finally:
            out.close()
            err.close()
        record = {"label": label, "command": command, "pid": proc.pid, "argv": args,
                  "cwd": str(self.root), "launched_at": monitor_task.utc_now(),
                  "identity_path": str(directory / "identity.json"),
                  "stdout": str(directory / "stdout.log"), "stderr": str(directory / "stderr.log")}
        identity_file = directory / "identity.json"
        try:
            limit = time.monotonic() + deadline
            while time.monotonic() < limit:
                if identity_file.is_file():
                    break
                if proc.poll() is not None:
                    tail = (directory / "stderr.log").read_text(encoding="utf-8", errors="replace")[-2000:]
                    raise loop.Failure("process_identity", label + " exited before identity: " + tail)
                time.sleep(0.05)
            loop.expect(identity_file.is_file(), "process_identity", label + " did not write identity")
            identity = read_json(identity_file)
            loop.expect(identity.get("pid") == proc.pid, "process_identity",
                        label + ": recorded PID differs from the spawned child")
            loop.expect(Path(identity.get("cwd", "")).resolve() == self.root, "process_identity",
                        label + ": recorded cwd differs from the verified cwd")
            loop.expect(Path(identity.get("script", "")).resolve() == self.child.resolve(),
                        "process_identity", label + ": recorded script differs")
            loop.expect(Path(identity.get("argv", [""])[0]).resolve() == self.child.resolve(),
                        "process_identity", label + ": argv[0] is not the driver script")
            loop.expect(identity.get("command") == command, "process_identity",
                        label + ": recorded command differs")
            os_start = process_start(proc.pid)
            loop.expect(os_start is not None, "process_identity",
                        label + ": OS start time is unavailable; the process is not attributable")
            if identity.get("process_start") is not None:
                loop.expect(int(identity["process_start"]) == int(os_start), "process_identity",
                            label + ": self-reported and OS start times differ")
        except BaseException:
            # Never leave a fixture child behind when attribution fails.
            try:
                proc.kill()
                proc.wait(timeout=10)
                release_handle(proc)
            except Exception:
                pass
            raise
        record.update(identity=identity, os_process_start=os_start,
                      verified={"pid": True, "start_time": True, "argv": True, "cwd": True,
                                "script": True})
        self.procs[label] = proc
        self.records.append(record)
        return record

    def wait(self, record, timeout=120):
        proc = self.procs[record["label"]]
        record["exit_code"] = proc.wait(timeout=timeout)
        record["ended_at"] = monitor_task.utc_now()
        result = Path(record["identity_path"]).parent / "result.json"
        record["result"] = read_json(result) if result.is_file() else None
        return record

    def stop(self, record, reason):
        proc = self.procs[record["label"]]
        current = process_start(proc.pid)
        loop.expect(current is not None, "kill_verification",
                    record["label"] + ": process is already gone; the exit cannot be attributed")
        loop.expect(int(current) == int(record["os_process_start"]), "kill_verification",
                    record["label"] + ": PID start time changed; refusing to kill a reused PID")
        identity = read_json(record["identity_path"])
        loop.expect(identity.get("pid") == proc.pid
                    and Path(identity.get("cwd", "")).resolve() == self.root
                    and identity.get("command") == record["command"]
                    and Path(identity.get("script", "")).resolve() == self.child.resolve(),
                    "kill_verification",
                    record["label"] + ": live command/cwd no longer matches the verified child")
        proc.kill()
        returncode = proc.wait(timeout=30)
        release_handle(proc)
        record["kill"] = {"reason": reason, "pid": proc.pid, "start_time": current,
                          "returncode": returncode, "at": monitor_task.utc_now(),
                          "mechanism": "Popen.kill on the verified OS process"}
        after = process_start(proc.pid)
        record["os_process_start_after"] = after
        record["exit_code_after"] = process_exit_code(proc.pid)
        loop.expect(after is None or record["exit_code_after"] not in (None, 259), "kill_verification",
                    record["label"] + ": the verified process is still active after the kill "
                    "(returncode=" + str(returncode) + ", start=" + str(current)
                    + ", after=" + str(after) + ", exit_after=" + str(record["exit_code_after"]) + ")")
        result = Path(record["identity_path"]).parent / "result.json"
        if result.is_file() and "result" not in record:
            record["result"] = read_json(result)
        return record

    def observations(self, record):
        path = Path(record["identity_path"]).parent / "observations.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]


def wait_file(path, timeout, stage, label):
    limit = time.monotonic() + timeout
    while time.monotonic() < limit:
        if Path(path).is_file():
            return read_json(path)
        time.sleep(0.05)
    raise loop.Failure(stage, label + " did not produce " + str(path))


def wait_absent(process, timeout=20):
    limit = time.monotonic() + timeout
    while time.monotonic() < limit:
        if process.poll() is not None:
            return process.returncode
        time.sleep(0.05)
    return None


def database_check(state):
    con = service._connect(service._db_path(state))
    try:
        return {"integrity_check": con.execute("PRAGMA integrity_check").fetchone()[0]}
    finally:
        con.close()


def task_create(state, root, controller, epoch, task_id, title):
    created = loop.call(state, root, controller, "awo_task_create", loop.args_for(
        epoch, task_id=task_id, title=title, body="Disposable recovery fixture; no user data.",
        priority="P3"))
    loop.expect(created.get("status") == "CREATED", "task_create",
                "task creation failed for " + task_id + ": " + json.dumps(created)[:400])
    return created


def enqueue(state, actor, epoch, task_id, revision, request, message, budget_estimate,
            agent_id, resources=None, dependencies=None):
    value = loop.command(state, actor, epoch, "scheduler.enqueue", {
        "task_id": task_id, "expected_revision": revision, "request": request,
        "message": message, "agent_id": agent_id, "budget": budget_estimate,
        "resources": resources or {"native_slot": 1}, "workspace_mode": "directory",
        "task_kind": "artifact", "dependencies": dependencies or []})
    loop.expect(value.get("status") == "QUEUED", "enqueue",
                "enqueue failed for " + task_id + ": " + json.dumps(value)[:500])
    return value


def extra_request(run, root, name, attempt_id):
    nonce = uuid.uuid4().hex
    request = loop.request_for(attempt_id, nonce, Path(root) / "seed", run["state"],
                               run["worker_spec"], run["reviewer_spec"])
    request["completion"]["marker"] = "TASK_DONE " + attempt_id + " " + nonce
    delivery = allocate(request, Path(root) / "deliveries")
    delivery["worker"]["baseline_completion_seq"] = native_worker.status(
        Path(run["worker_spec"]["expected_identity"]["launch_cwd"]))["completion_seq"]
    message = json.dumps({"task_id": delivery["task_id"], "nonce": delivery["nonce"],
                          "cwd": delivery["cwd"], "prompt": "Recovery fixture for " + name
                          + "; do not execute.", "marker": delivery["completion"]["marker"],
                          "authorization": {"source": delivery["authorization"]["source"],
                                            "terminate_on_timeout": True}}, ensure_ascii=True)
    return delivery, message


def termination_note(root, name, request, reason):
    path = Path(root) / "terminations" / (name + ".md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(request["task_id"] + " " + request["nonce"] + "\nOutcome: terminated\n"
                    + reason + "\n", encoding="utf-8", newline="\n")
    return path


def cancel(state, actor, epoch, assignment_id, note):
    revision = q1(state, "SELECT revision FROM v2_assignment WHERE assignment_id=?",
                  (assignment_id,))["revision"]
    value = loop.command(state, actor, epoch, "assignment.close", {
        "assignment_id": assignment_id, "expected_revision": revision,
        "outcome": "cancelled", "evidence_path": str(note)})
    loop.expect(value.get("status") == "CLOSED" and value.get("outcome") == "cancelled",
                "cancel", "cancellation failed: " + json.dumps(value)[:500])
    return value


def budget_set(state, actor, epoch, scope_type, scope_id, limits, policy=None):
    status = budget.budget_status(state, scope_type, scope_id)
    version = status.get("version", 1) if status.get("exists") else 1
    payload = {"scope_type": scope_type, "scope_id": scope_id, "limits": limits,
               "expected_revision": version}
    if policy:
        payload["policy"] = policy
    response = budget.budget_limit(state, *actor, {
        "tool": "budget.limit", "payload": payload,
        "command_id": "v2recovery-budget-" + uuid.uuid4().hex, "expected_epoch": epoch})
    loop.expect(response.get("status") == "LIMITS_SET", "budget_limit",
                "budget limit failed: " + json.dumps(response)[:400])
    return response


def budget_close_run(state, actor, epoch, assignment_id, run_id, generation, label, usage):
    response = budget.budget_close(state, *actor, {
        "tool": "budget.close",
        "payload": {"run_id": run_id, "assignment_id": assignment_id, "generation": generation,
                    "state": label, "usage": usage},
        "command_id": "v2recovery-budget-" + uuid.uuid4().hex, "expected_epoch": epoch})
    loop.expect(response.get("status") == "CLOSED", "budget_close",
                "budget close failed: " + json.dumps(response)[:400])
    return response


def zero_usage(reason):
    return {dim: {"state": "actual", "value": 0,
                  "source": "no adapter call was made; zero observed usage (" + reason + ")"}
            for dim in ("tokens.input", "tokens.output")}


def measured_usage(report):
    totals = report["totals"]
    if totals == "unknown":
        return {dim: {"state": "unknown", "source": "provider output log " + report["path"],
                      "reason": "provider turn.completed usage was not observed"}
                for dim in ("tokens.input", "tokens.output")}, "unknown"
    usage = {}
    mapping = (("input_tokens", "tokens.input"), ("output_tokens", "tokens.output"))
    for source, dimension in mapping:
        if source in totals:
            usage[dimension] = {"state": "actual", "value": totals[source],
                                "source": "provider turn.completed usage in " + report["path"]}
    if len(usage) == 2:
        return usage, "actual"
    for dimension in ("tokens.input", "tokens.output"):
        usage.setdefault(dimension, {"state": "unknown",
                                     "source": "provider output log " + report["path"],
                                     "reason": "the provider did not report " + dimension})
    return usage, "partial"


def execute_recovery(root):
    root = Path(root).resolve()
    run = read_json(root / "run.json")
    meta = read_json(root / "provenance.json")
    loop.expect(not (meta.get("evidence_class") == "native" and os.environ.get("HERDR_ENV") == "1"),
                "execute", "native-only acceptance requires a caller outside Herdr")
    for name, digest in meta["source_files"].items():
        loop.expect((SOURCE.parent / name).is_file() and sha(SOURCE.parent / name) == digest,
                    "execute", "validation source changed: " + name)
    state, epoch, actor = Path(run["state"]), run["epoch"], tuple(run["actor"])
    deadline = time.monotonic() + run["timeout"] * 4 + 90
    controller = loop.token(run, "controller_token")
    worker = loop.token(run, "worker_token")
    reviewer = loop.token(run, "reviewer_token")
    request_a = run["prepared"]["NATIVE-A"]["request"]
    worker_dir = Path(run["worker_spec"]["expected_identity"]["launch_cwd"])
    reviewer_dir = Path(root) / "reviewer"
    driver = Driver(root)
    traces = []
    payload = {"status": PASSED[meta["evidence_class"]], "stage": "running",
               "evidence_class": meta["evidence_class"], "scenarios": {}, "processes": [],
               "child_driver_sha256": driver.child_sha256, "herdr_invoked": False,
               "synthetic_negative_inputs": [], "started_at": monitor_task.utc_now()}

    def emit(value, scenario=None):
        if scenario:
            value = dict(value, scenario=scenario)
        traces.append(loop.trace(root, value))
        return value

    def checkpoint():
        save(root / "recovery.json", payload)

    def scenario(name, values):
        payload["scenarios"][name] = values
        checkpoint()

    # ---------------------------------------------------------------- 1a: claim, workspace, post-send kill
    claim = loop.call(state, root, worker, "awo_assignment_claim", loop.args_for(
        epoch, task_id="NATIVE-A", expected_revision=run["queue_a"]["revision"]))
    loop.expect(claim.get("status") == "RESERVED", "claim", "claim A failed: "
                + json.dumps(claim)[:400])
    aid = claim["assignment_id"]
    emit({"stage": "claim", "status": claim["status"], "assignment_id": aid,
          "run_id": claim["run_id"], "reservation_ids": claim.get("reservation_ids")}, "1a")
    ws_a, spec_a, marker_a = loop.create_managed_workspace(
        state, root, actor, epoch, aid, request_a["cwd"], ["HANDOFF.md", "RESULT.md", "REVIEW.md"])
    emit({"stage": "workspace_create", "workspace_id": ws_a["workspace_id"], "status": ws_a["status"],
          "path": ws_a["path"], "marker_sha256": marker_a,
          "assignment_workspace_id": q1(state, "SELECT workspace_id FROM v2_assignment WHERE "
                                              "assignment_id=?", (aid,))["workspace_id"]}, "1a")

    label = "dispatch-after-send"
    fault_dir = root / "faults" / label
    fault_dir.mkdir(parents=True, exist_ok=False)
    driver.spawn(label, "dispatch", {
        "state": str(state), "assignment_id": aid, "actor": list(actor), "epoch": epoch,
        "fault": "after_send", "marker": str(fault_dir / "after_send.json"),
        "unblock": str(fault_dir / "release"), "block_seconds": 120})
    marker = wait_file(fault_dir / "after_send.json", 120, "fault_window", label)
    loop.expect(marker.get("stage") == "after_send" and marker.get("sent") is True
                and marker.get("pid") == driver.records[-1]["pid"], "fault_window",
                "the post-send marker is not attributed to the verified driver")
    prekill = snapshot(state, aid)
    loop.expect(prekill["intent"]["send_state"] == "attempted"
                and prekill["intent"]["submitted_at"] is None
                and prekill["assignment"]["holds_slot"] == 1
                and "dispatch_attempted" in prekill["journal"]
                and "dispatch_submitted" not in prekill["journal"], "fault_window",
                "phase-1 attempted state is not the durable pre-send state")
    jobs_after_send = job_list(worker_dir)
    emit({"stage": "fault_after_send_marker", "marker": marker, "jobs": jobs_after_send,
          "prekill_intent_send_state": prekill["intent"]["send_state"]}, "1a")
    driver.stop(driver.records[-1], "kill the disposable driver after the real native send, "
                                    "before the post-send commit")
    postkill = snapshot(state, aid)
    check = database_check(state)
    loop.expect(postkill["intent"]["send_state"] == "attempted"
                and postkill["intent"]["submitted_at"] is None
                and postkill["assignment"]["holds_slot"] == 1
                and postkill["assignment"]["state"] == "reserved"
                and "dispatch_submitted" not in postkill["journal"]
                and len(job_list(worker_dir)) == 1 and check["integrity_check"] == "ok",
                "post_send_kill", "post-kill state is not one attempted intent and one send")
    emit({"stage": "fault_after_send_killed", "kill": driver.records[-1]["kill"],
          "database": check, "postkill_intent": postkill["intent"],
          "send_count": len(job_list(worker_dir))}, "1a")

    restart = driver.spawn("dispatch-after-restart", "dispatch_once", {
        "state": str(state), "assignment_id": aid, "actor": list(actor), "epoch": epoch})
    driver.wait(restart)
    restarted = restart["result"]
    loop.expect(restarted and restarted["outcome"] == "returned"
                and restarted["value"].get("status") == "ALREADY_ATTEMPTED"
                and restarted["value"].get("sent") is False
                and restarted["value"].get("send_state") == "attempted", "no_blind_resend",
                "restarted dispatch did not return ALREADY_ATTEMPTED/sent=False: "
                + json.dumps(restarted)[:600])
    loop.expect(len(job_list(worker_dir)) == 1, "no_blind_resend",
                "restarted dispatch produced another send/job")
    scenario("1a_post_send_kill", {
        "driver": driver.records[-2], "kill": driver.records[-2]["kill"],
        "restart_driver": restart, "restart_result": restarted,
        "prekill_intent": prekill["intent"], "prekill_assignment": prekill["assignment"],
        "postkill_intent": postkill["intent"], "postkill_assignment": postkill["assignment"],
        "postkill_journal": postkill["journal"], "database": check,
        "jobs": job_list(worker_dir), "send_count": len(job_list(worker_dir))})
    emit({"stage": "dispatch_after_restart", "status": restarted["value"]["status"],
          "sent": False, "send_count": len(job_list(worker_dir))}, "1a")

    # ---------------------------------------------------------------- 2: observer killed/restarted while the job runs
    observer = driver.spawn("observer-1", "reconcile", {
        "state": str(state), "assignment_id": aid, "actor": list(actor), "epoch": epoch,
        "loop": True, "interval": 0.5, "max_observations": 10000})
    observation_file = Path(observer["identity_path"]).parent / "observations.jsonl"
    limit = time.monotonic() + 90
    while time.monotonic() < limit:
        if observation_file.is_file():
            lines = [line for line in observation_file.read_text(encoding="utf-8").splitlines()
                     if line.strip()]
            if len(lines) >= 2:
                break
        time.sleep(0.1)
    loop.expect(observation_file.is_file()
                and len(observation_file.read_text(encoding="utf-8").splitlines()) >= 2,
                "observer_restart", "the first observer never recorded two observations")
    running_snapshot = snapshot(state, aid)
    driver.stop(observer, "kill the disposable observer process while the native job is running")
    first_observations = driver.observations(observer)
    emit({"stage": "observer_killed", "kill": observer["kill"],
          "last_observations": first_observations[-3:],
          "assignment_state": running_snapshot["assignment"]["state"],
          "intent_send_state": running_snapshot["intent"]["send_state"]}, "2")

    second = driver.spawn("observer-2-once", "reconcile", {
        "state": str(state), "assignment_id": aid, "actor": list(actor), "epoch": epoch,
        "loop": False})
    driver.wait(second)
    second_observations = driver.observations(second)
    loop.expect(second_observations
                and second_observations[0]["result"]["claims_retained"] is True,
                "observer_restart", "the restarted observer did not retain claims")
    second_result = second_observations[0]["result"]
    loop.wait_turn(worker_dir, request_a["worker"]["baseline_completion_seq"], deadline, "worker_turn")
    result_path = Path(request_a["cwd"]) / "RESULT.md"
    handoff_path = Path(request_a["cwd"]) / "HANDOFF.md"
    loop.expect(result_path.is_file() and handoff_path.is_file(), "worker_turn",
                "the recovered native job did not write its delivery artifacts")
    artifact = result_path.read_text(encoding="utf-8")
    loop.expect(request_a["completion"]["marker"] in handoff_path.read_text(encoding="utf-8"),
                "worker_turn", "the recovered delivery marker is missing")
    status_after_turn = native_worker.status(worker_dir)
    emit({"stage": "worker_turn_recovered", "status": status_after_turn,
          "result_sha256": sha(result_path), "handoff_sha256": sha(handoff_path),
          "artifact": artifact[:2000], "restarted_observer": second_result}, "2")

    third = driver.spawn("observer-3-complete", "reconcile", {
        "state": str(state), "assignment_id": aid, "actor": list(actor), "epoch": epoch,
        "loop": False})
    driver.wait(third)
    third_result = driver.observations(third)[0]["result"]
    loop.expect(third_result.get("status") == "REVIEW_READY", "reconcile",
                "the restarted observer did not reach REVIEW_READY: "
                + json.dumps(third_result)[:500])
    event_id = third_result.get("event_id")
    receipt = json.loads(q1(state, "SELECT data FROM events WHERE id=?", (event_id,))["data"])["receipt"]
    receipt_ok = (receipt.get("task_id") == request_a["task_id"]
                  and receipt.get("nonce") == request_a["nonce"]
                  and receipt.get("binding_artifact", {}).get("sha256") == sha(handoff_path))
    completed_snapshot = snapshot(state, aid)
    loop.expect(receipt_ok, "reconcile", "the recovered receipt is not bound to the original task/nonce/bytes")
    loop.expect(completed_snapshot["assignment"]["state"] == "completed"
                and completed_snapshot["assignment"]["review_status"] == "awaiting_review"
                and completed_snapshot["assignment"]["holds_slot"] == 1
                and completed_snapshot["task"]["task_state"] == "review_ready"
                and not completed_snapshot["reviews"] and not completed_snapshot["evidence"],
                "reconcile", "observation inferred acceptance or released the claim")

    fourth = driver.spawn("observer-4-duplicate", "reconcile", {
        "state": str(state), "assignment_id": aid, "actor": list(actor), "epoch": epoch,
        "loop": False})
    driver.wait(fourth)
    fourth_result = driver.observations(fourth)[0]["result"]
    loop.expect(fourth_result.get("event_id") == event_id, "duplicate_reconcile",
                "a repeated reconcile changed the completion event identity")
    event_rows = q(state, "SELECT id FROM events WHERE task=?", (request_a["task_id"],))
    loop.expect(len(event_rows) == 1 and event_rows[0]["id"] == event_id, "duplicate_reconcile",
                "a repeated reconcile created another completion event")
    loop.expect(completed_snapshot == snapshot(state, aid), "duplicate_reconcile",
                "a repeated reconcile changed the assignment/task state")
    scenario("2_observer_restart", {
        "killed_observer": observer["kill"], "prekill_observations": first_observations,
        "restarted_status": second_result.get("status"), "completion": third_result,
        "duplicate": fourth_result, "event_rows": event_rows,
        "receipt_task_nonce_ok": receipt_ok, "receipt": receipt,
        "assignment": completed_snapshot["assignment"], "task": completed_snapshot["task"],
        "reviews": completed_snapshot["reviews"], "evidence": completed_snapshot["evidence"]})
    emit({"stage": "observer_restart_complete", "status": third_result["status"],
          "event_id": event_id, "duplicate_event_id": fourth_result.get("event_id"),
          "completion_events": len(event_rows)}, "2")

    # ---------------------------------------------------------------- 3: evidence, completion, negatives
    current = q1(state, "SELECT revision FROM v2_assignment WHERE assignment_id=?",
                 (aid,))["revision"]
    recorded = []
    for name in ("RESULT.md", "HANDOFF.md"):
        raw = (Path(request_a["cwd"]) / name).read_bytes()
        value = loop.call(state, root, worker, "awo_evidence_record", loop.args_for(
            epoch, assignment_id=aid, expected_revision=current, kind="artifact", path=name,
            sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw)))
        loop.expect(value.get("status") == "EVIDENCE_RECORDED", "evidence",
                    "evidence record failed: " + json.dumps(value)[:400])
        recorded.append(value)
        current = value["revision"]
    assignment = q1(state, "SELECT * FROM v2_assignment WHERE assignment_id=?", (aid,))
    completed = loop.call(state, root, worker, "awo_assignment_complete", loop.args_for(
        epoch, assignment_id=aid, generation=assignment["generation"], expected_revision=current,
        outcome="completed", evidence_ids=[item["evidence_id"] for item in recorded],
        summary="Recovered native turn completed and observed."))
    loop.expect(completed.get("status") == "REVIEW_READY"
                and completed.get("event_id") == event_id
                and completed.get("independently_accepted") is False, "complete",
                "worker completion did not match the recovered observation: "
                + json.dumps(completed)[:500])
    emit({"stage": "complete", "status": completed["status"], "event_id": completed["event_id"],
          "evidence_ids": [item["evidence_id"] for item in recorded],
          "independently_accepted": False}, "3")

    baseline_negative = snapshot(state, aid)

    def denied(stage, function, *args, **kwargs):
        try:
            function(*args, **kwargs)
        except Exception as exc:
            return {"type": type(exc).__name__, "code": getattr(exc, "code", None),
                    "message": str(exc)}
        raise loop.Failure(stage, "the negative operation unexpectedly succeeded")

    stale_close = denied("negative_stale_close", loop.command, state, actor, epoch,
                         "assignment.close", {"assignment_id": aid,
                                              "expected_revision": baseline_negative["assignment"]["revision"] - 1,
                                              "outcome": "accepted", "event_id": event_id})
    loop.expect(stale_close["message"].startswith("stale or invalid expected_revision; current is "),
                "negative_stale_close", "stale close was not rejected with the intended message: "
                + json.dumps(stale_close))
    wrong_generation = denied("negative_generation", loop.call, state, root, worker,
                              "awo_assignment_complete", loop.args_for(
                                  epoch, assignment_id=aid,
                                  generation=baseline_negative["assignment"]["generation"] + 1,
                                  expected_revision=baseline_negative["assignment"]["revision"],
                                  outcome="completed",
                                  evidence_ids=[item["evidence_id"] for item in recorded],
                                  summary="wrong generation"))
    loop.expect(wrong_generation["code"] == "state_conflict"
                and wrong_generation["message"] == "Assignment generation changed",
                "negative_generation", "wrong generation was not rejected exactly: "
                + json.dumps(wrong_generation))
    replayed_event_close = denied("negative_replayed_event", loop.command, state, actor, epoch,
                                  "assignment.close", {"assignment_id": aid,
                                                       "expected_revision": baseline_negative["assignment"]["revision"],
                                                       "outcome": "accepted", "event_id": "not-" + event_id})
    loop.expect("not recorded" in replayed_event_close["message"].lower()
                or "missing or changed" in replayed_event_close["message"].lower(),
                "negative_replayed_event", "a fabricated completion event was not rejected: "
                + json.dumps(replayed_event_close))
    loop.expect(snapshot(state, aid) == baseline_negative, "negative_state",
                "a negative completion attempt changed authority state")

    class WrongIdentity:
        def status(self, spec):
            return {"identity": {"kind": "codex", "terminal": "synthetic-not-the-worker",
                                 "launch_cwd": str(root / "elsewhere")},
                    "status": "idle", "completion_seq": 0}

        def output(self, spec):
            return ""

    wrong_identity = scheduler.reconcile_assignment(
        state, aid, *actor, expected_epoch=epoch, transport=WrongIdentity())
    loop.expect(wrong_identity.get("status") == "IDENTITY_CHANGED"
                and wrong_identity.get("claims_retained") is True,
                "negative_identity", "synthetic wrong-identity observation was not rejected")
    uncertain_snapshot = snapshot(state, aid)
    loop.expect(uncertain_snapshot["assignment"]["holds_slot"] == 1
                and uncertain_snapshot["assignment"]["review_status"] == "awaiting_review"
                and not uncertain_snapshot["reviews"], "negative_identity",
                "the synthetic observation released or reviewed the claim")
    restored = scheduler.reconcile_assignment(state, aid, *actor, expected_epoch=epoch)
    loop.expect(restored.get("status") == "REVIEW_READY" and restored.get("event_id") == event_id,
                "negative_identity", "the real observation did not restore REVIEW_READY: "
                + json.dumps(restored)[:500])
    restored_snapshot = snapshot(state, aid)
    loop.expect(restored_snapshot["assignment"]["holds_slot"] == 1
                and restored_snapshot["assignment"]["review_status"] == "awaiting_review"
                and restored_snapshot["assignment"]["state"] == "completed"
                and restored_snapshot["intent"]["send_state"] == "review_ready"
                and not restored_snapshot["reviews"]
                and restored_snapshot["evidence"] == baseline_negative["evidence"],
                "negative_identity", "the corrected observation did not restore the recovered review state: "
                + json.dumps({key: restored_snapshot[key] for key in
                              ("assignment", "intent", "reviews", "evidence")}))

    receipt_request = copy.deepcopy(request_a)
    receipt_request["worker"]["adapter"] = "receipt"

    class WrongNonce:
        def status(self, spec):
            return {"identity": receipt_request["worker"]["expected_identity"], "status": "idle",
                    "task_id": receipt_request["task_id"], "nonce": "wrong-" + receipt_request["nonce"],
                    "completion_seq": receipt_request["worker"]["baseline_completion_seq"] + 1}

    observed, detail, _, _ = monitor_task.inspect_once(receipt_request, WrongNonce())
    loop.expect(observed == "identity_changed" and detail == "Receipt belongs to another task/nonce.",
                "negative_nonce", "wrong-nonce receipt was not rejected exactly: "
                + str(observed) + " / " + str(detail))
    loop.expect(snapshot(state, aid) == restored_snapshot, "negative_nonce",
                "the in-memory wrong-nonce check changed database state")
    payload["synthetic_negative_inputs"].append(
        {"name": "wrong_nonce_receipt", "where": "monitor_task.inspect_once (in memory)",
         "status": observed, "detail": detail,
         "note": "adapter='receipt' synthetic observation; no database write and no model output"})
    payload["synthetic_negative_inputs"].append(
        {"name": "wrong_identity_observation", "where": "priority_scheduler.reconcile_assignment",
         "status": wrong_identity.get("status"), "detail": wrong_identity.get("detail"),
         "note": "synthetic transport observation through the real reconcile authority"})
    scenario("4_negative_inputs", {
        "stale_close": stale_close, "wrong_generation": wrong_generation,
        "replayed_completion_event": replayed_event_close,
        "wrong_identity": wrong_identity, "corrected_observation": restored,
        "wrong_nonce": {"status": observed, "detail": detail},
        "state_unchanged": True})
    emit({"stage": "negatives", "stale_close": stale_close, "wrong_generation": wrong_generation,
          "replayed_event": replayed_event_close,
          "wrong_identity_status": wrong_identity.get("status"),
          "wrong_nonce_status": observed}, "3")

    # ---------------------------------------------------------------- dependent Task stays blocked
    prepared_b = run["prepared"]["NATIVE-B"]
    prepared_b["request"]["worker"]["baseline_completion_seq"] = native_worker.status(worker_dir)["completion_seq"]
    message_b = json.dumps({"task_id": prepared_b["request"]["task_id"],
                            "nonce": prepared_b["request"]["nonce"],
                            "cwd": prepared_b["request"]["cwd"], "prompt": prepared_b["prompt"],
                            "marker": prepared_b["request"]["completion"]["marker"],
                            "authorization": {"source": prepared_b["request"]["authorization"]["source"],
                                              "terminate_on_timeout": True}}, ensure_ascii=True)
    queued_b = loop.command(state, actor, epoch, "scheduler.enqueue", {
        "task_id": "NATIVE-B", "expected_revision": run["tasks"]["NATIVE-B"]["revision"],
        "request": prepared_b["request"], "message": message_b, "agent_id": run["worker_agent"],
        "budget": {"tokens.input": 40000, "tokens.output": 8000},
        "resources": {"native_slot": 1}, "workspace_mode": "directory",
        "task_kind": "artifact", "dependencies": ["NATIVE-A"]})
    loop.expect(queued_b.get("status") == "QUEUED", "enqueue_b",
                "enqueue B failed: " + json.dumps(queued_b)[:400])
    blocked_b = loop.call(state, root, worker, "awo_assignment_claim", loop.args_for(
        epoch, task_id="NATIVE-B", expected_revision=queued_b["revision"]))
    loop.expect(blocked_b.get("status") == "WAITING"
                and not q1(state, "SELECT 1 AS x FROM v2_assignment WHERE task_id='NATIVE-B'"),
                "dependency_gate", "a completed-but-unaccepted Task unlocked its dependent: "
                + json.dumps(blocked_b)[:500])
    emit({"stage": "dependency_blocked_after_recovery", "status": blocked_b["status"],
          "reasons": blocked_b.get("reasons")}, "3")

    # ---------------------------------------------------------------- reviewer turn on recovered artifacts
    expected = ("the JSON must contain secret=" + run["secret"] + " and answer=323") if run["secret"] else \
        "the only content must be the integer 323"
    requirement = ("the artifact is a JSON object whose answer is the product of 17 and 19 and whose "
                   "secret matches the value the worker received earlier") if run["secret"] else \
        "the artifact contains the product of 17 and 19"
    review, review_dir = loop.review_native(root, run, request_a, artifact, requirement, expected,
                                            deadline)
    emit({"stage": "review", "review": review, "review_cwd": str(review_dir),
          "artifact_sha256": sha(result_path)}, "3")
    if review["verdict"] != "accept":
        return loop.finish(root, dict(payload, status=FAILED[meta["evidence_class"]],
                                      stage="review_rejected", review=review), traces)
    note = Path(request_a["review_path"])
    note.write_text(request_a["task_id"] + " " + request_a["nonce"] + "\nVerdict: accept\n"
                    "Independent review of the recovered delivery: "
                    + str(review.get("reason", "")) + "\n", encoding="utf-8", newline="\n")
    reviewed = loop.call(state, root, reviewer, "awo_review_submit", loop.args_for(
        epoch, assignment_id=aid, expected_revision=q1(
            state, "SELECT revision FROM v2_assignment WHERE assignment_id=?", (aid,))["revision"],
        task_id="NATIVE-A", event_id=event_id,
        evidence_ids=[item["evidence_id"] for item in recorded], verdict="accept",
        review_note_path=note.resolve().relative_to(root).as_posix(), review_note_sha256=sha(note)))
    loop.expect(reviewed.get("status") == "REVIEW_RECORDED" and reviewed.get("claims_retained") is True,
                "review_submit", "review submit failed: " + json.dumps(reviewed)[:400])
    sealed_ws = q1(state, "SELECT workspace_id,verdict,inventory_sha256,inventory_json FROM "
                           "v2_workspace_review WHERE assignment_id=?", (aid,))
    loop.expect(sealed_ws and sealed_ws["verdict"] == "accept"
                and sealed_ws["workspace_id"] == ws_a["workspace_id"], "workspace_review",
                "the review gate did not seal the recovered workspace inventory")
    inventory = json.loads(sealed_ws["inventory_json"])
    loop.expect(inventory["scope_ok"] and not inventory["undeclared_paths"]
                and sorted(inventory["changed_paths"]) == ["HANDOFF.md", "RESULT.md", "REVIEW.md"],
                "workspace_review", "sealed inventory is not the recovered delivery: "
                + json.dumps(inventory["changed_paths"]))
    emit({"stage": "review_submit", "status": reviewed["status"], "event_id": event_id,
          "workspace_verdict": sealed_ws["verdict"], "inventory_sha256": sealed_ws["inventory_sha256"],
          "changed_paths": inventory["changed_paths"]}, "3")

    # ---------------------------------------------------------------- tampered artifact negative
    pristine = result_path.read_bytes()
    sealed_evidence = snapshot(state, aid)
    result_path.write_bytes(pristine + b"\nTAMPERED BY THE RECOVERY FIXTURE\n")
    tampered = denied("negative_tamper", loop.command, state, actor, epoch, "assignment.close", {
        "assignment_id": aid,
        "expected_revision": q1(state, "SELECT revision FROM v2_assignment WHERE assignment_id=?",
                                (aid,))["revision"],
        "outcome": "accepted", "event_id": event_id})
    loop.expect(tampered["message"].startswith("Reviewed evidence is missing or changed:")
                and "artifact hash differs from the sealed receipt" in tampered["message"],
                "negative_tamper", "a tampered artifact was not rejected exactly: "
                + json.dumps(tampered)[:600])
    after_tamper = snapshot(state, aid)
    loop.expect(after_tamper["assignment"]["holds_slot"] == 1
                and after_tamper["assignment"]["review_status"] == "awaiting_review"
                and after_tamper["task"]["task_state"] == "review_ready"
                and not after_tamper["reviews"]
                and after_tamper["evidence"] == sealed_evidence["evidence"],
                "negative_tamper", "the tampered close released, accepted, or resealed evidence")
    result_path.write_bytes(pristine)
    restored = sha(result_path)
    loop.expect(any(row["path"].endswith("RESULT.md") and row["sealed_sha256"] == restored
                    for row in sealed_evidence["evidence"]), "negative_tamper",
                "the original artifact bytes were not restored")

    revision = q1(state, "SELECT revision FROM v2_assignment WHERE assignment_id=?", (aid,))["revision"]
    closed = loop.command(state, actor, epoch, "assignment.close", {
        "assignment_id": aid, "expected_revision": revision, "outcome": "accepted",
        "event_id": event_id})
    loop.expect(closed.get("status") == "CLOSED" and closed.get("outcome") == "accepted",
                "close", "accepted close failed: " + json.dumps(closed)[:500])
    sealed = {"task": q1(state, "SELECT task_state,accepted_evidence_id FROM v2_task "
                                "WHERE task_id='NATIVE-A'"),
              "evidence": q(state, "SELECT path,sealed_sha256,bytes,sealed,state FROM v2_evidence "
                                   "WHERE task_id='NATIVE-A' ORDER BY path"),
              "reviews": q(state, "SELECT verdict,note_path,note_sha256,state FROM v2_review "
                                   "WHERE task_id='NATIVE-A'")}
    loop.expect(sealed["task"]["task_state"] == "accepted" and sealed["reviews"], "close",
                "accepted state is incomplete")
    emit({"stage": "tamper_rejected_then_accepted", "tamper_error": tampered,
          "tamper_state_unchanged": True, "restored_sha256": sha(result_path),
          "close": closed, "sealed": sealed}, "3")

    ws_revision = q1(state, "SELECT revision FROM v2_workspace WHERE workspace_id=?",
                     (ws_a["workspace_id"],))["revision"]
    frozen = loop.workspace_call(state, actor, epoch, "workspace.freeze", {
        "workspace_id": ws_a["workspace_id"], "expected_revision": ws_revision,
        "evidence_root": str(root / "quiescence")})
    loop.expect(frozen.get("status") == "WORKSPACE_FROZEN"
                and frozen["workspace"]["status"] == "frozen", "workspace_freeze",
                "accepted workspace was not frozen: " + json.dumps(frozen)[:500])
    quiescence = json.loads(q1(state, "SELECT quiescence_json FROM v2_workspace WHERE "
                                     "workspace_id=?", (ws_a["workspace_id"],))["quiescence_json"])
    loop.expect(Path(quiescence["evidence_path"]).is_file()
                and sha(quiescence["evidence_path"]) == quiescence["evidence_sha256"],
                "workspace_freeze", "freeze quiescence evidence is missing or changed")
    emit({"stage": "workspace_freeze", "status": frozen["workspace"]["status"],
          "quiescence_sha256": quiescence["evidence_sha256"],
          "holds_slot": q1(state, "SELECT holds_slot FROM v2_assignment WHERE assignment_id=?",
                           (aid,))["holds_slot"]}, "3")

    unlocked_b = loop.call(state, root, worker, "awo_assignment_claim", loop.args_for(
        epoch, task_id="NATIVE-B", expected_revision=queued_b["revision"]))
    loop.expect(unlocked_b.get("status") == "RESERVED", "unlock",
                "accepted recovery did not unlock the dependent Task: "
                + json.dumps(unlocked_b)[:500])
    bid = unlocked_b["assignment_id"]
    note_b = termination_note(root, "dependent-b", prepared_b["request"],
                              "Disposable dependency probe; never dispatched.")
    cancelled_b = cancel(state, actor, epoch, bid, note_b)
    budget_close_b = budget_close_run(state, actor, epoch, bid, unlocked_b["run_id"],
                                      q1(state, "SELECT generation FROM v2_assignment WHERE "
                                                "assignment_id=?", (bid,))["generation"],
                                      "actual", zero_usage("dependent probe never dispatched"))
    emit({"stage": "dependent_unlocked_then_cancelled", "assignment_id": bid,
          "status": unlocked_b["status"], "cancel": cancelled_b,
          "budget_close": budget_close_b}, "3")

    # ---------------------------------------------------------------- 5a: real actual usage overrun is post-hoc
    usage_report = job_usage(worker_dir, loop.task_key(request_a))
    usage, close_state = measured_usage(usage_report)
    limits_a = {"tokens.input": 10, "tokens.output": 1}
    budget_set(state, actor, epoch, "task", "NATIVE-A", limits_a)
    intent_a = q1(state, "SELECT run_id,attempt_id FROM v2_scheduler_intent WHERE assignment_id=?",
                  (aid,))
    close_a = budget_close_run(state, actor, epoch, aid, intent_a["run_id"],
                               q1(state, "SELECT generation FROM v2_assignment WHERE "
                                         "assignment_id=?", (aid,))["generation"],
                               close_state, usage)
    status_a = budget.budget_status(state, "task", "NATIVE-A")
    detected = status_a["state"] == "exceeded" and status_a["effective_usage"].get("tokens.input", 0) > limits_a["tokens.input"]
    loop.expect(detected, "budget_overrun",
                "the real actual usage was not detected as an overrun: "
                + json.dumps(status_a)[:600])
    scenario("5a_actual_usage_overrun", {
        "job_usage": usage_report, "close_state": close_state, "close_response": close_a,
        "limits": limits_a, "budget_status": status_a, "detected_post_hoc": detected,
        "mid_call_cancellation": False,
        "note": "Detection only after the native turn and budget.close; nothing cancelled the "
                "in-flight call and this fixture does not claim enforcement."})
    emit({"stage": "budget_overrun_detected", "limits": limits_a, "usage": usage,
          "state": status_a["state"], "effective_usage": status_a["effective_usage"]}, "5a")

    # ---------------------------------------------------------------- 3: concurrent duplicate claims
    task_create(state, root, controller, epoch, "RECOVERY-R3", "Concurrent claim fixture")
    request_r3, message_r3 = extra_request(run, root, "RECOVERY-R3", "RECOVERY-R3-RUN")
    queued_r3 = enqueue(state, actor, epoch, "RECOVERY-R3",
                        q1(state, "SELECT revision FROM v2_task WHERE task_id='RECOVERY-R3'")["revision"],
                        request_r3, message_r3, {"tokens.input": 40000, "tokens.output": 8000},
                        run["worker_agent"])
    gate = root / "faults" / "concurrent-start"
    gate.parent.mkdir(parents=True, exist_ok=True)
    claim_payload = {"task_id": "RECOVERY-R3", "expected_revision": queued_r3["revision"],
                     "agent_id": run["worker_agent"]}
    envelope_a = {"tool": "scheduler.claim", "payload": claim_payload,
                  "command_id": "v2recovery-claim-" + uuid.uuid4().hex, "expected_epoch": epoch}
    envelope_b = dict(envelope_a, command_id="v2recovery-claim-" + uuid.uuid4().hex)
    first = driver.spawn("claim-process-1", "execute", {
        "state": str(state), "actor": list(actor), "envelope": envelope_a,
        "start_gate": str(gate), "gate_seconds": 60})
    second_claim = driver.spawn("claim-process-2", "execute", {
        "state": str(state), "actor": list(actor), "envelope": envelope_b,
        "start_gate": str(gate), "gate_seconds": 60})
    gate.write_text("start\n", encoding="utf-8")
    driver.wait(first)
    driver.wait(second_claim)
    pairs = [(first, envelope_a), (second_claim, envelope_b)]
    values = [item["result"] for item, _ in pairs]
    winners = [(item, envelope) for item, envelope in pairs
               if item["result"].get("outcome") == "returned"
               and item["result"]["value"].get("status") == "RESERVED"]
    losers = [(item, envelope) for item, envelope in pairs if (item, envelope) not in winners]
    loop.expect(len(winners) == 1 and len(losers) == 1, "concurrent_claim",
                "two concurrent claim processes did not produce exactly one RESERVED: "
                + json.dumps(values)[:900])
    winner, winner_env = winners[0]
    loser, _ = losers[0]
    loser_result = loser["result"]
    if loser_result.get("outcome") == "exception":
        loop.expect(loser_result["message"].startswith("stale or invalid expected_revision; current is "),
                    "concurrent_claim",
                    "the losing claim process failed for an unexpected reason: "
                    + json.dumps(loser_result))
        loser_kind = "stale_expected_revision"
    else:
        loop.expect(loser_result["value"].get("status") == "WAITING"
                    and loser_result["value"].get("reasons"),
                    "concurrent_claim", "the losing claim process neither waited nor rejected: "
                    + json.dumps(loser_result))
        loser_kind = "waiting_admission"
    aid3 = winner["result"]["value"]["assignment_id"]
    rows3 = {"assignments": q(state, "SELECT assignment_id,holds_slot,state FROM v2_assignment "
                                     "WHERE task_id='RECOVERY-R3'"),
             "intents": q(state, "SELECT assignment_id,run_id,send_state FROM v2_scheduler_intent "
                                 "WHERE assignment_id=?", (aid3,)),
             "reservations": q(state, "SELECT b.scope_type,r.dimension,r.amount,r.state FROM "
                                      "v2_budget_reservation r JOIN v2_budget b ON b.budget_id=r.budget_id "
                                      "WHERE r.assignment_id=? ORDER BY b.scope_type,r.dimension", (aid3,)),
             "command_log": q(state, "SELECT command_id,tool FROM v2_command_log WHERE command_id=?",
                              (winner_env["command_id"],))}
    loop.expect(len(rows3["assignments"]) == 1 and rows3["assignments"][0]["holds_slot"] == 1
                and len(rows3["intents"]) == 1 and rows3["intents"][0]["send_state"] == "ready"
                and len(rows3["reservations"]) == 6
                and len(rows3["command_log"]) == 1, "concurrent_claim",
                "concurrent claim rows are not one assignment/intent, six held reservations and one "
                "recorded command: " + json.dumps(rows3)[:900])
    loop.expect(len(job_list(worker_dir)) == 1, "concurrent_claim",
                "admission sent a message instead of only reserving capacity")

    replay = driver.spawn("claim-replay", "execute", {
        "state": str(state), "actor": list(actor), "envelope": winner_env})
    driver.wait(replay)
    replay_value = replay["result"]["value"]
    loop.expect(replay_value.get("status") == "RESERVED" and replay_value.get("replayed") is True
                and replay_value.get("assignment_id") == aid3, "claim_replay",
                "identical command replay did not return the stored response: "
                + json.dumps(replay_value)[:600])
    conflict_env = dict(winner_env, payload=dict(claim_payload, agent_id=run["worker_agent"],
                                                 slot_id=1))
    conflict = driver.spawn("claim-conflict", "execute", {
        "state": str(state), "actor": list(actor), "envelope": conflict_env})
    driver.wait(conflict)
    conflict_result = conflict["result"]
    loop.expect(conflict_result["outcome"] == "exception"
                and conflict_result["message"] == "command_id was reused with a different tool or payload",
                "claim_conflict", "conflicting command reuse was not rejected exactly: "
                + json.dumps(conflict_result))
    loop.expect(not q1(state, "SELECT 1 AS x FROM v2_assignment WHERE task_id='RECOVERY-R3' "
                              "AND assignment_id<>?", (aid3,)), "claim_conflict",
                "the conflicting command created another Assignment")
    scenario("3_concurrent_claims", {
        "processes": [first, second_claim], "results": values,
        "loser_kind": loser_kind, "loser_result": loser_result,
        "winner_assignment_id": aid3, "db_rows": rows3,
        "jobs": job_list(worker_dir), "replay": replay_value,
        "conflict": conflict_result})
    emit({"stage": "concurrent_claims", "winner": aid3, "loser_kind": loser_kind,
          "statuses": [item["result"].get("value", {}).get("status")
                       or item["result"].get("message") for item in (first, second_claim)],
          "assignments": len(rows3["assignments"]),
          "reservations": len(rows3["reservations"]),
          "replay": replay_value.get("replayed")}, "3")

    # ---------------------------------------------------------------- 5b: lowered cap fences the reserved run
    budget_set(state, actor, epoch, "task", "RECOVERY-R3", {"tokens.input": 10})
    try:
        scheduler.dispatch_assignment(state, aid3, *actor, expected_epoch=epoch)
    except Exception as exc:
        fence = {"type": type(exc).__name__, "message": str(exc)}
    else:
        raise loop.Failure("budget_fence", "the reserved run was sent despite a lowered cap")
    loop.expect(fence["message"] == "Budget cap now blocks this reserved run.", "budget_fence",
                "the pre-send budget fence did not use the intended message: "
                + json.dumps(fence))
    after_fence = snapshot(state, aid3)
    loop.expect(after_fence["intent"]["send_state"] == "ready"
                and after_fence["intent"]["attempted_at"] is None
                and len(job_list(worker_dir)) == 1, "budget_fence",
                "the fenced run changed its send intent or sent a message")
    note_r3 = termination_note(root, "recovery-r3", request_r3,
                               "Budget-fenced before any send; disposable fixture.")
    cancel_r3 = cancel(state, actor, epoch, aid3, note_r3)
    budget_close_run(state, actor, epoch, aid3, rows3["intents"][0]["run_id"],
                     q1(state, "SELECT generation FROM v2_assignment WHERE assignment_id=?",
                        (aid3,))["generation"], "actual", zero_usage("budget fence"))
    scenario("5b_fenced_reserved_run", {
        "fence": fence, "intent_after": after_fence["intent"],
        "jobs": job_list(worker_dir), "send_count_for_run": 0, "cancel": cancel_r3})
    emit({"stage": "budget_fence", "message": fence["message"],
          "send_state": after_fence["intent"]["send_state"]}, "5b")

    # ---------------------------------------------------------------- 5c: admission refusal
    task_create(state, root, controller, epoch, "RECOVERY-R5", "Admission budget fixture")
    budget_set(state, actor, epoch, "task", "RECOVERY-R5", {"tokens.input": 10})
    request_r5, message_r5 = extra_request(run, root, "RECOVERY-R5", "RECOVERY-R5-RUN")
    queued_r5 = enqueue(state, actor, epoch, "RECOVERY-R5",
                        q1(state, "SELECT revision FROM v2_task WHERE task_id='RECOVERY-R5'")["revision"],
                        request_r5, message_r5, {"tokens.input": 40000, "tokens.output": 8000},
                        run["worker_agent"])
    refused = loop.call(state, root, worker, "awo_assignment_claim", loop.args_for(
        epoch, task_id="RECOVERY-R5", expected_revision=queued_r5["revision"]))
    loop.expect(refused.get("status") == "WAITING"
                and any(item["reason"] == "budget cap exceeded for task:tokens.input"
                        for item in refused.get("reasons", []))
                and not q1(state, "SELECT 1 AS x FROM v2_assignment WHERE task_id='RECOVERY-R5'"),
                "budget_refusal", "admission did not refuse the over-cap estimate exactly: "
                + json.dumps(refused)[:700])
    scenario("5c_admission_refused", {
        "limits": {"tokens.input": 10}, "estimate": {"tokens.input": 40000},
        "claim": refused, "assignment_rows": 0})
    emit({"stage": "budget_admission_refused", "reasons": refused.get("reasons")}, "5c")

    # ---------------------------------------------------------------- 1b: kill before send
    task_create(state, root, controller, epoch, "RECOVERY-B", "Pre-send fault fixture")
    request_b, message_b = extra_request(run, root, "RECOVERY-B", "RECOVERY-B-RUN")
    queued_b2 = enqueue(state, actor, epoch, "RECOVERY-B",
                        q1(state, "SELECT revision FROM v2_task WHERE task_id='RECOVERY-B'")["revision"],
                        request_b, message_b, {"tokens.input": 1000, "tokens.output": 1000},
                        run["worker_agent"])
    claim_b = loop.call(state, root, worker, "awo_assignment_claim", loop.args_for(
        epoch, task_id="RECOVERY-B", expected_revision=queued_b2["revision"]))
    loop.expect(claim_b.get("status") == "RESERVED", "claim_pre_send",
                "pre-send claim failed: " + json.dumps(claim_b)[:400])
    bid2 = claim_b["assignment_id"]
    label = "dispatch-before-send"
    fault_dir = root / "faults" / label
    fault_dir.mkdir(parents=True, exist_ok=False)
    payload["processes"].append(driver.spawn(label, "dispatch", {
        "state": str(state), "assignment_id": bid2, "actor": list(actor), "epoch": epoch,
        "fault": "before_send", "marker": str(fault_dir / "before_send.json"),
        "unblock": str(fault_dir / "release"), "block_seconds": 120}))
    pre_send_record = driver.records[-1]
    marker = wait_file(fault_dir / "before_send.json", 60, "fault_window_pre", label)
    loop.expect(marker.get("stage") == "before_send" and marker.get("sent") is False,
                "fault_window_pre", "the pre-send marker is not the expected window")
    driver.stop(pre_send_record, "kill the disposable driver inside send() before the bridge "
                                 "was invoked")
    prekill = snapshot(state, bid2)
    check = database_check(state)
    loop.expect(prekill["intent"]["send_state"] == "attempted"
                and prekill["intent"]["attempted_at"] is not None
                and prekill["assignment"]["holds_slot"] == 1
                and "dispatch_attempted" in prekill["journal"]
                and "dispatch_submitted" not in prekill["journal"]
                and len(job_list(worker_dir)) == 1,
                "pre_send_kill", "the pre-send kill did not leave one attempted, unsent intent")
    loop.expect(len(job_list(worker_dir)) == 1 and check["integrity_check"] == "ok",
                "pre_send_kill", "the pre-send kill sent a job or damaged the database")
    restart = driver.spawn("dispatch-before-restart", "dispatch_once", {
        "state": str(state), "assignment_id": bid2, "actor": list(actor), "epoch": epoch})
    driver.wait(restart)
    restarted = restart["result"]["value"]
    loop.expect(restarted.get("status") == "ALREADY_ATTEMPTED"
                and restarted.get("sent") is False and len(job_list(worker_dir)) == 1,
                "no_blind_resend_pre", "the restarted pre-send dispatch sent or retried: "
                + json.dumps(restarted)[:500])
    reconcile_b = driver.spawn("reconcile-uncertain-b", "reconcile", {
        "state": str(state), "assignment_id": bid2, "actor": list(actor), "epoch": epoch,
        "loop": True, "interval": 0.8, "max_observations": 5})
    driver.wait(reconcile_b, timeout=60)
    observations_b = driver.observations(reconcile_b)
    final_observation = observations_b[-1]["result"] if observations_b else {}
    uncertain = snapshot(state, bid2)
    loop.expect(final_observation.get("claims_retained") is True
                and uncertain["assignment"]["holds_slot"] == 1
                and uncertain["intent"]["send_state"] == "uncertain"
                and not uncertain["reviews"], "uncertain_retained",
                "the restarted observer did not retain the uncertain claim: "
                + json.dumps(final_observation)[:500])
    note_b2 = termination_note(root, "recovery-b", request_b,
                               "Inspected uncertain pre-send intent; permitted termination.")
    cancel_b2 = cancel(state, actor, epoch, bid2, note_b2)
    budget_close_run(state, actor, epoch, bid2, claim_b["run_id"],
                     q1(state, "SELECT generation FROM v2_assignment WHERE assignment_id=?",
                        (bid2,))["generation"], "actual", zero_usage("pre-send kill"))
    scenario("1b_pre_send_kill", {
        "driver": pre_send_record, "kill": pre_send_record["kill"],
        "prekill_intent": prekill["intent"], "journal": prekill["journal"],
        "restart_result": restarted, "observations": observations_b,
        "uncertain_assignment": uncertain["assignment"], "cancel": cancel_b2,
        "jobs": job_list(worker_dir), "send_count_for_run": 0})
    emit({"stage": "pre_send_kill_complete", "restart": restarted.get("status"),
          "last_observation": final_observation.get("status"),
          "cancel": cancel_b2.get("status")}, "1b")

    # ---------------------------------------------------------------- 5d: unknown usage blocks admission
    budget_set(state, actor, epoch, "workflow", run["project_id"],
               {"tokens.input": 1000000, "tokens.output": 100000})
    task_create(state, root, controller, epoch, "RECOVERY-R6", "Unknown-usage fixture")
    request_r6, message_r6 = extra_request(run, root, "RECOVERY-R6", "RECOVERY-R6-RUN")
    queued_r6 = enqueue(state, actor, epoch, "RECOVERY-R6",
                        q1(state, "SELECT revision FROM v2_task WHERE task_id='RECOVERY-R6'")["revision"],
                        request_r6, message_r6, {"tokens.input": 40000, "tokens.output": 8000},
                        run["worker_agent"])
    claim_r6 = loop.call(state, root, worker, "awo_assignment_claim", loop.args_for(
        epoch, task_id="RECOVERY-R6", expected_revision=queued_r6["revision"]))
    loop.expect(claim_r6.get("status") == "RESERVED", "unknown_setup",
                "R6 claim failed: " + json.dumps(claim_r6)[:400])
    aid6 = claim_r6["assignment_id"]
    intent6 = q1(state, "SELECT run_id FROM v2_scheduler_intent WHERE assignment_id=?", (aid6,))
    unknown_usage = {dim: {"state": "unknown",
                           "source": "disposable fixture: no adapter call was made",
                           "reason": "provider usage was not observed for this disposable run"}
                     for dim in ("tokens.input", "tokens.output")}
    close_unknown = budget_close_run(state, actor, epoch, aid6, intent6["run_id"],
                                     q1(state, "SELECT generation FROM v2_assignment WHERE "
                                               "assignment_id=?", (aid6,))["generation"],
                                     "unknown", unknown_usage)
    note_r6 = termination_note(root, "recovery-r6", request_r6,
                               "Unknown usage retained after budget.close; disposable fixture.")
    cancel_r6 = cancel(state, actor, epoch, aid6, note_r6)
    workflow_after = budget.budget_status(state, "workflow", run["project_id"])
    task_create(state, root, controller, epoch, "RECOVERY-R7", "Unknown-usage blocker fixture")
    request_r7, message_r7 = extra_request(run, root, "RECOVERY-R7", "RECOVERY-R7-RUN")
    queued_r7 = enqueue(state, actor, epoch, "RECOVERY-R7",
                        q1(state, "SELECT revision FROM v2_task WHERE task_id='RECOVERY-R7'")["revision"],
                        request_r7, message_r7, {"tokens.input": 1, "tokens.output": 1},
                        run["worker_agent"])
    blocked_r7 = loop.call(state, root, worker, "awo_assignment_claim", loop.args_for(
        epoch, task_id="RECOVERY-R7", expected_revision=queued_r7["revision"]))
    loop.expect(blocked_r7.get("status") == "WAITING"
                and any(item["reason"] == "unknown usage blocks admission for tokens.input"
                        for item in blocked_r7.get("reasons", []))
                and not q1(state, "SELECT 1 AS x FROM v2_assignment WHERE task_id='RECOVERY-R7'"),
                "unknown_block", "unknown usage did not block the later admission exactly: "
                + json.dumps(blocked_r7)[:700])
    scenario("5d_unknown_blocks_admission", {
        "unknown_close": close_unknown, "workflow_after_close": workflow_after,
        "cancel_r6": cancel_r6, "claim_r7": blocked_r7,
        "retained_unknown_reservations": q(state, "SELECT r.assignment_id,r.dimension,r.state "
                                                  "FROM v2_budget_reservation r JOIN v2_budget b "
                                                  "ON b.budget_id=r.budget_id WHERE b.scope_type='workflow' "
                                                  "AND r.state='unknown' ORDER BY r.assignment_id")})
    emit({"stage": "unknown_usage_blocked", "reasons": blocked_r7.get("reasons"),
          "workflow_unknown": workflow_after["unknown"]}, "5d")

    # ---------------------------------------------------------------- final state
    final = {
        "assignments": q(state, "SELECT task_id,assignment_id,state,review_status,holds_slot,generation "
                                "FROM v2_assignment ORDER BY task_id"),
        "tasks": q(state, "SELECT task_id,task_state,revision,accepted_evidence_id FROM v2_task "
                          "ORDER BY task_id"),
        "open_slots": q(state, "SELECT assignment_id FROM v2_assignment WHERE holds_slot=1"),
        "workspaces": q(state, "SELECT assignment_id,kind,status,path FROM v2_workspace ORDER BY assignment_id"),
        "reviews": q(state, "SELECT task_id,assignment_id,verdict,state FROM v2_review ORDER BY task_id"),
        "evidence": q(state, "SELECT task_id,path,sealed_sha256,sealed,state FROM v2_evidence ORDER BY task_id,path"),
        "jobs": job_list(worker_dir),
        "worker_status": native_worker.status(worker_dir),
        "reviewer_status": native_worker.status(reviewer_dir),
    }
    loop.expect(not final["open_slots"], "final_state", "an Assignment still holds capacity")
    loop.expect(final["worker_status"].get("status") == "idle"
                and final["reviewer_status"].get("status") == "idle", "final_state",
                "a native worker is not idle at the end of the fixture")
    for record in driver.records:
        if "kill" not in record:
            loop.expect(record.get("exit_code") is not None, "final_state",
                        "a disposable child did not exit: " + record["label"])
    payload.update(status=PASSED[meta["evidence_class"]], stage="complete",
                   final=final, finished_at=monitor_task.utc_now(),
                   processes=driver.records,
                   send_counts={"native_a_jobs": len(final["jobs"]),
                                "pre_send_jobs": 0,
                                "scheduler_restarts": 2},
                   budget={"task_a": budget.budget_status(state, "task", "NATIVE-A"),
                           "workflow": budget.budget_status(state, "workflow", run["project_id"]),
                           "task_r3": budget.budget_status(state, "task", "RECOVERY-R3")},
                   driver_shutdown=[{"label": item["label"], "command": item["command"],
                                     "pid": item["pid"], "kill": item.get("kill"),
                                     "exit_code": item.get("exit_code")}
                                    for item in driver.records])
    scenario("final_state", final)
    emit({"stage": "final_state", **final}, "final")
    return loop.finish(root, payload, traces)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--harness", choices=("codex", "pi", "omp"))
    parser.add_argument("--executable")
    parser.add_argument("--mode", choices=("one-shot", "session"), default="one-shot")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--evidence-class", choices=EVIDENCE_CLASSES, default="native")
    parser.add_argument("--fixture-script", help="only for labelled protocol-fixture evidence")
    args = parser.parse_args()
    root = Path(args.directory).resolve()
    if root.exists():
        parser.error("a new validation directory is required; existing evidence is never overwritten")
    evidence_class = args.evidence_class
    try:
        if not args.harness:
            parser.error("--harness is required")
        candidates = ([args.harness + suffix for suffix in (".exe", ".cmd", "")]
                      if sys.platform == "win32" else [args.harness])
        executable = args.executable or next(
            (found for name in candidates if (found := shutil.which(name))), None)
        if not executable:
            parser.error("native CLI executable not found")
        loop.prepare(root, args.harness, executable, args.mode, args.timeout,
                     evidence_class, args.fixture_script)
        result = execute_recovery(root)
    except Exception as exc:
        if not root.exists():
            raise
        result = {"status": FAILED[evidence_class], "stage": getattr(exc, "stage", "unknown"),
                  "error": str(exc), "herdr_invoked": False, "evidence_class": evidence_class}
        try:
            loop.finish(root, result, [])
        except Exception as nested:
            result["finish_error"] = str(nested)
        print(json.dumps(result, ensure_ascii=True))
        return 2
    print(json.dumps({key: value for key, value in result.items() if key not in ("final",)},
                     ensure_ascii=True))
    return 0 if result["status"] in PASSED.values() else 2


if __name__ == "__main__":
    sys.exit(main())
