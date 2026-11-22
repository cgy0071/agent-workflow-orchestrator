import json
import unittest
import subprocess
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

import awo_kb_gate
import monitor_task
import test_priority_scheduler as native_fixture
import priority_scheduler as native
import test_workflow
import workflow_control as control
import workflow_scheduler as scheduler
from importlib import metadata
import importlib.util

HAS_KB = importlib.util.find_spec("awo_kb") is not None
if HAS_KB:
    from awo_kb import store


# An independently supplied test pin lets the same functional suite cover both wheels.
FIXTURE_DIGEST = ({
    "0.4.1": "sha256:3edb4eba44c4b2a82c56e888812192d442094892c25667d0dcad42caa0e0e9a5",
    "0.4.0": "sha256:818b448bcb6c2884eb8c62090a6337d608226088ecf3f1ca654b4b4bbf367f34",
    "0.2.0": "sha256:de1591b7142e47aa8b18b204f4f27b44afd7c8e6bbe302fb2f25ea33138ca786",
    "0.3.0": "sha256:434567906f19d534eaff71163076de0b798d65e9d7396157a2423df9d6926539",
}.get(metadata.version("awo-kb")) if HAS_KB else None)


class GateTests(unittest.TestCase):
    setUp = test_workflow.WorkflowTests.setUp
    tearDown = test_workflow.WorkflowTests.tearDown
    spec = test_workflow.WorkflowTests.spec
    actor = test_workflow.WorkflowTests.actor

    def configure(self, slots=None):
        if not HAS_KB:
            self.skipTest("awo-kb wheel not installed")
        with control.database(self.state) as con:
            self.worker = control.active(con, "executor")["instance_id"]
        self.cfg = {"vault": str(self.root / "vault"),
                    "policy": {"actors": {self.worker: {"read": ["P"]}},
                               "projects": {"P": {"profiles": [], "sources": []}}}}
        self.policy = self.state / "kb-host.json"
        self.req["completion"]["artifacts"] = ["delivery.json"]
        self.req["kb_gate"] = {"task": {"kb_revision": store.GENESIS, "project": "P", "profiles": []},
                               "claim_slots": slots or [], "artifact": "delivery.json",
                               "producer": {"runtime": "offline", "model": "fixture", "prompt_rev": "1"}}

        self.save_policy()

    def save_policy(self):
        from awo_kb.handoff import fingerprint
        rule = {"nonce":self.req["nonce"], "worker_instance":self.worker,
                "gate_sha256":fingerprint(self.req["kb_gate"])}
        cfg = {"enabled":True, "artifact_digest":FIXTURE_DIGEST,
               "read_operations":["kb.check"], "projects":{"P":dict(self.cfg,
               tasks={self.req["task_id"]:{"acceptance":rule}})}}
        self.policy.write_text(json.dumps(cfg), encoding="utf-8")

    def event(self, receipt=True, judgments=None):
        control.register_task(self.state, self.req, *self.actor())
        self.evidence = self.root / "delivery.json"
        self.evidence.write_text(json.dumps({"refs": [], "terms": [], "coinages": [], "judgments": judgments or []}), encoding="utf-8")
        (self.root / "HANDOFF.md").write_text("TASK-ONE nonce-one", encoding="utf-8")
        receipt_data = {"task_id": self.req["task_id"], "nonce": self.req["nonce"],
                        "artifacts": [monitor_task.checked_artifact(self.root, "delivery.json")],
                        "binding_artifact": monitor_task.checked_artifact(self.root, "HANDOFF.md")}
        self.ev = control.publish_event(self.state, self.req, {"status": "review_ready"},
                                        receipt_data if receipt else None)
        self.review = self.root / "REVIEW.md"
        self.review.write_text("TASK-ONE nonce-one\nVerdict: accept\n", encoding="utf-8")

    def sign(self):
        checked = awo_kb_gate.preview(self.state, self.ev, *self.actor())
        with self.review.open("a", encoding="utf-8") as out:
            out.write("KB-Check-SHA256: " + checked["sha256"] + "\n")
        return checked

    def settle(self):
        return control.settle(self.state, self.ev, *self.actor(), self.review, "accept")

    def test_admission_denies_missing_project_grant(self):
        self.configure()
        self.req["kb_gate"]["task"]["project"] = "elsewhere"
        with self.assertRaises(PermissionError):
            control.register_task(self.state, self.req, *self.actor())

    def test_missing_check_then_approved_review_and_revoked_followup(self):
        self.configure()
        self.event()
        self.assertEqual(self.settle()["status"], "SETTLE_INCOMPLETE")
        self.sign()
        self.assertEqual(self.settle()["status"], "SETTLED")
        with control.database(self.state) as con:
            self.assertTrue(scheduler.acceptance(con, "TASK-ONE"))
        self.cfg["policy"]["actors"][self.worker]["read"] = []
        self.save_policy()
        with self.assertRaises(PermissionError):
            control.next_gate(self.state, self.ev, *self.actor())
        with control.database(self.state) as con:
            self.assertFalse(scheduler.acceptance(con, "TASK-ONE"))
        self.assertEqual(self.settle()["status"], "SETTLE_INCOMPLETE")

    def test_substitution_blocks_acceptance(self):
        self.configure()
        self.event()
        self.sign()
        self.evidence.write_bytes(self.evidence.read_bytes() + b" ")
        self.assertEqual(self.settle()["status"], "SETTLE_INCOMPLETE")

    def test_omitted_slot_and_missing_receipt(self):
        self.configure(["first claim"])
        self.event()
        with self.assertRaisesRegex(ValueError, "blocked"):
            self.sign()
        self.assertEqual(self.settle()["status"], "SETTLE_INCOMPLETE")

    def test_missing_receipt(self):
        self.configure()
        self.event(receipt=False)
        with self.assertRaisesRegex(ValueError, "receipt"):
            self.sign()

    def test_failed_handoff_suppresses_notification_then_corrected_evidence_can_be_reviewed(self):
        self.configure()
        self.event(receipt=False)
        blocked = control.deliver_event(self.state, self.ev, None)
        self.assertEqual(blocked["status"], "KB_REVISION_REQUIRED")
        with control.database(self.state) as con:
            row = json.loads(con.execute("SELECT data FROM events WHERE id=?", (self.ev,)).fetchone()[0])
            self.assertEqual(row["report"]["status"], "kb_revision_required")
            self.assertIn("receipt", blocked["detail"])
            self.assertFalse(con.execute("SELECT * FROM notifications").fetchall())
            self.assertFalse(con.execute("SELECT * FROM reviews").fetchall())
        receipt = {"task_id": self.req["task_id"], "nonce": self.req["nonce"],
                   "artifacts":[monitor_task.checked_artifact(self.root, "delivery.json")],
                   "binding_artifact":monitor_task.checked_artifact(self.root, "HANDOFF.md")}
        self.ev = control.publish_event(self.state, self.req, {"status":"review_ready"}, receipt)
        self.sign()
        self.assertEqual(self.settle()["status"], "SETTLED")

    def test_monitor_reports_revision_and_resumes_after_corrected_bytes(self):
        self.configure()
        control.register_task(self.state, self.req, *self.actor())
        request_file = self.root / "request.json"
        request_file.write_text(json.dumps(self.req), encoding="utf-8")
        delivery = self.root / "delivery.json"
        delivery.write_text("{}", encoding="utf-8")
        (self.root / "HANDOFF.md").write_text("TASK-ONE nonce-one", encoding="utf-8")
        with patch.object(monitor_task.Transport, "status", return_value={"status":"idle",
                          "identity":self.req["worker"]["expected_identity"], "completion_seq":11}), \
             patch.object(monitor_task.Transport, "send") as send:
            with patch.object(sys, "argv", ["monitor", "--request", str(request_file), "--notify"]):
                monitor_task.main()
            self.assertEqual(json.loads((self.root / "monitor_summary.json").read_bytes())["status"],
                             "kb_revision_required")
            send.assert_not_called()
            delivery.write_text(json.dumps({"refs":[], "terms":[], "coinages":[], "judgments":[]}), encoding="utf-8")
            with patch.object(sys, "argv", ["monitor", "--request", str(request_file), "--resume"]):
                monitor_task.main()
            self.assertEqual(json.loads((self.root / "monitor_summary.json").read_bytes())["status"], "review_ready")
        with control.database(self.state) as con:
            self.assertFalse(con.execute("SELECT * FROM reviews").fetchall())

    def test_revoked_after_publication_suppresses_even_authorized_notification(self):
        self.configure()
        self.event()
        self.cfg["policy"]["actors"][self.worker]["read"] = []
        self.save_policy()
        self.assertEqual(control.deliver_event(self.state, self.ev, None)["status"], "KB_REVISION_REQUIRED")
        with control.database(self.state) as con:
            self.assertFalse(con.execute("SELECT * FROM notifications").fetchall())

    def claim(self, wording, review_by="2099-01-01"):
        expected = "The observed gain depends on the data split."
        self.configure([expected])
        vault = self.cfg["vault"]
        common = {"project": "P", "profiles": ["core"]}
        pin = store.append(vault, dict(common, type="SRC", id="S1", author="Ada", version="1",
                                       quote="Held-out scores varied across splits.", locator="Table 1"), store.GENESIS)
        pin = store.append(vault, dict(common, type="JUD", id="J1", holder="Ada", stance=expected,
                                       source_ref="S1", subject="validation", as_of="2026-11-20",
                                       review_by=review_by), pin)
        self.req["kb_gate"]["task"]["kb_revision"] = pin
        self.save_policy()
        self.event(judgments=[{"claim": wording or expected, "judgment_ref": "J1"}])

    def test_research_claim_review_via_cli(self):
        self.claim(None)
        actor, version, context = self.actor()
        output = subprocess.check_output([sys.executable, "-B", str(Path(awo_kb_gate.__file__)),
                                          "--state", str(self.state), "--event", self.ev,
                                          "--actor", actor, "--version", str(version), "--context", context])
        attestation = json.loads(output)
        with self.review.open("a", encoding="utf-8") as out:
            out.write("KB-Check-SHA256: " + attestation["sha256"] + "\n")
        self.assertEqual(self.settle()["status"], "SETTLED")

    def test_same_count_substituted_claim(self):
        self.claim("The observed gain is independent of the data split.")
        with self.assertRaisesRegex(ValueError, "blocked"):
            self.sign()

    def test_due_date_rollover_preserves_signed_review_and_rechecks_grants(self):
        self.claim(None, review_by="2026-11-21")
        with patch("awo_kb.service.date") as clock:
            clock.today.return_value = date(2026, 11, 21)
            signed = self.sign()
            self.assertEqual(self.settle()["status"], "SETTLED")
            clock.today.return_value = date(2026, 11, 22)
            current = awo_kb_gate.preview(self.state, self.ev, *self.actor())
            self.assertNotEqual(signed["sha256"], current["sha256"])
            report = current["result"]["check"]["check"]
            self.assertFalse(report["blocked"])
            self.assertIn("stale", [f["category"] for f in report["findings"]])
            with control.database(self.state) as con:
                self.assertEqual(scheduler.acceptance(con, "TASK-ONE")["kb_check"], signed)
            gate = control.next_gate(self.state, self.ev, *self.actor())
            self.assertEqual(gate["parent_review"]["kb_check"], signed)
            self.cfg["policy"]["actors"][self.worker]["read"] = []
            self.save_policy()
            with control.database(self.state) as con:
                self.assertIsNone(scheduler.acceptance(con, "TASK-ONE"))
            with self.assertRaises(PermissionError):
                control.next_gate(self.state, self.ev, *self.actor())

    def test_legacy_no_gate_settles_without_kb_policy(self):
        self.req["completion"]["artifacts"] = ["delivery.json"]
        self.event()
        self.assertEqual(self.settle()["status"], "SETTLED")

    def test_omitted_required_gate_and_outside_path(self):
        self.configure()
        gate = self.req.pop("kb_gate")
        with self.assertRaisesRegex(PermissionError, "requires"):
            control.register_task(self.state, self.req, *self.actor())
        self.req["kb_gate"] = gate
        gate["artifact"] = "../elsewhere"
        self.req["completion"]["artifacts"] = [gate["artifact"]]
        self.save_policy()
        with self.assertRaises(ValueError):
            control.register_task(self.state, self.req, *self.actor())

    def test_duplicate_or_different_signature_blocks_review(self):
        self.configure()
        self.event()
        original = self.review.read_bytes()
        control.acknowledge(self.state, self.ev, *self.actor())
        for lines in (["0" * 64], [self.sign()["sha256"]] * 2):
            self.review.write_bytes(original + "".join("KB-Check-SHA256: " + s + "\n" for s in lines).encode())
            with self.assertRaisesRegex(ValueError, "approve this KB check"):
                self.settle()

    def test_disabled_and_wrong_deployment_block_opt_in(self):
        self.configure()
        cfg = json.loads(self.policy.read_text())
        for changes in ({"enabled": False}, {"artifact_digest": "sha256:" + "0" * 64}, {"read_operations": []}):
            self.policy.write_text(json.dumps(dict(cfg, **changes)), encoding="utf-8")
            with self.assertRaises(PermissionError):
                control.register_task(self.state, self.req, *self.actor())

    def test_dag_consumption_rechecks_on_revocation(self):
        self.configure()
        self.event()
        self.sign()
        self.assertEqual(self.settle()["status"], "SETTLED")
        child = dict(self.req, task_id="CHILD")
        graph = {"nodes": [{"id": "a", "request": self.req}, {"id": "b", "request": child, "needs": ["a"]}]}
        with control.database(self.state) as con:
            con.execute("INSERT INTO dag_runs VALUES (?,?)", ("run", json.dumps({"graph":graph})))
            self.assertEqual(scheduler.assert_ready(con, "run", "b", child)["id"], "b")
            self.cfg["policy"]["actors"][self.worker]["read"] = []
            self.save_policy()
            with self.assertRaisesRegex(ValueError, "no valid accepted evidence"):
                scheduler.assert_ready(con, "run", "b", child)


