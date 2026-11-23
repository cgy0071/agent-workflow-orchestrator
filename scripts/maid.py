"""Use a local conversation directory for one authorized project task."""
import argparse
import json
from pathlib import Path
import sys

import maid_routing
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


def act(root, message, route_resolver=None):
    path = root / "plan.json"
    if message.startswith("submit "):
        plan = entry.prepare(Path(message[7:].strip()), root, route_resolver=route_resolver)
        print(json.dumps({"state": "prepared", "task": plan["derived"]["task_id"],
                          "dispatched": False, **route_fields(plan)}))
        return 0
    if message not in {"start --confirm-execute", "status", "进度", "结果", "做完了吗"}:
        raise ValueError("Use submit SPEC_PATH, start --confirm-execute, status, or exit")
    plan = entry.load_plan(path)
    if Path(plan["derived"]["paths"]["root"]).resolve() != root:
        raise ValueError("Conversation directory does not match the frozen plan")
    if message == "start --confirm-execute":
        return entry.main(["run", "--plan", str(path), "--confirm-execute"])
    result = dict(report(path), **route_fields(plan))
    print(json.dumps(result))
    return 0


def route_fields(plan):
    derived = plan["derived"]
    return {"route": derived["route"]} if "route" in derived else {}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thread", required=True, type=Path, help="Directory named by spec.work_root")
    parser.add_argument("--directory", type=Path, help="Operator-managed project directory JSON")
    parser.add_argument("--project", help="Requesting project key in the directory")
    parser.add_argument("--material", help="Optional named export for the requesting project")
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("submit").add_argument("--spec", required=True)
    actions.add_parser("start").add_argument("--confirm-execute", action="store_true", required=True)
    actions.add_parser("status")
    actions.add_parser("chat")
    args = parser.parse_args(argv)
    if bool(args.directory) != bool(args.project) or (args.material and not args.directory):
        parser.error("supply --directory and --project together when routing a task")
    if args.directory and args.action not in {"submit", "chat"}:
        parser.error("the route is selected at submission and retained in the thread")

    def resolve(spec, thread):
        return maid_routing.select(args.directory, args.project, args.material, spec, thread)

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
                    act(root, message, resolve if args.directory else None)
                except errors as exc:
                    print(str(exc), file=sys.stderr)
            return 0
        message = {"submit": "submit " + getattr(args, "spec", ""),
                   "start": "start --confirm-execute", "status": "status"}[args.action]
        return act(root, message, resolve if args.directory else None)
    except errors as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
