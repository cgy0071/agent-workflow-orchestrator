"""Regression tests for the opt-in AWO V2 service boundary and migration."""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import unittest
import uuid
from unittest import mock
from unittest.mock import patch

import awo_service as service
import workflow_control as control
import workflow_scheduler as scheduler


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = (Path(__file__).resolve().parents[1] / "runtime").resolve()
        self.sandbox.mkdir(exist_ok=True)
        self.root = self.sandbox / ("test-awo-service-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.state = self.root / "state"
        self.config = {
            "roles": {"planner": {}, "executor": {}},
            "workflow": {"dispatch_role": "planner", "execution_role": "executor",
                         "reviewer_role": "planner",
                         "transitions": {"accept": "next", "repair": "repair", "reject": "stop"}},
        }
        control.initialize(self.state, self.config)
        self.planner_spec = self.spec("planner", reviewer=True)
        self.worker_spec = self.spec("executor")
        self.planner = control.bind(self.state, "planner", self.planner_spec,
                                    self.planner_spec["expected_identity"], "controller")
        control.bind(self.state, "executor", self.worker_spec, self.worker_spec["expected_identity"])
        self.actor = (self.planner["instance_id"], self.planner["version"], "main")

    def tearDown(self):
        target = self.root.resolve()
        if target.parent != self.sandbox or not target.name.startswith("test-awo-service-"):
            raise RuntimeError("refuse unsafe cleanup")
        shutil.rmtree(target)

    def spec(self, role, reviewer=False):
        identity = {"kind": "any-harness", "terminal": "terminal-" + role,
                    "launch_cwd": str(self.root)}
        value = {"role": role, "harness": "any-harness", "adapter": "command",
                 "expected_identity": identity}
        status = {"identity": identity, "status": "idle", "completion_seq": 10}
        value["identity_argv" if reviewer else "status_argv"] = [
            sys.executable, "-c", "print(" + repr(json.dumps(status)) + ")"]
        value["prompt_argv"] = [sys.executable, "-c", "print('offline')", "{message}"]
        value["output_argv"] = [sys.executable, "-c", "print('offline')"]
        return value

    @contextmanager
    def db(self):
        con = sqlite3.connect(self.state / "workflow.sqlite3")
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def request(self, task_id, nonce, artifacts, generation=1):
        return {
            "task_id": task_id, "nonce": nonce, "cwd": str(self.root),
            "dispatch_confirmed": True, "generation": generation,
            "worker": dict(self.worker_spec, baseline_completion_seq=10),
            "reviewer": self.planner_spec, "workflow_state": str(self.state),
            "authorization": {"source": "offline service tests", "notify_reviewer": False},
            "completion": {"binding_artifact": artifacts[0], "artifacts": artifacts[1:]},
        }

    def write_files(self, task_id, nonce):
        handoff = self.root / (task_id + "-HANDOFF.md")
        result = self.root / (task_id + "-RESULT.md")
        handoff.write_text("# " + task_id + " " + nonce + "\nDone.\n", encoding="utf-8")
        result.write_text("# " + task_id + " " + nonce + "\nResult.\n", encoding="utf-8")
        return handoff, result

    def receipt(self, task_id, nonce, handoff, result, missing_sha=False):
        def item(path):
            row = {"path": str(path), "bytes": path.stat().st_size}
            if not missing_sha:
                row["sha256"] = service._file_sha(path)
            return row
        return {"task_id": task_id, "nonce": nonce, "completion_seq": 11,
                "sequence_source": "completion_seq",
                "artifacts": [item(result)], "binding_artifact": item(handoff)}

    def insert_task(self, task_id="T-ONE", status="resolved", dispatch_status="resolved",
                    review_verdict="accept", review_ready=True, reviewed=True,
                    missing_artifact=False, change_artifact=False, missing_sha=False,
                    change_review=False, missing_review=False, terminate=False,
                    request_hash_match=True, receipt_artifacts="list", generation=1,
                    tamper_source_after_hash=False):
        nonce = "nonce-" + task_id
        handoff, result = self.write_files(task_id, nonce)
        req = self.request(task_id, nonce, [str(handoff), str(result)], generation=generation)
        request_hash = control.digest(req)
        if tamper_source_after_hash:
            req["authorization"]["source"] = "changed after the frozen digest"
        rec = self.receipt(task_id, nonce, handoff, result, missing_sha=missing_sha)
        if receipt_artifacts != "list":
            rec["artifacts"] = receipt_artifacts
            rec["binding_artifact"] = receipt_artifacts
        if missing_artifact:
            result.unlink()
        if change_artifact:
            result.write_text("changed after the receipt\n", encoding="utf-8")
        event_id = "event-" + task_id
        report_status = "review_ready" if review_ready else "failed"
        event_data = {"event_id": event_id, "request_sha256": request_hash,
                      "report": {"status": report_status}, "receipt": rec,
                      "created_at": "2026-11-10T00:00:00+00:00"}
        with self.db() as con:
            con.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                        (task_id, nonce, request_hash, "executor", status,
                         json.dumps({"request": req, "request_sha256": request_hash,
                                     "authorization_sha256": control.digest(req["authorization"]),
                                     "registered_at": "2026-11-10T00:00:00+00:00"})))
            if dispatch_status:
                value = {"generation": 1, "message": "offline", "message_sha256": "x",
                         "resources": {}, "workspaces": [str(self.root)]}
                con.execute("INSERT INTO dispatches VALUES (?,?,?)",
                            (task_id, dispatch_status, json.dumps(value)))
            con.execute("INSERT INTO events VALUES (?,?,?,?,?,?)",
                        (event_id, task_id, nonce, "planner",
                         "reviewed" if reviewed else "pending", json.dumps(event_data)))
            if review_verdict:
                review_path = self.root / (task_id + "-REVIEW.md")
                review_path.write_text("# " + task_id + " " + nonce + "\nVerdict: " +
                                       review_verdict + "\n", encoding="utf-8")
                review_sha = service._file_sha(review_path)
                if change_review:
                    review_path.write_text("# " + task_id + " " + nonce +
                                           "\nVerdict: " + review_verdict + "\nchanged\n",
                                           encoding="utf-8")
                if missing_review:
                    review_path.unlink()
                review = {"event_id": event_id, "task_id": task_id, "nonce": nonce,
                          "path": str(review_path), "sha256": review_sha,
                          "verdict": review_verdict, "actor": {"role": "planner"},
                          "reviewed_at": "2026-11-10T00:00:00+00:00"}
                con.execute("INSERT INTO reviews VALUES (?,?)", (event_id, json.dumps(review)))
            if terminate:
                con.execute("INSERT INTO audit(at,action,data) VALUES (?,?,?)",
                            ("2026-11-10T00:00:00+00:00", "task_termination_inspected",
                             json.dumps({"task_id": task_id, "nonce": nonce})))
        return task_id

    def rows(self, sql, params=()):
        with self.db() as con:
            con.row_factory = sqlite3.Row
            return [dict(row) for row in con.execute(sql, params)]

    def apply(self):
        return service.apply_migration(self.state, *self.actor)

    def test_dry_run_is_read_only_and_does_not_create_schema(self):
        self.insert_task()
        before = sorted(p.name for p in self.state.iterdir())
        report = service.migration_report(self.state)
        after = sorted(p.name for p in self.state.iterdir())
        self.assertEqual(before, after)
        self.assertFalse(report["schema_present"])
        self.assertEqual(report["counts"]["legacy_tasks"], 1)
        with self.db() as con:
            self.assertFalse(con.execute(
                "SELECT 1 FROM sqlite_master WHERE name='v2_task'").fetchone())
        status = service.service_status(self.state)
        self.assertEqual(status["v2"], "absent")
        self.assertEqual(status["legacy"]["tasks"], 1)

    def test_apply_is_repeatable(self):
        self.insert_task()
        first = self.apply()
        self.assertEqual(first["status"], "APPLIED")
        counts = first["counts"]
        second = self.apply()
        self.assertEqual(second["counts"], counts)
        with self.db() as con:
            self.assertEqual(con.execute("SELECT count(*) FROM v2_task").fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT count(*) FROM v2_assignment").fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT count(*) FROM v2_legacy_map").fetchone()[0], 1)

    def test_apply_rolls_back_entirely(self):
        self.insert_task()
        original = service._insert_plan

        def broken(con, state, plans):
            original(con, state, plans)
            raise RuntimeError("injected failure after partial writes")

        with patch.object(service, "_insert_plan", side_effect=broken):
            with self.assertRaises(RuntimeError):
                self.apply()
        with self.db() as con:
            names = {row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn("v2_task", names)
            self.assertNotIn("v2_migration_run", names)
            self.assertEqual(con.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)

    def test_registered_only_is_preserved_with_historical_assignment(self):
        self.insert_task(task_id="T-REG", status="running", dispatch_status=None,
                         review_verdict=None, reviewed=False, generation=3)
        self.apply()
        task = self.rows("SELECT * FROM v2_task WHERE task_id='T-REG'")[0]
        ass = self.rows("SELECT * FROM v2_assignment WHERE task_id='T-REG'")[0]
        self.assertEqual(task["task_state"], "active")
        self.assertEqual(task["legacy_status"], "running")
        self.assertEqual(ass["state"], "historical_registered")
        self.assertEqual(ass["generation"], 3)
        self.assertEqual(ass["holds_slot"], 1)
        mapping = self.rows("SELECT * FROM v2_legacy_map WHERE legacy_task_id='T-REG'")[0]
        self.assertEqual(mapping["v2_assignment_id"], ass["assignment_id"])

    def test_resolved_without_accept_is_never_accepted(self):
        self.insert_task(review_verdict=None, reviewed=False)
        self.apply()
        task = self.rows("SELECT * FROM v2_task WHERE task_id='T-ONE'")[0]
        self.assertEqual(task["task_state"], "migration_uncertain")
        self.assertIsNone(task["accepted_evidence_id"])

    def test_accepted_requires_sealed_review_and_evidence(self):
        self.insert_task(terminate=True)
        self.apply()
        task = self.rows("SELECT * FROM v2_task WHERE task_id='T-ONE'")[0]
        ass = self.rows("SELECT * FROM v2_assignment WHERE task_id='T-ONE'")[0]
        evidence = self.rows("SELECT * FROM v2_evidence WHERE task_id='T-ONE'")
        self.assertEqual(task["task_state"], "accepted")
        self.assertIsNotNone(task["accepted_evidence_id"])
        self.assertEqual(ass["holds_slot"], 0)
        self.assertTrue(evidence)
        self.assertTrue(all(row["state"] == "sealed" and row["sealed_sha256"] == row["current_sha256"]
                            for row in evidence))

    def test_missing_sha_is_unknown_not_sealed(self):
        self.insert_task(missing_sha=True)
        self.apply()
        task = self.rows("SELECT * FROM v2_task WHERE task_id='T-ONE'")[0]
        evidence = self.rows("SELECT * FROM v2_evidence WHERE task_id='T-ONE'")
        self.assertEqual(task["task_state"], "migration_uncertain")
        self.assertNotIn(task["task_state"], ("review_ready", "accepted"))
        self.assertTrue(evidence)
        self.assertTrue(all(row["state"] == "unknown" and row["sealed_sha256"] is None
                            for row in evidence))

    def test_changed_artifact_keeps_sealed_hash_and_is_not_upgraded(self):
        self.insert_task(change_artifact=True)
        self.apply()
        task = self.rows("SELECT * FROM v2_task WHERE task_id='T-ONE'")[0]
        evidence = self.rows("SELECT * FROM v2_evidence WHERE task_id='T-ONE'")
        self.assertEqual(task["task_state"], "migration_uncertain")
        self.assertTrue(any(row["state"] == "changed" and row["sealed_sha256"] and
                            row["current_sha256"] != row["sealed_sha256"] for row in evidence))

    def test_changed_or_missing_review_is_not_upgraded(self):
        self.insert_task(task_id="T-CHANGED-REVIEW", change_review=True)
        self.insert_task(task_id="T-MISSING-REVIEW", missing_review=True)
        self.apply()
        for task_id in ("T-CHANGED-REVIEW", "T-MISSING-REVIEW"):
            task = self.rows("SELECT * FROM v2_task WHERE task_id=?", (task_id,))[0]
            self.assertEqual(task["task_state"], "migration_uncertain")
            self.assertIsNone(task["accepted_evidence_id"])

    def test_accept_on_non_review_ready_event_does_not_authorize(self):
        self.insert_task(review_ready=False, reviewed=True, review_verdict="accept")
        self.apply()
        task = self.rows("SELECT * FROM v2_task WHERE task_id='T-ONE'")[0]
        self.assertNotEqual(task["task_state"], "accepted")
        self.assertIsNone(task["accepted_evidence_id"])

    def test_uncertain_slot_is_retained_and_blocks_unfreeze(self):
        self.insert_task(task_id="T-UNCERTAIN", status="running", dispatch_status="uncertain",
                         review_verdict=None, reviewed=False)
        self.apply()
        ass = self.rows("SELECT * FROM v2_assignment WHERE task_id='T-UNCERTAIN'")[0]
        self.assertEqual(ass["state"], "uncertain")
        self.assertEqual(ass["holds_slot"], 1)
        service.set_freeze(self.state, *self.actor, True, "test")
        with self.assertRaises(service.ServiceError):
            service.set_freeze(self.state, *self.actor, False, "")

    def test_accepted_but_unclosed_keeps_slot(self):
        self.insert_task(status="finished", dispatch_status="submitted", terminate=False)
        self.apply()
        task = self.rows("SELECT * FROM v2_task WHERE task_id='T-ONE'")[0]
        ass = self.rows("SELECT * FROM v2_assignment WHERE task_id='T-ONE'")[0]
        self.assertEqual(task["task_state"], "accepted")
        self.assertEqual(ass["holds_slot"], 1)

    def test_changed_review_on_finished_accept_is_never_review_ready(self):
        self.insert_task(task_id="T-FINISHED-CHANGED", status="finished",
                         dispatch_status="submitted", change_review=True)
        self.apply()
        task = self.rows("SELECT * FROM v2_task WHERE task_id='T-FINISHED-CHANGED'")[0]
        review = self.rows("SELECT * FROM v2_review WHERE task_id='T-FINISHED-CHANGED'")[0]
        self.assertEqual(task["task_state"], "migration_uncertain")
        self.assertNotEqual(task["task_state"], "review_ready")
        self.assertIsNone(task["accepted_evidence_id"])
        self.assertEqual(review["state"], "changed")

    def test_tampered_frozen_request_is_never_accepted(self):
        self.insert_task(task_id="T-TAMPERED", tamper_source_after_hash=True, terminate=True)
        self.apply()
        task = self.rows("SELECT * FROM v2_task WHERE task_id='T-TAMPERED'")[0]
        self.assertEqual(task["task_state"], "migration_uncertain")
        self.assertIsNone(task["accepted_evidence_id"])

    def test_reapply_refreshes_changed_artifact_projection(self):
        self.insert_task(task_id="T-REAPPLY", terminate=True)
        self.apply()
        self.assertEqual(self.rows("SELECT task_state FROM v2_task WHERE task_id='T-REAPPLY'")[0]["task_state"],
                         "accepted")
        (self.root / "T-REAPPLY-RESULT.md").write_text("changed after first apply\n", encoding="utf-8")
        self.apply()
        task = self.rows("SELECT * FROM v2_task WHERE task_id='T-REAPPLY'")[0]
        ass = self.rows("SELECT * FROM v2_assignment WHERE task_id='T-REAPPLY'")[0]
        evidence = self.rows("SELECT * FROM v2_evidence WHERE task_id='T-REAPPLY'")
        self.assertEqual(task["task_state"], "migration_uncertain")
        self.assertIsNone(task["accepted_evidence_id"])
        self.assertEqual(ass["holds_slot"], 1)
        self.assertTrue(any(row["state"] == "changed" for row in evidence))

    def test_enable_refuses_new_unmigrated_closed_task(self):
        self.insert_task(task_id="T-FIRST", terminate=True)
        self.apply()
        self.insert_task(task_id="T-LATE", terminate=True)
        with self.assertRaises(service.ServiceError):
            service.set_enabled(self.state, *self.actor, True)
        self.apply()
        service.set_enabled(self.state, *self.actor, True)
        status = service.service_status(self.state)
        self.assertTrue(status["v2_enabled"])

    def test_closed_accepted_with_running_dispatch_is_completed_assignment(self):
        self.insert_task(task_id="T-RUNNING-CLOSED", status="resolved",
                         dispatch_status="running", terminate=True)
        self.apply()
        ass = self.rows("SELECT * FROM v2_assignment WHERE task_id='T-RUNNING-CLOSED'")[0]
        self.assertEqual(ass["state"], "completed")
        self.assertEqual(ass["holds_slot"], 0)
        self.assertEqual(ass["legacy_dispatch_status"], "running")

    def test_inspected_termination_releases_without_acceptance(self):
        self.insert_task(task_id="T-TERMINATED", status="running",
                         dispatch_status="uncertain", review_verdict=None, reviewed=False)
        note = self.root / "T-TERMINATED-TERMINATION.md"
        note.write_text("# T-TERMINATED nonce-T-TERMINATED\nOutcome: terminated\n",
                        encoding="utf-8")
        control.resolve_task(self.state, "T-TERMINATED", *self.actor, note)
        self.apply()
        task = self.rows("SELECT * FROM v2_task WHERE task_id='T-TERMINATED'")[0]
        ass = self.rows("SELECT * FROM v2_assignment WHERE task_id='T-TERMINATED'")[0]
        self.assertEqual(task["task_state"], "migration_uncertain")
        self.assertIsNone(task["accepted_evidence_id"])
        self.assertEqual(ass["holds_slot"], 0)
        self.assertEqual(ass["legacy_dispatch_status"], "uncertain")

    def test_closed_accepted_history_does_not_block_disable(self):
        self.insert_task(terminate=True)
        self.apply()
        service.set_enabled(self.state, *self.actor, True)
        service.set_enabled(self.state, *self.actor, False)
        status = service.service_status(self.state)
        self.assertFalse(status["v2_enabled"])

    def test_malformed_receipt_is_uncertain_not_a_crash(self):
        self.insert_task(receipt_artifacts="not-a-list")
        self.apply()
        task = self.rows("SELECT * FROM v2_task WHERE task_id='T-ONE'")[0]
        self.assertEqual(task["task_state"], "migration_uncertain")

    def test_service_idempotency_revision_and_epoch(self):
        self.apply()
        service.set_enabled(self.state, *self.actor, True)
        epoch = service.service_status(self.state)["controller_epoch"]
        envelope = {"command_id": "cmd-" + uuid.uuid4().hex, "tool": "task.create",
                    "expected_epoch": epoch,
                    "payload": {"task_id": "T-SERVICE", "title": "Service task",
                                "body": "body", "priority": "P2"}}
        first = service.execute_command(self.state, *self.actor, envelope)
        self.assertFalse(first["replayed"])
        second = service.execute_command(self.state, *self.actor, envelope)
        self.assertTrue(second["replayed"])
        self.assertEqual(first["task_id"], second["task_id"])
        conflict = dict(envelope)
        conflict["payload"] = dict(envelope["payload"], title="different")
        with self.assertRaises(service.ServiceError):
            service.execute_command(self.state, *self.actor, conflict)
        update = {"command_id": "cmd-" + uuid.uuid4().hex, "tool": "task.update",
                  "expected_epoch": epoch,
                  "payload": {"task_id": "T-SERVICE", "expected_revision": 1,
                              "title": "Updated"}}
        updated = service.execute_command(self.state, *self.actor, update)
        self.assertEqual(updated["revision"], 2)
        stale = dict(update)
        stale["command_id"] = "cmd-" + uuid.uuid4().hex
        with self.assertRaises(service.ServiceError):
            service.execute_command(self.state, *self.actor, stale)
        stale_epoch = dict(envelope)
        stale_epoch["command_id"] = "cmd-" + uuid.uuid4().hex
        stale_epoch["expected_epoch"] = epoch + 1
        with self.assertRaises(service.ServiceError):
            service.execute_command(self.state, *self.actor, stale_epoch)

    def test_role_permissions_fence_service_authority(self):
        self.apply()
        config = json.loads(json.dumps(self.config))
        config["roles"]["planner"]["permissions"] = []
        other = self.root / "state-empty"
        control.initialize(other, config)
        planner = control.bind(other, "planner", self.planner_spec,
                               self.planner_spec["expected_identity"], "controller")
        with self.assertRaises(ValueError):
            service.set_freeze(other, planner["instance_id"], planner["version"], "main", True, "")
        with self.assertRaises(ValueError):
            service.apply_migration(other, planner["instance_id"], planner["version"], "main")

    def test_freeze_blocks_legacy_register(self):
        self.apply()
        service.set_freeze(self.state, *self.actor, True, "test freeze")
        task_id, nonce = "T-FREEZE", "nonce-freeze"
        handoff, result = self.write_files(task_id, nonce)
        req = self.request(task_id, nonce, [str(handoff), str(result)])
        with self.assertRaises(ValueError):
            control.register_task(self.state, req, *self.actor)

    def test_freeze_blocks_scheduler_dispatch_before_send(self):
        self.apply()
        service.set_freeze(self.state, *self.actor, True, "test freeze")
        task_id, nonce = "T-DISPATCH", "nonce-dispatch"
        handoff, result = self.write_files(task_id, nonce)
        req = self.request(task_id, nonce, [str(handoff), str(result)])
        req["dispatch_confirmed"] = False
        req["worker"] = dict(req["worker"], baseline_completion_seq=10)
        observed = {"identity": self.worker_spec["expected_identity"], "status": "idle",
                    "completion_seq": 10}
        with patch.object(scheduler.Transport, "status", return_value=observed), \
             patch.object(scheduler.Transport, "send", side_effect=AssertionError("must not send")):
            with self.assertRaises(ValueError):
                scheduler.dispatch(self.state, req, task_id + " " + nonce, *self.actor)

    def test_freeze_between_continuation_reservation_and_send_blocks_prompt(self):
        self.insert_task(task_id="T-CONT", status="running", dispatch_status="running",
                         review_verdict=None, reviewed=False)
        self.apply()
        original_journal = scheduler.journal

        def freezing_journal(con, task, generation, actor, kind, data, event_id=None):
            if kind == "continue_reserved":
                con.execute("UPDATE v2_service_state SET frozen=1, "
                            "controller_epoch=controller_epoch+1 WHERE id=1")
            return original_journal(con, task, generation, actor, kind, data, event_id)

        observed = {"identity": self.worker_spec["expected_identity"], "status": "idle",
                    "completion_seq": 10}
        transport = mock.Mock()
        transport.status.return_value = observed
        transport.send.side_effect = AssertionError("a frozen continuation must not send")
        with self.db() as con:
            con.execute("INSERT OR REPLACE INTO task_observations VALUES (?,?)",
                        ("T-CONT", json.dumps({"status": "incomplete", "stall_episode": 1})))
        with patch.object(scheduler, "journal", side_effect=freezing_journal):
            result = scheduler.continue_task(self.state, "T-CONT", *self.actor, transport=transport)
        self.assertEqual(result["status"], "CONTINUE_NOT_SENT")
        transport.send.assert_not_called()
        status = service.service_status(self.state)
        self.assertTrue(status["frozen"])

    def test_cli_dry_run_apply_status_and_freeze(self):
        self.insert_task(terminate=True)
        script = Path(service.__file__).resolve()
        base = [sys.executable, "-B", str(script), "--state", str(self.state)]
        dry = subprocess.run(base + ["migrate"], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual(json.loads(dry.stdout)["mode"], "dry_run")
        with self.db() as con:
            self.assertFalse(con.execute(
                "SELECT 1 FROM sqlite_master WHERE name='v2_task'").fetchone())
        applied = subprocess.run(base + ["migrate", "--apply", "--actor", self.actor[0],
                                         "--version", str(self.actor[1]), "--context", "main"],
                                 cwd=self.root, capture_output=True, text=True)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual(json.loads(applied.stdout)["status"], "APPLIED")
        status = subprocess.run(base + ["status"], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["v2"], "present")
        frozen = subprocess.run(base + ["freeze", "--actor", self.actor[0],
                                       "--version", str(self.actor[1]), "--context", "main",
                                       "--reason", "cli test"],
                                cwd=self.root, capture_output=True, text=True)
        self.assertEqual(frozen.returncode, 0, frozen.stderr)
        self.assertTrue(json.loads(frozen.stdout)["frozen"])


if __name__ == "__main__":
    unittest.main()