class NativeGateTests(unittest.TestCase):
    @unittest.skipUnless(HAS_KB, "awo-kb wheel not installed")
    def test_service_review_close_and_dependency_use_same_approval(self):
        from awo_kb.handoff import fingerprint
        f = native_fixture.PrioritySchedulerTests()
        self.addCleanup(f.doCleanups)
        f.setUp()
        request = f.request()
        project = f.project
        worker = f.bindings["executor"]["instance_id"]
        gate = {"task":{"kb_revision":store.GENESIS, "project":project, "profiles":[]},
                "claim_slots":[], "artifact":"delivery.json",
                "producer":{"runtime":"local", "model":"fixture", "prompt_rev":"1"}}
        request["kb_gate"] = gate
        request["completion"]["artifacts"].append("delivery.json")
        Path(request["cwd"], "delivery.json").write_text(json.dumps(
            {"refs":[], "terms":[], "coinages":[], "judgments":[]}), encoding="utf-8")
        policy = {"actors":{worker:{"read":[project]}}, "projects":{project:{"profiles":[], "sources":[]}}}
        rule = {"nonce":request["nonce"], "worker_instance":worker, "gate_sha256":fingerprint(gate)}
        cfg = {"enabled":True, "artifact_digest":FIXTURE_DIGEST,
               "read_operations":["kb.check"], "projects":{project:{"vault":str(f.root/"vault"), "policy":policy,
               "tasks":{request["task_id"]:{"acceptance":rule}}}}}
        path = f.state / "kb-host.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        f.enqueue(request=request)
        aid = f.claim(task_id="T-A", expected_revision=2)["assignment_id"]
        self.assertEqual(f.dispatch(aid)["status"], "SUBMITTED")
        delivery = Path(request["cwd"], "delivery.json")
        valid = delivery.read_bytes()
        delivery.write_bytes(b"{}")
        blocked = f.reconcile(aid)
        self.assertEqual(blocked["status"], "KB_REVISION_REQUIRED")
        self.assertTrue(blocked["claims_retained"])
        self.assertIsNone(blocked["event_id"])
        self.assertNotEqual(f.get_assignment(aid)["review_status"], "awaiting_review")
        self.assertFalse(f.rows("SELECT * FROM events"))
        self.assertFalse(f.rows("SELECT * FROM reviews"))
        delivery.write_bytes(valid)
        completion = f.reconcile(aid)
        self.assertEqual(completion["status"], "REVIEW_READY")
        with self.assertRaisesRegex(ValueError, "approve this KB check"):
            f.review(completion, request, same_connection=True)
        self.assertFalse(f.rows("SELECT * FROM reviews"))
        eid = completion["event_id"]
        signed = awo_kb_gate.preview(f.state, eid, *f.actor)
        note = Path(request["cwd"], "REVIEW.md")
        with note.open("a", encoding="utf-8") as out:
            out.write("KB-Check-SHA256: " + signed["sha256"] + "\n")
        with f.db() as con:
            result = native.review_in_transaction(con, project, *f.actor, eid, note, "accept")
        self.assertEqual(result["kb_check"], signed)
        policy["actors"][worker]["read"] = []
        path.write_text(json.dumps(cfg), encoding="utf-8")
        with self.assertRaises(PermissionError):
            f.close(aid, completion)
        policy["actors"][worker]["read"] = [project]
        path.write_text(json.dumps(cfg), encoding="utf-8")
        f.close(aid, completion)
        f.enqueue("T-B", "second", dependencies=["T-A"])
        policy["actors"][worker]["read"] = []
        path.write_text(json.dumps(cfg), encoding="utf-8")
        self.assertEqual(f.claim(task_id="T-B", expected_revision=2)["status"], "WAITING")
        policy["actors"][worker]["read"] = [project]
        path.write_text(json.dumps(cfg), encoding="utf-8")
        self.assertEqual(f.claim(task_id="T-B", expected_revision=2)["status"], "RESERVED")
