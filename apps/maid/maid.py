"""MAID secretary commands for the current terminal."""
import argparse
from contextlib import closing
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
from uuid import UUID

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def direct(argv):
    parser = argparse.ArgumentParser(description="Open the existing secretary conversation here.")
    parser.add_argument("--thread", type=Path, required=True)
    root = parser.parse_args(argv).thread.resolve()
    from harness_probe import binary, ProbeError
    try:
        path = root.with_name("." + root.name + ".agy.sqlite3")
        with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=1)) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT model, conversation FROM session").fetchone()
            if not row or not re.fullmatch(r"gemini-[A-Za-z0-9_.-]+", row[0]):
                raise ValueError("Missing Gemini session.")
            with tempfile.TemporaryDirectory(prefix="maid-direct-") as cwd:
                return subprocess.call([binary("agy"), "--sandbox", "--model", row[0],
                                        "--conversation", str(UUID(row[1]))], cwd=cwd)
    except (ValueError, OSError, sqlite3.Error, ProbeError) as exc:
        print("Cannot open the secretary conversation: " + str(exc), file=sys.stderr)
        return 2


def main():
    sys.path.insert(0, str(SCRIPTS))
    args = sys.argv[1:]
    if args[:1] == ["direct"]:
        return direct(args[1:])
    if args[:1] == ["request"]:
        import secretary_request
        return secretary_request.main(args[1:])
    if args in ([], ["--help"], ["-h"]):
        print("MAID: request --help | direct --help | --thread PATH submit/start/status/chat")
        return 0
    return subprocess.call([sys.executable, "-B", str(SCRIPTS / "maid.py"), *args])


if __name__ == "__main__":
    raise SystemExit(main())
