"""Worker result blocks, cause-aware continuation, the repair-chain limit and task history; offline."""
import json
from pathlib import Path
import subprocess
import sys
import unittest

import monitor_task as monitor
import workflow_control as control
import workflow_scheduler as scheduler
import workflow_status
import test_followup_flow
import test_scheduler


class HandoffTests(unittest.TestCase):
    Base = test_followup_flow.FlowTests
    setUp, tearDown, spec, request, actor = Base.setUp, Base.tearDown, Base.spec, Base.request, Base.actor
    message, send, confirm, delivered = Base.message, Base.send, Base.confirm, Base.delivered

    def test_worker_report_parses_last_block_and_never_trusts_bad_input(self):
        self.assertIsNone(monitor.worker_report("no block here"))
        text = ("notes\n```awo-result\n{\"outcome\": \"partial\"}\n```\n"
                "```awo-result {\"outcome\": \"done\", \"summary\": \"Parser added\", \"remaining\": []} ```\nTASK_DONE")
        self.assertEqual(monitor.worker_report(text), {"outcome": "done", "summary": "Parser added", "remaining": []})
        self.assertIn("error", monitor.worker_report("```awo-result\n{not json}\n```"))
        self.assertIn("error", monitor.worker_report("```awo-result\n{\"outcome\": \"accepted\"}\n```"))
        long = monitor.worker_report("```awo-result\n" + json.dumps({"outcome": "partial", "remaining": ["x" * 900] * 30})
                                     + "\n```")
        self.assertEqual((len(long["remaining"]), len(long["remaining"][0])), (10, 200))

    def test_stall_cause_uses_the_block_and_review_ready_keeps_it_as_a_hint(self):
        req = self.request("REPORT")
        handoff = Path(req["cwd"]) / "HANDOFF.md"
        silent = type("Silent", (), {"output": lambda inner, spec: "idle"})()
        def cause(block):
            handoff.write_text(req["task_id"] + " " + req["nonce"] + "\n```awo-result\n" + json.dumps(block) + "\n```\n",
                               encoding="utf-8")
            return monitor.stall_cause(req, silent)
        asked = cause({"outcome": "blocked", "question": "Which port should the server use?"})
        self.assertEqual((asked["cause"], asked["evidence"]), ("worker_question", "Which port should the server use?"))
        self.assertEqual(cause({"outcome": "done", "summary": "All tests pass"})["cause"], "marker_missing")
        partial = cause({"outcome": "partial", "remaining": ["link the binary", "run case A"]})
        self.assertEqual((partial["cause"], partial["evidence"]), ("handoff_without_marker", "link the binary; run case A"))
        loud = type("Loud", (), {"output": lambda inner, spec: "Error: 429 rate limit"})()
        partial_loud = monitor.stall_cause(req, loud)
        self.assertEqual(partial_loud["cause"], "provider_error")  # A rate limit explains a partial stop.
        self.assertEqual(partial_loud["worker_report"]["outcome"], "partial")
        cause({"outcome": "blocked", "question": "Which port?"})
        self.assertEqual(monitor.stall_cause(req, loud)["cause"], "worker_question")  # A stated question wins.
        self.assertIn("rewrites only the end", monitor.event_guidance("incomplete", "T", "marker_missing"))

    def test_continuation_opens_by_cause_and_carries_one_controller_note(self):
        req = self.request(); self.send(req); req = self.confirm(req)
        report = {"task_id": req["task_id"], "nonce": req["nonce"], "status": "incomplete", "stall_episode": 1,
                  "cause": "marker_missing"}
        scheduler.record_observation(self.state, req, report)
        control.publish_event(self.state, req, report)
        with self.assertRaisesRegex(ValueError, "note"):
            scheduler.continue_task(self.state, req["task_id"], *self.actor(), transport=self.fake, note="x" * 2001)
        result = scheduler.continue_task(self.state, req["task_id"], *self.actor(), transport=self.fake,
                                         note="Use port 8080.")
        self.assertEqual(result["status"], "CONTINUE_SUBMITTED")
        sent = self.fake.messages[-1]
        self.assertTrue(sent.startswith("Your handoff says the work is done"))
        self.assertIn("Controller note (from the reviewer, about this task only): Use port 8080.", sent)
        self.assertIn("awo-result", sent)
        journal = scheduler.snapshot(self.state, req["task_id"])["tasks"][0]["journal"]
        reserved = next(j for j in journal if j["kind"] == "continue_reserved")["data"]
        self.assertEqual((reserved["cause"], reserved["note"]), ("marker_missing", True))
        self.assertNotIn("8080", json.dumps(reserved))  # The journal keeps the hash, not the note text.

    def chain_of_repairs(self, count):
        """A root task followed by count repair reviews in a row, each step a real follow-up."""
        req, event, review = self.delivered("ROOT", "repair")
        control.settle(self.state, event, *self.actor(), review, "repair")
        for step in range(1, count):
            book = self.root / ("book-%d" % step) / "TASK.md"
            book.parent.mkdir(); book.write_text("# Repair step\n", encoding="utf-8")
            follow = scheduler.follow_up(self.state, event, "FIX-%d" % step, book, actor=self.actor()[0], version=1,
                                         context="main", source="owner", transport=self.fake)
            child = follow["request"]
            scheduler.dispatch(self.state, child, follow["message"], *self.actor(), transport=self.fake)
            child = self.confirm(child)
            for name in ("HANDOFF.md", "RESULT.md"):
                (Path(child["cwd"]) / name).write_text(child["task_id"] + " " + child["nonce"] + "\n" +
                                                       child["completion"]["marker"] + "\n", encoding="utf-8")
            done = type("Done", (), {"status": lambda inner, spec: {"identity": spec["expected_identity"],
                                                                    "status": "done", "completion_seq": 11},
                                     "output": lambda inner, spec: ""})()
            status, _, _, receipt = monitor.inspect_once(child, done)
            self.assertEqual(status, "review_ready")
            event = control.publish_event(self.state, child, {"status": status}, receipt)
            review = Path(child["cwd"]) / "REVIEW.md"
            review.write_text(child["task_id"] + " " + child["nonce"] + "\nVerdict: repair\n", encoding="utf-8")
            self.assertEqual(control.settle(self.state, event, *self.actor(), review, "repair")["status"], "SETTLED")
            self.fake.sequences["executor"] = 10
        return event

    def test_repair_chain_limit_stops_for_the_owner_and_needs_an_explicit_override(self):
        event = self.chain_of_repairs(3)
        book = self.root / "TASK.md"; book.write_text("# Fourth repair\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "repairs in a row"):
            scheduler.follow_up(self.state, event, "FIX-3", book, actor=self.actor()[0], version=1,
                                context="main", source="owner", transport=self.fake)
        self.assertFalse((self.root / "FIX-3").exists())
        follow = scheduler.follow_up(self.state, event, "FIX-3", book, actor=self.actor()[0], version=1, context="main",
                                     source="Owner chose to keep repairing", transport=self.fake, allow_long_chain=True)
        self.assertTrue(follow["request"]["authorization"]["allow_long_repair_chain"])
        hand_made = dict(follow["request"], authorization=dict(follow["request"]["authorization"]))
        del hand_made["authorization"]["allow_long_repair_chain"]
        with self.assertRaisesRegex(ValueError, "max_repair_chain"):  # The gate is in the ledger, not only the helper.
            scheduler.dispatch(self.state, hand_made, follow["message"], *self.actor(), transport=self.fake)
        self.assertEqual(scheduler.dispatch(self.state, follow["request"], follow["message"], *self.actor(),
                                            transport=self.fake)["status"], "SUBMITTED")
        with control.database(self.state) as con:
            self.assertEqual(control.repair_chain(con, "FIX-2"), 3)
        for bad in (-1, 51, True):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                control.apply_config_changes({"roles": {"a": {}}}, {"scheduler.max_repair_chain": bad})

    def test_history_shows_the_chain_and_timeline_read_only(self):
        self.chain_of_repairs(2)
        database = self.state / "workflow.sqlite3"
        before = database.read_bytes()
        history = workflow_status.task_history(self.state, "FIX-1")
        self.assertEqual([item["task_id"] for item in history["chain"]], ["ROOT", "FIX-1"])
        self.assertEqual(history["chain"][0]["verdicts"], ["repair"])
        kinds = [entry["kind"] for entry in history["timeline"]]
        for kind in ("dispatch_reserved", "dispatch_submitted", "dispatch_reconciled", "delivery_review_ready",
                     "review_repair", "attempt_closed_after_review"):
            self.assertIn(kind, kinds)
        self.assertEqual(database.read_bytes(), before)
        text = workflow_status.format_history(history)
        self.assertIn("Chain: ROOT[repair] -> FIX-1[repair]", text)
        proc = subprocess.run([sys.executable, "-B", str(Path(control.__file__)), "--state", str(self.state),
                               "history", "--task", "FIX-1"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("review_repair", proc.stdout)
        with self.assertRaisesRegex(ValueError, "unknown task"):
            workflow_status.task_history(self.state, "NOPE")


if __name__ == "__main__":
    unittest.main()
