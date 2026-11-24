import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import awo_app
from test_project_entry import Fixture


class AppBoundaryTests(unittest.TestCase):
    def test_capabilities_readonly_subprocess(self):
        with tempfile.TemporaryDirectory() as td:
            proc = subprocess.run([sys.executable, "-I", "-B",
                                   str(Path(awo_app.__file__).resolve()), "capabilities"],
                                  cwd=td, capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(proc.stdout), {"protocol": 1, "capabilities": ["profile", "maid"]})
            self.assertFalse(list(Path(td).iterdir()))

    def test_profile_exclusive_and_scoped(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = Fixture(td).repo
            dest = base / "profile.json"
            thread = base / "one"
            argv = ["profile", "--project", str(repo), "--thread", str(thread),
                    "--allow", "app.py", "--verify-script", "tests/smoke.py", "--profile", str(dest)]
            with patch("harness_probe.binary", return_value=sys.executable), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(awo_app.main(argv), 0)
                raw = dest.read_bytes()
                profile = json.loads(raw)
                self.assertEqual(profile["work_root"], str(thread.resolve()))
                self.assertEqual(profile["target_ref"], "refs/heads/maid-one")
                self.assertFalse(thread.exists())
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    awo_app.main(argv)
                self.assertEqual(dest.read_bytes(), raw)
                another = argv[:-1] + [str(base / "other.json")]
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    awo_app.main([s if s != "tests/smoke.py" else "../profile.json" for s in another])
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    awo_app.main([s if s != "app.py" else "../elsewhere" for s in another])
                self.assertFalse((base / "other.json").exists())

    def test_maid_transparent_argv_and_exit(self):
        with patch("maid.main", return_value=2) as entry:
            args = ["--thread", "example thread", "start"]
            self.assertEqual(awo_app.main(["maid", *args]), 2)
            entry.assert_called_once_with(args)
