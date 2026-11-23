"""Prepare a secretary task using a trusted local AWO project profile."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def main(argv):
    sys.path.insert(0, str(SCRIPTS))
    import secretary_adapter
    parser = argparse.ArgumentParser(prog="maid.py request")
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--thread", type=Path, required=True)
    secretary_adapter.options(parser)
    parser.add_argument("request")
    args = parser.parse_args(argv)
    import sqlite3
    import project_entry
    from harness_probe import ProbeError

    try:
        root = args.thread.resolve()
        if root.exists() or not args.request.strip():
            parser.error("Use a new thread and a nonempty request.")
        spec = json.loads(args.profile.read_text(encoding="utf-8"))
        spec.update(goal=args.request, work_root=str(root))
        project_entry.validate_spec(dict(spec))
        reply = secretary_adapter.reply(args.secretary, root, args.model,
            "Draft a concise task objective in the user's language. Preserve the request "
            "and its limits. Return the objective only. Use no tools.\n" + args.request)
        spec["goal"] = args.request + "\n\nSecretary draft:\n" + reply["response"].strip()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "spec.json"
            path.write_text(json.dumps(spec), encoding="utf-8")
            plan = project_entry.prepare(path, root)
        print(json.dumps({"task": plan["derived"]["task_id"], "goal": spec["goal"],
                          "allowed_paths": spec["allowed_paths"], "dispatched": False},
                         ensure_ascii=True))
        return 0
    except (ValueError, TypeError, OSError, sqlite3.Error, ProbeError, project_entry.EntryError,
            subprocess.TimeoutExpired) as exc:
        print(str(exc), file=sys.stderr)
        return 2
