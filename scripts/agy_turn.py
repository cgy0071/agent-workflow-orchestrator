"""Send a single Gemini prompt through the installed agy CLI."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile

from harness_probe import ProbeError, binary, environment


def turn(model, prompt):
    with tempfile.TemporaryDirectory(prefix="awo-agy-") as folder:
        result = subprocess.run(
            [binary("agy"), "--model", model, "--sandbox", "--disable-slash-commands",
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
            "response": response, "usage": reply.get("usage")}


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
