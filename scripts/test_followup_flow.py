"""Offline follow-up, settlement, and observed-start regression tests."""
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import monitor_task as monitor
import workflow_control as control
import workflow_scheduler as scheduler
import test_scheduler


class FlowTests(unittest.TestCase):
    setUp = test_scheduler.SchedulerTests.setUp
    tearDown = test_scheduler.SchedulerTests.tearDown
    spec = test_scheduler.SchedulerTests.spec
    request = test_scheduler.SchedulerTests.request
    actor = test_scheduler.SchedulerTests.actor
    message = test_scheduler.SchedulerTests.message
    send = test_scheduler.SchedulerTests.send
    confirm = test_scheduler.SchedulerTests.confirm
    finish = test_scheduler.SchedulerTests.finish

    def delivered(self, name, verdict="accept"):
        req = self.request(name)
        self.assertEqual(self.send(req)["status"], "SUBMITTED")
        req = self.confirm(req)
        for file in ("HANDOFF.md", "RESULT.md"):
            (Path(req["cwd"]) / file).write_text(name + " " + req["nonce"] + "\n", encoding="utf-8")
        class Done:
            def status(inner, spec):
                return {"identity": spec["expected_identity"], "status": "done", "completion_seq": 11}
        status, _, _, receipt = monitor.inspect_once(req, Done())
        self.assertEqual(status, "review_ready")
        event = control.publish_event(self.state, req, {"status": status}, receipt)
        review = Path(req["cwd"]) / "REVIEW.md"
        review.write_text(name + " " + req["nonce"] + "\nVerdict: " + verdict + "\n", encoding="utf-8")
        return req, event, review

    def test_follow_up_accept_and_repair_use_current_bindings_and_dispatch(self):
        for verdict in ("accept", "repair"):
            with self.subTest(verdict=verdict):
                req, event, review = self.delivered("PARENT-" + verdict, verdict)
                result = control.settle(self.state, event, *self.actor(), review, verdict)
                self.assertEqual(result["status"], "SETTLED")
                self.assertEqual(scheduler.snapshot(self.state, req["task_id"])["tasks"][0]["execution"], "resolved")
                book = self.root / ("book-" + verdict) / "TASK.md"
                book.parent.mkdir()
                book.write_text("# Implement next task\n", encoding="utf-8")
                name = "NEXT-" + verdict
                follow = scheduler.follow_up(self.state, event, name, book, actor=self.actor()[0],
                                             version=1, context="main", source="owner approved", transport=self.fake)
                child = follow["request"]
                self.assertEqual((child["parent_task"], child["parent_review"]["event_id"]), (req["task_id"], event))
                self.assertEqual(child["authorization"]["source"], "owner approved")
                self.assertEqual(Path(child["taskbook_path"]).read_text(encoding="utf-8"), "# Implement next task\n")
                self.assertIn("The final line of HANDOFF.md must be exactly: " + child["completion"]["marker"],
                              follow["message"])
                self.assertIn(scheduler._delivery_instructions(child), scheduler._continue_message(child))
                self.assertEqual(scheduler.dispatch(self.state, child, follow["message"], *self.actor(),
                                                    transport=self.fake)["status"], "SUBMITTED")
                self.assertEqual(scheduler.snapshot(self.state, name)["tasks"][0]["dispatch"]["status"], "submitted")
                self.confirm(child, "terminated")

    def test_follow_up_reject_and_changed_parent_evidence_fail(self):
        req, event, review = self.delivered("REJECTED", "reject")
        self.assertEqual(control.settle(self.state, event, *self.actor(), review, "reject")["status"], "SETTLED")
        book = self.root / "TASK.md"
        book.write_text("# Next\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "does not admit"):
            scheduler.follow_up(self.state, event, "NOPE", book, actor=self.actor()[0], version=1,
                                context="main", source="owner", transport=self.fake)
        req2, event2, review2 = self.delivered("PARENT-EDIT")
        control.settle(self.state, event2, *self.actor(), review2, "accept")
        review2.write_text(review2.read_text(encoding="utf-8") + "changed\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "changed"):
            scheduler.follow_up(self.state, event2, "EDITED", book, actor=self.actor()[0], version=1,
                                context="main", source="owner", transport=self.fake)

    def test_follow_up_refreshes_bindings_and_existing_output_is_never_overwritten(self):
        req, event, review = self.delivered("PARENT-SWITCH")
        self.assertEqual(control.settle(self.state, event, *self.actor(), review, "accept")["status"], "SETTLED")
        worker = dict(self.specs["executor"])
        worker["expected_identity"] = dict(worker["expected_identity"], terminal="new-worker")
        control.bind(self.state, "executor", worker, worker["expected_identity"], reason="new worker")
        reviewer = dict(self.specs["planner"])
        reviewer["expected_identity"] = dict(reviewer["expected_identity"], terminal="new-reviewer")
        controller = control.bind(self.state, "planner", reviewer, reviewer["expected_identity"], "controller",
                                  reason="new reviewer")
        book = self.root / "TASK.md"
        book.write_text("# Follow up\n", encoding="utf-8")
        follow = scheduler.follow_up(self.state, event, "SWITCHED", book, actor=controller["instance_id"],
                                     version=controller["version"], context="main", source="owner", transport=self.fake)
        self.assertEqual(follow["request"]["worker"]["expected_identity"]["terminal"], "new-worker")
        self.assertEqual(follow["request"]["reviewer"]["expected_identity"]["terminal"], "new-reviewer")
        review.write_text(review.read_text(encoding="utf-8") + "tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "parent review hash mismatch"):
            scheduler.dispatch(self.state, follow["request"], follow["message"], controller["instance_id"],
                               controller["version"], "main", transport=self.fake)
        review.write_text(review.read_text(encoding="utf-8").replace("tampered\n", ""), encoding="utf-8")
        out = self.root / "request-out.json"
        out.write_text("keep", encoding="utf-8")
        message_out = self.root / "message.txt"
        argv = [sys.executable, "-B", "scripts/workflow_scheduler.py", "--state", str(self.state),
                "follow-up", "--parent-event", event, "--task-id", "COLLISION", "--taskbook", str(book),
                "--source", "owner", "--out", str(out), "--message-out", str(message_out),
                "--actor", controller["instance_id"], "--version", str(controller["version"]), "--context", "main"]
        proc = subprocess.run(argv, capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(out.read_text(encoding="utf-8"), "keep")
        self.assertFalse(message_out.exists())
        self.assertFalse((self.root / "COLLISION").exists())

    def test_follow_up_failure_leaves_no_folder_so_a_rerun_works(self):
        req, event, review = self.delivered("PARENT-RETRY")
        control.settle(self.state, event, *self.actor(), review, "accept")
        book = self.root / "TASK.md"
        book.write_text("# Retry\n", encoding="utf-8")
        with patch.object(scheduler, "validate_request", side_effect=ValueError("bad request")):
            with self.assertRaisesRegex(ValueError, "bad request"):
                scheduler.follow_up(self.state, event, "RETRY", book, actor=self.actor()[0], version=1,
                                    context="main", source="owner", transport=self.fake)
        self.assertFalse((self.root / "RETRY").exists())
        follow = scheduler.follow_up(self.state, event, "RETRY", book, actor=self.actor()[0], version=1,
                                     context="main", source="owner", transport=self.fake)
        self.assertEqual(follow["status"], "FOLLOW_UP_READY")

    def test_settle_restarts_after_release_failure_without_duplicate_review(self):
        req, event, review = self.delivered("SETTLE-FAIL")
        with patch.object(scheduler, "release", side_effect=RuntimeError("temporary failure")):
            first = control.settle(self.state, event, *self.actor(), review, "accept")
        self.assertEqual((first["status"], first["completed"]), ("SETTLE_INCOMPLETE", ["acknowledged", "reviewed"]))
        second = control.settle(self.state, event, *self.actor(), review, "accept")
        self.assertEqual(second["status"], "SETTLED")
        with control.database(self.state) as con:
            self.assertEqual(con.execute("SELECT count(*) FROM reviews WHERE event=?", (event,)).fetchone()[0], 1)
        with self.assertRaisesRegex(ValueError, "existing review differs"):
            control.settle(self.state, event, *self.actor(), review, "repair")

    def test_repair_settle_closes_finished_attempt_with_review_evidence(self):
        req, event, review = self.delivered("REPAIR-SETTLE", "repair")
        result = control.settle(self.state, event, *self.actor(), review, "repair")
        self.assertEqual(result["status"], "SETTLED")
        self.assertEqual(result["parent_review"]["event_id"], event)
        task = scheduler.snapshot(self.state, req["task_id"])["tasks"][0]
        self.assertEqual(task["execution"], "resolved")
        closes = [entry for entry in task["journal"] if entry["kind"] == "attempt_closed_after_review"]
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0]["data"]["event_id"], event)
        self.assertEqual(control.settle(self.state, event, *self.actor(), review, "repair")["status"], "SETTLED")
        self.assertEqual(len([entry for entry in scheduler.snapshot(self.state, req["task_id"])["tasks"][0]["journal"]
                              if entry["kind"] == "attempt_closed_after_review"]), 1)

    def test_settle_rejects_open_stall_without_mutating_ledger(self):
        req = self.request("STALLED")
        self.assertEqual(self.send(req)["status"], "SUBMITTED")
        req = self.confirm(req)
        event = control.publish_event(self.state, req, {"status": "incomplete"})
        review = Path(req["cwd"]) / "REVIEW.md"
        review.write_text(req["task_id"] + " " + req["nonce"] + "\nVerdict: repair\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "still open"):
            control.settle(self.state, event, *self.actor(), review, "repair")
        with control.database(self.state) as con:
            self.assertEqual(con.execute("SELECT status FROM events WHERE id=?", (event,)).fetchone()[0], "pending")

    def test_confirm_start_three_read_only_conditions_and_no_observation(self):
        for condition in ("working", "completion_seq_advanced", "task_and_nonce_in_output"):
            with self.subTest(condition=condition):
                req = self.request("OBS-" + condition)
                self.assertEqual(self.send(req)["status"], "SUBMITTED")
                class Observed:
                    sends = 0
                    def status(inner, spec):
                        return {"identity": spec["expected_identity"],
                                "status": "working" if condition == "working" else "idle",
                                "completion_seq": 11 if condition == "completion_seq_advanced" else 10}
                    def output(inner, spec):
                        return req["task_id"] + " " + req["nonce"] if condition == "task_and_nonce_in_output" else ""
                    def send(inner, spec, message):
                        inner.sends += 1
                        raise AssertionError("confirm-start must never send")
                transport = Observed()
                result = scheduler.confirm_start(self.state, req["task_id"], Path(req["cwd"]) / "confirmed.json",
                                                 *self.actor(), transport=transport)
                self.assertEqual(result["status"], "RUNNING")
                self.assertIn(condition, result["conditions"])
                self.assertEqual(transport.sends, 0)
                confirmed = json.loads(Path(result["request_out"]).read_text(encoding="utf-8"))
                monitor.validate_request(confirmed)
                control.check_registered(self.state, confirmed)
                with self.assertRaisesRegex(ValueError, "unconfirmed"):
                    scheduler.observe_start(self.state, req["task_id"], transport)
                self.confirm(req, "terminated")
        req = self.request("NOT-OBSERVED")
        self.assertEqual(self.send(req)["status"], "SUBMITTED")
        self.assertEqual(scheduler.observe_start(self.state, req["task_id"], self.fake)["status"], "NOT_OBSERVED")
        self.assertFalse((Path(req["cwd"]) / "EXECUTION.md").exists())

    def test_confirm_start_wait_and_identity_change_never_send(self):
        req = self.request("WAIT-START")
        self.assertEqual(self.send(req)["status"], "SUBMITTED")
        class Sequence:
            reads = 0
            sends = 0
            def status(inner, spec):
                inner.reads += 1
                return {"identity": spec["expected_identity"],
                        "status": "working" if inner.reads > 1 else "idle", "completion_seq": 10}
            def output(inner, spec): return ""
            def send(inner, spec, message): inner.sends += 1
        client = Sequence()
        waited = []
        result = scheduler.confirm_start(self.state, req["task_id"], Path(req["cwd"]) / "confirmed.json",
                                         *self.actor(), wait=10, transport=client, sleep=waited.append)
        self.assertEqual((result["status"], client.reads, client.sends), ("RUNNING", 2, 0))
        self.assertEqual(waited, [5])
        self.confirm(req, "terminated")
        other = self.request("CHANGED-ID")
        self.assertEqual(self.send(other)["status"], "SUBMITTED")
        class Changed(Sequence):
            def status(inner, spec):
                return {"identity": dict(spec["expected_identity"], terminal="other"),
                        "status": "working", "completion_seq": 11}
        result = scheduler.confirm_start(self.state, other["task_id"], Path(other["cwd"]) / "confirmed.json",
                                         *self.actor(), transport=Changed())
        self.assertEqual(result["status"], "IDENTITY_CHANGED")
        self.assertFalse((Path(other["cwd"]) / "EXECUTION.md").exists())

    def test_fast_poll_wakes_on_change_and_stop_and_can_be_disabled(self):
        req = self.request("POLL")
        class Live:
            calls = 0
            def status(inner, spec):
                inner.calls += 1
                return {"status": "working" if inner.calls == 2 else "idle", "completion_seq": 10}
        client = Live()
        waits = []
        reason = monitor.wait_for_change(req, client, ("idle", 10), Path(req["cwd"]), 300, 30,
                                         sleep=waits.append)
        self.assertEqual((reason, waits, client.calls), ("changed", [30, 30], 2))
        waits.clear()
        self.assertEqual(monitor.wait_for_change(req, client, ("idle", 10), Path(req["cwd"]), 300, 0,
                                                 sleep=waits.append), "interval")
        self.assertEqual(waits, [300])
        stop = Path(req["cwd"]) / "STOP_MONITOR"
        def sleep(_): stop.write_text("stop", encoding="utf-8")
        self.assertEqual(monitor.wait_for_change(req, client, ("idle", 10), Path(req["cwd"]), 300, 30,
                                                 sleep=sleep), "closed")
        stop.unlink()
        class Broken:
            def status(inner, spec): raise OSError("temporary")
        self.assertEqual(monitor.wait_for_change(req, Broken(), ("idle", 10), Path(req["cwd"]), 300, 30,
                                                 sleep=lambda _: None), "query_failed")

    def test_fast_poll_validation_rejects_bad_values(self):
        req = self.request("BAD-POLL")
        req["monitor"] = {"interval_seconds": 10}
        for bad in (-1, 11, True, 1.5):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "fast_poll_seconds"):
                monitor.validate_request(dict(req, monitor={"interval_seconds": 10, "fast_poll_seconds": bad}),
                                         require_dispatch=False)


if __name__ == "__main__":
    unittest.main()
