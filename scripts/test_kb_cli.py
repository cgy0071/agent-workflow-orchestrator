"""Inventory command uses the host's bound task and current grants."""
import json
from pathlib import Path
import subprocess
import sys
import unittest

import test_kb_host_bridge as bridge_tests


class InventoryCommandTests(unittest.TestCase):
    def test_scope_and_revocation_in_fresh_processes(self):
        fixture = bridge_tests.BridgeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        token = fixture.f.root / "inventory.token"
        token.write_text(fixture.token, encoding="ascii")
        script = Path(__file__).with_name("kb_cli.py")
        args = [sys.executable, "-B", str(script), "inventory", "--state", str(fixture.f.state),
                "--project-root", str(fixture.f.root), "--token-file", str(token),
                "--variant", "evidence", "--task"]
        def run(task):
            return subprocess.run(args + [task], capture_output=True, timeout=30)
        approved = run("T-A")
        self.assertEqual(approved.returncode, 0, approved.stderr)
        self.assertIn("similar_titles", json.loads(approved.stdout))
        self.assertNotEqual(run("OTHER").returncode, 0)
        project, = fixture.cfg["projects"]
        del fixture.cfg["projects"][project]["tasks"]["T-A"]["source_grants"][fixture.actor]["kb.inventory"]
        fixture.save()
        denied = run("T-A")
        self.assertNotEqual(denied.returncode, 0)
        self.assertFalse(denied.stdout)
        self.assertNotIn(fixture.token.encode(), denied.stderr)
