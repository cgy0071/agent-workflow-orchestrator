import concurrent.futures
import inspect
import json
import unittest
from unittest.mock import patch

import awo_kb_gate
import kb_action_review
import monitor_task
import test_kb_authority as fixture
import workflow_control as control


if fixture.HAS_KB:
    from awo_kb.service import Reader
    HAS_ACTIONS = "note_refs" in inspect.signature(Reader).parameters
else:
    HAS_ACTIONS = False


@unittest.skipUnless(HAS_ACTIONS, "requires B04 Reader action reports")
class ActionReviewTests(unittest.TestCase):
    setUp = fixture.GateTests.setUp
    tearDown = fixture.GateTests.tearDown
    spec = fixture.GateTests.spec
    actor = fixture.GateTests.actor
    configure = fixture.GateTests.configure
    save_policy = fixture.GateTests.save_policy

    def delivery(self, blocked=False, reviewer_missing_source=False):
        # Exercise the real Reader, ledger, registration and receipt path. The
        # Core artifact admission handshake is exercised in test_kb_authority.
        from awo_kb import store
        dep = patch("kb_host_bridge.deployment", return_value=(
            None, {"kb.check"}, fixture.FIXTURE_DIGEST))
        dep.start()
        self.addCleanup(dep.stop)
        self.configure()
        policy = self.cfg["policy"]
        reviewer = self.actor()[0]
        policy["projects"]["P"]["sources"] = ["Q"]
        policy["actors"][self.worker]["read"] = ["P", "Q"]
        policy["actors"][reviewer] = {"kind": "human", "read": ["P", "Q"], "write": ["P"]}
        term = {"id": "t", "type": "TERM", "project": "P", "canonical": "x",
                "definition": "original", "profiles": ["core"]}
        pin = store.append(self.cfg["vault"], term, store.GENESIS)
        if reviewer_missing_source:
            pin = store.append(self.cfg["vault"], {"id": "s", "type": "SRC", "project": "Q",
                "profiles": ["core"], "author": "A", "version": "1", "locator": "p.1", "quote": "x"}, pin)
            policy["actors"][reviewer]["read"] = ["P"]
        self.req["kb_gate"]["task"]["kb_revision"] = pin
        self.save_policy()
        control.register_task(self.state, self.req, *self.actor())
        self.file = self.root / "delivery.json"
        refs = ["s"] if reviewer_missing_source else (["absent"] if blocked else [])
        self.file.write_text(json.dumps({"refs": refs,
            "judgments": [], "terms": [{"term_ref": "t", "value": "x", "definition": "other"}],
            "coinages": [] if reviewer_missing_source else [
                {"project": p, "concept": "C", "name": p} for p in ("P", "Q")]}))
        (self.root / "HANDOFF.md").write_text("TASK-ONE nonce-one")
        receipt = {"task_id": self.req["task_id"], "nonce": self.req["nonce"],
                   "artifacts": [monitor_task.checked_artifact(self.root, "delivery.json")],
                   "binding_artifact": monitor_task.checked_artifact(self.root, "HANDOFF.md")}
        self.event = control.publish_event(self.state, self.req, {"status": "review_ready"}, receipt)

    def run_apply(self, **kwargs):
        return kb_action_review.apply(self.state, self.event, *self.actor(), **kwargs)

    def decision_count(self):
        with control.database(self.state) as con:
            return con.execute("SELECT count(*) FROM audit WHERE action='kb_action_decided'").fetchone()[0]

    def test_exact_replay_concurrency_and_rollback(self):
        self.delivery()
        actions = self.run_apply()
        self.assertEqual({v["action"]["type"] for v in actions}, {"gap", "conflict"})
        decision = dict(action_id=actions[0]["action"]["id"], outcome="dismiss", reason="Fixture assessment")
        original = control.audit
        def fail_after_write(con, *args):
            original(con, *args)
            raise RuntimeError("interrupted")
        with patch.object(control, "audit", fail_after_write), self.assertRaises(RuntimeError):
            self.run_apply(**decision)
        self.assertEqual(self.decision_count(), 0)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            a, b = [f.result() for f in [pool.submit(self.run_apply, **decision) for _ in range(2)]]
        self.assertEqual(a, b)
        self.assertEqual(a["proposal"], actions[0]["action"])
        with self.assertRaisesRegex(ValueError, "already resolved"):
            self.run_apply(**dict(decision, outcome="uphold"))
        self.assertEqual(self.decision_count(), 1)

    def test_current_authority_and_worker_identity(self):
        self.delivery()
        key = self.run_apply()[0]["action"]["id"]
        args = dict(action_id=key, outcome="uphold", reason="Fixture")
        reviewer = self.cfg["policy"]["actors"][self.actor()[0]]
        for prop, denied, allowed in (("kind", "agent", "human"), ("write", [], ["P"]),
                                     ("read", [], ["P", "Q"])):
            reviewer[prop] = denied
            self.save_policy()
            with self.assertRaises(PermissionError):
                self.run_apply(**args)
            reviewer[prop] = allowed
        self.save_policy()
        with self.assertRaises(ValueError):
            kb_action_review.apply(self.state, self.event, self.worker, 1, "main", **args)
        with self.assertRaises(ValueError):
            kb_action_review.apply(self.state, self.event, self.actor()[0], 9999, "main", **args)
        self.assertEqual(self.decision_count(), 0)

    def test_changed_exact_bytes_between_checks(self):
        self.delivery()
        check = awo_kb_gate.check
        def tamper(*args):
            result = check(*args)
            self.file.write_bytes(self.file.read_bytes() + b" ")
            return result
        with patch.object(awo_kb_gate, "check", tamper), self.assertRaisesRegex(ValueError, "changed"):
            self.run_apply()
        self.assertEqual(self.decision_count(), 0)

    def test_blocking_delivery_has_no_adjudication(self):
        self.delivery(blocked=True)
        with self.assertRaises(ValueError):
            self.run_apply()
        self.assertEqual(self.decision_count(), 0)

    def test_reviewer_cannot_resolve_one_action_of_partly_invisible_delivery(self):
        self.delivery(reviewer_missing_source=True)
        with self.assertRaisesRegex(PermissionError, "complete delivery"):
            self.run_apply()
        self.assertEqual(self.decision_count(), 0)
