"""Offline checks for the user-facing harness connectivity command."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import harness_probe as probe


class ProbeTests(unittest.TestCase):
    def reply(self):
        return {"type": "result", "is_error": False, "modelUsage": {probe.DS_MODEL: {}},
                "result": "marker", "total_cost_usd": 0.001}

    def test_refuses_wrong_model_marker_and_bad_cost(self):
        for change in ({"modelUsage": {"other": {}}}, {"result": "invented"},
                       {"total_cost_usd": -1},
                       {"total_cost_usd": float("nan")}, {"total_cost_usd": True},
                       {"is_error": True}):
            with self.subTest(change=change), self.assertRaises(probe.ProbeError):
                probe.check_reply(dict(self.reply(), **change), "marker")

    def test_reports_cost_without_a_monetary_ceiling(self):
        self.assertEqual(probe.check_reply(dict(self.reply(), total_cost_usd=2), "marker")["cli_estimated_usd"], 2)

    def test_paid_invocation_is_bounded_and_uses_no_project_content(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                "ANTHROPIC_BASE_URL": "https://example.invalid", "ANTHROPIC_API_KEY": "dummy",
                "OTHER_PROJECT_SECRET": "must-not-be-passed"}), patch.object(probe, "binary", return_value="mock"):
            cwd = Path(tmp) / "empty"
            cwd.mkdir()

            def invoke(cmd, **kwargs):
                self.assertEqual(cmd[cmd.index("--tools") + 1], "")
                self.assertEqual(cmd[cmd.index("--max-turns") + 1], "1")
                self.assertNotIn("--max-budget-usd", cmd)
                self.assertEqual(kwargs["cwd"], cwd)
                self.assertEqual(list(cwd.iterdir()), [])
                self.assertNotIn("OTHER_PROJECT_SECRET", kwargs["env"])
                self.assertRegex(kwargs["input"], r"^AWO-[a-f0-9]{24}$")
                reply = dict(self.reply(), result=kwargs["input"])
                return subprocess.CompletedProcess(cmd, 0, json.dumps(reply), "")

            with patch.object(subprocess, "run", side_effect=invoke) as call:
                self.assertEqual(probe.claude_probe(cwd)["status"], "live_marker_echoed")
                call.assert_called_once()


if __name__ == "__main__":
    unittest.main()
