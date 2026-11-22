"""Exercise project credentials, real scheduler admission, dispatch and revision fences."""
import json
from pathlib import Path
import subprocess
import sys
import unittest

import awo_service as service
import project_roles as roles
import test_service_api as api_fixture


class ProjectRoleTests(unittest.TestCase):
    def setUp(self):
        api = api_fixture.ServiceAPITests()
        api.setUp()
        self.addCleanup(api.doCleanups)
        self.api, self.f = api, api.f
        self.target = self.f.agents["executor"]
        self.name = "paper-reader"

    def op(self, action, rev=None, **options):
        return roles.operate(self.f.state, options.pop("root", self.f.root),
                             options.pop("token", self.api.controller), action, self.target, self.name,
                             epoch=options.pop("epoch", self.f.epoch), expected_revision=rev, **options)

    def test_role_cannot_be_self_granted_or_inferred_from_wrong_project_or_scoped_token(self):
        self.f.create()
        scoped = self.api.issue("controller", "planner", task_id="T-A")
        for opts in (dict(token=self.api.worker), dict(root=self.f.state), dict(token=scoped),
                     dict(epoch=self.f.epoch+1), dict(token="not-a-project-token")):
            with self.subTest(opts=list(opts)), self.assertRaises(service.ServiceError):
                self.op("grant", 0, **opts)
        self.assertFalse(self.op("check")["eligible"])

    def test_denied_omit_other_agent_and_granted_actual_dispatch(self):
        with self.assertRaisesRegex(service.ServiceError, "project role"):
            self.f.enqueue(required_project_role=self.name)
        self.op("grant", 0)
        with self.assertRaisesRegex(service.ServiceError, "project role"):
            self.f.enqueue("T-B", role="second", required_project_role=self.name)
        q, _ = self.f.enqueue(create=False, required_project_role=self.name)
        a = self.api.call(self.api.worker, "awo_assignment_claim",
                          self.api.args(task_id="T-A", expected_revision=q["revision"]))
        self.assertEqual(self.f.get_assignment(a["assignment_id"])["agent_id"], self.target)
        self.assertEqual(self.f.dispatch(a["assignment_id"])["status"], "SUBMITTED")
        self.assertTrue((self.f.root / "worker-executor" / "sends.jsonl").is_file())

    def test_revocation_and_regrant_do_not_revive_queued_contract(self):
        self.op("grant", 0)
        q, _ = self.f.enqueue(required_project_role=self.name)
        self.op("revoke", 1)
        self.op("grant", 2)
        res = self.api.call(self.api.worker, "awo_assignment_claim",
                            self.api.args(task_id="T-A", expected_revision=q["revision"]))
        self.assertEqual(res["status"], "WAITING")
        self.assertIn("project role", res["reasons"][0]["reason"])
        self.assertFalse(res["sent"])
        self.assertFalse(self.f.rows("SELECT * FROM v2_assignment"))
        self.assertFalse(self.f.rows("SELECT * FROM v2_scheduler_intent"))
        self.assertFalse((self.f.root / "worker-executor" / "sends.jsonl").exists())

    def test_revoking_after_claim_prevents_actual_send(self):
        self.op("grant", 0)
        self.f.enqueue(required_project_role=self.name)
        a = self.f.claim()
        self.op("revoke", 1)
        with self.assertRaisesRegex(service.ServiceError, "project role"):
            self.f.dispatch(a["assignment_id"])
        self.assertFalse((self.f.root / "worker-executor" / "sends.jsonl").exists())

    def test_cli_persistence_replay_revision_and_schema_upgrade(self):
        with self.f.db() as con:
            con.execute("DROP TABLE v2_project_role_grant")
        with self.assertRaisesRegex(service.ServiceError, "schema-upgrade"):
            self.op("check")
        service.set_enabled(self.f.state, *self.f.actor, False)
        service.upgrade_schema(self.f.state, *self.f.actor)
        self.f.epoch = service.set_enabled(self.f.state, *self.f.actor, True)["epoch"]
        p = self.f.root / "controller-role-test.key"
        p.write_text(self.api.controller)
        args = ["--state", str(self.f.state), "--project-root", str(self.f.root), "--token-file", str(p),
                "--role", self.name, "--agent", self.target]
        def cli(*cmd):
            return subprocess.run([sys.executable, "-B", str(Path(roles.__file__)), *cmd, *args],
                                  capture_output=True, text=True, timeout=20)
        cmd = ["grant", "--epoch", str(self.f.epoch), "--expected-revision", "0", "--command-id", "role-demo-123"]
        granted = cli(*cmd)
        self.assertEqual(granted.returncode, 0, granted.stderr)
        self.assertEqual(json.loads(granted.stdout)["revision"], 1)
        self.assertTrue(json.loads(cli(*cmd).stdout)["replayed"])
        self.assertTrue(json.loads(cli("check").stdout)["eligible"])
        with self.assertRaisesRegex(service.ServiceError, "revision changed"):
            self.op("revoke", 0)
        self.assertFalse(self.op("revoke", 1)["active"])
        self.assertFalse(json.loads(cli("check").stdout)["eligible"])
        self.assertTrue(json.loads(cli(*cmd).stdout)["replayed"])
        self.assertFalse(self.op("check")["active"])
