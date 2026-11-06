"""Checks for the optional Windows scripts in contrib/: plain ASCII, and valid PowerShell where it can be parsed."""
from pathlib import Path
import shutil
import subprocess
import unittest

CONTRIB = Path(__file__).resolve().parents[1] / "contrib"


class ContribScriptTests(unittest.TestCase):
    def scripts(self):
        files = sorted(p for p in CONTRIB.rglob("*") if p.suffix in (".ps1", ".cmd"))
        self.assertTrue(files, "no contrib scripts found")
        return files

    def test_scripts_are_plain_ascii(self):
        # Windows PowerShell 5.1 reads a script without BOM in the ANSI code page, so non-ASCII text would break.
        for path in self.scripts():
            with self.subTest(path=path.name):
                path.read_bytes().decode("ascii")

    def test_powershell_scripts_parse(self):
        shell = shutil.which("powershell") or shutil.which("pwsh")
        if not shell:
            self.skipTest("PowerShell is not installed")
        for path in (p for p in self.scripts() if p.suffix == ".ps1"):
            with self.subTest(path=path.name):
                check = ("$e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
                         "'" + str(path).replace("'", "''") + "', [ref]$null, [ref]$e); exit $e.Count")
                result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", check],
                                        capture_output=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout.decode(errors="replace") +
                                 result.stderr.decode(errors="replace"))

    def test_setup_script_keeps_its_safety_settings(self):
        text = (CONTRIB / "windows-remote" / "setup-ssh-tailscale.ps1").read_text(encoding="ascii")
        for setting in ("PasswordAuthentication no", "KbdInteractiveAuthentication no", "AllowTcpForwarding no",
                        "ListenAddress $tsIp", "-RemoteAddress '100.64.0.0/10'", "sshd.exe\" -t"):
            with self.subTest(setting=setting):
                self.assertIn(setting, text)


if __name__ == "__main__":
    unittest.main()
