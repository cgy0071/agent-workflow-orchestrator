"""Send a single Gemini prompt through the installed agy CLI."""
import argparse
import json
from pathlib import Path
import re
import sqlite3
from contextlib import closing
from uuid import UUID
import subprocess
import tempfile

from harness_probe import ProbeError, binary, environment


def turn(model, prompt, conversation=None):
    if not re.fullmatch(r"gemini-[A-Za-z0-9_.-]+", model):
        raise ProbeError("Select a Gemini model ID from agy models.")
    if not prompt.strip() or prompt.startswith("-"):
        raise ProbeError("Provide a nonempty prompt starting with text.")
    resume = ["--conversation", str(UUID(conversation))] if conversation else []
    with tempfile.TemporaryDirectory(prefix="awo-agy-") as folder:
        result = subprocess.run(
            [binary("agy"), *resume, "--model", model, "--sandbox", "--disable-slash-commands",
             "--print-timeout", "45s", "--output-format", "json", "--print", prompt],
            cwd=Path(folder), env=environment(), stdin=subprocess.DEVNULL,
            capture_output=True, encoding="utf-8", timeout=55)
    if result.returncode:
        raise ProbeError("agy exited with code %s." % result.returncode)
    reply = json.loads(result.stdout)
    if not isinstance(reply, dict) or reply.get("status") != "SUCCESS":
        raise ProbeError("agy returned no successful final reply.")
    response = reply.get("response")
    if not isinstance(response, str) or not response.strip():
        raise ProbeError("agy returned an empty reply.")
    return {"harness": "agy", "requested_model": model, "reported_model": reply.get("model"),
            "conversation_id": reply.get("conversation_id"), "response": response, "usage": reply.get("usage")}


def secretary_turn(root, model, prompt):
    # Task preparation requires a new work_root. Keep chat state beside it.
    root.parent.mkdir(parents=True, exist_ok=True)
    path = root.with_name("." + root.name + ".agy.sqlite3")
    with closing(sqlite3.connect(path, timeout=1)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE IF NOT EXISTS session (model TEXT, conversation TEXT)")
        row = db.execute("SELECT model, conversation FROM session").fetchone()
        if row and row[0] != model:
            raise ProbeError("Reopen this thread with its original model: " + row[0])
        reply = turn(model, prompt, row[1] if row else None)
        handle = reply.get("conversation_id")
        if not isinstance(handle, str) or str(UUID(handle)) != handle or (row and row[1] != handle):
            raise ProbeError("agy returned an invalid or changed conversation ID.")
        if not row:
            db.execute("INSERT INTO session VALUES (?, ?)", (model, handle))
    return reply


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("harness", choices=["agy"])
    parser.add_argument("--model", required=True, help="Gemini model ID from agy models")
    parser.add_argument("--prompt", required=True)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"gemini-[A-Za-z0-9_.-]+", args.model):
        parser.error("Select a Gemini model ID from agy models.")
    if not args.prompt.strip() or args.prompt.startswith("-"):
        parser.error("Provide a nonempty prompt starting with text.")
    try:
        output = turn(args.model, args.prompt)
    except (ProbeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"status": "failed", "error": str(exc) if isinstance(exc, ProbeError)
                          else type(exc).__name__}))
        return 1
    print(json.dumps(output))
    return 0
