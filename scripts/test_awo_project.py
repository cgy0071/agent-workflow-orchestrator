"""Check the user entry without relying on a project or installed optional plugins."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ENTRY = Path(__file__).resolve().with_name("awo_project.py")


class EntrypointTests(unittest.TestCase):
    def test_all_commands_offer_help_from_unrelated_directory(self):
        with tempfile.TemporaryDirectory() as cwd:
            for group in ("role", "retrieve", "probe", "inventory", "task"):
                with self.subTest(group=group):
                    result = subprocess.run([sys.executable, "-B", str(ENTRY), group, "--help"],
                                            cwd=cwd, capture_output=True, timeout=20)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(b"usage:", result.stdout.lower())
            self.assertEqual(os.listdir(cwd), [])
