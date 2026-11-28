import unittest
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from task_session import access


class SessionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = temp.name
        self.plan = {"spec": {"worker": {"harness": "codex"}}, "derived": {"paths": {
            "root": root, "workspace": "work", "delivery": "delivery"}}}
    identity = dict(project="P", task="T", attempt="A", nonce="N", role="executor",
                    instance="I", session="S")

    def test_ephemeral_only_shows_records(self):
        def forbidden(*args):
            self.fail("one-shot job contacted a terminal")
        self.assertFalse(access(self.plan, adapter=forbidden)["focus"])
        self.assertEqual(access(self.plan, "records", forbidden)["workspace"], "work")
        with self.assertRaises(ValueError):
            access(self.plan, "resume", forbidden)

    def test_focus_never_sends_input_and_requires_current_occupant(self):
        with patch("task_session.binding", return_value=self.identity):
            for key in self.identity:
                calls = []
                def broker(action, identity):
                    calls.append(action)
                    if action == "inspect":
                        return dict(binding=identity, focus=True)
                    return dict(binding=dict(identity, **{key: "other"}), opened=False)
                with self.subTest(key=key), self.assertRaises(ValueError):
                    access(self.plan, "focus", broker)
                self.assertEqual(calls, ["inspect", "focus"])
            def verified(action, identity):
                self.assertIn(action, ("inspect", "focus", "resume"))
                return dict(binding=identity, focus=True, resume=True, opened=True)
            for action in ("focus", "resume"):
                self.assertTrue(access(self.plan, action, verified)["opened"])

    def test_real_ledger_and_synthetic_codex_receipt(self):
        from test_project_entry import Fixture
        import project_entry
        from task_session import binding
        import sys
        with tempfile.TemporaryDirectory() as tmp:
            f = Fixture(tmp)
            sid = "61e83576-5382-4979-a457-457552f42576"
            f.fake.write_text(f.fake.read_text().replace("fixture-thread", sid))
            root = Path(tmp) / "run"
            code, *result = f.prepare(root, "refs/heads/session-b", worker=dict(
                harness="codex", executable=sys.executable, session_mode="session"))
            self.assertEqual(code, 0, result)
            with patch.dict(os.environ, f.write_mode()):
                project_entry.execute_plan(root / "plan.json")
            plan = project_entry.load_plan(root / "plan.json")
            self.assertEqual(binding(plan)["session"], sid)
            cfgpath = root / "worker" / "config.json"
            cfg = json.loads(cfgpath.read_text())
            cfg["native_session_id"] = "a-different-session"
            cfgpath.write_text(json.dumps(cfg))
            self.assertFalse(access(plan)["resume"])
