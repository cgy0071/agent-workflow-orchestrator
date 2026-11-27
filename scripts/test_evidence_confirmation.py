import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import awo_app_operations as app
import evidence_confirmation as evidence
import evidence_draft
import test_project_entry as runs
import test_project_retrieve as docs
from service_access import principal
import awo_service


class FrozenDeliveryTests(unittest.TestCase):
    def setUp(self):
        fixture = docs.ProjectRetrieveTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        env = fixture.env
        key = fixture.publish("Only synthetic source: latency is 17 ms.")
        self.key = key
        task = "paper-task"
        awo_service.execute_command(env.state, *env.f.f.actor, env.f.f.envelope(
            "task.create", {"task_id": task, "title": "test",
                            "body": json.dumps({"evidence_document_ids": [key]})}))
        route = dict(root=str(env.root), state=str(env.state), task=task)
        for role, actor in (("worker", "executor"), ("reviewer", "planner")):
            token = env.f.issue(role, actor, task_id=task)
            who = principal(env.state, env.root, token)
            route["project_id"] = who["project_id"]
            section = {"agent_id": who["agent_id"]}
            for field, value in (("task_token_file", token), ("doc_token_file",
                                 env.f.issue(role, actor, knowledge_id=key))):
                path = env.root / (role + field)
                path.write_text(value)
                section[field] = str(path)
            route[role] = section
        self.route = route
        self.env = env
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.f = runs.Fixture(temp.name)
        self.root = self.f.base / "thread"
        self.spec = self.f.spec(self.root, "refs/heads/evidence", evidence_route=route)
        cfg = {**{k: route[k] for k in ("root", "state", "task", "project_id")}, **route["worker"]}
        ref = evidence_draft.handle(cfg, {"query": "latency"})["items"][0]["ref"]
        self.draft = evidence_draft.handle(cfg, {"goal": self.spec["goal"], "selected": [ref]})["draft"]
        self.plan = {"spec": self.spec, "plan_sha256": "a"*64, "derived": {"goal": self.spec["goal"]}}

    def revoke(self, role):
        import service_access
        token = Path(self.route[role]["task_token_file"]).read_text()
        who = principal(self.env.state, self.env.root, token)
        env = self.env.f.f
        service_access.revoke_token(self.env.state, *env.actor,
                                    env.envelope("access.revoke", {"token_id": who["token_id"], "expected_revision": 1}))

    def confirm(self):
        return evidence.confirm(self.plan, self.draft, self.draft["draft_sha256"])

    def test_change_before_confirmation(self):
        self.draft["items"][0]["pinned_revision"]["sha256"] = "0"*64
        self.draft["selection_sha256"] = evidence.checksum(self.draft["items"])
        self.draft.pop("draft_sha256")
        self.draft["draft_sha256"] = evidence.checksum(self.draft)
        with self.assertRaises(ValueError):
            self.confirm()
        self.assertFalse(evidence.database(self.route).exists())

    def test_revoked_before_confirm(self):
        self.revoke("worker")
        with self.assertRaises(awo_service.ServiceError):
            self.confirm()
        self.assertFalse(evidence.database(self.route).exists())

    def test_revoked_before_execution(self):
        packet = self.confirm()
        self.revoke("worker")
        with self.assertRaises(awo_service.ServiceError):
            evidence.resolve(self.route, "worker", packet)

    def test_revoked_before_review(self):
        packet = self.confirm()
        evidence.resolve(self.route, "worker", packet)
        self.revoke("reviewer")
        with self.assertRaises(awo_service.ServiceError):
            evidence.resolve(self.route, "reviewer", packet)

    def test_single_confirmation_across_threads(self):
        self.confirm()
        self.plan["plan_sha256"] = "b"*64
        with self.assertRaises(PermissionError):
            self.confirm()

    def test_ledger_with_independent_receipts(self):
        spec_file = self.f.base / "spec.json"
        spec_file.write_text(json.dumps(self.spec))
        draft_file = self.f.base / "draft.json"
        draft_file.write_text(json.dumps(self.draft))
        args = ["--thread", str(self.root)]
        app.handle("prepare", args + ["--spec", str(spec_file)])
        self.f.install_fixture_executables(self.root)
        start = args + ["--confirm-execute", "--draft", str(draft_file),
                        "--draft-digest", self.draft["draft_sha256"]]
        with patch.dict(os.environ, self.f.write_mode()):
            self.assertEqual(app.handle("start", start)[0], "complete")
            self.assertTrue(app.handle("start", start)[2])
        receipts = [json.loads(p.read_text()) for p in self.root.rglob("task-evidence-receipt.json")]
        self.assertEqual(sorted(r["role"] for r in receipts), ["reviewer", "reviewer", "worker"])
        self.assertEqual(len({r["packet_sha256"] for r in receipts}), 1)
        self.assertEqual(len({r["agent_id"] for r in receipts}), 2)
        for p in self.root.rglob("job.json"):
            self.assertNotIn("latency is 17", p.read_text())
