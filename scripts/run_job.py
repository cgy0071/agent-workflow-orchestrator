"""Run an approved one-shot harness command and emit task-bound lifecycle receipts."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

from monitor_task import read_json, save_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True, help="JSON with task_id, nonce, cwd, identity, argv, output_dir")
    args = parser.parse_args()
    job = read_json(args.job)
    if not job.get("authorization") or not job.get("task_id") or not job.get("nonce"):
        raise ValueError("explicit job authorization and task identity required")
    argv = job["argv"]
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
        raise ValueError("argv must be a nonempty string list")
    directory = Path(job["output_dir"]).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    state = {"task_id": job["task_id"], "nonce": job["nonce"], "identity": job["identity"],
             "status": "working", "completion_seq": 0, "argv": argv, "cwd": job["cwd"]}
    save_json(directory / "status.json", state)
    try:
        with (directory / "output.log").open("wb") as log:
            proc = subprocess.Popen(argv, cwd=job["cwd"], stdout=log, stderr=subprocess.STDOUT, shell=False)
            state["pid"] = proc.pid
            save_json(directory / "status.json", state)
            code = proc.wait()
        state.update(status="done" if code == 0 else "failed", completion_seq=1, returncode=code)
    except Exception as exc:
        state.update(status="failed", completion_seq=1, error=str(exc))
        save_json(directory / "status.json", state)
        raise
    save_json(directory / "status.json", state)
    print(json.dumps({k: state[k] for k in ("task_id", "status", "returncode")}))
    return state["returncode"]


if __name__ == "__main__":
    sys.exit(main())
