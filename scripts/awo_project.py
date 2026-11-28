"""Windows project commands. Each operation uses its existing project authority."""
import sys

COMMANDS = {
    "materials": ("material_packets", "Search authorized document and task-source excerpts"),
    "schedule": ("project_schedule", "Queue, dispatch and inspect tasks in a shared project ledger"),
    "domain": ("project_domain", "Resolve a bound project identity across source revisions"),
    "role": ("project_roles", "Manage project-scoped roles and inspect authorization"),
    "retrieve": ("project_retrieve", "Read authorized project documents with a selected backend"),
    "probe": ("harness_probe", "Check a local Claude, Codex or agy harness"),
    "ask": ("agy_turn", "Send one prompt through agy"),
    "inventory": ("kb_cli", "Read the authenticated task knowledge inventory"),
    "task": ("project_entry", "Prepare, run, inspect or recover a project task"),
    "discover-cli": ("cli_lanes", "Discover installed local CLIs and manage named lanes"),
}


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("--help", "-h"):
        print("Usage: python scripts/awo_project.py COMMAND [OPTIONS]\n")
        for name, (_, description) in COMMANDS.items():
            print("  %-12s%s" % (name, description))
        print("\nFor command options, use COMMAND --help.")
        return 0
    command = args.pop(0)
    if command not in COMMANDS:
        print("Unknown project command: " + command, file=sys.stderr)
        return 2
    from importlib import import_module
    module = import_module(COMMANDS[command][0])
    return module.main((["inventory"] if command == "inventory" else []) + args)


if __name__ == "__main__":
    raise SystemExit(main())
