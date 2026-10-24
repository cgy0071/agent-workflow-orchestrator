"""Command/Receipt bridge for native CLI jobs. No Herdr dependency or session guessing."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

from monitor_task import read_json, save_json
from native_events import extract
import run_job
from workflow_evidence import task_key


def cli_prefix(executable):
    path = Path(executable).resolve()
    if path.suffix.lower() in (".cmd", ".bat"):
        # Avoid cmd.exe reinterpretation of prompts. Resolve the inspected Node
        # launchers and continue with an argv array, never shell=True.
        candidates = [path.parent / "pi-launcher.js", path.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"]
        script = next((item for item in candidates if item.is_file()), None)
        node = shutil.which("node")
        if not script or not node: raise ValueError("unverified Windows native launcher")
        return [node, str(script)]
    return [str(path)]


def selection(harness, provider=None, model=None):
    if harness != "pi" and (provider is not None or model is not None):
        raise ValueError("provider/model overrides currently require Pi")
    for value in (provider, model):
        if value is not None and (not isinstance(value, str) or not value.strip() or value.startswith("-")
                                  or any(ord(char) < 32 for char in value)):
            raise ValueError("provider/model must be nonempty CLI values")
    if provider is not None and model is None:
        raise ValueError("Pi --provider requires --model")
    return {"provider": provider, "model": model}


def initialize(directory, harness, executable, mode="one-shot", timeout=45, provider=None, model=None):
    if harness not in ("codex", "pi") or mode not in ("one-shot", "session"):
        raise ValueError("unsupported native harness or execution mode")
    from monitor_task import number
    number(timeout, "native test timeout", maximum=3600)
    requested = selection(harness, provider, model)
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=False)
    identity = {"kind": harness, "terminal": "native-bridge-" + uuid.uuid4().hex, "launch_cwd": str(root)}
    config = {"harness": harness, "executable": str(Path(executable).resolve()), "mode": mode,
              "timeout_seconds": timeout, "identity": identity, "native_session_id": None, **requested}
    save_json(root / "config.json", config)
    script = str(Path(__file__).resolve())
    base = [sys.executable, "-B", script, "--worker", str(root)]
    return {"adapter": "command", "harness": harness, "expected_identity": identity,
            "status_argv": base + ["status"], "identity_argv": base + ["status"],
            "output_argv": base + ["output"], "prompt_argv": base + ["send", "--message", "{message}"]}


def status(directory):
    root = Path(directory)
    config = read_json(root / "config.json")
    active = read_json(root / "active.json") if (root / "active.json").exists() else None
    result = {"identity": config["identity"], "status": "idle", "completion_seq": 0,
              "execution_semantics": config["mode"], "identity_origin": "bridge",
              "native_session_id": config.get("native_session_id")}
    if active:
        result.update(task_id=active["task_id"], nonce=active["nonce"], completion_seq=active["baseline"])
        job = Path(active["directory"])
        receipt = read_json(job / "receipt" / "status.json") if (job / "receipt" / "status.json").exists() else {}
        result.update(status="working", execution_started=bool(receipt.get("pid")), receipt_status=receipt.get("status"),
                      receipt_path=str(job / "receipt" / "status.json"), output_path=str(job / "receipt" / "output.log"))
        if (job / "outcome.json").exists():
            outcome = read_json(job / "outcome.json")
            result.update(status="idle" if outcome["status"] == "done" else outcome["status"],
                          completion_seq=active["baseline"] + (1 if outcome["status"] == "done" else 0),
                          native_session_id=outcome.get("native_session_id"))
    return result


def argv_for(config, payload, root):
    executable = config.get("argv_prefix") or cli_prefix(config["executable"])
    if config["harness"] == "codex":
        if config["mode"] == "session" and config.get("native_session_id"):
            return executable + ["exec", "resume", config["native_session_id"], "--json", "--skip-git-repo-check", payload["prompt"]]
        argv = executable + ["exec", "--json", "--sandbox", "read-only", "--skip-git-repo-check"]
        if config["mode"] == "one-shot": argv.append("--ephemeral")
        return argv + [payload["prompt"]]
    argv = executable + ["--print", "--mode", "json", "--no-tools", "--no-extensions", "--no-mcp",
            "--no-skills", "--no-prompt-templates", "--no-context-files", "--offline"]
    for key, value in selection("pi", config.get("provider"), config.get("model")).items():
        if value is not None: argv += ["--" + key, value]
    if config["mode"] == "one-shot": argv.append("--no-session")
    elif config.get("native_session_id"):
        # Pi groups --session-id lookups by cwd, and each task has its own cwd.
        # Resume by the exact session file instead of silently starting empty.
        argv += ["--session", str(pi_session_file(root, config["native_session_id"]))]
    else:
        session = str(uuid.UUID(hex=config["identity"]["terminal"].removeprefix("native-bridge-")))
        argv += ["--session-dir", str(Path(root) / "sessions"), "--session-id", session]
    return argv + [payload["prompt"]]


def pi_session_file(root, session):
    matches = sorted((Path(root) / "sessions").glob("*_" + session + ".jsonl"))
    if len(matches) != 1:
        raise ValueError("Pi session " + session + " needs exactly one session file; found " + str(len(matches)))
    return matches[0].resolve()


def send(directory, payload):
    root = Path(directory).resolve()
    for key in ("task_id", "nonce", "cwd", "prompt"):
        if not isinstance(payload.get(key), str) or not payload[key].strip(): raise ValueError("missing " + key)
    if payload.get("authorization", {}).get("source") is None:
        raise ValueError("native job needs explicit authorization source")
    lock = root / "send.lock"
    with lock.open("x", encoding="ascii") as stream: stream.write(str(os.getpid()))
    try:
        current = status(root)
        if current["status"] != "idle": raise ValueError("native worker still occupied or uncertain")
        config = read_json(root / "config.json")
        argv = argv_for(config, payload, root)
        directory = root / "jobs" / task_key(payload)
        directory.mkdir(parents=True, exist_ok=False)
        job = dict(payload, argv=argv, identity=config["identity"],
                   output_dir=str(directory / "receipt"), timeout_seconds=config["timeout_seconds"],
                   completion_contract={"type": "native_turn", "harness": config["harness"]})
        save_json(directory / "job.json", job)
        save_json(root / "active.json", {"task_id": payload["task_id"], "nonce": payload["nonce"],
                                       "directory": str(directory), "baseline": current["completion_seq"]})
        with (directory / "runner.log").open("wb") as log:
            proc = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "--worker", str(root),
                                     "run", "--job", str(directory / "job.json")], stdout=log, stderr=log,
                                    creationflags=0x08000000 if os.name == "nt" else 0)
        return {"status": "submitted", "runner_pid": proc.pid}
    finally:
        lock.unlink(missing_ok=True)


def run(directory, job_path):
    root, job = Path(directory), read_json(job_path)
    config = read_json(root / "config.json")
    target = Path(job_path).parent
    try:
        receipt = run_job.run(job)
        turn = receipt.get("native_turn", {})
        if turn.get("status") != "completed":
            raise ValueError(turn.get("error", "native turn did not complete"))
        text, session = turn["text"], turn["native_session_id"]
        # Preserve a completed answer even when process cleanup remains blocked.
        # The blocked outcome below still prevents review/continuation/reuse.
        output = Path(job["cwd"])
        (output / "RESULT.md").write_text(text + "\n", encoding="utf-8")
        binding = job["task_id"] + " " + job["nonce"] + "\n" + job.get("marker", "") + "\n"
        (output / "HANDOFF.md").write_text(binding, encoding="utf-8")
        if receipt["status"] != "done":
            raise ValueError("native turn completed; process termination still requires inspection")
        if config["mode"] == "session":
            if not session: raise ValueError("session mode requires a native session identifier")
            config["native_session_id"] = session
            save_json(root / "config.json", config)
        outcome = {"status": "done", "native_session_id": session, "execution_semantics": config["mode"],
                   "turn_status": turn["status"], "process_status": receipt["process_status"]}
    except Exception as exc:
        receipt_file = target / "receipt" / "status.json"
        saved = read_json(receipt_file) if receipt_file.exists() else {}
        outcome = {"status": "blocked" if saved.get("status") == "blocked" else "failed", "error": str(exc),
                   "execution_semantics": config["mode"], "turn_status": saved.get("native_turn", {}).get("status", "unverified"),
                   "process_status": saved.get("process_status", "unverified")}
    save_json(target / "outcome.json", outcome)
    return outcome


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status"); sub.add_parser("output")
    submit = sub.add_parser("send"); submit.add_argument("--message", required=True)
    execute = sub.add_parser("run"); execute.add_argument("--job", required=True)
    args = parser.parse_args()
    if args.command == "status": result = status(args.worker)
    elif args.command == "send": result = send(args.worker, json.loads(args.message))
    elif args.command == "run": result = run(args.worker, args.job)
    else:
        active = read_json(Path(args.worker) / "active.json")
        print((Path(active["directory"]) / "receipt" / "output.log").read_text(encoding="utf-8", errors="replace"))
        return
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
