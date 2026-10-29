"""Regenerate docs/images/dashboard-overview.png. Run from the repository root.

The offline demo is extended with a short repair chain (one task stopped before delivery,
one waiting for review) and a few older finished tasks, so the picture shows the follow-up
chain card, the inbox, an alert and the folded history. No real workflow data is used.
Needs Microsoft Edge or Google Chrome; it starts a headless copy with a private profile.

  python -B docs/images/dashboard_shot.py
"""
import base64
import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
import dashboard  # noqa: E402
import demo_scheduler  # noqa: E402

BROWSERS = [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/microsoft-edge"]


class DevTools:
    """Minimal Chrome DevTools Protocol client over a stdlib WebSocket."""

    def __init__(self, url):
        host, path = url[len("ws://"):].split("/", 1)
        self.sock = socket.create_connection(tuple(host.rsplit(":", 1)[:1]) + (int(host.rsplit(":", 1)[1]),), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(("GET /%s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (path, host, key)).encode())
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            buffer += self.sock.recv(4096)
        self.extra, self.count = buffer.split(b"\r\n\r\n", 1)[1], 0

    def _read(self, size):
        while len(self.extra) < size:
            self.extra += self.sock.recv(65536)
        data, self.extra = self.extra[:size], self.extra[size:]
        return data

    def _message(self):
        parts = []
        while True:
            first, second = self._read(2)
            size = second & 127
            if size == 126:
                size = struct.unpack(">H", self._read(2))[0]
            elif size == 127:
                size = struct.unpack(">Q", self._read(8))[0]
            parts.append(self._read(size))
            if first & 0x80:
                return b"".join(parts)

    def call(self, method, **params):
        self.count += 1
        payload = json.dumps({"id": self.count, "method": method, "params": params}).encode()
        mask, size = os.urandom(4), len(payload)
        head = bytes([0x81]) + (bytes([0x80 | size]) if size < 126 else
                                bytes([0x80 | 126]) + struct.pack(">H", size) if size < 65536 else
                                bytes([0x80 | 127]) + struct.pack(">Q", size))
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
        while True:
            reply = json.loads(self._message())
            if reply.get("id") == self.count:
                if "error" in reply:
                    raise RuntimeError(reply["error"])
                return reply.get("result", {})


def build_ledger(root):
    """Real dispatch, reconcile, delivery and review through the scheduler; only timestamps are moved back."""
    import workflow_control as control
    import workflow_scheduler as scheduler
    from monitor_task import Transport, inspect_once
    with contextlib.redirect_stdout(io.StringIO()):
        demo_scheduler.demo(root / "first-run")
    state = root / "first-run" / "state"
    with sqlite3.connect(state / "workflow.sqlite3") as con:  # The repair step needs a configured repair verdict.
        config = json.loads(con.execute("SELECT value FROM meta WHERE key='config'").fetchone()[0])
        config["workflow"]["transitions"]["repair"] = "repair"
        con.execute("UPDATE meta SET value=? WHERE key='config'", (json.dumps(config),))
    with control.database(state) as con:
        who = (control.active(con, "planner")["instance_id"], control.active(con, "planner")["version"], "main")
        specs = {role: control.active(con, role)["spec"] for role in ("planner", "executor", "executor-b")}

    def run(task_id, title, role, parent=None):
        cwd = root / "work" / ".ai" / "history" / task_id
        cwd.mkdir(parents=True)
        (cwd / "TASK.md").write_text("# " + task_id + " - " + title + "\n\nOnly this case runs; earlier code stays frozen.\n",
                                     encoding="utf-8")
        baseline = Transport({}).status(specs[role])["completion_seq"]
        marker = "TASK_DONE " + task_id + " nonce-" + task_id
        request = {"task_id": task_id, "nonce": "nonce-" + task_id, "generation": 1, "cwd": str(cwd),
                   "workflow_state": str(state), "worker": dict(specs[role], baseline_completion_seq=baseline),
                   "reviewer": specs["planner"], "dispatch_confirmed": False,
                   "authorization": {"source": "local Python-child fixture only", "dispatch_worker": True, "notify_reviewer": False},
                   "completion": {"binding_artifact": "HANDOFF.md", "artifacts": ["RESULT.md"], "marker": marker,
                                  "marker_source": "artifact"}}
        if parent:
            request.update(parent_task=parent[0], parent_review=parent[1])
        message = json.dumps({"task_id": task_id, "nonce": request["nonce"], "cwd": str(cwd), "marker": marker,
                              "completion_status": "idle"})
        if scheduler.dispatch(state, request, message, *who)["status"] != "SUBMITTED":
            raise RuntimeError("fixture dispatch failed: " + task_id)
        note = cwd / "EXECUTION.md"
        note.write_text(task_id + " " + request["nonce"] + "\nOutcome: running\nObserved the fixture worker.\n", encoding="utf-8")
        return scheduler.reconcile(state, task_id, "running", note, *who)["request"]

    def deliver(request):
        status, _, _, receipt = inspect_once(request, Transport(request))
        if status != "review_ready":
            raise RuntimeError("fixture delivery missing: " + request["task_id"])
        scheduler.record_observation(state, request, {"task_id": request["task_id"], "nonce": request["nonce"],
                                                      "status": status, "observed_at": datetime.now(timezone.utc).isoformat()})
        return control.publish_event(state, request, {"status": status}, receipt)

    def review(request, event, verdict):
        control.acknowledge(state, event, *who)
        path = Path(request["cwd"]) / "REVIEW.md"
        path.write_text(request["task_id"] + " " + request["nonce"] + "\nVerdict: " + verdict + "\n", encoding="utf-8")
        control.record_review(state, event, *who, path, verdict)
        gate = control.next_gate(state, event, *who)["parent_review"]
        if verdict == "accept":
            scheduler.release(state, request["task_id"], *who)
        else:  # A repaired attempt is closed after inspection so its follow-up can use the same worker.
            note = Path(request["cwd"]) / "CLOSED.md"
            note.write_text(request["task_id"] + " " + request["nonce"] + "\nOutcome: terminated\nRepair planned.\n", encoding="utf-8")
            scheduler.reconcile(state, request["task_id"], "terminated", note, *who)
        return request["task_id"], gate

    for n in range(4):  # Older finished work that the default range folds away.
        request = run("CHECK-%d" % (n + 1), "Archived interface check %d" % (n + 1), "executor")
        review(request, deliver(request), "accept")
    first = run("WIRE-001", "Keep partial responses after disconnect", "executor")
    parent = review(first, deliver(first), "accept")
    second = run("WIRE-002", "Stream receive: case A only", "executor", parent)
    parent = review(second, deliver(second), "repair")
    third = run("WIRE-003", "Stream receive: link and run case A", "executor", parent)
    (Path(third["cwd"]) / "HANDOFF.md").unlink()  # The worker stopped before its handoff, e.g. on a quota limit.
    stall = {"task_id": "WIRE-003", "nonce": third["nonce"], "status": "incomplete", "stall_episode": 1,
             "anomaly_summary": "missing delivery", "observed_at": datetime.now(timezone.utc).isoformat()}
    scheduler.record_observation(state, third, stall)
    control.publish_event(state, third, stall)
    waiting = run("PAPER-01", "Check the paper tables against the results", "executor-b")
    deliver(waiting)

    now = datetime.now(timezone.utc)
    shift = {"CHECK-%d" % (n + 1): timedelta(days=9 + n) for n in range(4)}
    shift.update({"WIRE-001": timedelta(days=3), "WIRE-002": timedelta(days=2), "WIRE-003": timedelta(minutes=40),
                  "PAPER-01": timedelta(minutes=25)})
    with sqlite3.connect(state / "workflow.sqlite3") as con:
        for task, ago in shift.items():
            rows = con.execute("SELECT seq FROM task_journal WHERE task=? ORDER BY seq", (task,)).fetchall()
            for i, (seq,) in enumerate(rows):
                con.execute("UPDATE task_journal SET at=? WHERE seq=?", ((now - ago + timedelta(minutes=2 * i)).isoformat(), seq))
            for event_id, data in con.execute("SELECT id,data FROM events WHERE task=?", (task,)).fetchall():
                value = json.loads(data)
                value["created_at"] = (now - ago + timedelta(minutes=2 * len(rows))).isoformat()
                con.execute("UPDATE events SET data=? WHERE id=?", (json.dumps(value), event_id))
    return state


def main():
    browser = next((p for p in BROWSERS if Path(p).is_file()), None)
    if not browser:
        sys.exit("Edge or Chrome is required")
    root = Path(tempfile.mkdtemp(prefix="dashboard-shot-"))
    server = proc = None
    try:
        state = build_ledger(root)
        server = dashboard.DashboardServer(("127.0.0.1", 0), state, recent_file=root / "recent.json")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        proc = subprocess.Popen([browser, "--headless=new", "--disable-gpu", "--remote-debugging-port=%d" % port,
                                 "--force-color-profile=srgb", "--user-data-dir=" + str(root / "profile"), "about:blank"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                pages = json.load(urllib.request.urlopen("http://127.0.0.1:%d/json" % port))
                page = next(t for t in pages if t["type"] == "page")
                break
            except Exception:
                time.sleep(0.2)
        tools = DevTools(page["webSocketDebuggerUrl"])
        tools.call("Page.enable")
        tools.call("Emulation.setDeviceMetricsOverride", width=1280, height=900, deviceScaleFactor=1, mobile=False)
        tools.call("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": "light"}])
        tools.call("Page.navigate", url="http://127.0.0.1:%d/?lang=en#%s" % (server.server_port, server.token))
        time.sleep(3)
        # The state lives in a temporary folder; show the path a reader of the README would use.
        tools.call("Runtime.evaluate", expression="document.getElementById('state').textContent = 'runtime/first-run/state'")
        # Show the follow-up chain as a graph, the view the README describes.
        tools.call("Runtime.evaluate", expression=(
            "[...document.querySelectorAll('.chain-actions button')].filter(b => b.textContent === 'Show as graph')"
            ".forEach(b => b.click())"))
        time.sleep(0.5)
        bottom = tools.call("Runtime.evaluate", returnByValue=True, expression=(
            "Math.round(Math.max(...[...document.querySelectorAll('#inbox, #alerts')]"
            ".map(e => e.closest('section').getBoundingClientRect().bottom)) + scrollY + 16)"))["result"]["value"]
        shot = tools.call("Page.captureScreenshot", format="png", captureBeyondViewport=True,
                          clip={"x": 0, "y": 0, "width": 1280, "height": bottom, "scale": 1})
        target = REPO / "docs" / "images" / "dashboard-overview.png"
        target.write_bytes(base64.b64decode(shot["data"]))
        print("wrote", target.relative_to(REPO), "height", bottom)
    finally:
        if proc:
            proc.terminate()
            proc.wait(timeout=10)
        if server:
            server.shutdown()
            server.server_close()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
