"""Offline integration checks for binding, switching, review, and inbox routing."""
import copy
import json
from pathlib import Path
import shutil
import sys
import unittest
import uuid
from unittest.mock import patch

import monitor_task as monitor
import workflow_control as control


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = (Path(__file__).resolve().parents[1] / "runtime").resolve()
        self.sandbox.mkdir(exist_ok=True)
        self.root = self.sandbox / ("test-workflow-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.state = self.root / "state"
        self.config = {"roles": {"planner": {}, "executor": {}, "paper": {}}, "workflow": {
            "dispatch_role": "planner", "execution_role": "executor", "reviewer_role": "planner",
            "transitions": {"accept": "next_task", "repair": "repair_task", "reject": "stop"}}}
        control.initialize(self.state, self.config)
        self.planner_spec = self.spec("planner", reviewer=True)
        self.worker_spec = self.spec("executor")
        self.planner = control.bind(self.state, "planner", self.planner_spec,
                                    self.planner_spec["expected_identity"], "controller")
        control.bind(self.state, "executor", self.worker_spec, self.worker_spec["expected_identity"])
        self.req = {"task_id": "TASK-ONE", "nonce": "nonce-one", "cwd": str(self.root),
                    "dispatch_confirmed": True, "worker": dict(self.worker_spec, baseline_completion_seq=10),
                    "reviewer": self.planner_spec, "workflow_state": str(self.state),
                    "authorization": {"source": "offline tests only", "notify_reviewer": True},
                    "completion": {"binding_artifact": "HANDOFF.md", "artifacts": ["RESULT.md"]}}

    def tearDown(self):
        target = self.root.resolve()
        if target.parent != self.sandbox or not target.name.startswith("test-workflow-"):
            raise RuntimeError("refuse unsafe cleanup")
        shutil.rmtree(target)

    def spec(self, role, reviewer=False):
        identity = {"kind": "any-harness", "terminal": "terminal-" + role, "launch_cwd": str(self.root)}
        value = {"role": role, "harness": "any-harness", "adapter": "command", "expected_identity": identity}
        status = {"identity": identity, "status": "idle", "completion_seq": 10}
        value["identity_argv" if reviewer else "status_argv"] = [sys.executable, "-c", "print(" + repr(json.dumps(status)) + ")"]
        value["prompt_argv" if reviewer else "output_argv"] = [sys.executable, "-c", "print('offline')"] + (["{message}"] if reviewer else [])
        return value

    def actor(self, binding=None, context="main"):
        binding = binding or self.planner
        return binding["instance_id"], binding["version"], context

    def register(self):
        return control.register_task(self.state, self.req, *self.actor())

    def event(self, status="review_ready"):
        self.register()
        return control.publish_event(self.state, self.req, {"status": status})

    def review(self, event, verdict="accept"):
        control.acknowledge(self.state, event, *self.actor())
        file = self.root / "REVIEW.md"
        file.write_text("# TASK-ONE nonce-one\nVerdict: " + verdict + "\nEvidence: offline fixture\n", encoding="utf-8")
        return control.record_review(self.state, event, *self.actor(), file, verdict)

    def test_init_is_non_overwriting_and_roles_are_configurable(self):
        with self.assertRaises(FileExistsError): control.initialize(self.state, self.config)
        other = self.root / "other"
        control.initialize(other, {"roles": {"designer": {}}, "workflow": {"reviewer_role": "designer"}})

    def test_real_read_only_preflight(self):
        self.assertEqual(monitor.preflight(self.req)["status"], "PREFLIGHT_OK")
        bad = copy.deepcopy(self.req); bad["worker"]["baseline_completion_seq"] = 9
        with self.assertRaises(ValueError): monitor.preflight(bad)

    def test_side_unknown_and_stale_controller_are_rejected(self):
        for ctx in ("side", "unknown"):
            with self.assertRaises(ValueError): control.register_task(self.state, self.req, *self.actor(context=ctx))
        spec = self.spec("planner", True); spec["expected_identity"]["terminal"] = "new-terminal"
        new = control.bind(self.state, "planner", spec, spec["expected_identity"], "controller", reason="handoff")
        self.assertEqual(new["version"], 2)
        with self.assertRaises(ValueError): self.register()

    def test_running_worker_switch_is_deferred_and_rechecked(self):
        self.register()
        spec = self.spec("executor"); spec["expected_identity"]["terminal"] = "replacement"
        with self.assertRaises(ValueError): control.bind(self.state, "executor", spec, spec["expected_identity"], reason="switch")
        pending = control.bind(self.state, "executor", spec, spec["expected_identity"], defer=True, reason="after current")
        self.assertEqual(pending["status"], "pending")
        with self.assertRaises(ValueError): control.activate(self.state, "executor", spec["expected_identity"])
        control.publish_event(self.state, self.req, {"status": "review_ready"})
        with self.assertRaises(ValueError): control.activate(self.state, "executor", self.worker_spec["expected_identity"])
        self.assertEqual(control.activate(self.state, "executor", spec["expected_identity"])["version"], 2)

    def test_uncertain_monitor_does_not_release_worker(self):
        self.event("adapter_error")
        with self.assertRaises(ValueError):
            control.bind(self.state, "executor", self.worker_spec, self.worker_spec["expected_identity"], reason="unsafe switch")

    def test_uncertain_task_requires_inspected_termination_note(self):
        self.event("adapter_error")
        note = self.root / "TERMINATION.md"
        note.write_text("wrong task", encoding="utf-8")
        with self.assertRaises(ValueError):
            control.resolve_task(self.state, "TASK-ONE", *self.actor(), note)
        note.write_text("TASK-ONE nonce-one\nInspected: worker is stopped; lease released.\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            control.resolve_task(self.state, "TASK-ONE", *self.actor(context="side"), note)
        control.resolve_task(self.state, "TASK-ONE", *self.actor(), note)
        new = control.bind(self.state, "executor", self.worker_spec, self.worker_spec["expected_identity"], reason="verified stopped")
        self.assertEqual(new["version"], 2)

    def test_exact_thread_requires_real_branch_fields(self):
        with self.assertRaises(ValueError):
            control.bind(self.state, "paper", self.spec("paper", True), self.spec("paper", True)["expected_identity"], "reviewer", "exact_thread")

    def test_inbox_is_durable_without_sending_to_a_side_context(self):
        event = self.event()
        class NoSend:
            def send(self, *args): raise AssertionError("must not send")
        self.assertEqual(control.deliver_event(self.state, event, NoSend())["status"], "INBOX_ONLY")
        with self.assertRaises(ValueError): control.acknowledge(self.state, event, *self.actor(context="side"))
        self.assertEqual(control.acknowledge(self.state, event, *self.actor())["status"], "ACKNOWLEDGED")

    def test_controller_switch_keeps_pending_events_and_original_request(self):
        event = self.event(); before = control.digest(self.req)
        spec = self.spec("planner", True); spec["expected_identity"]["terminal"] = "new-main"
        new = control.bind(self.state, "planner", spec, spec["expected_identity"], "controller", reason="controller handoff")
        with self.assertRaises(ValueError): control.acknowledge(self.state, event, *self.actor())
        control.acknowledge(self.state, event, *self.actor(new))
        with control.database(self.state) as con:
            snapshot = json.loads(con.execute("SELECT data FROM tasks").fetchone()[0])["request"]
        self.assertEqual(control.digest(snapshot), before)

    def test_review_hash_gate_and_configurable_rejection(self):
        event = self.event()
        with self.assertRaises(ValueError): control.next_gate(self.state, event, *self.actor())
        review = self.review(event)
        self.assertEqual(control.next_gate(self.state, event, *self.actor())["status"], "NEXT_GATE_PASS")
        Path(review["path"]).write_text("tampered")
        with self.assertRaises(ValueError): control.next_gate(self.state, event, *self.actor())

    def test_reject_review_blocks_followup(self):
        event = self.event(); self.review(event, "reject")
        with self.assertRaises(ValueError): control.next_gate(self.state, event, *self.actor())

    def test_followup_is_bound_to_exact_parent_review(self):
        event = self.event()
        review = self.review(event)
        next_req = copy.deepcopy(self.req)
        next_req.update(task_id="TASK-TWO", nonce="nonce-two", parent_task="TASK-ONE", parent_review=review)
        broken = copy.deepcopy(next_req)
        broken["parent_review"]["event_id"] = "not-a-reviewed-event"
        with self.assertRaises(ValueError):
            control.register_task(self.state, broken, *self.actor())
        self.assertEqual(control.register_task(self.state, next_req, *self.actor())["status"], "REGISTERED")

    def test_registration_rejects_edited_frozen_request(self):
        self.register()
        control.check_registered(self.state, self.req)
        changed = copy.deepcopy(self.req); changed["authorization"]["source"] = "changed"
        with self.assertRaises(ValueError): control.check_registered(self.state, changed)
        with self.assertRaises(ValueError): control.register_task(self.state, changed, *self.actor())

    def test_separate_paper_reviewer_and_controller(self):
        paper_spec = self.spec("paper", True)
        paper = control.bind(self.state, "paper", paper_spec, paper_spec["expected_identity"], "reviewer")
        self.req["reviewer"] = paper_spec
        self.req["authorization"]["allow_reviewer_override"] = True
        event = self.event()
        control.acknowledge(self.state, event, *self.actor(paper))
        review_file = self.root / "REVIEW.md"
        review_file.write_text("TASK-ONE nonce-one\nVerdict: accept\n", encoding="utf-8")
        control.record_review(self.state, event, *self.actor(paper), review_file, "accept")
        with self.assertRaises(ValueError): control.next_gate(self.state, event, *self.actor(paper))
        self.assertEqual(control.next_gate(self.state, event, *self.actor())["status"], "NEXT_GATE_PASS")

    def test_same_window_with_another_branch_is_not_exact_thread(self):
        spec = copy.deepcopy(self.planner_spec)
        spec["expected_identity"].update(conversation_id="conversation-1", branch_id="main-1")
        self.planner = control.bind(self.state, "planner", spec, spec["expected_identity"], "controller", "exact_thread", reason="exact main branch")
        event = self.event()
        class OtherBranch:
            def reviewer_identity(self, target):
                return dict(target["expected_identity"], branch_id="side-2")
            def send(self, *args): raise AssertionError("side branch must not be notified")
        self.assertEqual(control.deliver_event(self.state, event, OtherBranch())["status"], "IDENTITY_CHANGED")

    def test_cross_role_override_is_explicit(self):
        bad = copy.deepcopy(self.req); bad["reviewer"]["role"] = "paper"
        with self.assertRaises(ValueError): control.check_request_links(self.state, bad)
        bad["authorization"]["allow_reviewer_override"] = True
        self.assertEqual(control.check_request_links(self.state, bad)["reviewer_role"], "paper")

    def test_notification_attempt_has_target_and_authorization_hash(self):
        spec = self.planner_spec
        control.bind(self.state, "planner", spec, spec["expected_identity"], "controller", "dedicated_window", reason="exclusive main window")
        # New controller owns this task, not the retired binding.
        with control.database(self.state) as con: self.planner = control.active(con, "planner")
        event = self.event()
        class Fake:
            calls = 0
            def reviewer_identity(self, target): return target["expected_identity"]
            def send(self, target, message):
                self.calls += 1
                raise TimeoutError("uncertain send")
        fake = Fake()
        result = control.deliver_event(self.state, event, fake)
        self.assertEqual(result["status"], "delivery_uncertain")
        self.assertEqual(result["authorization_sha256"], control.digest(self.req["authorization"]))
        self.assertEqual(control.deliver_event(self.state, event, fake)["status"], "ALREADY_ATTEMPTED")
        self.assertEqual(fake.calls, 1)

    def test_main_loop_recovers_from_one_query_timeout(self):
        for name in ("RESULT.md", "HANDOFF.md"):
            (self.root / name).write_text("TASK-ONE nonce-one")
        req = copy.deepcopy(self.req); req.pop("workflow_state")
        reqfile = self.root / "request.json"; reqfile.write_text(json.dumps(req))
        identity = req["worker"]["expected_identity"]
        class Fake:
            def __init__(self, request): self.calls = 0
            def status(self, spec):
                self.calls += 1
                if self.calls == 1: raise TimeoutError("temporary")
                return {"identity": identity, "status": "done", "completion_seq": 11}
        with patch.object(sys, "argv", ["monitor", "--request", str(reqfile)]), patch.object(monitor, "Transport", Fake), patch.object(monitor.time, "sleep"):
            monitor.main()
        self.assertEqual(json.loads((self.root / "monitor_summary.json").read_text())["status"], "review_ready")
        self.assertEqual(json.loads((self.root / "frozen_request.json").read_text()), req)

    def test_error_budget_and_explicit_monitor_resumption(self):
        for name in ("RESULT.md", "HANDOFF.md"):
            (self.root / name).write_text("TASK-ONE nonce-one", encoding="utf-8")
        req = copy.deepcopy(self.req); req.pop("workflow_state")
        req["monitor"] = {"max_consecutive_errors": 2}
        reqfile = self.root / "request.json"
        reqfile.write_text(json.dumps(req), encoding="utf-8")
        class Failing:
            calls = 0
            def __init__(self, request): pass
            def status(self, spec):
                self.calls += 1
                raise TimeoutError("query unavailable")
        with patch.object(sys, "argv", ["monitor", "--request", str(reqfile)]), patch.object(monitor, "Transport", Failing), patch.object(monitor.time, "sleep"):
            monitor.main()
        self.assertEqual(json.loads((self.root / "monitor_summary.json").read_text())["status"], "adapter_error")
        identity = req["worker"]["expected_identity"]
        class Ready:
            def __init__(self, request): pass
            def status(self, spec): return {"identity": identity, "status": "done", "completion_seq": 11}
        with patch.object(sys, "argv", ["monitor", "--request", str(reqfile), "--resume"]), patch.object(monitor, "Transport", Ready):
            monitor.main()
        self.assertEqual(len(list(self.root.glob("monitor_summary.history.*.json"))), 1)
        self.assertEqual(json.loads((self.root / "monitor_summary.json").read_text())["status"], "review_ready")
        with patch.object(sys, "argv", ["monitor", "--request", str(reqfile), "--resume"]):
            with self.assertRaises(ValueError): monitor.main()


if __name__ == "__main__":
    unittest.main(verbosity=2)
