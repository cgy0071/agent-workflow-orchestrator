import tempfile
import unittest
from pathlib import Path

import task_rework as rework
from test_project_entry import APP_FIXED, Fixture


class ReworkTests(unittest.TestCase):
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
