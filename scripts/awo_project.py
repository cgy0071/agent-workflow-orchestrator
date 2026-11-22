"""Windows project commands. Each operation uses its existing project authority."""
import sys

COMMANDS = {
    "role": ("project_roles", "Manage project-scoped roles and inspect authorization"),
    "retrieve": ("project_retrieve", "Read authorized project documents with a selected backend"),
    "probe": ("harness_probe", "Check a local Claude or Codex harness"),
    "inventory": ("kb_cli", "Read the authenticated task knowledge inventory"),
    "task": ("project_entry", "Prepare, run, inspect or recover a project task"),
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
