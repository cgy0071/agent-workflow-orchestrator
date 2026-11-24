import json
from pathlib import Path
import subprocess
import sys
import unittest

import awo_service as service
import project_schedule as schedule
import test_service_api as fixtures


class SharedSchedulingTests(unittest.TestCase):
    def setUp(self):
        base = fixtures.ServiceAPITests()
        base.setUp()
        self.addCleanup(base.doCleanups)
        self.f = base.f
        self.binding = self.f.state, self.f.root, base.controller, self.f.project
        self.worker = base.worker
        self.requests = {}
        self.token_path = self.f.root / "secretary.token"
        self.token_path.write_text(base.controller, encoding="ascii")
        service.execute_command(self.f.state, *self.f.actor, self.f.envelope(
            "project.bind_root", {"project_root": str(self.f.root), "expected_revision": 0}))

    def enqueue(self, task, worker, **extra):
        request = self.f.request("ATTEMPT-" + task, worker)
        self.requests[task] = request
        created = schedule.command(*self.binding, self.f.envelope("task.create", {"task_id": task, "title": task}))
        payload = {"task_id": task, "expected_revision": created["revision"], "request": request,
                   "agent_id": self.f.agents[worker], "budget": {"tokens.input": 10},
                   "message": json.dumps({k: request[k] for k in ("task_id", "nonce", "cwd")}), **extra}
        queued = schedule.command(*self.binding, self.f.envelope("scheduler.enqueue", payload))
        return self.f.envelope("scheduler.claim", {"task_id": task, "expected_revision": queued["revision"]})

    def test_persistent_identity_claims_and_send_once(self):
        a = self.enqueue("T-A", "executor", write_paths=["src/report.md"])
        b = self.enqueue("T-B", "second", write_paths=["src/report.md"])
        first = schedule.command(*self.binding, a)
        aid = first["assignment_id"]
        self.assertTrue(first["reservation_ids"])
        self.assertEqual("WAITING", schedule.command(*self.binding, b)["status"])
        self.assertEqual(aid, schedule.command(*self.binding, a)["assignment_id"])
        self.assertEqual("SUBMITTED", schedule.dispatch(*self.binding, aid, self.f.epoch)["status"])
        args = [sys.executable, "-B", str(Path(schedule.__file__).with_name("awo_project.py")), "schedule",
                "--state", str(self.f.state), "--project-root", str(self.f.root), "--token-file", str(self.token_path),
                "--expected-project-id", self.f.project]
        proc = subprocess.run(args + ["status", "--task", "T-A"], capture_output=True, check=True)
        recovered = json.loads(proc.stdout)["assignments"]
        self.assertEqual([aid], [row["assignment_id"] for row in recovered])
        self.assertEqual("submitted", recovered[0]["send_state"])
        proc = subprocess.run(args + ["dispatch", "--assignment", aid, "--epoch", str(self.f.epoch)],
                              capture_output=True, check=True)
        self.assertFalse(json.loads(proc.stdout)["sent"])
        self.assertEqual(1, len((self.f.root / "worker-executor/sends.jsonl").read_text().splitlines()))

        completion = schedule.reconcile(*self.binding, aid, self.f.epoch)
        self.assertEqual("REVIEW_READY", completion["status"])
        self.assertIsNone(schedule.status(*self.binding, "T-A")["accepted_evidence_id"])
        self.f.review(completion, self.requests["T-A"])
        self.f.close(aid, completion)
        after = schedule.status(*self.binding, "T-A")
        self.assertEqual("accepted", after["task_state"])
        self.assertEqual("accepted", after["assignments"][0]["review_status"])
        retry = self.f.envelope("scheduler.claim", b["payload"])
        self.assertEqual("RESERVED", schedule.command(*self.binding, retry)["status"])

    def test_separate_declared_files_can_run_together(self):
        a = self.enqueue("T-A", "executor", write_paths=["docs/a.md"])
        b = self.enqueue("T-B", "second", write_paths=["docs/b.md"])
        self.assertEqual("RESERVED", schedule.command(*self.binding, a)["status"])
        self.assertEqual("RESERVED", schedule.command(*self.binding, b)["status"])

    def test_two_workers_share_resource_capacity(self):
        a = self.enqueue("T-A", "executor", resources={"gpu": 1})
        b = self.enqueue("T-B", "second", resources={"gpu": 1})
        self.assertEqual("RESERVED", schedule.command(*self.binding, a)["status"])
        refused = schedule.command(*self.binding, b)
        self.assertEqual("WAITING", refused["status"])
        self.assertIn("resource capacity", str(refused["reasons"]))
        self.assertEqual([], schedule.status(*self.binding, "T-B")["assignments"])

    def test_combined_budget_and_project_credentials(self):
        self.f.set_limits({"tokens.input": 15})
        a = self.enqueue("T-A", "executor")
        b = self.enqueue("T-B", "second")
        with self.assertRaises(service.ServiceError):
            schedule.command(self.f.state, self.f.root, self.worker, self.f.project, a)
        with self.assertRaises(service.ServiceError):
            schedule.command(*self.binding[:3], "different-project", a)
        self.assertEqual("RESERVED", schedule.command(*self.binding, a)["status"])
        refused = schedule.command(*self.binding, b)
        self.assertEqual("WAITING", refused["status"])
        self.assertIn("budget cap", str(refused["reasons"]))
        self.assertEqual([], schedule.status(*self.binding, "T-B")["assignments"])
