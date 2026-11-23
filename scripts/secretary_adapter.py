"""Select the secretary from a built-in harness or a local command profile."""
import json
from pathlib import Path
import subprocess
import tempfile

import agy_turn


def options(parser):
    parser.add_argument("--secretary", default="agy", help="agy or a local JSON command profile")
    parser.add_argument("--model", help="Model ID (optional for a command profile)")


def command(profile, action, root, model):
    config = json.loads(Path(profile).read_text(encoding="utf-8-sig"))
    args = config.get(action) if isinstance(config, dict) else None
    if not isinstance(args, list) or not args or not all(isinstance(x, str) and x for x in args):
        raise ValueError("Secretary profile requires a command array for " + action)
    return [x.replace("{thread}", str(root)).replace("{model}", model or config.get("model", ""))
            for x in args]


def reply(profile, root, model, prompt):
    if profile == "agy":
        return agy_turn.secretary_turn(root, model or "gemini-3.8-flash-low", prompt)
    argv = command(profile, "turn", root, model)
    with tempfile.TemporaryDirectory(prefix="maid-secretary-") as cwd:
        proc = subprocess.run(argv, input=prompt, encoding="utf-8", cwd=cwd,
                              capture_output=True, timeout=120)
    if proc.returncode or not proc.stdout.strip():
        raise ValueError("Secretary returned no usable reply (exit %s)." % proc.returncode)
    return {"response": proc.stdout.strip()}
