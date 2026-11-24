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
             "uncertain" if journal else "prepared")
    return state, current


def handle(operation, argv):
    parser = Arguments(prog="awo_app.py " + operation)
    parser.add_argument("--thread", type=Path, required=True)
    if operation == "prepare":
        parser.add_argument("--spec", type=Path, required=True)
    if operation in ("start", "recover"):
        parser.add_argument("--confirm-execute", action="store_true")
    args = parser.parse_args(argv)
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
    if operation == "prepare":
        if digest(args.spec.read_bytes()) != pin["spec"]:
            raise entry.EntryError("prepare", "Different spec for an existing thread.", "conflict")
        duplicate = not creating
    state, result = snapshot(path)
    if operation == "start":
        if not args.confirm_execute:
            raise entry.EntryError("start", "Explicit execution confirmation required.", "not_confirmed")
        if state != "prepared":
            duplicate = True
        else:
            entry.execute_plan(path)
            state, result = snapshot(path)
    if operation == "recover":
        if state == "complete":
            duplicate = True
        else:
            report = entry.recover(path, args.confirm_execute)
            state, result = snapshot(path)
            result["recovery"] = report
    result["backend_version"] = version
    return state, result, duplicate


def main(operation, argv):
    response = dict(protocol=1, operation=operation, ok=False, state="refused", duplicate=False,
                    result={}, error=None)
    try:
        state, result, duplicate = handle(operation, argv)
        response.update(state=state, result=result, duplicate=duplicate, ok=True)
        if operation != "status" and state == "uncertain":
            response.update(ok=False, error=dict(code="uncertain", message="Inspect before recovery."))
    except Exception as exc:
        response["error"] = dict(code=getattr(exc, "code", "operation_failed"), message=str(exc))
    print(json.dumps(response, ensure_ascii=True))
    return 0 if response["ok"] else 2
