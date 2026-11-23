import codecs
from contextlib import redirect_stderr
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import harness_probe
import kb_cli
import project_roles
import project_retrieve
from mcp_gateway import read_token_file, GatewayError
from project_cli_inputs import diagnostic


class WindowsInputs(unittest.TestCase):
    def test_bom_and_invalid_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "skill.json"
            path.write_bytes(codecs.BOM_UTF8 + b'{"skill":"test@1","fields":["A"]}')
            self.assertEqual(project_retrieve.load_skill(path)[0]["skill"], "test@1")
            for raw in (b"", b"{", b"\xff", b"x" * 8193):
                path.write_bytes(raw)
                with self.assertRaisesRegex(ValueError, "invalid_skill"):
                    project_retrieve.load_skill(path)
            path.unlink()
            with self.assertRaisesRegex(ValueError, "invalid_skill"):
                project_retrieve.load_skill(path)

    def test_relative_bom_token_through_role(self):
        with tempfile.TemporaryDirectory(dir=".") as tmp:
            path = os.path.relpath(Path(tmp) / "token")
            Path(path).write_bytes(codecs.BOM_UTF8 + b"abc123")
            self.assertEqual(read_token_file(path), "abc123")
            with patch.object(project_roles, "operate", return_value={}) as op:
                self.assertEqual(project_roles.main(["check", "--state", ".", "--project-root", ".",
                    "--token-file", path, "--agent", "a", "--role", "b"]), 0)
                self.assertEqual(op.call_args.args[2], "abc123")
            Path(path).write_bytes(b"\xff")
            with self.assertRaises(GatewayError):
                read_token_file(path)

    def test_missing_write_flags_before_io(self):
        for action in ("grant", "revoke"):
            stderr = io.StringIO()
            with redirect_stderr(stderr), self.assertRaises(SystemExit):
                project_roles.main([action, "--state", ".", "--project-root", ".", "--token-file",
                                    "absent", "--agent", "a", "--role", "b"])
            for arg in ("--epoch", "--expected-revision"):
                self.assertIn(arg, stderr.getvalue().splitlines()[-1])

    def test_read_commands_explain_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            for main, flags in [(project_retrieve.main, ["--query", "x", "--doc", "d"]),
                                (kb_cli.main, ["inventory", "--task", "t"])]:
                for root, message in [(".", "--project-root"), (tmp, "--state")]:
                    stderr = io.StringIO()
                    with redirect_stderr(stderr):
                        self.assertEqual(main(flags + ["--state", tmp, "--project-root", root,
                                                       "--token-file", "missing"]), 2)
                    self.assertIn(message, stderr.getvalue())
        self.assertIn("invalid_payload:", diagnostic(GatewayError("invalid_payload")))

    @unittest.skipUnless(os.name == "nt", "Windows npm layout")
    def test_resolve_native_without_executing_cmd(self):
        with tempfile.TemporaryDirectory() as tmp:
            shim = Path(tmp) / "codex.cmd"
            shim.touch()
            exe = Path(tmp) / ("node_modules/@openai/codex/node_modules/@openai/"
                               "codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe")
            with patch.dict(os.environ, {"PATH": tmp}), patch("platform.machine", return_value="AMD64"):
                with self.assertRaises(harness_probe.ProbeError):
                    harness_probe.binary("codex")
                exe.parent.mkdir(parents=True)
                exe.touch()
                self.assertEqual(harness_probe.binary("codex"), str(exe.resolve()))
