"""Offline HTTP, ledger, and one-attempt dispatch tests for the local dashboard."""

from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor
import copy
import http.client
import io
import json
from pathlib import Path
import threading
import sys
import unittest
from unittest.mock import patch

import dashboard
import demo_scheduler
import workflow_control as control
import workflow_scheduler as scheduler
from test_scheduler import FakeTransport
import test_scheduler


class CountingTransport(FakeTransport):
    def __init__(self):
        super().__init__()
        self.status_calls = 0

    def status(self, spec):
        self.status_calls += 1
        return super().status(spec)


class DashboardTests(unittest.TestCase):
    spec = test_scheduler.SchedulerTests.spec
    request = test_scheduler.SchedulerTests.request
    actor = test_scheduler.SchedulerTests.actor

    def setUp(self):
        test_scheduler.SchedulerTests.setUp(self)
        self.fake = CountingTransport()
        self.tasks_root = self.root / "dashboard-tasks"
        self.tasks_root.mkdir()
        self.server = None
        self.thread = None
        self.start_server()

    def tearDown(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
        test_scheduler.SchedulerTests.tearDown(self)

    def start_server(self, allow_dispatch=False, state="default", recent_file=None):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
        options = {"allow_dispatch": allow_dispatch, "transport_factory": lambda _request: self.fake,
                   "recent_file": recent_file}
        if allow_dispatch:
            options.update(actor=self.controller["instance_id"], version=self.controller["version"],
                           context="main", tasks_root=self.tasks_root)
        state = self.state if state == "default" else state
        self.server = dashboard.DashboardServer(("127.0.0.1", 0), state, **options)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    def http(self, method, path, payload=None, *, token=True, host=None, origin=None):
        port = self.server.server_port
        host = host or "127.0.0.1:" + str(port)
        headers = {"Host": host}
        if token:
            headers["X-Dashboard-Token"] = self.server.token
        if method == "POST":
            headers["Origin"] = "http://" + host if origin is None else origin
            headers["Content-Type"] = "application/json"
        body = json.dumps(payload or {}) if method == "POST" else None
        con = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            con.request(method, path, body=body, headers=headers)
            response = con.getresponse()
            raw = response.read()
            return response.status, json.loads(raw) if path.startswith("/api/") else raw.decode("utf-8"), response.headers
        finally:
            con.close()

    @staticmethod
    def form(task_id="DASH-1"):
        return {"worker_role": "executor", "task_id": task_id, "body": "Implement one bounded offline task.\nAcceptance: write evidence.",
                "binding_artifact": "HANDOFF.md", "artifacts": ["RESULT.md"], "source": "current owner authorization",
                "notify_reviewer": False, "resources": {}}

    def test_token_host_origin_and_headers(self):
        self.assertEqual(self.http("GET", "/api/status", token=False)[0], 403)
        self.assertEqual(self.http("GET", "/api/status", host="evil.example")[0], 403)
        self.assertEqual(self.http("POST", "/api/probe", origin="http://evil.example")[0], 403)
        status, body, headers = self.http("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertTrue(body["read_only"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertEqual(self.fake.status_calls, 0)

    def test_status_never_writes_or_queries_adapters(self):
        database = self.state / "workflow.sqlite3"
        before = database.read_bytes()
        files = {p.name for p in self.state.iterdir()}
        for _ in range(3):
            self.assertEqual(self.http("GET", "/api/status")[0], 200)
        self.assertEqual(database.read_bytes(), before)
        self.assertEqual({p.name for p in self.state.iterdir()}, files)
        self.assertEqual(self.fake.status_calls, 0)
        self.assertEqual(self.http("POST", "/api/probe")[0], 200)
        self.assertEqual(self.fake.status_calls, 2)

    def test_status_includes_bindings_inbox_dag_dependencies_resources(self):
        first = self.request("DAG-A", "executor")
        second = self.request("DAG-B", "second")
        scheduler.load_dag(self.state, "sample", {"nodes": [
            {"id": "A", "needs": [], "request": first, "message": "DAG-A " + first["nonce"]},
            {"id": "<script>", "needs": ["A"], "request": second, "message": "DAG-B " + second["nonce"]},
        ]}, *self.actor())
        request = self.request("EVENT-X", "executor")
        self.assertEqual(scheduler.dispatch(self.state, request, "EVENT-X " + request["nonce"],
                                            *self.actor(), transport=self.fake)["status"], "SUBMITTED")
        note = Path(request["cwd"]) / "EXECUTION.md"
        note.write_text("EVENT-X " + request["nonce"] + "\nOutcome: running\nObserved offline.\n", encoding="utf-8")
        confirmed = scheduler.reconcile(self.state, "EVENT-X", "running", note, *self.actor())["request"]
        control.publish_event(self.state, confirmed, {"status": "review_ready"})
        status, data, _ = self.http("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["roles"]), 3)
        self.assertEqual(data["inbox"][0]["status"], "pending")
        self.assertEqual(data["dags"]["sample"]["dependencies"]["<script>"], ["A"])
        self.assertIn("<script>", json.dumps(data))
        self.assertEqual(data["resource_capacities"], {"gpu": 1})
        self.assertIn("DAG-B", data["task_details"])
        self.assertEqual(self.fake.status_calls, 2)

    def test_artifacts_reads_only_declared_files_without_writes(self):
        request = self.request("ART-1", "executor")
        self.assertEqual(scheduler.dispatch(self.state, request, "ART-1 " + request["nonce"],
                                            *self.actor(), transport=self.fake)["status"], "SUBMITTED")
        cwd = Path(request["cwd"])
        (cwd / "RESULT.md").write_bytes(b"<b>done</b>\n")
        (cwd / "secret.txt").write_text("not declared\n", encoding="utf-8")
        database = self.state / "workflow.sqlite3"
        before = database.read_bytes()
        status, data, _ = self.http("GET", "/api/artifacts?task=ART-1")
        self.assertEqual(status, 200)
        files = {f["name"]: f for f in data["files"]}
        self.assertEqual(set(files), {"HANDOFF.md", "RESULT.md"})
        self.assertEqual(files["RESULT.md"]["text"], "<b>done</b>\n")
        self.assertFalse(files["HANDOFF.md"]["exists"])
        self.assertNotIn("not declared", json.dumps(data))
        self.assertEqual(database.read_bytes(), before)
        self.assertEqual(self.http("GET", "/api/artifacts?task=../x")[0], 400)
        self.assertEqual(self.http("GET", "/api/artifacts?task=NOPE")[0], 400)
        self.assertEqual(self.http("GET", "/api/artifacts?task=ART-1", token=False)[0], 403)

    confirm = test_scheduler.SchedulerTests.confirm
    message = test_scheduler.SchedulerTests.message
    finish = test_scheduler.SchedulerTests.finish

    def test_status_links_followup_to_parent_without_dag(self):
        parent = self.request("PARENT", "executor")
        self.assertEqual(scheduler.dispatch(self.state, parent, self.message(parent), *self.actor(),
                                            transport=self.fake)["status"], "SUBMITTED")
        parent, event = self.finish(parent)
        scheduler.release(self.state, "PARENT", *self.actor())
        child = self.request("CHILD", "executor")
        child.update(parent_task="PARENT", parent_review=control.next_gate(self.state, event, *self.actor())["parent_review"])
        self.assertEqual(scheduler.dispatch(self.state, child, self.message(child), *self.actor(),
                                            transport=self.fake)["status"], "SUBMITTED")
        data = self.http("GET", "/api/status")[1]
        self.assertEqual(data["dags"], {})
        self.assertEqual(data["task_details"]["CHILD"]["parent_task"], "PARENT")
        self.assertIsNone(data["task_details"]["PARENT"]["parent_task"])

    def test_task_summary_prefers_human_text_and_never_raises(self):
        import workflow_status
        cwd = self.root / "SUM-1"
        cwd.mkdir()
        (cwd / "task.md").write_text("Task ID: SUM-1\nNonce: abc\nCwd: x\n\n# SUM-1: Measure skip cost\n\n"
                                     "Compare cycles with and without skipping.\n", encoding="utf-8")
        summary = workflow_status.task_summary("SUM-1", str(cwd), [None])
        self.assertEqual(summary, {"title": "Measure skip cost", "excerpt": "Compare cycles with and without skipping."})
        self.assertEqual(workflow_status.task_summary("X", str(self.root / "missing"), ['{"task_id": "X"}']),
                         {"title": "", "excerpt": ""})
        self.assertEqual(workflow_status.task_summary("X", None, [None, "Plain message\nDo it."])["title"], "Plain message")

    def test_startup_rejects_public_host_or_incomplete_controller(self):
        with self.assertRaises(ValueError):
            dashboard.DashboardServer(("0.0.0.0", 0), self.state)
        for options in ({}, {"actor": self.controller["instance_id"]},
                        {"actor": self.controller["instance_id"], "version": 1, "context": "side",
                         "tasks_root": self.tasks_root},
                        {"actor": "wrong", "version": 1, "context": "main", "tasks_root": self.tasks_root}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                dashboard.DashboardServer(("127.0.0.1", 0), self.state, allow_dispatch=True, **options)

    def test_dispatch_disabled_by_default(self):
        for path in ("/api/dispatch/preview", "/api/dispatch/confirm"):
            self.assertEqual(self.http("POST", path, self.form())[0], 403)
        status, html, _ = self.http("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn('id="dispatch-form" class="hidden"', html)

    def review_event(self, name):
        request = self.request(name, "executor")
        self.assertEqual(scheduler.dispatch(self.state, request, name + " " + request["nonce"],
                                            *self.actor(), transport=self.fake)["status"], "SUBMITTED")
        note = Path(request["cwd"]) / "EXECUTION.md"
        note.write_text(name + " " + request["nonce"] + "\nOutcome: running\nObserved offline.\n", encoding="utf-8")
        confirmed = scheduler.reconcile(self.state, name, "running", note, *self.actor())["request"]
        return confirmed, control.publish_event(self.state, confirmed, {"status": "review_ready"})

    def test_review_actions_disabled_by_default(self):
        _, event = self.review_event("REV-OFF")
        for path in ("/api/review/preview", "/api/review/confirm"):
            self.assertEqual(self.http("POST", path, {"event_id": event})[0], 403)
        self.assertEqual(self.http("GET", "/api/status")[1]["inbox"][0]["status"], "pending")

    def test_review_preview_confirm_is_one_time_and_gated(self):
        request, event = self.review_event("REV-1")
        self.start_server(True)
        database = self.state / "workflow.sqlite3"
        self.assertEqual(self.http("POST", "/api/review/preview", {"event_id": "short"})[0], 400)
        forged = self.http("POST", "/api/review/confirm", {"event_id": event, "preview_sha256": "0" * 64})
        self.assertEqual(forged[0], 400)
        self.assertIn("changed", forged[1]["error"])
        before = database.read_bytes()
        status, step, _ = self.http("POST", "/api/review/preview", {"event_id": event})
        self.assertEqual((status, step["step"]), (200, "acknowledge"))
        self.assertEqual(database.read_bytes(), before)
        confirm = {"event_id": event, "preview_sha256": step["preview_sha256"]}
        # A confirm that fails in the gate still consumes the preview, even though the ledger is unchanged.
        with patch.object(dashboard.control, "acknowledge", side_effect=ValueError("gate refused")):
            self.assertEqual(self.http("POST", "/api/review/confirm", confirm)[1]["error"], "gate refused")
        reused = self.http("POST", "/api/review/confirm", confirm)
        self.assertEqual(reused[0], 400)
        self.assertIn("preview missing", reused[1]["error"])
        step = self.http("POST", "/api/review/preview", {"event_id": event})[1]
        confirm = {"event_id": event, "preview_sha256": step["preview_sha256"]}
        self.assertEqual(self.http("POST", "/api/review/confirm", confirm)[1]["status"], "ACKNOWLEDGED")
        self.assertEqual(self.http("POST", "/api/review/confirm", confirm)[0], 400)
        review = Path(request["cwd"]) / "REVIEW.md"
        self.assertEqual(self.http("POST", "/api/review/preview", {"event_id": event, "verdict": "accept"})[0], 400)
        review.write_text("REV-1 " + request["nonce"] + "\nVerdict: reject\n", encoding="utf-8")
        status, data, _ = self.http("POST", "/api/review/preview", {"event_id": event, "verdict": "accept"})
        self.assertEqual(status, 400)
        self.assertIn("Verdict: accept", data["error"])
        status, data, _ = self.http("POST", "/api/review/preview", {"event_id": event, "verdict": "nope"})
        self.assertEqual(status, 400)
        self.assertIn("workflow.transitions", data["error"])
        review.write_text("REV-1 " + request["nonce"] + "\nVerdict: accept\n", encoding="utf-8")
        status, step, _ = self.http("POST", "/api/review/preview", {"event_id": event, "verdict": "accept"})
        self.assertEqual((status, step["step"], step["review_path"]), (200, "record_review", str(review.resolve())))
        review.write_text("REV-1 " + request["nonce"] + "\nChanged after preview.\nVerdict: accept\n", encoding="utf-8")
        stale = {"event_id": event, "verdict": "accept", "preview_sha256": step["preview_sha256"]}
        self.assertEqual(self.http("POST", "/api/review/confirm", stale)[0], 400)
        step = self.http("POST", "/api/review/preview", {"event_id": event, "verdict": "accept"})[1]
        status, result, _ = self.http("POST", "/api/review/confirm",
                                      {"event_id": event, "verdict": "accept", "preview_sha256": step["preview_sha256"]})
        self.assertEqual((status, result["status"], result["verdict"]), (200, "REVIEW_RECORDED", "accept"))
        self.assertEqual(result["actor"]["instance_id"], self.controller["instance_id"])
        data = self.http("GET", "/api/status")[1]
        self.assertEqual(data["inbox"][0]["status"], "reviewed")
        self.assertEqual(data["actor_role"], "planner")
        self.assertEqual(data["verdicts"], ["accept", "reject", "repair"])
        self.assertEqual(self.http("POST", "/api/review/preview", {"event_id": event, "verdict": "accept"})[0], 400)

    def test_preview_is_read_only_and_returns_bound_request(self):
        self.start_server(True)
        before = (self.state / "workflow.sqlite3").read_bytes()
        status, preview, _ = self.http("POST", "/api/dispatch/preview", self.form())
        self.assertEqual(status, 200)
        self.assertEqual(preview["baseline_completion_seq"], 10)
        self.assertRegex(preview["form"]["nonce"], r"^[0-9a-f]{16}$")
        self.assertTrue(preview["message"].startswith("Task ID: DASH-1\nNonce: "))
        self.assertEqual(preview["request"]["worker"]["role"], "executor")
        self.assertFalse((self.tasks_root / "DASH-1").exists())
        self.assertEqual((self.state / "workflow.sqlite3").read_bytes(), before)
        self.assertEqual(self.fake.messages, [])

    def test_confirm_rejects_changed_preview_or_baseline_without_send(self):
        self.start_server(True)
        status, preview, _ = self.http("POST", "/api/dispatch/preview", self.form())
        self.assertEqual(status, 200)
        payload = dict(preview["form"], preview_sha256="0" * 64)
        self.assertEqual(self.http("POST", "/api/dispatch/confirm", payload)[0], 400)
        self.fake.sequences["executor"] = 11
        payload["preview_sha256"] = preview["preview_sha256"]
        self.assertEqual(self.http("POST", "/api/dispatch/confirm", payload)[0], 400)
        self.assertFalse((self.tasks_root / "DASH-1").exists())
        self.assertEqual(self.fake.messages, [])

    def test_confirm_sends_once_and_freezes_active_binding(self):
        self.start_server(True)
        status, preview, _ = self.http("POST", "/api/dispatch/preview", self.form())
        self.assertEqual(status, 200)
        payload = dict(preview["form"], preview_sha256=preview["preview_sha256"])
        status, result, _ = self.http("POST", "/api/dispatch/confirm", payload)
        self.assertEqual((status, result["status"]), (200, "SUBMITTED"))
        self.assertEqual(len(self.fake.messages), 1)
        folder = self.tasks_root / "DASH-1"
        self.assertTrue((folder / "task.md").is_file())
        request = json.loads((folder / "request.json").read_text(encoding="utf-8"))
        self.assertEqual({k:v for k,v in request["worker"].items() if k != "baseline_completion_seq"}, self.specs["executor"])
        self.assertEqual(scheduler.snapshot(self.state, "DASH-1")["tasks"][0]["dispatch"]["status"], "submitted")
        phases = {item["task_id"]: item["phase"] for item in self.server.status_data()["tasks"]}
        self.assertEqual(phases["DASH-1"], "execution_unconfirmed")
        self.assertIn("--request-out", result["reconcile_command"])
        self.assertNotIn("dashboard_resources", request)
        scripts = Path(dashboard.__file__).resolve().parent
        self.assertTrue(result["reconcile_command"].startswith(
            "& '" + sys.executable + "' -B '" + str(scripts / "workflow_scheduler.py") + "' "))
        self.assertIn("--state '" + str(self.state) + "'", result["reconcile_command"])
        self.assertTrue(result["monitor_command"].startswith(
            "& '" + sys.executable + "' -B '" + str(scripts / "monitor_task.py") + "' "))
        self.assertEqual(self.http("POST", "/api/dispatch/confirm", payload)[0], 409)
        self.assertEqual(len(self.fake.messages), 1)

    def test_confirm_rejects_changed_resources_without_send(self):
        self.start_server(True)
        _, preview, _ = self.http("POST", "/api/dispatch/preview", self.form())
        payload = dict(preview["form"], preview_sha256=preview["preview_sha256"], resources={"gpu": 1})
        self.assertEqual(self.http("POST", "/api/dispatch/confirm", payload)[0], 400)
        self.assertFalse((self.tasks_root / "DASH-1").exists())
        self.assertEqual(self.fake.messages, [])

    def test_confirm_validation_failure_removes_empty_task_directory(self):
        self.start_server(True)
        _, preview, _ = self.http("POST", "/api/dispatch/preview", self.form())
        payload = dict(preview["form"], preview_sha256=preview["preview_sha256"])
        original = dashboard.validate_request

        def fail_exact(request, **kwargs):
            if Path(request["cwd"]) != self.tasks_root:
                raise ValueError("injected validation failure")
            return original(request, **kwargs)

        dashboard.validate_request = fail_exact
        try:
            self.assertEqual(self.http("POST", "/api/dispatch/confirm", payload)[0], 400)
        finally:
            dashboard.validate_request = original
        self.assertFalse((self.tasks_root / "DASH-1").exists())
        self.assertEqual(self.fake.messages, [])
        _, preview, _ = self.http("POST", "/api/dispatch/preview", self.form())
        payload = dict(preview["form"], preview_sha256=preview["preview_sha256"])
        self.assertEqual(self.http("POST", "/api/dispatch/confirm", payload)[0], 200)
        self.assertEqual(len(self.fake.messages), 1)

    def test_concurrent_confirm_sends_once(self):
        self.start_server(True)
        _, preview, _ = self.http("POST", "/api/dispatch/preview", self.form())
        payload = dict(preview["form"], preview_sha256=preview["preview_sha256"])
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: self.http("POST", "/api/dispatch/confirm", payload), range(2)))
        self.assertEqual(sorted(response[0] for response in responses), [200, 409])
        self.assertEqual(len(self.fake.messages), 1)

    def test_uncertain_send_is_never_retried(self):
        self.start_server(True)
        _, preview, _ = self.http("POST", "/api/dispatch/preview", self.form())
        self.fake.error = TimeoutError("delivery may have arrived")
        payload = dict(preview["form"], preview_sha256=preview["preview_sha256"])
        status, result, _ = self.http("POST", "/api/dispatch/confirm", payload)
        self.assertEqual((status, result["status"]), (200, "DISPATCH_UNCERTAIN"))
        self.assertEqual(len(self.fake.messages), 1)
        self.assertEqual(self.http("POST", "/api/dispatch/confirm", payload)[0], 409)
        self.assertEqual(len(self.fake.messages), 1)

    def test_invalid_task_and_delivery_paths(self):
        self.start_server(True)
        for task_id in ("../x", "a/b", "", "x" * 65):
            with self.subTest(task_id=task_id):
                self.assertEqual(self.http("POST", "/api/dispatch/preview", self.form(task_id))[0], 400)
        for path in ("../outside", "C:\\outside", "", "."):
            with self.subTest(path=path):
                form = self.form()
                form["binding_artifact"] = path
                self.assertEqual(self.http("POST", "/api/dispatch/preview", form)[0], 400)
        self.assertEqual(self.fake.messages, [])

    def test_html_is_local_and_escapes_ledger_strings(self):
        status, html, _ = self.http("GET", "/")
        self.assertEqual(status, 200)
        # The SVG namespace URI is an identifier, not a fetch; no other absolute URL may appear.
        self.assertEqual(html.replace("http://www.w3.org/2000/svg", "").count("://"), 0)
        self.assertNotRegex(html, r"""(?i)\b(src|href)\s*=\s*["']?[a-z][a-z0-9+.-]*:""")
        self.assertNotIn("innerHTML", html)
        self.assertIn("textContent", html)
        self.assertIn("prefers-color-scheme", html)

    def test_start_without_state_then_pick_and_remember(self):
        recent = self.root / "recent" / "dashboard-recent.json"
        self.start_server(state=None, recent_file=recent)
        status, data, _ = self.http("GET", "/api/status")
        self.assertEqual((status, data["state"], data["can_switch"], data["recent"]), (200, None, True, []))
        self.assertEqual(self.http("POST", "/api/probe")[0], 400)
        for bad in ("relative/state", str(self.root), "", 7):
            with self.subTest(bad=bad):
                self.assertEqual(self.http("POST", "/api/state", {"state": bad})[0], 400)
        self.assertEqual(self.http("POST", "/api/state", {"state": str(self.state)}, origin="http://evil.example")[0], 403)
        self.assertEqual(self.http("POST", "/api/state", {"state": str(self.state)}, token=False)[0], 403)
        status, chosen, _ = self.http("POST", "/api/state", {"state": '"' + str(self.state / "workflow.sqlite3") + '"'})
        self.assertEqual((status, chosen["state"]), (200, str(self.state.resolve())))
        status, data, _ = self.http("GET", "/api/status")
        self.assertEqual((status, data["state"], data["recent"]), (200, str(self.state.resolve()), [str(self.state.resolve())]))
        self.assertIn("executor", [role["role"] for role in data["roles"]])
        self.assertEqual(json.loads(recent.read_text(encoding="utf-8")), {"states": [str(self.state.resolve())]})
        self.assertEqual(self.fake.status_calls, 0)

    def test_recent_list_is_bounded_deduplicated_and_tolerates_corruption(self):
        recent = self.root / "recent.json"
        recent.write_text("not json", encoding="utf-8")
        self.assertEqual(dashboard._recent(recent), [])
        for index in range(12):
            dashboard._remember(recent, self.root / ("s" + str(index)))
        dashboard._remember(recent, self.root / "s5")
        values = dashboard._recent(recent)
        self.assertEqual(len(values), dashboard.RECENT_LIMIT)
        self.assertEqual(values[0], str(self.root / "s5"))
        self.assertEqual(values.count(str(self.root / "s5")), 1)
        self.start_server(recent_file=recent)
        self.assertEqual(self.http("GET", "/api/status")[1]["recent"], [str(self.state.resolve())])

    def test_dispatch_mode_fixes_state(self):
        with self.assertRaises(ValueError):
            dashboard.DashboardServer(("127.0.0.1", 0), None, allow_dispatch=True,
                                      actor=self.controller["instance_id"], version=self.controller["version"],
                                      context="main", tasks_root=self.tasks_root)
        self.start_server(True)
        self.assertFalse(self.http("GET", "/api/status")[1]["can_switch"])
        self.assertEqual(self.http("POST", "/api/state", {"state": str(self.state)})[0], 403)

    def test_busy_default_port_falls_back_to_free_port(self):
        original = dashboard.DEFAULT_PORT
        dashboard.DEFAULT_PORT = self.server.server_port
        try:
            fallback = dashboard.open_server("127.0.0.1", self.server.server_port, self.state)
            try:
                self.assertNotEqual(fallback.server_port, self.server.server_port)
            finally:
                fallback.server_close()
        finally:
            dashboard.DEFAULT_PORT = original
        with self.assertRaises(OSError):
            dashboard.open_server("127.0.0.1", self.server.server_port, self.state)

    def test_launchers_call_dashboard_and_cmd_keeps_crlf(self):
        root = Path(dashboard.__file__).resolve().parents[1]
        cmd = (root / "dashboard.cmd").read_bytes()
        self.assertTrue(cmd.isascii())
        self.assertEqual(cmd.count(b"\n"), cmd.count(b"\r\n"))
        self.assertIn(b"scripts\\dashboard.py", cmd)
        self.assertNotIn(b"(\r\n", cmd)
        shell = (root / "dashboard.command").read_bytes()
        self.assertTrue(shell.startswith(b"#!/bin/sh\n"))
        self.assertNotIn(b"\r", shell)
        self.assertIn(b"scripts/dashboard.py", shell)

    def test_demo_worker_accepts_four_line_message_and_rejects_duplicate(self):
        folder = self.root / "plain-worker"
        folder.mkdir()
        output = self.root / "plain-delivery"
        output.mkdir()
        (folder / "worker_status.json").write_text(json.dumps({"identity": self.specs["executor"]["expected_identity"],
                                                          "status": "idle", "completion_seq": 0}), encoding="utf-8")
        message = "Task ID: DASH-DEMO\nNonce: 1234567890abcdef\nCwd: " + str(output) + (
            "\nCompletion marker: TASK_DONE DASH-DEMO 1234567890abcdef\n\nObjective: offline fixture\n")
        with redirect_stdout(io.StringIO()):
            demo_scheduler.worker(folder, message)
        self.assertEqual(json.loads((folder / "worker_status.json").read_text())["completion_seq"], 1)
        self.assertTrue((output / "RESULT.md").is_file())
        self.assertIn("TASK_DONE DASH-DEMO 1234567890abcdef", (output / "HANDOFF.md").read_text())
        with self.assertRaises(ValueError), redirect_stdout(io.StringIO()):
            demo_scheduler.worker(folder, message)


if __name__ == "__main__":
    unittest.main()
