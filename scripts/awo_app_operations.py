"""Versioned JSON operations over the existing AWO project-entry authority."""
import argparse
import hashlib
import json
from pathlib import Path

import project_entry as entry

OPERATIONS = ("prepare", "start", "status", "recover")


class Arguments(argparse.ArgumentParser):
    def error(self, message):
        raise entry.EntryError("arguments", message, "invalid_arguments")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def backend_version():
    scripts = Path(__file__).resolve().parent
    # Content identity also works in source archives with no Git metadata.
    files = sorted(p for p in scripts.glob("*.py") if not p.name.startswith("test_"))
    return digest(json.dumps([(p.name, digest(p.read_bytes())) for p in files]).encode())


def snapshot(plan_path):
    current = entry.status(plan_path)
    journal = current["journal"]
    action = current["next_action"]
    state = ("complete" if action.startswith("complete;") else
             "terminal" if action.startswith("terminal:") else
             "blocked" if (journal or {}).get("phase") == "evidence_blocked" or
             any(b.get("status") == "blocked" for b in current.get("bridges", {}).values()) else
             "uncertain" if journal else "prepared")
    return state, current


def handle(operation, argv):
    parser = Arguments(prog="awo_app.py " + operation)
    parser.add_argument("--thread", type=Path, required=True)
    if operation == "session":
        parser.add_argument("--action", choices=("records", "focus", "resume"), required=True)
    if operation == "prepare":
        parser.add_argument("--spec", type=Path, required=True)
    if operation in ("start", "recover"):
        parser.add_argument("--confirm-execute", action="store_true")
    if operation == "recover":
        parser.add_argument("--handoff", choices=("continue", "manual", "reassign"))
        parser.add_argument("--state-version")
        parser.add_argument("--recipient")
        parser.add_argument("--lanes", type=Path)
        parser.add_argument("--reassign-lane")
    if operation in ("status", "start", "recover"):
        parser.add_argument("--rework-attempt", action="store_true")
        parser.add_argument("--handoff-attempt", action="store_true")
    if operation == "start":
        parser.add_argument("--draft", type=Path)
        parser.add_argument("--draft-digest")
    if operation == "rework":
        parser.add_argument("--evidence-route", type=Path)
    args = parser.parse_args(argv)
    if operation == "recover" and (args.lanes or args.reassign_lane):
        if not (args.lanes and args.reassign_lane and args.handoff == "reassign"):
            parser.error("Lane reassignment requires --lanes, --reassign-lane and --handoff reassign")
    if operation == "recover" and any((args.handoff, args.state_version, args.recipient)):
        if not all((args.handoff, args.state_version, args.recipient, args.confirm_execute)):
            parser.error("A handoff requires action, observed state version, recipient and confirmation.")
    root = args.thread.resolve()
    path = root / "plan.json"
    marker = root / "application-pin.json"
    duplicate = False
    version = backend_version()
    creating = operation == "prepare" and not root.exists()
    if creating:
        spec_digest = digest(args.spec.read_bytes())
        plan = entry.prepare(args.spec, root)
        if digest((root / "spec.json").read_bytes()) != spec_digest:
            raise entry.EntryError("pin", "Spec changed during preparation.", "conflict")
        with marker.open("x", encoding="ascii") as out:
            json.dump({"protocol": 1, "backend": version, "plan": plan["plan_sha256"],
                       "spec": spec_digest}, out)
    else:
        if not marker.is_file():
            raise entry.EntryError("pin", "No completed application preparation at this path.", "missing_pin")
    pin = json.loads(marker.read_text(encoding="ascii"))
    plan = entry.load_plan(path)
    if (Path(plan["derived"]["paths"]["root"]).resolve() != root or
            pin.get("plan") != plan["plan_sha256"] or pin.get("protocol") != 1 or
            pin.get("backend") != version or pin.get("spec") != digest((root / "spec.json").read_bytes())):
        raise entry.EntryError("pin", "Run identity, backend, plan or frozen spec changed.", "pin_mismatch")
    if operation == "rework":
        from task_rework import prepare
        link, duplicate = prepare(plan, entry.read_json(args.evidence_route) if args.evidence_route else None)
        return "prepared", link, duplicate
    if getattr(args, "rework_attempt", False) or getattr(args, "handoff_attempt", False):
        if args.rework_attempt:
            from task_rework import linked_child
        else:
            from task_handoff import linked_child
        child, _ = linked_child(plan)
        rest = ["--thread", str(child.parent)]
        if args.rework_attempt and args.handoff_attempt:
            rest.append("--handoff-attempt")
        if getattr(args, "confirm_execute", False):
            rest.append("--confirm-execute")
        if getattr(args, "handoff", None):
            rest.extend(["--handoff", args.handoff, "--state-version", args.state_version,
                         "--recipient", args.recipient])
            if args.reassign_lane:
                rest.extend(["--lanes", str(args.lanes), "--reassign-lane", args.reassign_lane])
        if getattr(args, "draft", None):
            rest += ["--draft", str(args.draft)]
        if getattr(args, "draft_digest", None):
            rest += ["--draft-digest", args.draft_digest]
        return handle(operation, rest)
    if operation == "session":
        from task_session import access
        return "inspected", access(plan, args.action), False
    if operation == "prepare":
        if digest(args.spec.read_bytes()) != pin["spec"]:
            raise entry.EntryError("prepare", "Different spec for an existing thread.", "conflict")
        duplicate = not creating
    state, result = snapshot(path)
    if operation == "start":
        if not args.confirm_execute:
            raise entry.EntryError("start", "Explicit execution confirmation required.", "not_confirmed")
        if "evidence_route" in plan["spec"]:
            from evidence_confirmation import confirm, load
            if not args.draft or not args.draft_digest:
                raise ValueError("Provide the reviewed draft and its displayed digest")
            if state == "prepared":
                confirm(plan, json.loads(args.draft.read_text(encoding="utf-8")), args.draft_digest)
            elif load(plan)["draft_digest"] != args.draft_digest:
                raise ValueError("Different draft already confirmed")
        elif args.draft or args.draft_digest:
            raise ValueError("Prepare a run with an evidence route first")
        if state != "prepared":
            duplicate = True
        else:
            entry.execute_plan(path)
            state, result = snapshot(path)
    if operation == "recover":
        import task_handoff
        if args.handoff:
            worker = None
            if args.reassign_lane:
                import cli_lanes
                worker = cli_lanes.native_bridge(cli_lanes.load_lanes(args.lanes), args.reassign_lane, "executor")
            result["recovery"], duplicate = task_handoff.apply(
                plan, args.handoff, args.state_version, args.recipient, worker)
            state = "handoff_recorded"
        elif not args.confirm_execute:
            result["recovery"] = entry.recover(path, False)
            result["recovery"]["handoff"] = task_handoff.inspect(plan)
            duplicate = state == "complete"
        elif state == "complete":
            duplicate = True
        else:
            report = entry.recover(path, args.confirm_execute)
            state, result = snapshot(path)
            result["recovery"] = report
    if operation == "status" and (root / "rework" / "link.json").is_file():
        from task_rework import card
        result["continuity"]["rework"] = card(plan)
    result["backend_version"] = version
    return state, result, duplicate


def main(operation, argv):
    response = dict(protocol=1, operation=operation, ok=False, state="refused", duplicate=False,
                    result={}, error=None)
    try:
        state, result, duplicate = handle(operation, argv)
        response.update(state=state, result=result, duplicate=duplicate, ok=True)
        diagnosis = operation == "recover" and "--confirm-execute" not in argv
        if operation != "status" and not diagnosis and state in ("uncertain", "blocked"):
            response.update(ok=False, error=dict(code=state, message="Inspect before recovery."))
    except Exception as exc:
        response["error"] = dict(code=getattr(exc, "code", "operation_failed"), message=str(exc))
    print(json.dumps(response, ensure_ascii=True))
    return 0 if response["ok"] else 2
