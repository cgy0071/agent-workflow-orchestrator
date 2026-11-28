import tempfile
import unittest
from pathlib import Path

import task_rework as rework
from test_project_entry import APP_FIXED, Fixture


class ReworkTests(unittest.TestCase):
    def test_rejected_delivery_has_no_rework(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(tmp)
            root = Path(tmp) / "original"
            path = root / "plan.json"
            self.assertEqual(fixture.prepare(root, "refs/heads/demo")[0], 0)
            self.assertEqual(fixture.cli("run", "--plan", str(path), "--confirm-execute",
                                         env=fixture.write_mode(code_verdict="reject"))[0], 2)
            with self.assertRaisesRegex(ValueError, "repair"):
                rework.prepare(rework.e.load_plan(path))
            self.assertFalse((root / "rework").exists())

    def test_material_repair_needs_fresh_grants_for_both_roles(self):
        import json
        from test_evidence_confirmation import FrozenDeliveryTests
        import awo_service
        from service_access import principal
        case = FrozenDeliveryTests()
        self.addCleanup(case.doCleanups)
        case.setUp()
        _, result, _ = case.run_denial(fixture_support=False)
        self.assertEqual(result["reviews"][-1]["verdict"], "repair")
        plan = rework.e.load_plan(case.root / "plan.json")
        with self.assertRaises(PermissionError):
            rework.prepare(plan)
        old = case.route
        route = dict(old, task="repair-2")
        with self.assertRaises(PermissionError):
            rework.prepare(plan, old)
        env = case.env
        awo_service.execute_command(env.state, *env.f.f.actor, env.f.f.envelope(
            "task.create", {"task_id": route["task"], "title": "repair",
                            "body": json.dumps({"evidence_document_ids": [case.key]})}))
        for role, actor in (("worker", "executor"), ("reviewer", "planner")):
            token = env.f.issue(role, actor, task_id=route["task"])
            path = env.root / ("repair-" + role)
            path.write_text(token)
            route[role] = dict(old[role], task_token_file=str(path))
        link, duplicate = rework.prepare(plan, route)
        self.assertFalse(duplicate)
        self.assertNotIn("17 ms", json.dumps(link))
        self.assertNotIn("17 ms", (Path(link["thread"]) / "spec.json").read_text())
        import service_access
        who = principal(env.state, env.root, Path(route["reviewer"]["task_token_file"]).read_text())
        service_access.revoke_token(env.state, *env.f.f.actor, env.f.f.envelope(
            "access.revoke", {"token_id": who["token_id"], "expected_revision": 1}))
        with self.assertRaises(awo_service.ServiceError):
            rework.prepare(plan, route)
        self.assertFalse(list((Path(link["thread"]) / "worker").rglob("job.json")))

    def test_repair_isolated_and_reviewed_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(tmp)
            root = Path(tmp) / "original"
            path = root / "plan.json"
            self.assertEqual(fixture.prepare(root, "refs/heads/demo")[0], 0)
            self.assertEqual(fixture.cli("run", "--plan", str(path), "--confirm-execute",
                                         env=fixture.write_mode(code_verdict="repair"))[0], 2)
            plan = rework.e.load_plan(path)
            previous = rework.snapshot(plan)
            dirty = Path(previous["repository"]) / "unfinished.txt"
            dirty.write_bytes(b"keep me")
            with self.assertRaisesRegex(ValueError, "unfinished.txt"):
                rework.prepare(plan)
            self.assertEqual(dirty.read_bytes(), b"keep me")
            dirty.unlink()
            link, duplicate = rework.prepare(plan)
            self.assertFalse(duplicate)
            self.assertEqual(rework.prepare(plan), (link, True))
            child = Path(link["thread"])
            fixture.install_fixture_executables(child)
            run = fixture.cli("run", "--plan", str(child / "plan.json"), "--confirm-execute",
                              env=fixture.write_mode(worker_files={"app.py": APP_FIXED + "# fixed\n"}))
            self.assertEqual(run[0], 0, run)
            self.assertEqual(rework.card(plan)["review"], "accept")
            self.assertEqual(rework.snapshot(plan), previous)
            result = Path(previous["result_path"])
            result.write_bytes(result.read_bytes() + b"X")
            with self.assertRaisesRegex(ValueError, "RESULT.md"):
                rework.prepare(plan)
