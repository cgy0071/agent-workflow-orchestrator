"""Offline tests for dashboard management actions: preview is read-only, confirm runs one gated step."""

import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

import dashboard
import workflow_control as control
import workflow_scheduler as scheduler
import workflow_status
import test_dashboard


class FakeProcess:
    pid = 4242


class ManageTests(unittest.TestCase):
    # Reuse the dashboard fixture without re-running its tests.
    Base = test_dashboard.DashboardTests
    spec, request, actor, message, confirm, finish = (Base.spec, Base.request, Base.actor, Base.message,
                                                    Base.confirm, Base.finish)
    setUp, tearDown, start_server, http, review_event = Base.setUp, Base.tearDown, Base.start_server, Base.http, Base.review_event

    def manage_server(self, **extra):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
        self.started = []
        def popen(argv, **options):
            self.started.append((argv, options))
            return FakeProcess()
        self.server = dashboard.DashboardServer(("127.0.0.1", 0), self.state, allow_manage=True,
                                                actor=self.controller["instance_id"], version=self.controller["version"],
                                                context="main", tasks_root=self.tasks_root,
                                                transport_factory=lambda _request: self.fake, recent_file=None,
                                                popen=popen, **extra)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    def step(self, action, **payload):
        """Preview then confirm; asserts the preview changed nothing on disk."""
        database = self.state / "workflow.sqlite3"
        before = database.read_bytes()
        status, preview, _ = self.http("POST", "/api/manage/preview", dict(payload, action=action))
        self.assertEqual(status, 200, preview)
        self.assertEqual(database.read_bytes(), before)
        status, result, _ = self.http("POST", "/api/manage/confirm",
                                      dict(payload, action=action, preview_sha256=preview["preview_sha256"]))
        self.assertEqual(status, 200, result)
        return preview, result

    def sent(self, name):
        request = self.request(name, "executor", self.tasks_root / name)
        self.assertEqual(scheduler.dispatch(self.state, request, self.message(request), *self.actor(),
                                            transport=self.fake)["status"], "SUBMITTED")
        return request

    def test_management_disabled_by_default_and_needs_controller(self):
        for path in ("/api/manage/preview", "/api/manage/confirm", "/api/manage/panes"):
            self.assertEqual(self.http("POST", path, {"action": "labels"})[0], 403)
        self.assertFalse(self.http("GET", "/api/status")[1]["allow_manage"])
        for options in ({}, {"actor": self.controller["instance_id"], "version": 1, "context": "main"},
                        {"actor": "wrong", "version": 1, "context": "main", "tasks_root": self.tasks_root}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                dashboard.DashboardServer(("127.0.0.1", 0), self.state, allow_manage=True, **options)

    def test_reconcile_writes_evidence_then_confirms_once(self):
        self.manage_server()
        request = self.sent("MAN-1")
        bad = self.http("POST", "/api/manage/preview", {"action": "reconcile", "task_id": "MAN-1", "outcome": "running"})
        self.assertEqual(bad[0], 400)
        self.assertEqual(self.http("POST", "/api/manage/preview",
                                   {"action": "reconcile", "task_id": "MAN-1", "outcome": "terminated",
                                    "observation": "x"})[0], 400)
        forged = self.http("POST", "/api/manage/confirm", {"action": "reconcile", "task_id": "MAN-1", "outcome": "running",
                                                           "observation": "seen", "preview_sha256": "0" * 64})
        self.assertEqual(forged[0], 400)
        preview, result = self.step("reconcile", task_id="MAN-1", outcome="running", observation="Pane shows the task running.")
        self.assertEqual(result["status"], "RUNNING")
        evidence = self.tasks_root / "MAN-1" / "EXECUTION.md"
        self.assertIn("Outcome: running", evidence.read_text(encoding="utf-8"))
        confirmed = json.loads((self.tasks_root / "MAN-1" / "confirmed.json").read_text(encoding="utf-8"))
        self.assertTrue(confirmed["dispatch_confirmed"])
        control.check_registered(self.state, confirmed)  # The monitor can start from this exact file.
        again = self.http("POST", "/api/manage/confirm", {"action": "reconcile", "task_id": "MAN-1", "outcome": "running",
                                                          "observation": "Pane shows the task running.",
                                                          "preview_sha256": preview["preview_sha256"]})
        self.assertEqual(again[0], 400)  # No longer an unconfirmed send.
        self.assertEqual(len(self.fake.messages), 1)

    def test_monitor_start_and_stop_use_files_only(self):
        self.manage_server()
        self.sent("MAN-2")
        self.assertEqual(self.http("POST", "/api/manage/preview", {"action": "monitor_start", "task_id": "MAN-2"})[0], 400)
        self.step("reconcile", task_id="MAN-2", outcome="running", observation="started")
        self.assertEqual(self.http("GET", "/api/status")[1]["monitors"]["MAN-2"], "not_started")
        preview, result = self.step("monitor_start", task_id="MAN-2")
        self.assertEqual((result["status"], result["pid"]), ("MONITOR_STARTED", 4242))
        argv = self.started[0][0]
        self.assertEqual(argv[-2:], ["--request", str((self.tasks_root / "MAN-2" / "confirmed.json").resolve())])
        self.assertNotIn("--notify", argv)
        (self.tasks_root / "MAN-2" / "monitor.lock").write_text("4242", encoding="ascii")
        self.assertEqual(self.http("GET", "/api/status")[1]["monitors"]["MAN-2"], "running")
        self.assertEqual(self.http("POST", "/api/manage/preview", {"action": "monitor_start", "task_id": "MAN-2"})[0], 400)
        self.step("monitor_stop", task_id="MAN-2")
        self.assertTrue((self.tasks_root / "MAN-2" / "STOP_MONITOR").is_file())
        self.assertEqual(len(self.started), 1)
        self.assertEqual(len(self.fake.messages), 1)
        with control.database(self.state) as con:
            self.assertEqual(con.execute("SELECT count(*) FROM audit WHERE action='dashboard_monitor_started'").fetchone()[0], 1)

    def test_continue_is_bounded_and_close_never_sends(self):
        self.manage_server()
        request = self.sent("MAN-3")
        self.step("reconcile", task_id="MAN-3", outcome="running", observation="started")
        request = json.loads((self.tasks_root / "MAN-3" / "confirmed.json").read_text(encoding="utf-8"))
        self.assertEqual(self.http("POST", "/api/manage/preview", {"action": "continue", "task_id": "MAN-3"})[0], 400)
        scheduler.record_observation(self.state, request, {"task_id": "MAN-3", "nonce": request["nonce"],
                                                           "status": "blocked", "stall_episode": 1})
        blocked = self.http("POST", "/api/manage/preview", {"action": "continue", "task_id": "MAN-3"})
        self.assertIn("Owner", blocked[1]["error"])
        scheduler.record_observation(self.state, request, {"task_id": "MAN-3", "nonce": request["nonce"],
                                                           "status": "incomplete", "stall_episode": 1})
        preview, result = self.step("continue", task_id="MAN-3")
        self.assertEqual((preview["used"], preview["budget"], result["status"]), (0, 2, "CONTINUE_SUBMITTED"))
        self.assertEqual(len(self.fake.messages), 2)
        self.assertEqual(self.http("POST", "/api/manage/preview", {"action": "continue", "task_id": "MAN-3"})[0], 400)
        self.assertEqual(self.http("POST", "/api/manage/preview", {"action": "close", "task_id": "MAN-3"})[0], 400)
        _, result = self.step("close", task_id="MAN-3", reason="Quota exhausted; replanning.")
        self.assertEqual(result["status"], "TERMINATED")
        self.assertIn("Outcome: terminated", (self.tasks_root / "MAN-3" / "TERMINATION.md").read_text(encoding="utf-8"))
        self.assertEqual(scheduler.snapshot(self.state, "MAN-3")["tasks"][0]["execution"], "resolved")
        self.assertEqual(len(self.fake.messages), 2)

    def test_close_refuses_delivered_work_and_release_needs_acceptance(self):
        self.manage_server()
        request, event = self.review_event("MAN-4")
        closed = self.http("POST", "/api/manage/preview", {"action": "close", "task_id": "MAN-4", "reason": "x"})
        self.assertEqual(closed[0], 400)
        self.assertIn("review", closed[1]["error"])
        self.assertEqual(self.http("POST", "/api/manage/preview", {"action": "release", "task_id": "MAN-4"})[0], 400)
        done = self.request("MAN-5", "second")
        scheduler.dispatch(self.state, done, self.message(done), *self.actor(), transport=self.fake)
        self.finish(done)
        _, result = self.step("release", task_id="MAN-5")
        self.assertEqual(result["status"], "CLAIM_RELEASED")

    def test_config_changes_only_operating_limits(self):
        self.manage_server()
        settings = self.http("GET", "/api/status")[1]["settings"]
        self.assertEqual(settings["scheduler.max_concurrency"], 2)
        for changes in ({"roles": {}}, {"workflow.transitions": {}}, {"scheduler.max_concurrency": 0},
                        {"scheduler.max_continue_prompts": 11}, {"scheduler.resources": {"gpu": -1}}, {},
                        {"scheduler.max_concurrency": 2}):
            with self.subTest(changes=changes):
                self.assertEqual(self.http("POST", "/api/manage/preview", {"action": "config", "changes": changes})[0], 400)
        preview, result = self.step("config", changes={"scheduler.max_concurrency": 3, "goal": "Ship the offline suite"})
        self.assertEqual(result["status"], "CONFIG_UPDATED")
        self.assertEqual({d["key"] for d in preview["diff"]}, {"scheduler.max_concurrency", "goal"})
        with control.database(self.state) as con:
            config = control.config_of(con)
            audit = json.loads(con.execute("SELECT data FROM audit WHERE action='config_updated'").fetchone()[0])
        self.assertEqual((config["scheduler"]["max_concurrency"], config["goal"]), (3, "Ship the offline suite"))
        self.assertEqual(audit["changes"]["scheduler.max_concurrency"], 3)
        self.assertEqual(set(config["roles"]), {"planner", "executor", "second"})

    def test_assign_pane_rebinds_with_reason_and_never_touches_controller(self):
        self.manage_server()
        workspace = str(self.root / "new-pane")
        herdr = {"role": "second", "adapter": "herdr", "harness": "pi", "executable": "offline-herdr",
                 "session": "s1", "pane": "p1", "expected_identity": {"kind": "pi", "terminal": "t1", "launch_cwd": workspace}}
        control.bind(self.state, "second", herdr, herdr["expected_identity"], reason="move to herdr")
        self.fake.listed = [{"pane_id": "p1", "terminal_id": "t1", "agent": "pi", "cwd": workspace, "name": "awo:second"},
                            {"pane_id": "p2", "terminal_id": "t2", "agent": "codex", "cwd": workspace,
                             "agent_status": "idle", "name": None}]
        self.fake.reviewer_identity = lambda spec: spec["expected_identity"]
        panes = self.http("POST", "/api/manage/panes", {})[1]
        self.assertEqual([a["pane"] for a in panes["unbound"]], ["p2"])
        for payload in ({"role": "planner", "mode": "worker", "pane": "p2"},
                        {"role": "executor", "mode": "controller", "pane": "p2"},
                        {"role": "executor", "mode": "worker", "pane": "p9", "reason": "x"},
                        {"role": "executor", "mode": "worker", "pane": "p1", "reason": "x"},
                        {"role": "executor", "mode": "worker", "pane": "p2"}):
            with self.subTest(payload=payload):
                self.assertEqual(self.http("POST", "/api/manage/preview", dict(payload, action="assign"))[0], 400)
        preview, result = self.step("assign", role="executor", mode="worker", pane="p2", reason="use the codex pane")
        self.assertEqual((preview["defer"], result["status"], result["version"]), (False, "BINDING_ACTIVE", 2))
        with control.database(self.state) as con:
            active = control.active(con, "executor")
            controller = control.active(con, "planner")
        self.assertEqual((active["spec"]["pane"], active["spec"]["expected_identity"]["kind"]), ("p2", "codex"))
        self.assertEqual(controller["instance_id"], self.controller["instance_id"])

    def test_tick_and_labels_preview_then_run_once(self):
        self.manage_server()
        with patch.object(dashboard.control, "label_panes", return_value={"status": "LABELS_SYNCED"}) as labels:
            _, result = self.step("labels", force=False)
        self.assertEqual(result["status"], "LABELS_SYNCED")
        labels.assert_called_once()
        self.assertEqual(self.http("POST", "/api/manage/preview", {"action": "tick", "run": "missing"})[0], 400)
        self.assertEqual(self.http("POST", "/api/manage/preview", {"action": "nope"})[0], 400)

    def test_metrics_counts_stages_reviews_and_stalls_read_only(self):
        request = self.request("MET-1", "executor")
        scheduler.dispatch(self.state, request, self.message(request), *self.actor(), transport=self.fake)
        request = self.confirm(request)
        control.publish_event(self.state, request, {"status": "incomplete", "stall_episode": 1})
        before = (self.state / "workflow.sqlite3").read_bytes()
        status, data, _ = self.http("GET", "/api/metrics?days=7")
        self.assertEqual(status, 200)
        self.assertEqual((data["days"], len(data["daily"]), data["stalls"]), (7, 7, {"incomplete": 1}))
        self.assertEqual(data["stages"]["send_to_start"]["count"], 1)
        self.assertEqual(sum(day["sent"] for day in data["daily"]), 1)
        self.assertEqual((self.state / "workflow.sqlite3").read_bytes(), before)
        self.assertEqual(self.http("GET", "/api/metrics?days=0")[0], 400)
        self.assertEqual(self.http("GET", "/api/metrics?days=x")[0], 400)
        spread = workflow_status._spread([1, 2, 3, 4, 100])
        self.assertEqual((spread["median"], spread["p90"], spread["max"]), (3, 100, 100))


    def test_status_flags_unstarted_send_and_waiting_review(self):
        from datetime import datetime, timedelta, timezone
        self.sent("LATE-1")
        later = datetime.now(timezone.utc) + timedelta(minutes=11)
        rows = {r["task_id"]: r for r in workflow_status.summarize(self.state, now=later)["tasks"]}
        self.assertIn("start_unconfirmed", rows["LATE-1"]["warnings"])
        early = {r["task_id"]: r for r in workflow_status.summarize(self.state)["tasks"]}
        self.assertNotIn("start_unconfirmed", early["LATE-1"]["warnings"])
        request = self.request("LATE-2", "second")
        scheduler.dispatch(self.state, request, self.message(request), *self.actor(), transport=self.fake)
        control.publish_event(self.state, self.confirm(request), {"status": "review_ready"})
        later = datetime.now(timezone.utc) + timedelta(hours=2)
        rows = {r["task_id"]: r for r in workflow_status.summarize(self.state, now=later)["tasks"]}
        self.assertIn("review_waiting", rows["LATE-2"]["warnings"])

    def test_inbox_shows_stall_cause_without_paths(self):
        request, _ = self.review_event("CAUSE-1")
        control.publish_event(self.state, request, {"status": "incomplete", "stall_episode": 1, "cause": "provider_error",
                                                    "evidence": r"Error: quota exceeded at C:\Users\x\log.txt"})
        entry = next(e for e in self.http("GET", "/api/status")[1]["inbox"] if e["report_status"] == "incomplete")
        self.assertEqual(entry["cause"], "provider_error")
        self.assertIn("quota exceeded", entry["cause_evidence"])
        self.assertNotIn("Users", entry["cause_evidence"])

if __name__ == "__main__":
    unittest.main()
