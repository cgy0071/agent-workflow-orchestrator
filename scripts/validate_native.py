"""Real native Codex/Pi/OMP Command + Receipt acceptance. Herdr is never invoked."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time
import uuid

from monitor_task import Transport, inspect_once, read_json, save_json, utc_now
import native_worker
import workflow_control as control
from workflow_evidence import allocate
import workflow_scheduler as scheduler


def write_new(path, text):
    with Path(path).open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(text)


def provenance(root, harness, executable, mode, provider=None, model=None):
    source = Path(__file__).resolve().parents[1]
    version_argv = native_worker.cli_prefix(executable) + ["--version"]
    try:
        proc = subprocess.run(version_argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
        version = {"argv": version_argv, "returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
    except Exception as exc:
        version = {"argv": version_argv, "error": str(exc)}
    save_json(root / "version.json", version)
    proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source, capture_output=True, text=True)
    files = {str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in sorted((source / "scripts").glob("*.py"))}
    for name in files:
        destination = root / "source" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((source / name).read_bytes())
    value = {"harness": harness, "executable": str(executable), "execution_semantics": mode,
             "os": platform.platform(), "python": sys.version, "source_commit": proc.stdout.strip(),
             "source_files": files, "herdr_invoked": False, "recorded_at": utc_now(),
             "native_session_reuse": "unverified", "inflight_recovery": "unverified",
             "persistent_process_message_send": "unverified",
             "model_selection": {"requested": {"provider": provider, "model": model},
                                 "source": "per_invocation" if provider or model else "cli_default",
                                 "observed": {}}}
    save_json(root / "provenance.json", value)
    return value


class RecordingTransport(Transport):
    def __init__(self, request, root):
        super().__init__(request)
        self.root = Path(root)

    def record(self, kind, value):
        with (self.root / "adapter-events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"at": utc_now(), "kind": kind, "value": value}, ensure_ascii=True) + "\n")

    def status(self, spec):
        value = super().status(spec)
        self.record("status", value)
        return value

    def send(self, spec, message):
        self.record("send_attempt", {"identity": spec["expected_identity"], "message": message})
        response = super().send(spec, message)
        self.record("send_response", {"response": response})
        if (self.root / "crash-after-send").exists():
            (self.root / "crash-after-send").unlink()
            os._exit(90)  # Explicit validation-only crash, after reservation/send.
        return response


class ReviewTransport(RecordingTransport):
    def __init__(self, root, payload):
        super().__init__({}, root)
        self.payload = payload

    def send(self, spec, message):
        return super().send(spec, json.dumps(self.payload))


def prepare(root, harness, executable, mode, timeout, evidence_class="native", provider=None, model=None):
    if evidence_class not in ("native", "protocol-fixture"):
        raise ValueError("unknown evidence class")
    if evidence_class == "native" and os.environ.get("HERDR_ENV") == "1":
        raise ValueError("native-only acceptance requires a caller outside Herdr; no Herdr command was invoked")
    if harness == "omp" and mode != "one-shot":
        raise ValueError("OMP native bridge supports only one-shot execution")
    native_worker.selection(harness, provider, model)
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    meta = provenance(root, harness, executable, mode, provider, model)
    meta["evidence_class"] = evidence_class
    meta["timeout_seconds"] = timeout
    save_json(root / "provenance.json", meta)
    cfg = {"roles": {"planner": {}, "executor": {}},
           "workflow": {"reviewer_role": "planner", "dispatch_role": "planner",
                        "transitions": {"accept": "next", "reject": "stop", "repair": "new_scoped_task"}},
           "scheduler": {"max_concurrency": 1, "resources": {}}}
    state = root / "state"
    control.initialize(state, cfg)
    worker = dict(native_worker.initialize(root / "worker", harness, executable, mode, timeout, provider, model), role="executor")
    reviewer = dict(native_worker.initialize(root / "reviewer", harness, executable, "one-shot", timeout, provider, model), role="planner")
    policy = "dedicated_window"
    declaration = "Dedicated native validation review bridge; no unrelated conversation or work"
    reviewer.update(context_policy=policy, context_declaration=declaration)
    target = control.bind(state, "planner", reviewer, Transport({}).reviewer_identity(reviewer), "controller", policy, reason=declaration)
    control.bind(state, "executor", worker, Transport({}).status(worker)["identity"])
    memory = uuid.uuid4().hex
    nodes = []
    for name, dependencies, question in (("A", [], "Compute 17 * 19. Return only the integer."),
                                          ("B", ["A"], "Compute 17 * 19 + 1. Return only the integer.")):
        nonce = uuid.uuid4().hex
        if mode == "session":
            if name == "A": question = "Remember the secret " + memory + ". " + question
            else: question = 'Return JSON with "memory" equal to the secret from the earlier turn and "answer" equal to 17 * 19 + 1. Do not read files or use tools.'
        base = {"task_id": "NATIVE-" + name, "nonce": nonce, "generation": 1, "cwd": str(root), "workflow_state": str(state),
                "worker": dict(worker, baseline_completion_seq=0), "reviewer": reviewer, "dispatch_confirmed": False,
                "authorization": {"source": "User-authorized bounded native acceptance task", "dispatch_worker": True, "notify_reviewer": True},
                "completion": {"binding_artifact": "HANDOFF.md", "artifacts": ["RESULT.md"]},
                "monitor": {"interval_seconds": 1, "settle_grace_seconds": 3, "command_timeout_seconds": 15}}
        req = allocate(base, root / "delivery")
        payload = {"task_id": req["task_id"], "nonce": nonce, "cwd": req["cwd"], "prompt": question,
                   "marker": req["completion"]["marker"],
                   "authorization": {"source": req["authorization"]["source"], "terminate_on_timeout": True}}
        nodes.append({"id": name, "needs": dependencies, "request": req, "message": json.dumps(payload), "question": question})
    graph = {"nodes": nodes}
    save_json(root / "dag.json", graph)
    scheduler.load_dag(state, "native", graph, target["instance_id"], target["version"], "main")
    run = {"actor": target["instance_id"], "version": target["version"], "mode": mode,
           "memory": memory, "metadata": meta, "started_at": time.time(), "timeout": timeout}
    save_json(root / "run.json", run)
    return root


def task_data(state, task):
    with scheduler._read_database(state) as con:
        row = con.execute("SELECT data FROM tasks WHERE id=?", (task,)).fetchone()
        return json.loads(row[0])["request"] if row else None


def review_payload(root, node, req):
    path = Path(req["cwd"]) / "review-request.json"
    if path.exists(): return read_json(path)
    directory = root / "review-delivery" / node["id"]
    directory.mkdir(parents=True, exist_ok=False)
    answer = (Path(req["cwd"]) / "RESULT.md").read_text(encoding="utf-8")
    expected = ""
    run = read_json(Path(root) / "run.json")
    if run["mode"] == "session" and node["needs"]:
        # The reviewer cannot verify recall from the worker's own claim; give it
        # the secret the validator actually sent in the earlier turn.
        expected = ("\nExpected secret from the earlier turn, supplied by the validator rather than the worker: " +
                    run["memory"] + ". Accept only if memory equals it exactly.")
    prompt = ("Independently review this arithmetic task. Question: " + node["question"] + expected +
              "\nWorker response: " + answer + "\nReturn JSON only with task_id=" + req["task_id"] +
              ", nonce=" + req["nonce"] + ', verdict ("accept" or "reject"), and reason. Accept only a correct answer. Do not use tools.')
    payload = {"task_id": "REVIEW-" + req["task_id"], "nonce": uuid.uuid4().hex, "cwd": str(directory), "prompt": prompt,
               "authorization": {"source": req["authorization"]["source"], "terminate_on_timeout": True}}
    save_json(path, payload)
    return payload


def finish_bundle(root, result):
    root = Path(root)
    save_json(root / "result.json", result)
    meta = read_json(root / "provenance.json")
    observed = {}
    for role in ("worker", "reviewer"):
        observed[role] = []
        for path in sorted((root / role / "jobs").glob("*/receipt/status.json")):
            receipt = read_json(path)
            turn = receipt.get("native_turn", {})
            observed[role].append({"task_id": receipt["task_id"], "provider": turn.get("provider"),
                                   "model": turn.get("model"), "turn_status": turn.get("status", "unverified"),
                                   "process_status": receipt.get("process_status", "unverified")})
    meta.setdefault("model_selection", {"requested": {"provider": None, "model": None}, "source": "cli_default"})["observed"] = observed
    save_json(root / "provenance.json", meta)
    result["evidence_class"] = meta.get("evidence_class", "native")
    if result["status"] == "NATIVE_LOOP_PASS" and result["evidence_class"] != "native":
        result["status"] = "PROTOCOL_FIXTURE_PASS"
    save_json(root / "result.json", result)
    version = read_json(root / "version.json").get("stdout", "").strip()
    proofs = []
    for name in ("provenance.json", "version.json", "adapter-events.jsonl", "result.json"):
        if (root / name).is_file():
            proofs.append({"path": name, "sha256": hashlib.sha256((root / name).read_bytes()).hexdigest()})
    reproduction = [sys.executable, "-B", str(Path(__file__).resolve()), "--directory", "NEW-DIRECTORY",
                    "--harness", meta["harness"], "--executable", meta["executable"], "--mode", meta["execution_semantics"]]
    if meta.get("timeout_seconds") is not None: reproduction += ["--timeout", str(meta["timeout_seconds"])]
    requested = meta["model_selection"]["requested"]
    for key in ("provider", "model"):
        if requested.get(key) is not None: reproduction += ["--" + key, requested[key]]
    for adapter in ("command", "receipt"):
        profile = {"harness": meta["harness"], "harness_version": version or "unknown", "transport": adapter,
                   "transport_version": "stdlib-native-bridge-2", "os": meta["os"], "skill_commit": meta["source_commit"] or "unknown",
                   "execution_semantics": meta["execution_semantics"], "native_capabilities": {
                       "one_shot": "verified" if result["status"] == "NATIVE_LOOP_PASS" and meta["execution_semantics"] == "one-shot" else "unverified",
                       "conversation_reuse": "verified" if adapter == "command" and result.get("session_reuse") == "passed" and result["evidence_class"] == "native" else "unverified",
                       "inflight_recovery": "unverified", "live_process_message_send": "unverified"},
                   "operations": {key: {"status": "unverified"} for key in scheduler.OPERATIONS}}
        if result["status"] == "NATIVE_LOOP_PASS":
            for operation in (("spawn", "send", "status", "collect") if adapter == "command" else ("status", "collect")):
                profile["operations"][operation] = {"status": "verified", "commands": [reproduction],
                     "evidence": proofs, "scope": "native_cli_job" if adapter == "command" else "per_job_receipt_lifecycle"}
            if adapter == "command" and result.get("session_reuse") == "passed":
                profile["operations"]["attach_resume"] = {"status": "verified", "commands": profile["operations"]["send"]["commands"],
                                                         "evidence": proofs, "scope": "conversation_resume_after_clean_process_exit"}
        save_json(root / (adapter + "-capability-profile.json"), profile)
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name not in ("manifest.json", "runner.log"):
            files.append({"path": path.relative_to(root).as_posix(), "bytes": path.stat().st_size,
                          "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    save_json(root / "manifest.json", {"recorded_at": utc_now(), "status": result["status"], "files": files})
    return result


def execute(root, budget=None, recover_dispatch=False):
    root = Path(root).resolve()
    run = read_json(root / "run.json")
    if run["metadata"].get("evidence_class", "native") == "native" and os.environ.get("HERDR_ENV") == "1":
        raise ValueError("native-only acceptance requires a caller outside Herdr; no Herdr command was invoked")
    source = Path(__file__).resolve().parents[1]
    if any(not (source / name).is_file() or hashlib.sha256((source / name).read_bytes()).hexdigest() != digest
           for name, digest in run["metadata"]["source_files"].items()):
        raise ValueError("validation source changed; start a new acceptance run; archived source is retained for audit")
    state = root / "state"
    who = (run["actor"], run["version"], "main")
    graph = read_json(root / "dag.json")
    transport = RecordingTransport({}, root)
    deadline = time.monotonic() + (budget if budget is not None else run["timeout"] * 4 + 45)
    timers = {}
    while time.monotonic() < deadline:
        report = scheduler.tick(state, "native", *who, transport=transport)
        transport.record("tick", report)
        for node in graph["nodes"]:
            req = task_data(state, node["request"]["task_id"])
            if not req: continue
            task = scheduler.snapshot(state, req["task_id"])["tasks"][0]
            if task["acceptance_valid"]: continue
            dispatch = task["dispatch"]["status"]
            if dispatch in ("reserved", "uncertain"):
                observed = transport.status(req["worker"])
                if not recover_dispatch or (observed.get("task_id"), observed.get("nonce")) != (req["task_id"], req["nonce"]):
                    return finish_bundle(root, {"status": "DISPATCH_UNCERTAIN", "task_id": req["task_id"], "herdr_invoked": False})
                if not observed.get("execution_started"):
                    if observed["status"] == "working": continue
                    return finish_bundle(root, {"status": "ADAPTER_NOT_VERIFIED", "stage": "native_start", "task_id": req["task_id"], "herdr_invoked": False})
                note = Path(req["cwd"]) / "RECOVERED-EXECUTION.md"
                if not note.exists(): write_new(note, req["task_id"] + " " + req["nonce"] + "\nOutcome: running\n" + json.dumps(observed))
                req = scheduler.reconcile(state, req["task_id"], "running", note, *who)["request"]
                save_json(Path(req["cwd"]) / "confirmed-request.json", req)
            observed = transport.status(req["worker"])
            if not req["dispatch_confirmed"]:
                if not observed.get("execution_started"):
                    if observed["status"] in ("failed", "blocked"):
                        return finish_bundle(root, {"status": "ADAPTER_NOT_VERIFIED", "stage": "native_start", "task_id": req["task_id"], "herdr_invoked": False})
                    continue
                note = Path(req["cwd"]) / "EXECUTION.md"
                if not note.exists(): write_new(note, req["task_id"] + " " + req["nonce"] + "\nOutcome: running\n" + json.dumps(observed))
                req = scheduler.reconcile(state, req["task_id"], "running", note, *who)["request"]
                save_json(Path(req["cwd"]) / "confirmed-request.json", req)
            stage, detail, timer, receipt = inspect_once(req, transport, timers.get(req["task_id"]))
            timers[req["task_id"]] = timer
            observation = {"task_id": req["task_id"], "nonce": req["nonce"], "status": stage, "observed_at": utc_now(), "anomaly_summary": detail}
            scheduler.record_observation(state, req, observation)
            if stage in ("working", "settling"): continue
            if stage != "review_ready":
                control.publish_event(state, req, observation, receipt)
                return finish_bundle(root, {"status": "NATIVE_LOOP_FAILED", "stage": stage, "task_id": req["task_id"], "herdr_invoked": False})
            event = control.publish_event(state, req, observation, receipt)
            payload = review_payload(root, node, req)
            delivered = control.deliver_event(state, event, ReviewTransport(root, payload))
            transport.record("review_notification", delivered)
            if delivered["status"] in ("delivery_uncertain", "reserved_no_blind_retry"):
                return finish_bundle(root, {"status": "REVIEW_DELIVERY_UNCERTAIN", "task_id": req["task_id"], "herdr_invoked": False})
            review_state = transport.status(req["reviewer"])
            if review_state["status"] in ("failed", "blocked"):
                return finish_bundle(root, {"status": "NATIVE_REVIEW_FAILED", "task_id": req["task_id"], "herdr_invoked": False})
            output = Path(payload["cwd"]) / "RESULT.md"
            if review_state["status"] != "idle" or not output.exists(): continue
            raw = output.read_text(encoding="utf-8").strip()
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
            review = json.loads(raw)
            if (review.get("task_id"), review.get("nonce")) != (req["task_id"], req["nonce"]) or review.get("verdict") not in ("accept", "reject"):
                raise ValueError("native review response lacks exact task/nonce/verdict")
            path = Path(req["review_path"])
            text = req["task_id"] + " " + req["nonce"] + "\nVerdict: " + review["verdict"] + "\n" + str(review.get("reason", ""))
            if not path.exists(): write_new(path, text)
            control.acknowledge(state, event, *who)
            control.record_review(state, event, *who, path, review["verdict"])
            if review["verdict"] != "accept":
                return finish_bundle(root, {"status": "REVIEW_REJECTED", "task_id": req["task_id"], "herdr_invoked": False})
        snap = scheduler.snapshot(state)
        if set(snap["dags"]["native"]["nodes"].values()) == {"accepted"}:
            result = {"status": "NATIVE_LOOP_PASS", "herdr_invoked": False, "execution_semantics": run["mode"],
                      "command_adapter": "passed", "receipt_adapter": "unverified", "session_reuse": "unverified",
                      "persistent_process_message_send": "unverified", "inflight_recovery": "unverified", "snapshot": snap}
            # Check raw per-job Receipt Adapter independently, with that job's baseline.
            for node in graph["nodes"]:
                req = task_data(state, node["request"]["task_id"])
                from workflow_evidence import task_key
                job = root / "worker" / "jobs" / task_key(req)
                receipt_req = dict(req, worker={"adapter": "receipt", "status_path": str(job / "receipt" / "status.json"),
                                               "output_path": str(job / "receipt" / "output.log"), "expected_identity": req["worker"]["expected_identity"],
                                               "baseline_completion_seq": 0})
                if inspect_once(receipt_req, Transport(receipt_req))[0] != "review_ready":
                    raise ValueError("raw Receipt Adapter did not independently validate the native job")
            result["receipt_adapter"] = "passed"
            if run["mode"] == "session":
                from workflow_evidence import task_key
                outcomes = [read_json(root / "worker" / "jobs" / task_key(node["request"]) / "outcome.json") for node in graph["nodes"]]
                response = json.loads((Path(graph["nodes"][1]["request"]["cwd"]) / "RESULT.md").read_text())
                if not outcomes[0].get("native_session_id") or outcomes[0]["native_session_id"] != outcomes[1].get("native_session_id") or response.get("memory") != run["memory"]:
                    raise ValueError("native session identity/recall test failed")
                result["session_reuse"] = "passed"
            save_json(root / "final-snapshot.json", snap)
            return finish_bundle(root, result)
        time.sleep(.25)
    return finish_bundle(root, {"status": "VALIDATION_BUDGET_EXHAUSTED", "herdr_invoked": False,
                                "note": "No uncertain task was resent; inspect native job receipts and ledger before resuming."})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--harness", choices=("codex", "pi", "omp"))
    parser.add_argument("--executable")
    parser.add_argument("--mode", choices=("one-shot", "session"), default="one-shot")
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument("--provider", help="Pi/OMP per-invocation provider; requires --model")
    parser.add_argument("--model", help="Pi/OMP per-invocation model; does not change global settings")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--recover-dispatch", action="store_true", help="inspect exact native task/nonce and reconcile an uncertain send; never resend")
    parser.add_argument("--crash-after-send", action="store_true", help="explicit acceptance fault injection: exit after first send")
    args = parser.parse_args()
    if args.resume and (args.provider is not None or args.model is not None):
        parser.error("resume retains the frozen provider/model; start a new directory to change selection")
    root = Path(args.directory).resolve()
    if not args.resume and root.exists():
        parser.error("a new validation directory is required; existing evidence is never overwritten")
    if args.resume and (root / "result.json").exists() and read_json(root / "result.json")["status"] in ("NATIVE_LOOP_PASS", "PROTOCOL_FIXTURE_PASS"):
        parser.error("completed evidence is read-only; start a new validation directory")
    try:
        if not args.resume:
            if not args.harness: parser.error("--harness is required for a new validation run")
            candidates = ([args.harness + suffix for suffix in (".exe", ".cmd", "")]
                          if sys.platform == "win32" else [args.harness])
            executable = args.executable or next((found for name in candidates if (found := shutil.which(name))), None)
            if not executable: parser.error("native CLI executable not found")
            prepare(root, args.harness, executable, args.mode, args.timeout, provider=args.provider, model=args.model)
        if args.crash_after_send: write_new(root / "crash-after-send", "explicit acceptance fault injection\n")
        result = execute(root, recover_dispatch=args.recover_dispatch)
    except Exception as exc:
        if not root.exists(): raise
        result = {"status": "VALIDATION_ERROR", "error": str(exc), "herdr_invoked": False}
        if (root / "result.json").exists():
            save_json(root / ("invocation-error-" + uuid.uuid4().hex + ".json"), result)
        else: result = finish_bundle(root, result)
    print(json.dumps({key: value for key, value in result.items() if key != "snapshot"}, ensure_ascii=True))
    return 0 if result["status"] == "NATIVE_LOOP_PASS" else 2


if __name__ == "__main__":
    sys.exit(main())
