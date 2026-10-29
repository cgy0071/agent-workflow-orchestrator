"""Offline HTTP, ledger, and one-attempt dispatch tests for the local dashboard."""

from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor
import copy
import http.client
import io
import json
from pathlib import Path
import re
import socket
import ssl
import threading
import tempfile
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
        self.agent_calls = 0
        self.listed = []
        self.identity_drift = False

    def status(self, spec):
        self.status_calls += 1
        result = super().status(spec)
        if self.identity_drift:
            result["identity"] = dict(result["identity"], terminal="someone-else")
        return result

    def output(self, spec):
        return "recent output for " + spec["role"]

    def agents(self, spec):
        self.agent_calls += 1
        return self.listed


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

    def start_server(self, allow_dispatch=False, state="default", recent_file=None, public_origins=()):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
        options = {"allow_dispatch": allow_dispatch, "transport_factory": lambda _request: self.fake,
                   "recent_file": recent_file, "public_origins": public_origins}
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
        self.assertEqual({item["role"] for item in self.http("GET", "/api/status")[1]["chat_targets"]},
                         {"planner", "executor", "second"})
        self.assertEqual(self.fake.status_calls, 0)
        self.assertEqual(self.http("POST", "/api/probe")[0], 200)
        self.assertEqual(self.fake.status_calls, 3)

    def test_probe_all_modes_and_one_list_per_herdr_session(self):
        with control.database(self.state) as con:
            for role, pane, name in (("planner", "p1", "awo:planner"),
                                     ("executor", "p2", "My work"), ("second", "p3", None)):
                row = con.execute("SELECT id,data FROM bindings WHERE role=? AND status='active'", (role,)).fetchone()
                binding = json.loads(row["data"])
                spec = binding["spec"]
                spec.update(adapter="herdr", executable="fake-herdr", session="offline", pane=pane)
                spec["expected_identity"]["pane"] = pane
                con.execute("UPDATE bindings SET data=? WHERE id=?", (json.dumps(binding), row["id"]))
                self.fake.listed.append({"terminal_id": spec["expected_identity"]["terminal"],
                                         "pane_id": pane, "name": name})
        self.assertEqual(self.http("GET", "/api/status")[0], 200)
        self.assertEqual((self.fake.status_calls, self.fake.agent_calls), (0, 0))
        status, body, _ = self.http("POST", "/api/probe")
        self.assertEqual(status, 200)
        self.assertEqual((self.fake.status_calls, self.fake.agent_calls), (3, 1))
        self.assertEqual({x["mode"] for x in body["agents"]}, {"controller", "worker"})
        self.assertEqual({x["role"]: (x["name"], x["label_source"]) for x in body["agents"]},
                         {"planner": ("awo:planner", "awo"), "executor": ("My work", "custom"),
                          "second": (None, "none")})
        self.assertTrue(all(x["identity_matches"] for x in body["agents"]))

    def test_probe_failure_for_one_agent_keeps_other_results(self):
        original = self.fake.status
        def status(spec):
            if spec["role"] == "executor":
                raise RuntimeError("agent unavailable")
            return original(spec)
        self.fake.status = status
        code, body, _ = self.http("POST", "/api/probe")
        self.assertEqual(code, 200)
        self.assertIn("agent unavailable", next(x for x in body["agents"] if x["role"] == "executor")["error"])
        self.assertTrue(next(x for x in body["agents"] if x["role"] == "planner")["identity_matches"])

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

    def chat_server(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
        self.server = dashboard.DashboardServer(("127.0.0.1", 0), self.state, allow_chat=True,
                                                actor=self.controller["instance_id"], version=self.controller["version"],
                                                context="main", transport_factory=lambda _request: self.fake,
                                                recent_file=None)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    def test_chat_disabled_by_default_and_needs_controller(self):
        for path in ("/api/chat/send", "/api/chat/output"):
            self.assertEqual(self.http("POST", path, {"message": "hi"})[0], 403)
        self.assertEqual(self.fake.messages, [])
        self.assertFalse(self.http("GET", "/api/status")[1]["allow_chat"])
        for options in ({}, {"actor": self.controller["instance_id"], "version": 1, "context": "side"},
                        {"actor": "wrong", "version": 1, "context": "main"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                dashboard.DashboardServer(("127.0.0.1", 0), self.state, allow_chat=True, **options)
        with self.assertRaises(ValueError):
            dashboard.DashboardServer(("127.0.0.1", 0), None, allow_chat=True, actor=self.controller["instance_id"],
                                      version=self.controller["version"], context="main")

    def test_chat_sends_once_to_controller_and_audits_hash_only(self):
        self.chat_server()
        data = self.http("GET", "/api/status")[1]
        self.assertEqual((data["allow_chat"], data["actor_role"], data["can_switch"]), (True, "planner", False))
        self.assertFalse(data["allow_dispatch"])
        self.assertEqual(self.http("POST", "/api/dispatch/preview", self.form())[0], 403)
        for bad in ({}, {"message": "   "}, {"message": 5}, {"message": "x" * 20001}):
            with self.subTest(bad=bad):
                self.assertEqual(self.http("POST", "/api/chat/send", bad)[0], 400)
        self.assertEqual(self.fake.messages, [])
        status, result, _ = self.http("POST", "/api/chat/send", {"message": "How is TASK-A going?\r\n"})
        self.assertEqual((status, result["status"], result["role"]), (200, "SENT", "planner"))
        self.assertEqual(self.fake.messages, ["How is TASK-A going?"])
        status, output, _ = self.http("POST", "/api/chat/output", {})
        self.assertEqual((status, output["output"]), (200, "recent output for planner"))
        with control.database(self.state) as con:
            rows = [json.loads(r[0]) for r in con.execute("SELECT data FROM audit WHERE action='dashboard_chat'")]
        self.assertEqual(len(rows), 1)
        self.assertNotIn("TASK-A", json.dumps(rows))
        self.fake.error = RuntimeError("pane closed")
        status, result, _ = self.http("POST", "/api/chat/send", {"message": "again"})
        self.assertEqual((status, result["status"]), (200, "SEND_UNCERTAIN"))
        self.assertEqual(self.fake.messages, ["How is TASK-A going?", "again"])
        self.fake.error = None
        self.fake.identity_drift = True
        self.assertEqual(self.http("POST", "/api/chat/send", {"message": "third"})[0], 400)
        self.assertEqual(len(self.fake.messages), 2)

    def test_chat_targets_worker_and_busy_requires_explicit_confirmation(self):
        self.chat_server()
        request = self.request("BUSY-1", "executor")
        (Path(request["cwd"]) / "TASK.md").write_text("# Measure active work\n", encoding="utf-8")
        self.assertEqual(scheduler.dispatch(self.state, request, self.message(request),
                                            *self.actor(), transport=self.fake)["status"], "SUBMITTED")
        before = len(self.fake.messages)
        payload = {"role": "executor", "message": "Can you pause?"}
        status, body, _ = self.http("POST", "/api/chat/send", payload)
        self.assertEqual(status, 409)
        self.assertIn("Measure active work", body["error"])
        self.assertEqual(len(self.fake.messages), before)
        status, sent, _ = self.http("POST", "/api/chat/send", dict(payload, confirm_busy=True))
        self.assertEqual((status, sent["status"], sent["role"]), (200, "SENT", "executor"))
        self.assertEqual(self.fake.messages[-1], payload["message"])
        self.assertEqual(self.http("POST", "/api/chat/output", {"role": "second"})[1]["role"], "second")
        with control.database(self.state) as con:
            record = json.loads(con.execute("SELECT data FROM audit WHERE action='dashboard_chat' ORDER BY rowid DESC")
                                .fetchone()[0])
        self.assertEqual((record["actor"], record["version"], record["target_role"],
                          record["target_version"], record["confirm_busy"]),
                         (self.controller["instance_id"], 1, "executor", 1, True))
        self.assertNotIn(payload["message"], json.dumps(record))

    def test_chat_rechecks_target_binding_identity_and_prompt_capability(self):
        self.chat_server()
        self.assertEqual(self.http("POST", "/api/chat/send", {"role": "second", "message": "hello"})[0], 200)
        self.fake.identity_drift = True
        self.assertEqual(self.http("POST", "/api/chat/send", {"role": "second", "message": "again"})[0], 400)
        self.fake.identity_drift = False
        with control.database(self.state) as con:
            row = con.execute("SELECT id,data FROM bindings WHERE role='second' AND status='active'").fetchone()
            value = json.loads(row["data"])
            value["spec"]["adapter"] = "herdr"
            con.execute("UPDATE bindings SET data=? WHERE id=?", (json.dumps(value), row["id"]))
        status, body, _ = self.http("POST", "/api/chat/send", {"role": "second", "message": "hello"})
        self.assertEqual(status, 400)
        self.assertIn("prompt is unavailable", body["error"])
        self.assertEqual(self.fake.messages, ["hello"])
        with control.database(self.state) as con:
            con.execute("UPDATE bindings SET status='pending' WHERE role='second' AND status='active'")
        self.assertEqual(self.http("POST", "/api/chat/send", {"role": "second", "message": "still there?"})[0], 400)
        self.assertEqual(self.fake.messages, ["hello"])

    def test_chat_sender_binding_must_still_be_active(self):
        self.chat_server()
        with control.database(self.state) as con:
            con.execute("UPDATE bindings SET status='pending' WHERE role='planner' AND status='active'")
        self.assertEqual(self.http("POST", "/api/chat/send", {"role": "executor", "message": "hello"})[0], 403)
        self.assertEqual(self.fake.messages, [])

    def test_public_origin_allows_only_named_https_proxy(self):
        for value in ("http://example.test", "https://example.test/path", "https://example.test/",
                      "https://example.test?x=1", "https://user@example.test", "https://example.test:0",
                      "https://example.test:"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                dashboard.DashboardServer(("127.0.0.1", 0), self.state, public_origins=[value])
        self.start_server(public_origins=["https://demo.tailnet.ts.net", "https://other.example:8443"])
        self.assertEqual(self.http("GET", "/api/status", host="demo.tailnet.ts.net")[0], 200)
        self.assertEqual(self.http("POST", "/api/probe", host="demo.tailnet.ts.net",
                                   origin="https://demo.tailnet.ts.net")[0], 200)
        self.assertEqual(self.http("POST", "/api/probe", host="other.example:8443",
                                   origin="https://other.example:8443")[0], 200)
        self.assertEqual(self.http("POST", "/api/probe", host="demo.tailnet.ts.net",
                                   origin="http://demo.tailnet.ts.net")[0], 403)
        self.assertEqual(self.http("GET", "/api/status", host="unknown.example")[0], 403)
        self.assertEqual(self.http("GET", "/api/status")[0], 200)

    def test_page_embeds_inert_chinese_dictionary(self):
        status, html, _ = self.http("GET", "/")
        self.assertEqual(status, 200)
        block = html.split('<script type="application/json" id="i18n-zh">', 1)[1].split("</script>", 1)[0]
        self.assertTrue(block.isascii())
        data = json.loads(block)
        self.assertEqual(data["exact"]["Refresh page"], "刷新页面")
        for source, target in data["patterns"]:
            re.compile(source)
            self.assertTrue(target)
        self.assertIn('id="chat-panel"', html)
        self.assertIn('id="lang"', html)
        for element in ('id="inbox-toggle"', 'id="inbox-history"', 'id="task-range"', 'id="dispatch-panel"'):
            self.assertIn(element, html)  # Handled updates and old tasks are folded, not dropped.

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

    def test_summary_reads_spec_beside_delivery_and_hides_hashes_and_paths(self):
        import workflow_status
        history = self.root / "repo" / ".ai" / "history" / "SUM-2"
        history.mkdir(parents=True)
        (history / "TASK.md").write_text("# SUM-2 — Keep partial responses after disconnect\n\n"
                                         "Date: 2026-10-27. Wrapper b61826b0… and codec `C072374f0` are frozen.\n",
                                         encoding="utf-8")
        summary = workflow_status.task_summary("SUM-2", str(self.root / "repo"), [None], str(history))
        self.assertEqual(summary["title"], "Keep partial responses after disconnect")
        self.assertNotIn("b61826b0", summary["excerpt"])
        self.assertNotIn("C072374f0", summary["excerpt"])
        message = ("Authorized bounded continuation SUM-3, nonce f4529807dbb940309a63bec33988382e, assigned worktree "
                   + str(self.root) + ". Read the parent review and finish case A.")
        summary = workflow_status.task_summary("SUM-3", None, [None, message])
        self.assertEqual(summary, {"title": "Authorized bounded continuation", "excerpt": "Read the parent review and finish case A."})
        for text in ("SUM-3", "f4529807", str(self.root)):
            self.assertNotIn(text, json.dumps(summary))

    def test_status_history_has_no_producer_hashes_or_payloads(self):
        request = self.request("HIST-1", "executor")
        self.assertEqual(scheduler.dispatch(self.state, request, self.message(request), *self.actor(),
                                            transport=self.fake)["status"], "SUBMITTED")
        self.confirm(request)
        journal = self.http("GET", "/api/status")[1]["task_details"]["HIST-1"]["journal"]
        self.assertEqual([j["kind"] for j in journal], ["dispatch_reserved", "dispatch_submitted", "dispatch_reconciled"])
        self.assertEqual(journal[-1]["outcome"], "running")
        self.assertEqual({key for j in journal for key in j}, {"at", "kind", "outcome"})
        self.assertNotIn(self.controller["instance_id"], json.dumps(journal))

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
        for path in ("../outside", "C:\\outside", "C:outside", "\\\\server\\share\\x", "/etc/outside",
                     "sub\\..\\..\\outside", "", "."):
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


class LanHttpsTests(unittest.TestCase):
    def test_address_rules_and_auto_selection(self):
        for address in ("10.1.2.3", "172.16.0.1", "172.31.255.254", "192.168.1.2"):
            self.assertEqual(dashboard._private_lan_ip(address), address)
        for address in ("0.0.0.0", "127.0.0.1", "8.8.8.8", "169.254.1.2", "172.32.0.1",
                        "::1", "localhost", "example.com"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                dashboard._private_lan_ip(address)
        with patch.object(dashboard, "_lan_candidates", return_value=["10.1.2.3"]):
            self.assertEqual(dashboard._choose_lan("auto"), "10.1.2.3")
        with patch.object(dashboard, "_lan_candidates", return_value=["10.1.2.3", "192.168.1.2"]):
            with self.assertRaisesRegex(ValueError, "10.1.2.3, 192.168.1.2"):
                dashboard._choose_lan("auto")
        with patch.object(dashboard, "_lan_candidates", return_value=[]):
            with self.assertRaisesRegex(ValueError, "no local private"):
                dashboard._choose_lan("auto")

    def test_cli_rejects_control_without_acknowledgment_and_partial_certificate(self):
        for argv, error in ((["--lan", "10.1.2.3", "--allow-chat"], "--lan-allow-control"),
                            (["--lan", "10.1.2.3", "--allow-dispatch"], "--lan-allow-control"),
                            (["--lan", "10.1.2.3", "--tls-cert", "cert.pem"], "together"),
                            (["--lan", "10.1.2.3", "--tls-key", "key.pem"], "together")):
            with self.subTest(argv=argv), patch.object(sys, "stderr", io.StringIO()) as errors:
                with self.assertRaises(SystemExit):
                    dashboard.main(argv)
                self.assertIn(error, errors.getvalue())

    @unittest.skipUnless(dashboard._openssl_executable(), "OpenSSL unavailable; HTTPS fallback check skipped")
    def test_https_default_port_falls_back_but_invalid_bind_does_not(self):
        with tempfile.TemporaryDirectory() as folder:
            cert, key = dashboard._lan_certificate("127.0.0.1", Path(folder))
            with socket.socket() as occupied:
                occupied.bind(("127.0.0.1", 0))
                occupied.listen()
                port = occupied.getsockname()[1]
                with patch.object(dashboard, "DEFAULT_PORT", port):
                    server = dashboard.open_server("127.0.0.1", port, None, lan_tls=(cert, key))
                try:
                    self.assertNotEqual(server.server_port, port)
                    self.assertTrue(server.lan_tls)
                finally:
                    server.server_close()
            with self.assertRaises(OSError):
                dashboard.open_server("10.254.254.254", port, None, lan_tls=(cert, key))

    @unittest.skipUnless(dashboard._openssl_executable(), "OpenSSL unavailable; certificate and HTTPS checks skipped")
    def test_certificate_reuse_rotation_and_https_origin(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cert, key = dashboard._lan_certificate("127.0.0.1", root / "tls")
            first = cert.read_bytes()
            self.assertEqual(dashboard._lan_certificate("127.0.0.1", root / "tls"), (cert, key))
            self.assertEqual(cert.read_bytes(), first)
            other, _ = dashboard._lan_certificate("10.1.2.3", root / "tls")
            self.assertNotEqual(other.read_bytes(), first)
            self.assertIn(("IP Address", "10.1.2.3"), ssl._ssl._test_decode_cert(str(other))["subjectAltName"])
            self.assertTrue(dashboard._certificate_fingerprint(cert))
            with redirect_stdout(io.StringIO()):
                demo_scheduler.demo(root / "demo")
            state = root / "demo" / "state"
            server = dashboard.DashboardServer(("127.0.0.1", 0), state, lan_tls=(cert, key))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host = "127.0.0.1:" + str(server.server_port)

            def request(method, *, supplied_host=host, origin=None):
                con = http.client.HTTPSConnection("127.0.0.1", server.server_port, timeout=5,
                                                context=ssl._create_unverified_context())
                headers = {"Host": supplied_host, "X-Dashboard-Token": server.token}
                body = None
                if method == "POST":
                    headers.update({"Origin": origin or "https://" + host,
                                    "Content-Type": "application/json"})
                    body = json.dumps({"state": str(state)})
                try:
                    con.request(method, "/api/status" if method == "GET" else "/api/state", body=body,
                                headers=headers)
                    response = con.getresponse()
                    response.read()
                    return response.status
                finally:
                    con.close()

            try:
                self.assertEqual(server.origin(), "https://" + host)
                self.assertGreaterEqual(server.socket.context.minimum_version, ssl.TLSVersion.TLSv1_2)
                self.assertEqual(request("GET"), 200)
                self.assertEqual(request("POST"), 200)
                self.assertEqual(request("GET", supplied_host="evil.example"), 403)
                self.assertEqual(request("POST", supplied_host="evil.example"), 403)
                self.assertEqual(request("POST", origin="http://" + host), 403)
                with socket.create_connection(("127.0.0.1", server.server_port), timeout=5) as plain:
                    plain.sendall(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
                    try:
                        plain.recv(1024)
                    except (OSError, ConnectionError):
                        pass
                self.assertEqual(request("GET"), 200)
                with socket.create_connection(("127.0.0.1", server.server_port), timeout=5):
                    pass  # Client disconnects before the TLS handshake.
                self.assertEqual(request("GET"), 200)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
