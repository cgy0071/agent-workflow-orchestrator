import importlib.util
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock


SCRIPTS = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("portable_launcher", SCRIPTS.parent / "packaging/windows/start.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class ReopenTests(unittest.TestCase):
    def test_fresh_reopen_and_removed_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "demo"
            def launch(path):
                subprocess.run([sys.executable, "-B", SCRIPTS / "demo_scheduler.py",
                                "--directory", path], check=True, stdout=subprocess.DEVNULL)
            fresh, stamp = launcher.open_demo(data, launch)
            self.assertTrue(fresh)
            self.assertTrue(stamp)
            cannot_dispatch = Mock(side_effect=AssertionError("unexpected dispatch"))
            self.assertEqual(launcher.open_demo(data, cannot_dispatch), (False, stamp))
            # Leave the old success marker in place. The ledger must take precedence.
            self.assertTrue((data / "demo-result.json").exists())
            db = sqlite3.connect(data / "state/workflow.sqlite3")
            try:
                with db:
                    db.execute("DELETE FROM reviews WHERE event IN (SELECT id FROM events WHERE task='JOIN')")
            finally:
                db.close()
            with self.assertRaises(ValueError):
                launcher.open_demo(data, cannot_dispatch)
            cannot_dispatch.assert_not_called()

    def test_partial_directory_does_not_initialize_or_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            with self.assertRaises(sqlite3.Error):
                launcher.open_demo(data, self.fail)
            self.assertEqual(list(data.iterdir()), [])
