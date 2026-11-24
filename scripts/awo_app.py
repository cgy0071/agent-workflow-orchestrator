"""Public application CLI for local clients of AWO."""
import argparse
import json
from pathlib import Path
import sys


def profile(argv):
    from harness_probe import binary, ProbeError
    from project_entry import EntryError, validate_spec

    parser = argparse.ArgumentParser(prog="awo_app.py profile",
                                     description="Create a project profile without starting a model.")
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--thread", required=True, type=Path)
    parser.add_argument("--allow", action="append", required=True)
    parser.add_argument("--verify-script", required=True)
    parser.add_argument("--profile", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        repo = args.project.resolve()
        script = (repo / args.verify_script).resolve()
        if not script.is_relative_to(repo) or not script.is_file():
            parser.error("Verification script must exist inside the project.")
        bridge = dict(harness="codex", executable=binary("codex"), timeout_seconds=600)
        spec = dict(version=1, repository=str(repo), goal="Prepare a task", allowed_paths=args.allow,
                    verify_argv=[sys.executable, "-B", str(script.relative_to(repo))],
                    worker=bridge, reviewer=bridge,
                    authorization=dict(source="Local project profile", dispatch_worker=True,
                                       allow_local_land=True),
                    budget={"tokens.input": 40000, "tokens.output": 8000},
                    target_ref="refs/heads/maid-" + args.thread.name, work_root=str(args.thread.resolve()),
                    change_name="project-task", max_real_turns=12, timeout_seconds=300)
        validate_spec(dict(spec))
        data = json.dumps(spec, indent=2, ensure_ascii=False)
        with args.profile.open("x", encoding="utf-8") as output:
            output.write(data)
    except FileExistsError:
        parser.error("Profile already exists; choose a new output file.")
    except (ProbeError, EntryError, OSError, ValueError) as exc:
        parser.error(str(exc))
    print("Profile ready:", args.profile.resolve())
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("capabilities", "profile", "maid"))
    operation = parser.parse_args(argv[:1]).operation
    if operation == "capabilities":
        argparse.ArgumentParser(prog="awo_app.py capabilities").parse_args(argv[1:])
        print(json.dumps({"protocol": 1, "capabilities": ["profile", "maid"]}))
        return 0
    if operation == "profile":
        return profile(argv[1:])
    from maid import main as maid_main
    return maid_main(argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
