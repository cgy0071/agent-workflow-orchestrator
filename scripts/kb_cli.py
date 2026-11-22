"""Run the authenticated project's knowledge inventory from a Windows terminal."""
import argparse
import json
import os
import sys

from mcp_gateway import ServiceBackend, read_token_file, safe_error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["inventory"])
    parser.add_argument("--state", required=True)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--token-file", default=os.environ.get("AWO_AGENT_TOKEN_FILE"))
    parser.add_argument("--variant", action="append", default=[])
    args = parser.parse_args(argv)
    if not args.token_file:
        parser.error("supply --token-file or set AWO_AGENT_TOKEN_FILE")
    try:
        token = read_token_file(args.token_file)
        backend = ServiceBackend(args.state, args.project_root, extensions_enabled=True)
        report = backend.call(token, "kb.inventory", {"task_id": args.task, "variants": args.variant})
    except (Exception, KeyboardInterrupt) as exc:
        print("kb inventory: " + safe_error(exc).code, file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
