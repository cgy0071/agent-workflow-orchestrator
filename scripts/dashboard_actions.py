"""Dashboard management actions: each is a read-only preview, then one confirm that repeats every check.

The page never answers approval prompts, never resends a task, and never decides acceptance. Actions call the
same ledger functions as the command line (reconcile, continue, resolve-task, release, tick, set-config, bind,
activate, label-panes) as the controller bound at dashboard startup.
"""

import hmac
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

import workflow_control as control
import workflow_scheduler as scheduler
from monitor_task import identity_matches

SCRIPTS = Path(__file__).resolve().parent
NOTE_LIMIT = 4000  # characters of an operator observation written into an evidence file
PREVIEW_TTL = 600  # seconds a preview stays confirmable
TASK_ID = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
DISPATCH_OPEN = ("reserved", "uncertain", "submitted")
ACTIONS = ("reconcile", "observe_start", "monitor_start", "monitor_stop", "continue", "close", "release", "tick",
           "config", "labels", "activate", "assign")
ASSIGN_MODES = ("worker", "reviewer", "advisor")


def _text(payload, key, limit=NOTE_LIMIT):
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise ValueError(key.replace("_", " ") + " must be 1 to " + str(limit) + " characters of text")
    return value.replace("\r\n", "\n").strip()


