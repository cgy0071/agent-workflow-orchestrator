"""Task-bound status monitoring and one-time reviewer notification (stdlib only)."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
from pathlib import Path
import subprocess
import time
import uuid
from datetime import datetime, timezone


class AdapterResponseError(ValueError):
    """An invalid read-only response; safe to query again within a finite budget."""


def sequence(value):
    if type(value) is not int or value < 0:
        raise AdapterResponseError("completion sequence must be a nonnegative JSON integer")
    return value


def number(value, name, minimum=0, inclusive=False, maximum=None):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(name + " must be a finite number, not a boolean or string")
    if value < minimum or (value == minimum and not inclusive) or (maximum is not None and value > maximum):
        raise ValueError("invalid " + name)
    return value


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def object_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False).encode()).hexdigest()


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


def mapped_status(request, state):
    """Use the same explicit adapter labels for monitoring and dispatch admission."""
    mapping = request.get("status_map", {})
    if not isinstance(mapping, dict) or any(not isinstance(k, str) or not k or not isinstance(v, str) or not v
                                            for k, v in mapping.items()):
        raise ValueError("status_map must map nonempty status strings to nonempty strings")
    return mapping.get(state.get("status"), state.get("status"))


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
            identity = {"kind": agent.get("agent"), "terminal": agent.get("terminal_id"),
                        "launch_cwd": agent.get("cwd"), "pane": agent.get("pane_id")}
            # Copy real metadata only. Do not synthesize thread/branch identity.
            for key in ("conversation_id", "branch_id"):
                if agent.get(key): identity[key] = agent[key]
            source = "completion_seq" if agent.get("completion_seq") is not None else "state_change_seq"
            return {"identity": identity, "status": agent.get("agent_status"),
                    "completion_seq": sequence(agent.get(source)), "sequence_source": source,
                    "prompt_available": bool(agent.get("agent_session"))}
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
            state = self.status(spec)
            if spec.get("context_policy", "inbox_only") != "inbox_only":
                self.check_prompt_available(state)
            return state["identity"]
        if spec["adapter"] == "command":
            return json.loads(self.run(spec["identity_argv"]))["identity"]
        raise ValueError("reviewer needs a verified herdr or command adapter")

    @staticmethod
    def check_prompt_available(state):
        if not state.get("prompt_available"):
            raise ValueError("Herdr target exposes no agent_session; direct prompt is unavailable. "
                             "Use context_policy=inbox_only or a verified prompt-capable adapter.")

    def send(self, spec, message):
        if spec["adapter"] == "herdr":
            state = self.status(spec)
            if not identity_matches(state["identity"], spec["expected_identity"]):
                raise ValueError("Herdr target identity changed before prompt")
            self.check_prompt_available(state)
            return self.herdr(spec, "prompt", spec["pane"], message)
        argv = spec["prompt_argv"]
        if argv.count("{message}") != 1:
            raise ValueError("prompt_argv needs one standalone {message} argument")
        return self.run([message if x == "{message}" else x for x in argv])


def validate_identity(identity):
    if not isinstance(identity, dict) or not all(isinstance(v, str) and v.strip() for v in identity.values()):
        raise ValueError("identity must contain nonempty string values")
    if not identity.get("kind") or not identity.get("launch_cwd"):
        raise ValueError("identity needs kind and launch_cwd")
    if not any(identity.get(k) for k in ("terminal", "session_id", "instance_id", "conversation_id", "job_id")):
        raise ValueError("identity needs a unique session, terminal, or job identifier")


def validate_argv(value):
    if not isinstance(value, list) or not value or not all(isinstance(x, str) and x for x in value):
        raise ValueError("adapter command must be a nonempty argv list")


def validate_spec(spec, reviewer=False):
    if not isinstance(spec, dict): raise ValueError("adapter specification must be an object")
    validate_identity(spec.get("expected_identity"))
    adapter = spec.get("adapter")
    if adapter == "herdr":
        for key in ("executable", "session", "pane"):
            if not isinstance(spec.get(key), str) or not spec[key].strip(): raise ValueError("missing " + key)
    elif adapter == "command":
        for key in (("identity_argv", "prompt_argv") if reviewer else ("status_argv", "output_argv")):
            validate_argv(spec.get(key))
        if reviewer and spec["prompt_argv"].count("{message}") != 1:
            raise ValueError("prompt_argv needs one standalone {message}")
    elif adapter == "receipt" and not reviewer:
        for key in ("status_path", "output_path"):
            if not isinstance(spec.get(key), str) or not spec[key]: raise ValueError("missing " + key)
    else: raise ValueError("unsupported adapter")
    policy = spec.get("context_policy", "inbox_only")
    if policy not in ("inbox_only", "exact_thread", "dedicated_window"): raise ValueError("invalid context policy")
    if reviewer and policy == "exact_thread" and not all(spec["expected_identity"].get(k) for k in ("conversation_id", "branch_id")):
        raise ValueError("exact_thread needs pinned real conversation_id and branch_id")
    if reviewer and policy == "dedicated_window" and not spec.get("context_declaration"):
        raise ValueError("declare the exclusive main window before direct notification")


def validate_request(request, require_dispatch=True):
    if not isinstance(request, dict): raise ValueError("request must be an object")
    for key in ("task_id", "nonce", "cwd", "worker", "reviewer", "completion", "authorization"):
        if key not in request:
            raise ValueError("missing request field: " + key)
    if not all(isinstance(request[k], str) and request[k].strip() for k in ("task_id", "nonce", "cwd")):
        raise ValueError("task ID, nonce, and cwd must be nonempty strings")
    if type(request.get("generation", 1)) is not int or request.get("generation", 1) < 1:
        raise ValueError("generation must be a positive integer")
    if type(request.get("dispatch_confirmed")) is not bool or (require_dispatch and not request["dispatch_confirmed"]):
        raise ValueError("task identity and confirmed dispatch required")
    cwd = Path(request["cwd"]).resolve()
    if not cwd.is_dir(): raise ValueError("task cwd does not exist")
    worker = request["worker"]
    validate_spec(worker); validate_spec(request["reviewer"], reviewer=True)
    sequence(worker.get("baseline_completion_seq"))
    auth = request["authorization"]
    if not isinstance(auth, dict) or type(auth.get("notify_reviewer")) is not bool or not isinstance(auth.get("source"), str) or not auth["source"].strip():
        raise ValueError("authorization needs a boolean notify_reviewer and nonempty source")
    if "allow_reviewer_override" in auth and type(auth["allow_reviewer_override"]) is not bool:
        raise ValueError("reviewer override must be a boolean")
    if not isinstance(request["completion"], dict): raise ValueError("completion must be an object")
    if not request["completion"].get("binding_artifact"):
        raise ValueError("task/nonce binding artifact required")
    artifacts = request["completion"].get("artifacts", [])
    if not isinstance(artifacts, list): raise ValueError("artifacts must be an array")
    for item in artifacts + [request["completion"]["binding_artifact"]]:
        row = {"path": item} if isinstance(item, str) else item
        if not isinstance(row, dict) or not isinstance(row.get("path"), str) or not row["path"]:
            raise ValueError("invalid artifact path")
        (cwd / row["path"]).resolve().relative_to(cwd)
        if "nonempty" in row and type(row["nonempty"]) is not bool: raise ValueError("nonempty must be boolean")
    if "marker" in request["completion"] and not isinstance(request["completion"]["marker"], str):
        raise ValueError("completion marker must be a string")
    if request["completion"].get("marker_source", "output") not in ("output", "artifact", "both"):
        raise ValueError("marker_source must be output, artifact, or both")
    mapped_status(request, {})  # Validate optional mapping before any adapter query.
    monitor = request.get("monitor", {})
    if not isinstance(monitor, dict): raise ValueError("monitor must be an object")
    number(monitor.get("interval_seconds", 300), "interval_seconds", 1, True, 86400)
    number(monitor.get("command_timeout_seconds", 15), "command_timeout_seconds", maximum=3600)
    number(monitor.get("settle_grace_seconds", 120), "settle_grace_seconds", 0, True)
    number(monitor.get("retry_interval_seconds", 5), "retry_interval_seconds", maximum=86400)
    number(monitor.get("retry_max_seconds", 60), "retry_max_seconds", maximum=86400)
    sequence(monitor.get("max_consecutive_errors", 3))
    if monitor.get("max_consecutive_errors", 3) < 1: raise ValueError("error budget must be positive")
    if monitor.get("overall_timeout_seconds") is not None:
        number(monitor["overall_timeout_seconds"], "overall_timeout_seconds")
    parent = request.get("parent_review")
    if request.get("parent_task") and not parent: raise ValueError("follow-up needs a sealed parent review")
    if parent:
        raw = Path(parent["path"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != parent["sha256"]: raise ValueError("parent review hash mismatch")
        text = raw.decode("utf-8-sig")
        if not parent["task_id"] in text or not parent["nonce"] in text: raise ValueError("parent review identity mismatch")
        if parent["verdict"] not in request.get("allowed_parent_verdicts", ["accept", "repair"]):
            raise ValueError("parent review does not admit a follow-up")
        if not re.search(r"^Verdict:\s*" + re.escape(parent["verdict"]) + r"\s*$", text, re.M):
            raise ValueError("parent verdict differs from REVIEW.md")
        if request.get("parent_task") and request["parent_task"] != parent["task_id"]:
            raise ValueError("parent task differs from parent review")
    return request


def preflight(request):
    validate_request(request, require_dispatch=False)
    transport = Transport(request)
    state = transport.status(request["worker"])
    if not identity_matches(state.get("identity", {}), request["worker"]["expected_identity"]):
        raise ValueError("worker identity preflight failed")
    current = sequence(state.get("completion_seq"))
    if current != request["worker"]["baseline_completion_seq"]:
        raise ValueError("baseline completion sequence differs from live value " + str(current))
    if not identity_matches(transport.reviewer_identity(request["reviewer"]), request["reviewer"]["expected_identity"]):
        raise ValueError("reviewer identity preflight failed")
    return {"status": "PREFLIGHT_OK", "worker_status": state["status"], "completion_seq": current}


def inspect_once(request, transport, idle_since=None, now=None):
    now = time.monotonic() if now is None else now
    worker = request["worker"]
    state = transport.status(worker)
    if not identity_matches(state.get("identity", {}), worker["expected_identity"]):
        return "identity_changed", "Worker identity changed; no retargeting.", idle_since, None
    if worker["adapter"] == "receipt" and (state.get("task_id") != request["task_id"]
                                            or state.get("nonce") != request["nonce"]):
        return "identity_changed", "Receipt belongs to another task/nonce.", idle_since, None
    status = mapped_status(request, state)
    if status == "working":
        # State-change counters can oscillate while one delivery is pending.
        timer = idle_since if state.get("sequence_source") == "state_change_seq" else None
        return "working", "", timer, None
    if status in ("blocked", "exited", "failed"):
        return status, "Worker requires inspection; no approval answered or task resent.", idle_since, None
    if status not in ("idle", "done"):
        return "unknown_status", "Adapter returned unrecognized status.", idle_since, None
    timer = idle_since if isinstance(idle_since, dict) else {"idle_since": now, "delivery_since": None, "sequence": None}
    detail = "No fresh completion sequence."
    seq = sequence(state.get("completion_seq"))
    fresh = seq > worker["baseline_completion_seq"]
    if fresh:
        if timer["sequence"] is None or (state.get("sequence_source") != "state_change_seq" and timer["sequence"] != seq):
            timer.update(sequence=seq, delivery_since=now)
        try:
            cwd = Path(request["cwd"])
            completion = request["completion"]
            artifacts = [checked_artifact(cwd, x) for x in completion.get("artifacts", [])]
            binding = checked_artifact(cwd, completion["binding_artifact"])
            text = Path(binding["path"]).read_text(encoding="utf-8-sig")
            if request["task_id"] not in text or request["nonce"] not in text:
                raise ValueError("binding artifact lacks task ID/nonce")
            marker = completion.get("marker")
            source = completion.get("marker_source", "output")
            if marker and source in ("artifact", "both") and marker not in text:
                raise ValueError("completion marker missing from binding artifact")
            if marker and source in ("output", "both") and marker not in transport.output(worker):
                raise ValueError("completion marker missing from worker output")
            receipt = {"task_id": request["task_id"], "nonce": request["nonce"],
                       "completion_seq": state["completion_seq"],
                       "sequence_source": state.get("sequence_source", "completion_seq"), "artifacts": artifacts,
                       "binding_artifact": binding}
            return "review_ready", "Task-bound delivery received; independent review required.", timer, receipt
        except (OSError, ValueError) as exc:
            detail = str(exc)
    grace = float(request.get("monitor", {}).get("settle_grace_seconds", 120))
    since = timer["delivery_since"] if fresh else timer["idle_since"]
    if now - since >= grace:
        return "incomplete", detail, timer, None
    return "settling", detail, timer, None


def notify_once(request, report, directory, transport):
    if request.get("workflow_state"):
        from workflow_control import deliver_event, publish_event
        receipt_path = directory / "completion_receipt.json"
        event = publish_event(request["workflow_state"], request, report,
                              read_json(receipt_path) if receipt_path.exists() else None)
        return deliver_event(request["workflow_state"], event, transport)
    authorization = request["authorization"]
    if authorization.get("notify_reviewer") is not True or not authorization.get("source"):
        return {"status": "not_authorized"}
    if request["reviewer"].get("context_policy", "inbox_only") == "inbox_only":
        result = {"status": "inbox_only", "task_id": request["task_id"], "nonce": request["nonce"], "report": report}
        save_json(directory / "pending_event.json", result)
        return result
    attempt = directory / "notification_attempt.json"
    try:
        with attempt.open("x", encoding="utf-8") as stream:
            json.dump({"task_id": request["task_id"], "nonce": request["nonce"],
                       "request_sha256": object_hash(request), "authorization_sha256": object_hash(authorization),
                       "target": request["reviewer"], "attempted_at": utc_now(), "pid": os.getpid(),
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
                       "verify that this is the authorized main context. Side conversations are "
                       "advisory-only: do not acknowledge, accept, or dispatch from a side context. "
                       "review delivery, changes and acceptance evidence; record accept/repair/reject "
                       "or missing evidence, then follow the configured workflow within existing "
                       "authorization. Otherwise inspect the specific anomaly. Never treat idle as "
                       "success, answer approval dialogs, or blindly resend. One notification only.")
            reply = transport.send(reviewer, message)
            result = {"status": "submitted", "response": reply[:2000]}
    except Exception as exc:
        result = {"status": "delivery_uncertain", "error": str(exc)[:500]}
    result.update(task_id=request["task_id"], nonce=request["nonce"], request_sha256=object_hash(request),
                  authorization_sha256=object_hash(authorization), recorded_at=utc_now())
    save_json(directory / "notification_result.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request")
    parser.add_argument("--check-workflow")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--check", action="store_true", help="validate local request without contacting agents")
    parser.add_argument("--notify", action="store_true", help="send once only if request authorization permits")
    parser.add_argument("--resume", action="store_true", help="explicitly resume an inspected terminal monitor with unchanged inputs")
    args = parser.parse_args()
    if args.check_workflow:
        from workflow_control import validate_workflow
        validate_workflow(read_json(args.check_workflow)); print("WORKFLOW_STRUCTURE_OK"); return
    if not args.request: parser.error("--request required")
    path = Path(args.request).resolve()
    request = validate_request(read_json(path), require_dispatch=not (args.check or args.preflight))
    if request.get("workflow_state"):
        from workflow_control import check_request_links
        check_request_links(request["workflow_state"], request)
    if args.preflight:
        print(json.dumps(preflight(request))); return
    if args.check:
        print(json.dumps({"task_id": request["task_id"], "status": "STRUCTURE_OK"}))
        return
    if request.get("workflow_state"):
        from workflow_control import check_registered
        check_registered(request["workflow_state"], request)
    directory = path.parent
    lock = directory / "monitor.lock"
    with lock.open("x", encoding="ascii") as stream:
        stream.write(str(os.getpid()))
    try:
        frozen = directory / "frozen_request.json"
        if frozen.exists() and object_hash(read_json(frozen)) != object_hash(request):
            raise ValueError("request changed since monitoring started")
        if not frozen.exists(): save_json(frozen, request)
        final = directory / "monitor_summary.json"
        if args.resume and final.exists():
            previous = read_json(final)
            if previous.get("task_id") != request["task_id"] or previous.get("nonce") != request["nonce"]:
                raise ValueError("resume identity differs from the original task")
            if previous["status"] == "review_ready": raise ValueError("completed delivery needs review, not monitor resumption")
            save_json(directory / ("monitor_summary.history." + uuid.uuid4().hex + ".json"), previous)
            final.unlink()
        if final.exists():
            previous = read_json(final)
            if previous.get("task_id") != request["task_id"] or previous.get("nonce") != request["nonce"]:
                raise ValueError("monitor directory belongs to another task")
            if request.get("workflow_state"):
                from workflow_control import publish_event
                receipt_file = directory / "completion_receipt.json"
                publish_event(request["workflow_state"], request, previous,
                              read_json(receipt_file) if receipt_file.exists() else None)
            if args.notify and previous["status"] != "monitoring_stopped":
                notify_once(request, previous, directory, Transport(request))
            print(json.dumps(previous, ensure_ascii=False))
            return
        transport = Transport(request)
        checkpoint_path = directory / "monitor_checkpoint.json"
        checkpoint = read_json(checkpoint_path) if checkpoint_path.exists() else {}
        if checkpoint and checkpoint.get("request_sha256") != object_hash(request):
            raise ValueError("checkpoint belongs to another frozen request")
        # Wall-clock deadlines survive a process restart. A clock adjustment may
        # delay observation; it can never authorize cancellation or acceptance.
        started = time.time() if args.resume else checkpoint.get("started_at", time.time())
        idle_since = checkpoint.get("idle_since")
        errors = 0 if args.resume else checkpoint.get("consecutive_errors", 0)
        config = request.get("monitor", {})
        while True:
            try:
                if (directory / "STOP_MONITOR").exists():
                    status, detail, receipt = "monitoring_stopped", "Monitor stopped; worker not cancelled.", None
                else:
                    status, detail, idle_since, receipt = inspect_once(request, transport, idle_since, now=time.time())
                errors = 0
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
                errors += 1
                status = "transient_error" if errors < config.get("max_consecutive_errors", 3) else "adapter_error"
                detail, receipt = str(exc)[:500], None
            except Exception as exc:
                status, detail, receipt = "adapter_error", str(exc)[:500], None
            report = {"task_id": request["task_id"], "nonce": request["nonce"],
                      "status": status, "anomaly_summary": detail, "observed_at": utc_now(),
                      "consecutive_errors": errors, "elapsed_seconds": max(0, time.time() - started)}
            save_json(checkpoint_path, {"request_sha256": object_hash(request), "started_at": started,
                                       "idle_since": idle_since, "consecutive_errors": errors})
            if receipt:
                save_json(directory / "completion_receipt.json", receipt)
            limit = config.get("overall_timeout_seconds")
            if status in ("working", "settling", "transient_error") and limit is not None and time.time() - started >= limit:
                report.update(status="monitor_timeout", anomaly_summary="Configured monitor limit reached; task not cancelled or accepted.")
            save_json(directory / "monitor_status.json", report)
            if request.get("workflow_state"):
                from workflow_scheduler import record_observation
                record_observation(request["workflow_state"], request, report)
            print(json.dumps(report, ensure_ascii=False), flush=True)
            if report["status"] not in ("working", "settling", "transient_error"):
                save_json(final, report)
                if request.get("workflow_state"):
                    from workflow_control import publish_event
                    publish_event(request["workflow_state"], request, report, receipt)
                if args.notify and report["status"] != "monitoring_stopped":
                    notify_once(request, report, directory, transport)
                return
            retry = min(config.get("retry_max_seconds", 60), config.get("retry_interval_seconds", 5) * (2 ** min(max(errors - 1, 0), 16)))
            time.sleep(retry if status == "transient_error" else config.get("interval_seconds", 300))
    finally:
        lock.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
