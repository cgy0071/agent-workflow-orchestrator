"""Offline unit tests for read-only CLI auto-discovery and named lanes."""

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import awo_project
import cli_lanes


def _create_fake_exe(directory, name):
    if os.name == "nt":
        path = directory / (name + ".exe")
    else:
        path = directory / name
    path.write_bytes(b"fake-binary")
    if os.name != "nt":
        path.chmod(0o755)
    return path


class CliLanesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.bin_dir = Path(self._tmp.name) / "bin"
        self.bin_dir.mkdir()

    def test_discovery_finds_native_exe_and_ignores_ps1(self):
        codex_exe = _create_fake_exe(self.bin_dir, "codex")
        (self.bin_dir / "claude.ps1").write_text("Write-Host fake", encoding="utf-8")

        env_before = dict(os.environ)
        results = cli_lanes.discover_all(
            clis=("codex", "claude", "pi"),
            path_env=str(self.bin_dir),
            extra_dirs=[],
            check_version=False,
        )
        self.assertEqual(os.environ, env_before)
        by_cli = {item["cli"]: item for item in results}
        self.assertTrue(by_cli["codex"]["available"])
        self.assertEqual(Path(by_cli["codex"]["executable"]).resolve(), codex_exe.resolve())
        self.assertFalse(by_cli["claude"]["available"])
        self.assertIsNone(by_cli["claude"]["executable"])
        self.assertFalse(by_cli["pi"]["available"])

    def test_build_save_load_and_resolve_lanes(self):
        _create_fake_exe(self.bin_dir, "codex")
        _create_fake_exe(self.bin_dir, "claude")
        discovered = cli_lanes.discover_all(
            clis=("codex", "claude"),
            path_env=str(self.bin_dir),
            extra_dirs=[],
            check_version=False,
        )
        config = cli_lanes.build_default_lanes(discovered)
        self.assertIn("codex-default", config["lanes"])
        self.assertIn("claude-flash", config["lanes"])
        self.assertIn("reviewer-independent", config["lanes"])
        self.assertEqual(config["lanes"]["reviewer-independent"]["cli"], "claude")

        lanes_file = Path(self._tmp.name) / "lanes.json"
        cli_lanes.save_lanes(lanes_file, config)
        loaded = cli_lanes.load_lanes(lanes_file)
        self.assertEqual(loaded["lanes"], config["lanes"])

        exec_lane = cli_lanes.resolve_lane(loaded, "codex-default", role="executor")
        self.assertEqual(exec_lane["cli"], "codex")
        self.assertFalse(exec_lane["authorized"])

        rev_lane = cli_lanes.resolve_lane(
            loaded, "reviewer-independent", role="reviewer", peer_lane="codex-default"
        )
        self.assertEqual(rev_lane["cli"], "claude")
        self.assertFalse(rev_lane["authorized"])

    def test_enforces_role_and_forbids_self_review_and_ps1(self):
        bad_ps1 = {
            "schema_version": 1,
            "lanes": {
                "bad": {
                    "cli": "codex",
                    "executable": "C:/tools/codex.ps1",
                    "default_args": ["exec"],
                    "allowed_roles": ["executor"],
                }
            },
        }
        with self.assertRaises(cli_lanes.LaneError):
            cli_lanes.validate_lanes_config(bad_ps1)

        dual = {
            "schema_version": 1,
            "lanes": {
                "codex-default": {
                    "cli": "codex",
                    "executable": str(_create_fake_exe(self.bin_dir, "codex")),
                    "default_args": ["exec"],
                    "allowed_roles": ["executor"],
                },
                "shared-lane": {
                    "cli": "claude",
                    "executable": str(_create_fake_exe(self.bin_dir, "claude")),
                    "default_args": ["--print"],
                    "allowed_roles": ["executor", "reviewer"],
                },
            },
        }
        with self.assertRaises(cli_lanes.LaneError):
            cli_lanes.resolve_lane(dual, "codex-default", role="reviewer")
        with self.assertRaises(cli_lanes.LaneError):
            cli_lanes.resolve_lane(dual, "shared-lane", role="reviewer", peer_lane="shared-lane")

    def test_awo_project_discover_cli_command(self):
        lanes_file = Path(self._tmp.name) / "out-lanes.json"
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (patch.dict(os.environ, {"PATH": str(self.bin_dir)}),
              patch("cli_lanes._common_windows_dirs", return_value=[]),
              redirect_stdout(stdout), redirect_stderr(stderr)):
            code = awo_project.main([
                "discover-cli",
                "--clis", "codex,claude",
                "--no-version",
                "--write-lanes", str(lanes_file),
            ])
        self.assertEqual(code, 0, stderr.getvalue())
        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["authorized"])
        self.assertTrue(lanes_file.is_file())

        stdout2 = io.StringIO()
        with redirect_stdout(stdout2):
            code2 = awo_project.main(["discover-cli", "--validate-lanes", str(lanes_file)])
        self.assertEqual(code2, 0)
        self.assertTrue(json.loads(stdout2.getvalue())["valid"])

    def test_help_probe_and_native_bridge(self):
        executable = _create_fake_exe(self.bin_dir, "codex")
        with patch("cli_lanes._probe", side_effect=["codex 1.0", "exec Run non-interactively\nresume Resume"]):
            found = cli_lanes.discover_cli("codex", path_env=str(self.bin_dir))
        self.assertTrue(found["headless_supported"])
        self.assertTrue(found["supports_resume"])
        config = cli_lanes.build_default_lanes([found])
        config["lanes"]["codex-default"]["default_args"] += ["--session-mode", "session"]
        self.assertEqual(cli_lanes.native_bridge(config, "codex-default", "executor"),
                         dict(harness="codex", executable=str(executable.resolve()), session_mode="session"))


if __name__ == "__main__":
    unittest.main()