def _write_once(path, text):
    """Create a file exclusively; an identical file left by an interrupted confirm is reused."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
    except FileExistsError:
        if path.read_text(encoding="utf-8-sig") != text:
            raise FileExistsError(str(path) + " already exists with different content; inspect it first")


def _check_absent_or_same(path, text):
    if path.exists() and path.read_text(encoding="utf-8-sig") != text:
        raise FileExistsError(str(path) + " already exists with different content; inspect it first")


def _request_text(request):
    return json.dumps(request, ensure_ascii=True, indent=2) + "\n"


def _monitor_dir(tasks_root, task_id):
    # Dashboard-dispatched tasks already live here; other tasks get a fresh folder that no other task uses.
    return (tasks_root / task_id).resolve()


class Manager:
    """Preview/confirm workflow management for the controller bound at dashboard startup."""

    def __init__(self, state, actor, version, tasks_root, transport_factory, popen=subprocess.Popen):
        self.state, self.actor, self.version = state, actor, version
        self.tasks_root, self.transport_factory, self.popen = tasks_root, transport_factory, popen
        self.records, self.lock, self.records_lock = {}, threading.Lock(), threading.Lock()

    def who(self):
        return (self.actor, self.version, "main")

    def transport(self):
        return self.transport_factory({"monitor": {"command_timeout_seconds": 15}})

    def _guard(self, con):
        who = control.guard(con, self.actor, self.version, "main", "dispatch")
        if who["mode"] != "controller":
            raise PermissionError("management needs the active controller")
        return who

    def _task(self, con, task_id):
        if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
            raise ValueError("invalid task ID")
        row = con.execute("SELECT status,data FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise ValueError("unknown task")
        dispatch = con.execute("SELECT status,data FROM dispatches WHERE task=?", (task_id,)).fetchone() \
            if scheduler.has_schema(con) else None
        observation = con.execute("SELECT data FROM task_observations WHERE task=?", (task_id,)).fetchone() \
            if scheduler.has_schema(con) else None
        reports = [json.loads(item[0])["report"]["status"] for item in
                   con.execute("SELECT data FROM events WHERE task=? ORDER BY rowid", (task_id,))]
        return {"status": row["status"], "request": json.loads(row["data"])["request"],
                "dispatch": dispatch["status"] if dispatch else None,
                "observation": json.loads(observation[0]) if observation else {}, "reports": reports}

    # Each prepare_* is read-only on the ledger and the files; it returns the exact step a confirm performs.
    def prepare(self, action, payload):
        if action not in ACTIONS:
            raise ValueError("unknown management action")
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
        with scheduler._read_database(self.state) as con:
            self._guard(con)
            step = getattr(self, "_prepare_" + action)(con, payload)
        step["action"] = action
        step["preview_sha256"] = control.digest(step)
        return step

    def preview(self, action, payload):
        step = self.prepare(action, payload)
        with self.records_lock:
            now = time.monotonic()
            for key in [k for k, at in self.records.items() if now - at > PREVIEW_TTL]:
                del self.records[key]
            self.records[step["preview_sha256"]] = now
        return step

    def confirm(self, action, payload):
        supplied = payload.get("preview_sha256") if isinstance(payload, dict) else None
        if not isinstance(supplied, str):
            raise ValueError("preview_sha256 is required")
        with self.lock:
            step = self.prepare(action, payload)
            if not hmac.compare_digest(supplied, step["preview_sha256"]):
                raise ValueError("the state changed since the preview; preview again")
            with self.records_lock:
                created = self.records.pop(step["preview_sha256"], None)
            if created is None or time.monotonic() - created > PREVIEW_TTL:
                raise ValueError("preview missing or expired; preview again")
            result = getattr(self, "_run_" + action)(step)
        return dict(result, action=action)

    # Confirm that a send started (or never left), from an observation the operator typed after looking.
    def _prepare_reconcile(self, con, payload):
        task = self._task(con, payload.get("task_id"))
        outcome = payload.get("outcome")
        if outcome not in ("running", "not_sent"):
            raise ValueError("outcome must be running or not_sent; use Close attempt for a stopped worker")
        if task["dispatch"] not in DISPATCH_OPEN:
            raise ValueError("this task has no unconfirmed send (dispatch is " + str(task["dispatch"]) + ")")
        request, folder = task["request"], _monitor_dir(self.tasks_root, payload["task_id"])
        observed = _text(payload, "observation")
        name = "EXECUTION.md" if outcome == "running" else "NOT_SENT.md"
        text = ("Task ID: " + request["task_id"] + "\nNonce: " + request["nonce"] + "\nOutcome: " + outcome +
                "\nObserved execution: " + observed + "\nRecorded through the dashboard by controller " +
                self.actor + " version " + str(self.version) + ".\n")
        evidence = folder / name
        _check_absent_or_same(evidence, text)
        return {"task_id": request["task_id"], "outcome": outcome, "evidence_path": str(evidence),
                "evidence_text": text, "request_out": str(folder / "confirmed.json") if outcome == "running" else None}

    def _run_reconcile(self, step):
        _write_once(Path(step["evidence_path"]), step["evidence_text"])
        result = scheduler.reconcile(self.state, step["task_id"], step["outcome"], step["evidence_path"], *self.who())
        if step["request_out"]:
            _write_once(Path(step["request_out"]), _request_text(result["request"]))
        return {"status": result["status"], "task_id": step["task_id"], "request_out": step["request_out"]}

    def _prepare_observe_start(self, con, payload):
        task = self._task(con, payload.get("task_id"))
        if task["dispatch"] not in DISPATCH_OPEN:
            raise ValueError("this task has no unconfirmed send")
        observed = scheduler.observe_start(self.state, payload["task_id"], self.transport())
        return dict(observed, request_out=str(Path(task["request"]["cwd"]) / "confirmed.json"))

    def _run_observe_start(self, step):
        if step["status"] != "OBSERVED":
            raise ValueError("start was not observed; do not confirm")
        return scheduler.confirm_start(self.state, step["task_id"], step["request_out"],
                                       *self.who(), transport=self.transport())

    def monitor_info(self, task_id):
        folder = _monitor_dir(self.tasks_root, task_id)
        info = {"folder": str(folder), "state": "not_started"}
        if (folder / "monitor.lock").exists():
            info["state"] = "stopping" if (folder / "STOP_MONITOR").exists() else "running"
        elif (folder / "monitor_summary.json").exists():
            info["state"] = "finished"
        return info

    # Start the background monitor for a confirmed attempt; the monitor itself only reads the worker.
    def _prepare_monitor_start(self, con, payload):
        task = self._task(con, payload.get("task_id"))
        request = task["request"]
        if task["status"] != "running" or not request.get("dispatch_confirmed"):
            raise ValueError("start monitoring after the send is confirmed and while the attempt is open")
        if task["dispatch"] not in (None, "running"):
            raise ValueError("confirm the send first (dispatch is " + str(task["dispatch"]) + ")")
        info = self.monitor_info(request["task_id"])
        if info["state"] != "not_started":
            raise ValueError("monitor is already " + info["state"] + " in " + info["folder"] + "; inspect it first")
        folder = Path(info["folder"])
        path = folder / "confirmed.json"
        _check_absent_or_same(path, _request_text(request))
        notify = request["authorization"].get("notify_reviewer") is True
        argv = [sys.executable, "-B", str(SCRIPTS / "monitor_task.py"), "--request", str(path)] + (["--notify"] if notify else [])
        return {"task_id": request["task_id"], "request_path": str(path), "request_text": _request_text(request),
                "argv": argv, "notify": notify, "interval_seconds": request.get("monitor", {}).get("interval_seconds", 300)}

    def _run_monitor_start(self, step):
        path = Path(step["request_path"])
        _write_once(path, step["request_text"])
        options = {"cwd": str(path.parent), "stdin": subprocess.DEVNULL}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
        with (path.parent / "monitor.stdout.log").open("ab") as out, (path.parent / "monitor.stderr.log").open("ab") as err:
            process = self.popen(step["argv"], stdout=out, stderr=err, **options)
        with control.database(self.state) as con:
            control.audit(con, "dashboard_monitor_started", {"actor": self.actor, "version": self.version,
                                                             "task_id": step["task_id"], "pid": process.pid,
                                                             "notify": step["notify"]})
        return {"status": "MONITOR_STARTED", "task_id": step["task_id"], "pid": process.pid}

    def _prepare_monitor_stop(self, con, payload):
        task = self._task(con, payload.get("task_id"))
        info = self.monitor_info(payload["task_id"])
        if info["state"] != "running":
            raise ValueError("no running monitor was found in " + info["folder"])
        return {"task_id": payload["task_id"], "stop_file": str(Path(info["folder"]) / "STOP_MONITOR"),
                "task_status": task["status"]}

    def _run_monitor_stop(self, step):
        _write_once(Path(step["stop_file"]), "Stop requested through the dashboard by " + self.actor + ".\n")
        return {"status": "MONITOR_STOP_REQUESTED", "task_id": step["task_id"]}

    # Ask a stalled worker to resume the same attempt; workflow_scheduler.continue_task enforces the budget.
    def _prepare_continue(self, con, payload):
        task = self._task(con, payload.get("task_id"))
        if task["status"] != "running" or task["dispatch"] != "running":
            raise ValueError("continue needs a running attempt with a confirmed send")
        observation = task["observation"]
        if observation.get("status") != "incomplete":
            raise ValueError("continue applies only while the monitor reports incomplete; a blocked worker needs "
                             "the Owner to answer its prompt in the harness")
        done = [json.loads(item[0]) for item in con.execute(
            "SELECT data FROM task_journal WHERE task=? AND kind='continue_reserved'", (payload["task_id"],))]
        budget = control.config_of(con).get("scheduler", {}).get("max_continue_prompts", 2)
        episode = observation.get("stall_episode", 1)
        if any(item.get("stall_episode") == episode for item in done):
            raise ValueError("this stall episode already received a continuation; wait for the monitor")
        if len(done) >= budget:
            raise ValueError("continuation budget exhausted (" + str(budget) + "); close the attempt instead")
        return {"task_id": payload["task_id"], "stall_episode": episode, "used": len(done), "budget": budget,
                "message": scheduler._continue_message(task["request"])}

    def _run_continue(self, step):
        return scheduler.continue_task(self.state, step["task_id"], *self.who(), transport=self.transport())

    # Record an inspected stop of the attempt. It never cancels the worker process.
    def _prepare_close(self, con, payload):
        task = self._task(con, payload.get("task_id"))
        if task["status"] == "resolved":
            raise ValueError("this attempt is already closed")
        request, folder = task["request"], _monitor_dir(self.tasks_root, payload["task_id"])
        reason = _text(payload, "reason")
        evidence = folder / "TERMINATION.md"
        if task["reports"][-1:] == ["review_ready"]:
            raise ValueError("this attempt delivered work for review; record a review instead of closing it")
        if task["dispatch"] is not None:
            text = ("Task ID: " + request["task_id"] + "\nNonce: " + request["nonce"] + "\nOutcome: terminated\n"
                    "Inspected stop: " + reason + "\nRecorded through the dashboard by controller " + self.actor +
                    " version " + str(self.version) + ". The worker process was not cancelled.\n")
            how = "reconcile"
        else:
            if task["status"] != "running":
                raise ValueError("this attempt is not running; inspect it in the ledger")
            text = (request["task_id"] + " " + request["nonce"] + "\nInspected stop: " + reason +
                    "\nRecorded through the dashboard by controller " + self.actor + ". The worker was not cancelled.\n")
            how = "resolve"
        _check_absent_or_same(evidence, text)
        return {"task_id": request["task_id"], "evidence_path": str(evidence), "evidence_text": text, "how": how}

    def _run_close(self, step):
        _write_once(Path(step["evidence_path"]), step["evidence_text"])
        if step["how"] == "reconcile":
            result = scheduler.reconcile(self.state, step["task_id"], "terminated", step["evidence_path"], *self.who())
            return {"status": result["status"], "task_id": step["task_id"]}
        return control.resolve_task(self.state, step["task_id"], *self.who(), step["evidence_path"])

    def _prepare_release(self, con, payload):
        task = self._task(con, payload.get("task_id"))
        if task["status"] == "resolved":
            raise ValueError("the work slot is already released")
        if not scheduler.acceptance(con, payload["task_id"]):
            raise ValueError("release needs a valid accept review whose evidence is unchanged")
        return {"task_id": payload["task_id"]}

    def _run_release(self, step):
        return scheduler.release(self.state, step["task_id"], *self.who())

    # One scheduler pass for a frozen plan: dispatches only nodes whose prerequisites hold accepted evidence.
    def _prepare_tick(self, con, payload):
        run = payload.get("run")
        if not isinstance(run, str) or not run:
            raise ValueError("choose a task plan")
        graph, states = scheduler._dag_view(con, run)
        ready = [node["id"] for node in graph["nodes"] if states[node["id"]] == "ready"]
        accepted = [node["id"] for node in graph["nodes"] if states[node["id"]] == "accepted"]
        return {"run": run, "ready": ready, "release": accepted,
                "tasks": {node["id"]: node["request"]["task_id"] for node in graph["nodes"] if node["id"] in ready}}

    def _run_tick(self, step):
        return scheduler.tick(self.state, step["run"], *self.who(), transport=self.transport())

    def _prepare_config(self, con, payload):
        changes = payload.get("changes")
        old = control.config_of(con)
        new = control.apply_config_changes(old, changes)
        diff = [{"key": key, "old": control.config_value(old, key), "new": control.config_value(new, key)}
                for key in control.EDITABLE_CONFIG if control.config_value(old, key) != control.config_value(new, key)]
        if not diff:
            raise ValueError("these values are already in effect")
        return {"changes": changes, "diff": diff, "old_sha256": control.digest(old)}

    def _run_config(self, step):
        return control.update_config(self.state, step["changes"], *self.who())

    # Bindings. The controller binding that runs this dashboard is never changed from the page.
    def _prepare_labels(self, con, payload):
        force = payload.get("force", False)
        if type(force) is not bool:
            raise ValueError("force must be a boolean")
        return {"force": force}

    def _run_labels(self, step):
        return control.label_panes(self.state, force=step["force"])

    def _prepare_activate(self, con, payload):
        role = payload.get("role")
        row = con.execute("SELECT data FROM bindings WHERE role=? AND status='pending'", (role,)).fetchone()
        if not row:
            raise ValueError("no pending binding for this role")
        if con.execute("SELECT count(*) FROM tasks WHERE worker=? AND status='running'", (role,)).fetchone()[0]:
            raise ValueError("the old binding still has a running or uncertain task; close or finish it first")
        binding = json.loads(row[0])
        return {"role": role, "instance_id": binding["instance_id"], "version": binding["version"],
                "mode": binding["mode"], "pane": binding["spec"].get("pane")}

    def _observe(self, spec, mode):
        transport = self.transport()
        return transport.status(spec)["identity"] if mode == "worker" else transport.reviewer_identity(spec)

    def _run_activate(self, step):
        with control.database(self.state) as con:
            row = con.execute("SELECT data FROM bindings WHERE role=? AND status='pending'", (step["role"],)).fetchone()
        binding = json.loads(row[0]) if row else None
        if not binding or binding["instance_id"] != step["instance_id"]:
            raise ValueError("the pending binding changed; preview again")
        result = control.activate(self.state, step["role"], self._observe(binding["spec"], binding["mode"]))
        return {"status": "BINDING_ACTIVATED", "role": step["role"], "version": result["version"],
                "label": result.get("label"), "label_error": result.get("label_error")}

    def _prepare_assign(self, con, payload):
        role, mode, pane = payload.get("role"), payload.get("mode"), payload.get("pane")
        config = control.config_of(con)
        if role not in config["roles"]:
            raise ValueError("choose a configured role")
        if mode not in ASSIGN_MODES:
            raise ValueError("mode must be worker, reviewer or advisor; the controller changes only on the command line")
        me = self._guard(con)
        if role == me["role"]:
            raise ValueError("this dashboard runs as " + role + "; switch that role on the command line")
        if not isinstance(pane, str) or not pane:
            raise ValueError("choose a Herdr pane")
        current = con.execute("SELECT data FROM bindings WHERE role=? AND status='active'", (role,)).fetchone()
        current = json.loads(current[0]) if current else None
        if con.execute("SELECT 1 FROM bindings WHERE role=? AND status='pending'", (role,)).fetchone():
            raise ValueError("this role already has a pending binding; activate it first")
        reason = payload.get("reason", "")
        if current:
            reason = _text(payload, "reason", 500)
        elif not isinstance(reason, str) or len(reason) > 500:
            raise ValueError("reason must be at most 500 characters")
        running = con.execute("SELECT count(*) FROM tasks WHERE worker=? AND status='running'", (role,)).fetchone()[0]
        herdr = [json.loads(row[0])["spec"] for row in con.execute("SELECT data FROM bindings WHERE status='active'")]
        herdr = [spec for spec in herdr if spec["adapter"] == "herdr"]
        if not herdr:
            raise ValueError("no Herdr session is bound yet; bind the first pane on the command line")
        session = payload.get("session") or herdr[0]["session"]
        base = next((spec for spec in herdr if spec["session"] == session), None)
        if base is None:
            raise ValueError("choose a Herdr session that already holds a bound role")
        agent = next((a for a in self.transport().agents(base) if a.get("pane_id") == pane), None)
        if agent is None:
            raise ValueError("pane " + pane + " was not found in Herdr session " + session)
        if any(spec["session"] == session and spec.get("pane") == pane and spec.get("role") != role for spec in herdr):
            raise ValueError("pane " + pane + " is already bound to another role")
        identity = {"kind": agent.get("agent"), "terminal": agent.get("terminal_id"), "launch_cwd": agent.get("cwd")}
        if not all(isinstance(v, str) and v for v in identity.values()):
            raise ValueError("Herdr did not report the agent kind, terminal and directory for this pane")
        spec = {"role": role, "harness": payload.get("harness") or agent.get("agent"), "adapter": "herdr",
                "executable": base["executable"], "session": session, "pane": pane, "expected_identity": identity}
        if current and current["spec"] == spec and current["mode"] == mode:
            raise ValueError("this pane is already the active binding for " + role)
        return {"role": role, "mode": mode, "spec": spec, "reason": reason.strip(), "replaces": current["version"] if current else None,
                "defer": bool(current and running), "running_tasks": running,
                "pane_name": agent.get("name"), "pane_status": agent.get("agent_status")}

    def _run_assign(self, step):
        spec = control.prepare_binding_spec(step["spec"], step["mode"], "inbox_only", step["reason"])
        observed = self._observe(spec, step["mode"])
        if not identity_matches(observed, spec["expected_identity"]):
            raise ValueError("the pane identity changed since the preview; preview again")
        result = control.bind(self.state, step["role"], spec, observed, step["mode"], "inbox_only", step["defer"], step["reason"])
        return {"status": "BINDING_" + result["status"].upper(), "role": step["role"], "version": result["version"],
                "label": result.get("label"), "label_error": result.get("label_error")}

    def candidates(self):
        """Live Herdr panes and their roles for the assignment form; read-only."""
        return control.roles(self.state, self.transport())
