"""Create a disposable MAID project profile on the current computer."""
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from harness_probe import binary


def main():
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
