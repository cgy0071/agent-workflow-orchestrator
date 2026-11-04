"""Offline regression tests for identity, legacy admission, and exact replay."""
import copy
import json
from pathlib import Path
import unittest
import uuid

import monitor_task as monitor
import test_scheduler
import workflow_control as control
import workflow_scheduler as scheduler
import workflow_status


class InvariantTests(unittest.TestCase):
    setUp = test_scheduler.SchedulerTests.setUp
    tearDown = test_scheduler.SchedulerTests.tearDown
    spec = test_scheduler.SchedulerTests.spec
    request = test_scheduler.SchedulerTests.request
    message = test_scheduler.SchedulerTests.message
    actor = test_scheduler.SchedulerTests.actor
    send = test_scheduler.SchedulerTests.send
    confirm = test_scheduler.SchedulerTests.confirm

    def test_same_agent_uses_unique_identifiers_and_herdr_pane(self):
        a = {"terminal": "same", "launch_cwd": "one"}
        self.assertTrue(control.same_agent(a, {"terminal": "same", "launch_cwd": "two"}))
        self.assertFalse(control.same_agent(a, {"terminal": "different", "launch_cwd": "one"}))
        self.assertFalse(control.same_agent({"launch_cwd": "one"}, {"launch_cwd": "one"}))
        left = {"observed_identity": {"launch_cwd": "one"}, "spec": {"adapter": "herdr", "session": "s", "pane": "p"}}
        right = {"observed_identity": {"launch_cwd": "two"}, "spec": {"adapter": "herdr", "session": "s", "pane": "p"}}
        self.assertTrue(control.same_binding_agent(left, right))

    def test_binding_conflict_both_orders_and_allowed_pairs(self):
        same = copy.deepcopy(self.specs["executor"])
        same["role"] = "second"
        with self.assertRaisesRegex(ValueError, "executor"):
            control.bind(self.state, "second", same, same["expected_identity"], "reviewer", reason="switch")
        with self.assertRaisesRegex(ValueError, "executor"):
            control.bind(self.state, "second", same, same["expected_identity"], "reviewer",
                         defer=True, reason="prepare switch")
        reverse = self.root / "reverse"
        control.initialize(reverse, {"roles": {"planner": {}, "executor": {}},
                                     "workflow": {"reviewer_role": "planner", "transitions": {"accept": "next"}},
                                     "scheduler": {"max_concurrency": 2}})
        reviewer = copy.deepcopy(self.specs["executor"])
        reviewer["role"] = "planner"
        control.bind(reverse, "planner", reviewer, reviewer["expected_identity"], "reviewer")
        with self.assertRaisesRegex(ValueError, "planner"):
            control.bind(reverse, "executor", self.specs["executor"],
                         self.specs["executor"]["expected_identity"], "worker")
        allowed = control.bind(self.state, "second", same, same["expected_identity"], "advisor", reason="switch")
        self.assertEqual(allowed["status"], "active")
        distinct = self.specs["second"]
        self.assertEqual(control.bind(self.state, "second", distinct, distinct["expected_identity"],
                                      "reviewer", reason="switch")["status"], "active")

    def test_activation_rechecks_conflict_without_retiring_old_binding(self):
        spec = self.specs["second"]
        pending = control.bind(self.state, "second", spec, spec["expected_identity"],
                               "reviewer", defer=True, reason="prepare reviewer")
        with control.database(self.state) as con:
            row = con.execute("SELECT id,data FROM bindings WHERE role='executor' AND status='active'").fetchone()
            old = json.loads(row["data"])
            old["observed_identity"] = copy.deepcopy(spec["expected_identity"])
            old["spec"]["expected_identity"] = copy.deepcopy(spec["expected_identity"])
            con.execute("UPDATE bindings SET data=? WHERE id=?", (json.dumps(old), row["id"]))
        with self.assertRaisesRegex(ValueError, "executor"):
            control.activate(self.state, "second", spec["expected_identity"])
        with control.database(self.state) as con:
            self.assertEqual(con.execute("SELECT status FROM bindings WHERE id=?", (pending["instance_id"],)).fetchone()[0], "pending")
            self.assertEqual(con.execute("SELECT count(*) FROM bindings WHERE role='second' AND status='active'").fetchone()[0], 1)

    def test_old_ledger_self_review_rejected_before_ack(self):
        req = self.request()
        self.send(req)
        req = self.confirm(req)
        for name in ("HANDOFF.md", "RESULT.md"):
            (Path(req["cwd"]) / name).write_text(self.message(req), encoding="utf-8")
        class Done:
            def status(inner, spec):
                return {"identity": spec["expected_identity"], "status": "done", "completion_seq": 11}
        status, _, _, receipt = monitor.inspect_once(req, Done())
        self.assertEqual(status, "review_ready")
        event = control.publish_event(self.state, req, {"status": status}, receipt)
        review = Path(req["cwd"]) / "REVIEW.md"
        review.write_text(self.message(req) + "\nVerdict: accept\n", encoding="utf-8")
        with control.database(self.state) as con:
            original = self.controller
            bad = copy.deepcopy(original)
            bad["instance_id"] = uuid.uuid4().hex
            bad["version"] = original["version"] + 1
            bad["observed_identity"] = copy.deepcopy(self.specs["executor"]["expected_identity"])
            con.execute("UPDATE bindings SET status='retired' WHERE id=?", (original["instance_id"],))
            con.execute("INSERT INTO bindings VALUES (?,?,?,?,?,?,?)",
                        (bad["instance_id"], "planner", bad["version"], "controller", "inbox_only", "active", json.dumps(bad)))
        who = (bad["instance_id"], bad["version"], "main")
        with self.assertRaisesRegex(ValueError, "same agent"):
            control.settle(self.state, event, *who, review, "accept")
        with control.database(self.state) as con:
            self.assertEqual(con.execute("SELECT status FROM events WHERE id=?", (event,)).fetchone()[0], "pending")
        control.acknowledge(self.state, event, *who)
        with self.assertRaisesRegex(ValueError, "same agent"):
            control.record_review(self.state, event, *who, review, "accept")
        with control.database(self.state) as con:
            self.assertIsNone(con.execute("SELECT data FROM reviews WHERE event=?", (event,)).fetchone())
            self.assertEqual(con.execute("SELECT status FROM events WHERE id=?", (event,)).fetchone()[0], "acknowledged")

    def test_legacy_registration_warns_and_dispatch_still_refuses(self):
        control.update_config(self.state, {"scheduler.max_concurrency": 1}, *self.actor())
        first = self.request("OLD-A")
        first["dispatch_confirmed"] = True
        self.assertEqual(control.register_task(self.state, first, *self.actor())["warnings"], [])
        second = self.request("OLD-B", "second")
        second["dispatch_confirmed"] = True
        result = control.register_task(self.state, second, *self.actor())
        self.assertEqual(result["warnings"], ["max_concurrency_exceeded"])
        self.assertIn("over_concurrency", workflow_status.summarize(self.state, "OLD-B")["tasks"][0]["warnings"])
        with control.database(self.state) as con:
            self.assertEqual(con.execute("SELECT count(*) FROM audit WHERE action='register_over_concurrency'").fetchone()[0], 1)
        note = Path(first["cwd"]) / "TERMINATION.md"
        note.write_text(first["task_id"] + " " + first["nonce"] + "\nStopped after inspection.\n", encoding="utf-8")
        control.resolve_task(self.state, first["task_id"], *self.actor(), note)
        third = self.request("NEW-C")
        with self.assertRaisesRegex(ValueError, "max_concurrency reached"):
            scheduler.dispatch(self.state, third, self.message(third), *self.actor(), transport=self.fake)
        self.assertEqual(self.fake.messages, [])
        note = Path(second["cwd"]) / "TERMINATION.md"
        note.write_text(second["task_id"] + " " + second["nonce"] + "\nStopped after inspection.\n", encoding="utf-8")
        control.resolve_task(self.state, second["task_id"], *self.actor(), note)
        # A closed task no longer holds a slot, so the over-limit warning goes away.
        self.assertNotIn("over_concurrency", workflow_status.summarize(self.state, "OLD-B")["tasks"][0]["warnings"])

    def test_closed_reconcile_exact_replay_is_read_only(self):
        req = self.request()
        self.send(req)
        note = Path(req["cwd"]) / "TERMINATION.md"
        note.write_text(self.message(req) + "\nOutcome: terminated\n", encoding="utf-8")
        self.assertEqual(scheduler.reconcile(self.state, req["task_id"], "terminated", note, *self.actor())["status"], "TERMINATED")
        with control.database(self.state) as con:
            before = con.execute("SELECT count(*) FROM task_journal WHERE task=?", (req["task_id"],)).fetchone()[0]
        self.assertEqual(scheduler.reconcile(self.state, req["task_id"], "terminated", note, *self.actor())["status"], "ALREADY_RECONCILED")
        with control.database(self.state) as con:
            self.assertEqual(con.execute("SELECT count(*) FROM task_journal WHERE task=?", (req["task_id"],)).fetchone()[0], before)
        note.write_text(self.message(req) + "\nOutcome: terminated\nchanged\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "already closed"):
            scheduler.reconcile(self.state, req["task_id"], "terminated", note, *self.actor())
