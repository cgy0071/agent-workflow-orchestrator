import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import test_project_entry as fixtures


class SecretaryTest(unittest.TestCase):
    def test_fresh_process_recalls_review_without_resending(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = fixtures.Fixture(tmp)
            root = Path(tmp) / "conversation"
            spec = Path(tmp) / "spec.json"
            spec.write_text(json.dumps(f.spec(root, "refs/heads/demo")), encoding="utf-8")
            env = dict(os.environ, **f.write_mode())
            def call(*args, chat=None):
                return subprocess.run([sys.executable, "-B", str(Path(__file__).with_name("maid.py")),
                                       "--thread", str(root), *args], env=env, input=chat,
                                      text=True, capture_output=True, timeout=240)
            submitted = call("submit", "--spec", str(spec))
            self.assertEqual(submitted.returncode, 0, submitted.stderr)
            self.assertFalse(json.loads(submitted.stdout)["dispatched"])
            self.assertFalse(json.loads(call("status").stdout)["accepted"])
            f.install_fixture_executables(root)
            done = call("start", "--confirm-execute")
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            reopened = call("chat", chat="status\nexit\n")
            result = json.loads(reopened.stdout.splitlines()[-1])
            self.assertTrue(result["accepted"], result)
            self.assertTrue(result["accepted_evidence_id"], result)
            before = tuple(len(list((root / n / "jobs").iterdir())) for n in ("worker", "reviewer"))
            self.assertNotEqual(call("start", "--confirm-execute").returncode, 0)
            self.assertEqual(before, tuple(len(list((root / n / "jobs").iterdir()))
                                           for n in ("worker", "reviewer")))
