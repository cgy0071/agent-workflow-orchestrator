"""Use a local conversation directory for one authorized project task."""
import argparse
import json
from pathlib import Path
import sys

import project_entry as entry


def report(path):
    state = entry.status(path)
    task = state["task"] or {}
    assignment = state["assignment"] or {}
    reviews = state["reviews"]
    verdict = reviews[-1]["verdict"] if reviews else None
    accepted = (task.get("task_state") in ("accepted", "landed") and
                assignment.get("review_status") == "accepted" and verdict == "accept")
    return {"accepted": accepted, "task_state": task.get("task_state"),
            "assignment_state": assignment.get("state"), "review": verdict,
            "accepted_evidence_id": task.get("accepted_evidence_id") if accepted else None,
            "next_action": state["next_action"]}


def act(root, message):
    path = root / "plan.json"
    if message.startswith("submit "):
        plan = entry.prepare(Path(message[7:].strip()), root)
        print(json.dumps({"state": "prepared", "task": plan["derived"]["task_id"],
                          "dispatched": False}))
        return 0
    if message not in {"start --confirm-execute", "status", "进度", "结果", "做完了吗"}:
        raise ValueError("Use submit SPEC_PATH, start --confirm-execute, status, or exit")
    plan = entry.load_plan(path)
    if Path(plan["derived"]["paths"]["root"]).resolve() != root:
        raise ValueError("Conversation directory does not match the frozen plan")
    if message == "start --confirm-execute":
        return entry.main(["run", "--plan", str(path), "--confirm-execute"])
    result = report(path)
    print(json.dumps(result))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thread", required=True, type=Path, help="Directory named by spec.work_root")
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("submit").add_argument("--spec", required=True)
    actions.add_parser("start").add_argument("--confirm-execute", action="store_true", required=True)
    actions.add_parser("status")
    actions.add_parser("chat")
    args = parser.parse_args(argv)
    root = args.thread.resolve()
    errors = (entry.EntryError, ValueError, OSError)
    try:
        if args.action == "chat":
            print("MAID: submit SPEC_PATH | start --confirm-execute | status | exit", flush=True)
            for line in sys.stdin:
                message = line.strip()
                if message in {"exit", "退出"}:
                    break
                try:
                    act(root, message)
                except errors as exc:
                    print(str(exc), file=sys.stderr)
            return 0
        message = {"submit": "submit " + getattr(args, "spec", ""),
                   "start": "start --confirm-execute", "status": "status"}[args.action]
        return act(root, message)
    except errors as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
