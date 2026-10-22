"""Run an approved one-shot harness command and emit task-bound lifecycle receipts."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

from monitor_task import read_json, save_json


def finalize(job, state, directory):
    """Persist native completion separately from process cleanup evidence."""
    contract = job.get("completion_contract")
    if contract:
        from native_events import inspect_turn
        output = directory / "output.log"
        raw = output.read_bytes() if output.exists() else b""
        state["native_turn"] = inspect_turn(contract["harness"], raw)
        complete = state["native_turn"]["status"] == "completed"
        if state["status"] != "blocked":
            state.update(status="done" if complete else "failed", completion_seq=int(complete))
        if state.get("timed_out") and complete:
            state["warning"] = "native turn completed; CLI did not exit before deadline"
        elif complete and state.get("returncode"):
            state["warning"] = "native turn completed; process exited with nonzero status"
    save_json(directory / "status.json", state)
    return state


def run(job):
    if not job.get("authorization") or not job.get("task_id") or not job.get("nonce"):
        raise ValueError("explicit job authorization and task identity required")
    argv = job["argv"]
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
        raise ValueError("argv must be a nonempty string list")
    contract = job.get("completion_contract")
    if contract is not None and (not isinstance(contract, dict) or contract.get("type") != "native_turn"
                                 or contract.get("harness") not in ("codex", "pi")):
        raise ValueError("unsupported completion_contract")
    timeout = job.get("timeout_seconds")
    if timeout is not None:
        from monitor_task import number
        number(timeout, "timeout_seconds", maximum=3600)
        if not isinstance(job["authorization"], dict) or job["authorization"].get("terminate_on_timeout") is not True:
            raise ValueError("bounded jobs require explicit terminate_on_timeout authorization")
    directory = Path(job["output_dir"]).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    state = {"task_id": job["task_id"], "nonce": job["nonce"], "identity": job["identity"],
             "status": "working", "completion_seq": 0, "argv": argv, "cwd": job["cwd"]}
    save_json(directory / "status.json", state)
    proc = None
    try:
        with (directory / "output.log").open("wb") as log:
            proc = subprocess.Popen(argv, cwd=job["cwd"], stdout=log, stderr=subprocess.STDOUT, shell=False,
                                    stdin=subprocess.DEVNULL,
                                    start_new_session=os.name != "nt",
                                    creationflags=0x08000000 if os.name == "nt" else 0)
            state["pid"] = proc.pid
            save_json(directory / "status.json", state)
            try:
                code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                # This only targets the process tree created above, under the
                # explicit bounded-job authorization. Monitor timeouts never do this.
                if os.name == "nt":
                    stopped = subprocess.run(["taskkill.exe", "/PID", str(proc.pid), "/T", "/F"],
                                             stdout=log, stderr=log, timeout=10, creationflags=0x08000000)
                    if stopped.returncode:
                        # The retained Popen handle permits stopping our direct
                        # child even when OpenProcess/taskkill is denied. Do not
                        # claim that descendants were stopped or free the slot.
                        if proc.poll() is None: proc.kill()
                        state.update(status="blocked", completion_seq=0, timed_out=True,
                                     returncode=proc.wait(timeout=5), process_status="termination_uncertain",
                                     error="descendant termination unverified")
                        log.flush()
                        return finalize(job, state, directory)
                else:
                    os.killpg(proc.pid, signal.SIGKILL)
                code = proc.wait(timeout=5)
                state["timed_out"] = True
        state.update(status="done" if code == 0 and not state.get("timed_out") else "failed", completion_seq=1, returncode=code)
        state["process_status"] = ("terminated_on_timeout" if state.get("timed_out") else
                                   "clean_exit" if code == 0 else "nonzero_exit")
    except Exception as exc:
        uncertain = proc is not None and proc.poll() is None
        state.update(status="blocked" if uncertain else "failed", completion_seq=0,
                     process_status="termination_uncertain" if uncertain else "runner_error", error=str(exc))
        if proc is not None: state["returncode"] = proc.poll()
        finalize(job, state, directory)
        raise
    return finalize(job, state, directory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True, help="JSON with task_id, nonce, cwd, identity, argv, output_dir")
    args = parser.parse_args()
    state = run(read_json(args.job))
    print(json.dumps({k: state.get(k) for k in ("task_id", "status", "returncode")}))
    return 0 if state["status"] == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
