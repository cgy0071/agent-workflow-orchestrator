"""Task-bound status monitoring and one-time reviewer notification (stdlib only)."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def save_json(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + "." + str(os.getpid()) + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def norm_path(value):
    text = str(value).replace("\\", "/").rstrip("/")
    return text.casefold() if os.name == "nt" or (len(text) > 1 and text[1] == ":") else text


def identity_matches(actual, expected):
    if not expected:
        return False
    return all((norm_path(actual.get(k, "")) == norm_path(v) if k.endswith("cwd")
                else actual.get(k) == v) for k, v in expected.items())


def checked_artifact(cwd, item):
    item = {"path": item} if isinstance(item, str) else item
    path = (cwd / item["path"]).resolve()
    path.relative_to(cwd.resolve())  # Reject traversal and escaping symlinks.
    if not path.is_file() or (item.get("nonempty", True) and path.stat().st_size == 0):
        raise ValueError("missing/empty artifact: " + str(path))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if item.get("sha256") and digest != item["sha256"]:
        raise ValueError("artifact hash mismatch: " + str(path))
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": digest}


class Transport:
    def __init__(self, request):
        self.request = request
        self.timeout = float(request.get("monitor", {}).get("command_timeout_seconds", 15))

    def run(self, argv, cwd=None):
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
            raise ValueError("command must be a nonempty argv list")
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, encoding="utf-8",
                              errors="replace", timeout=self.timeout, shell=False)
        if proc.returncode:
            raise RuntimeError("adapter command exited " + str(proc.returncode) + ": " + proc.stderr[:300])
        return proc.stdout

    def herdr(self, spec, *args):
        return self.run([spec["executable"], "--session", spec["session"], "agent", *args])

    def status(self, spec):
        adapter = spec["adapter"]
        if adapter == "herdr":
            agent = json.loads(self.herdr(spec, "get", spec["pane"]))["result"]["agent"]
            return {"identity": {"kind": agent.get("agent"), "terminal": agent.get("terminal_id"),
                                 "launch_cwd": agent.get("cwd"), "pane": agent.get("pane_id")},
                    "status": agent.get("agent_status"), "completion_seq": agent.get("completion_seq", 0)}
        if adapter == "command":
            return json.loads(self.run(spec["status_argv"]))
        if adapter == "receipt":
            return read_json(spec["status_path"])
        raise ValueError("unsupported status adapter: " + adapter)

    def output(self, spec):
        if spec["adapter"] == "herdr":
            return self.herdr(spec, "read", spec["pane"], "--source", "recent-unwrapped", "--lines", "100")
        if spec["adapter"] == "command":
            return self.run(spec["output_argv"])
        with Path(spec["output_path"]).open("rb") as stream:
            stream.seek(max(0, stream.seek(0, 2) - 65536))
            return stream.read().decode("utf-8", errors="replace")

    def reviewer_identity(self, spec):
        if spec["adapter"] == "herdr":
            return self.status(spec)["identity"]
        if spec["adapter"] == "command":
            return json.loads(self.run(spec["identity_argv"]))["identity"]
        raise ValueError("reviewer needs a verified herdr or command adapter")

    def send(self, spec, message):
        if spec["adapter"] == "herdr":
            return self.herdr(spec, "prompt", spec["pane"], message)
        argv = spec["prompt_argv"]
        if argv.count("{message}") != 1:
            raise ValueError("prompt_argv needs one standalone {message} argument")
        return self.run([message if x == "{message}" else x for x in argv])


def validate_request(request):
    for key in ("task_id", "nonce", "cwd", "worker", "reviewer", "completion", "authorization"):
        if key not in request:
            raise ValueError("missing request field: " + key)
    if not request["task_id"] or not request["nonce"] or not request.get("dispatch_confirmed"):
        raise ValueError("task identity and confirmed dispatch required")
    worker = request["worker"]
    if not worker.get("expected_identity") or worker.get("baseline_completion_seq") is None:
        raise ValueError("freeze worker identity and pre-dispatch completion sequence")
    if not request["reviewer"].get("expected_identity"):
        raise ValueError("freeze reviewer identity")
    if not request["completion"].get("binding_artifact"):
        raise ValueError("task/nonce binding artifact required")
    monitor = request.get("monitor", {})
    if not 1 <= float(monitor.get("interval_seconds", 300)) <= 86400:
        raise ValueError("invalid polling interval")
    if not 0 < float(monitor.get("command_timeout_seconds", 15)) <= 3600:
        raise ValueError("invalid command timeout")
    return request


def inspect_once(request, transport, idle_since=None, now=None):
    now = time.monotonic() if now is None else now
    worker = request["worker"]
    state = transport.status(worker)
    if not identity_matches(state.get("identity", {}), worker["expected_identity"]):
        return "identity_changed", "Worker identity changed; no retargeting.", idle_since, None
    if worker["adapter"] == "receipt" and (state.get("task_id") != request["task_id"]
                                            or state.get("nonce") != request["nonce"]):
        return "identity_changed", "Receipt belongs to another task/nonce.", idle_since, None
    status = request.get("status_map", {}).get(state.get("status"), state.get("status"))
    if status == "working":
        return "working", "", None, None
    if status in ("blocked", "exited", "failed"):
        return status, "Worker requires inspection; no approval answered or task resent.", idle_since, None
    if status not in ("idle", "done"):
        return "unknown_status", "Adapter returned unrecognized status.", idle_since, None
    idle_since = now if idle_since is None else idle_since
    detail = "No fresh completion sequence."
    if int(state.get("completion_seq", 0)) > int(worker["baseline_completion_seq"]):
        try:
            cwd = Path(request["cwd"])
            completion = request["completion"]
            artifacts = [checked_artifact(cwd, x) for x in completion.get("artifacts", [])]
            binding = checked_artifact(cwd, completion["binding_artifact"])
            text = Path(binding["path"]).read_text(encoding="utf-8-sig")
            if request["task_id"] not in text or request["nonce"] not in text:
                raise ValueError("binding artifact lacks task ID/nonce")
            marker = completion.get("marker")
            if marker and marker not in transport.output(worker):
                raise ValueError("completion marker missing from worker output")
            receipt = {"task_id": request["task_id"], "nonce": request["nonce"],
                       "completion_seq": state["completion_seq"], "artifacts": artifacts,
                       "binding_artifact": binding}
            return "review_ready", "Task-bound delivery received; independent review required.", idle_since, receipt
        except (OSError, ValueError) as exc:
            detail = str(exc)
    grace = float(request.get("monitor", {}).get("settle_grace_seconds", 120))
    if now - idle_since >= grace:
        return "incomplete", detail, idle_since, None
    return "settling", detail, idle_since, None


def notify_once(request, report, directory, transport):
    authorization = request["authorization"]
    if not authorization.get("notify_reviewer") or not authorization.get("source"):
        return {"status": "not_authorized"}
    attempt = directory / "notification_attempt.json"
    try:
        with attempt.open("x", encoding="utf-8") as stream:
            json.dump({"task_id": request["task_id"], "nonce": request["nonce"],
                       "status": "reserved_no_blind_retry"}, stream)
    except FileExistsError:
        return {"status": "already_attempted"}
    try:
        reviewer = request["reviewer"]
        if not identity_matches(transport.reviewer_identity(reviewer), reviewer["expected_identity"]):
            result = {"status": "reviewer_identity_unverified"}
        else:
            message = ("Automatic task-status notification under existing Owner authorization; "
                       "this is not a new Owner instruction. Task " + request["task_id"] + ", nonce " +
                       request["nonce"] + ", status " + report["status"] + ". Read the frozen request "
                       "and monitor evidence at " + str(directory) + ". If review_ready, independently "
                       "review delivery, changes and acceptance evidence; record accept/repair/reject "
                       "or missing evidence, then follow the configured workflow within existing "
                       "authorization. Otherwise inspect the specific anomaly. Never treat idle as "
                       "success, answer approval dialogs, or blindly resend. One notification only.")
            reply = transport.send(reviewer, message)
            result = {"status": "submitted", "response": reply[:2000]}
    except Exception as exc:
        result = {"status": "delivery_uncertain", "error": str(exc)[:500]}
    save_json(directory / "notification_result.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--check", action="store_true", help="validate local request without contacting agents")
    parser.add_argument("--notify", action="store_true", help="send once only if request authorization permits")
    args = parser.parse_args()
    path = Path(args.request).resolve()
    request = validate_request(read_json(path))
    if args.check:
        print(json.dumps({"task_id": request["task_id"], "status": "REQUEST_VALID"}))
        return
    directory = path.parent
    lock = directory / "monitor.lock"
    with lock.open("x", encoding="ascii") as stream:
        stream.write(str(os.getpid()))
    try:
        final = directory / "monitor_summary.json"
        if final.exists():
            previous = read_json(final)
            if previous.get("task_id") != request["task_id"] or previous.get("nonce") != request["nonce"]:
                raise ValueError("monitor directory belongs to another task")
            if args.notify and previous["status"] != "monitoring_stopped":
                notify_once(request, previous, directory, Transport(request))
            print(json.dumps(previous, ensure_ascii=False))
            return
        transport = Transport(request)
        started = time.monotonic()
        idle_since = None
        config = request.get("monitor", {})
        while True:
            try:
                if (directory / "STOP_MONITOR").exists():
                    status, detail, receipt = "monitoring_stopped", "Monitor stopped; worker not cancelled.", None
                else:
                    status, detail, idle_since, receipt = inspect_once(request, transport, idle_since)
            except Exception as exc:
                status, detail, receipt = "adapter_error", str(exc)[:500], None
            report = {"task_id": request["task_id"], "nonce": request["nonce"],
                      "status": status, "anomaly_summary": detail}
            if receipt:
                save_json(directory / "completion_receipt.json", receipt)
            limit = config.get("overall_timeout_seconds")
            if status in ("working", "settling") and limit is not None and time.monotonic() - started >= float(limit):
                report.update(status="monitor_timeout", anomaly_summary="Configured monitor limit reached; task not cancelled or accepted.")
            save_json(directory / "monitor_status.json", report)
            print(json.dumps(report, ensure_ascii=False), flush=True)
            if report["status"] not in ("working", "settling"):
                save_json(final, report)
                if args.notify and report["status"] != "monitoring_stopped":
                    notify_once(request, report, directory, transport)
                return
            time.sleep(float(config.get("interval_seconds", 300)))
    finally:
        lock.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
