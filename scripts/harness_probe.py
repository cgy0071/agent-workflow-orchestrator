"""Probe an installed harness without passing it project files or project tasks."""

import argparse
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile
import threading


DS_MODEL = "ccs-claude-kedaya-ai-ds-2--deepseek-flash"
CLI_BUDGET_USD = 0.05


class ProbeError(Exception):
    """Safe-to-display diagnostic; never include raw CLI/provider output."""


def binary(name):
    if os.name == "nt":
        found = shutil.which(name + ".exe")
        if not found and name == "claude" and shutil.which("claude.cmd"):
            launcher = Path(shutil.which("claude.cmd"))
            candidate = launcher.parent / "node_modules/@anthropic-ai/claude-code/bin/claude.exe"
            found = str(candidate) if candidate.is_file() else None
    else:
        found = shutil.which(name)
    if not found:
        raise ProbeError("Install the native " + name + " CLI and put it on PATH.")
    return str(Path(found).resolve())


def environment():
    keys = ("SystemRoot", "WINDIR", "PATH", "TEMP", "TMP", "HOME", "USERPROFILE",
            "LOCALAPPDATA", "APPDATA", "PROGRAMDATA", "PATHEXT", "COMSPEC")
    return {k: os.environ[k] for k in keys if k in os.environ}


def check_reply(reply, marker):
    if not isinstance(reply, dict) or reply.get("type") != "result" or reply.get("is_error") is not False:
        raise ProbeError("Claude returned no successful final reply.")
    if reply.get("result", "").strip() != marker or set(reply.get("modelUsage") or {}) != {DS_MODEL}:
        raise ProbeError("Reply or provider/model identity did not match the probe.")
    cost = reply.get("total_cost_usd")
    if type(cost) not in (int, float) or not math.isfinite(cost) or not 0 <= cost <= CLI_BUDGET_USD:
        raise ProbeError("CLI cost was unavailable or exceeded its configured limit.")
    return {"harness": "claude", "status": "live_marker_echoed", "model": DS_MODEL,
            "cli_estimated_usd": cost, "cli_budget_usd": CLI_BUDGET_USD}


def claude_probe(cwd):
    exe = binary("claude")
    env = environment()
    for key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    if not env.get("ANTHROPIC_BASE_URL") or not (
            env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")):
        raise ProbeError("Configure the Kedaya Claude route in your environment first.")
    env.update({"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "128", "MAX_THINKING_TOKENS": "0",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "CLAUDE_CONFIG_DIR": str(cwd.parent / "claude")})
    marker = "AWO-" + secrets.token_hex(12)
    cmd = [exe, "--bare", "--print", "--model", DS_MODEL, "--output-format", "json",
           "--tools", "", "--strict-mcp-config", "--disable-slash-commands",
           "--no-session-persistence", "--permission-mode", "dontAsk",
           "--max-budget-usd", str(CLI_BUDGET_USD), "--max-turns", "1",
           "--system-prompt", "Reply only with the exact marker from the user."]
    result = subprocess.run(cmd, input=marker, text=True, encoding="utf-8", cwd=cwd,
                            env=env, capture_output=True, timeout=60)
    if result.returncode:
        raise ProbeError("Claude CLI invocation failed (exit %s)." % result.returncode)
    return check_reply(json.loads(result.stdout), marker)


def codex_probe(cwd):
    exe = binary("codex")
    env = environment()
    env["CODEX_HOME"] = str(cwd.parent / "codex")
    Path(env["CODEX_HOME"]).mkdir()
    req = {"method": "initialize", "id": secrets.randbelow(2000000000),
           "params": {"clientInfo": {"name": "awo_probe", "title": "AWO probe", "version": "1"}}}
    proc = subprocess.Popen([exe, "app-server", "--listen", "stdio://"], cwd=cwd, env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, encoding="utf-8")
    timer = threading.Timer(15, proc.kill)
    timer.start()
    try:
        proc.stdin.write(json.dumps(req) + "\n")
        proc.stdin.flush()
        for line in proc.stdout:
            response = json.loads(line)
            if response.get("id") != req["id"]:
                continue
            ua = response.get("result", {}).get("userAgent", "")
            version = re.search(r"\b(?:codex[^\s/]*|awo_probe)/(\d+(?:\.\d+)+)", ua, re.I)
            if not ua or "error" in response:
                break
            proc.stdin.write('{"method":"initialized"}\n')
            proc.stdin.flush()
            return {"harness": "codex", "status": "local_handshake_only",
                    "protocol": "app-server initialize", "model_turn_sent": False,
                    "remote_inference": "unverified", "version": version.group(1) if version else None}
        raise ProbeError("Codex did not acknowledge app-server initialize.")
    finally:
        timer.cancel()
        proc.kill()
        proc.communicate()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("harness", choices=("claude", "codex"),
                        help="claude: one budgeted DS marker; codex: local app-server handshake only")
    args = parser.parse_args(argv)
    try:
        with tempfile.TemporaryDirectory(prefix="awo-probe-") as tmp:
            cwd = Path(tmp) / "empty"
            cwd.mkdir()
            result = (claude_probe if args.harness == "claude" else codex_probe)(cwd)
    except ProbeError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}))
        return 1
    except (OSError, ValueError, TypeError, AttributeError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"status": "failed", "error": type(exc).__name__}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
