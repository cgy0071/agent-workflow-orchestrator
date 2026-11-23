"""Create a disposable MAID project profile on the current computer."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from harness_probe import ProbeError, binary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check default demo prerequisites only")
    args = parser.parse_args()
    problems = []
    for name, flags, remedy in (
        ("agy", ["--version"], "Install the Antigravity CLI, reopen PowerShell, then run agy to sign in."),
        ("codex", ["login", "status"], "Run codex login (or codex.cmd login), then retry."),
    ):
        try:
            exe = binary(name)
            result = subprocess.run([exe, *flags], capture_output=True, timeout=20)
            if result.returncode:
                raise ProbeError(remedy)
        except (ProbeError, OSError, subprocess.TimeoutExpired) as exc:
            problems.append(f"{name}: {exc} {remedy}")
    if problems:
        raise SystemExit("\n".join(problems))
    if args.check:
        print("agy executable found; Codex login confirmed. Sign in to agy before requesting a draft.")
        return
    codex = binary("codex")
    base = ROOT / "runtime" / "maid-demo"
    base.mkdir(parents=True, exist_ok=False)
    repo = base / "project"
    repo.mkdir()
    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    git("init", "-q")
    (repo / "app.py").write_text("def total(items):\n    return 0\n")
    (repo / "verify.py").write_text("from app import total\nassert total([4,-1]) == 3 and total([]) == 0\n")
    git("add", "app.py", "verify.py")
    git("-c", "user.name=MAID Demo", "-c", "user.email=demo@example.invalid", "commit", "-qm", "Initialize example")
    bridge = {"harness":"codex","executable":codex,"timeout_seconds":600}
    profile = dict(version=1, repository=str(repo), goal="Fix total(items).", allowed_paths=["app.py"],
                   verify_argv=[sys.executable, "-B", "verify.py"], worker=bridge, reviewer=bridge,
                   authorization={"source":"User-confirmed local MAID example","dispatch_worker":True,"allow_local_land":True},
                   budget={"tokens.input":40000,"tokens.output":8000}, target_ref="refs/heads/maid-demo",
                   work_root=str(base/"thread"), change_name="sum", max_real_turns=12, timeout_seconds=300)
    (base/"profile.json").write_text(json.dumps(profile, indent=2), encoding="utf-8")
    print("MAID DEMO READY")


if __name__ == "__main__":
    main()
