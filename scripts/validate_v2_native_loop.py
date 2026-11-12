"""V2 service-path acceptance with real native Command + Receipt turns.

The inner flow never invokes Herdr and must not inherit HERDR_ENV for native
evidence. It creates a disposable fixture project/state, uses the public service
gateway with scoped access tokens for task creation, claim, evidence, completion
and review, and the V2 scheduler's own dispatch gate to launch a native CLI
through native_worker -> run_job. A distinct reviewer bridge (or, for offline
protocol fixtures, a fixture executable) produces the review verdict.

This validator is opt-in and writes a new directory only; it never touches an
existing ledger, binding, monitor or user project.
"""
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

import awo_service as service
import monitor_task
import native_worker
import priority_scheduler as scheduler
import service_access as access
import workflow_control as control
import workspace_manager as workspace_lib
import workspace_runtime
from workflow_evidence import allocate, task_key

EVIDENCE_CLASSES = ("native", "protocol-fixture")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")


def write_new(path, text):
    with Path(path).open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(text)


def rows(state, sql, args=()):
    con = service._connect(service._db_path(state), readonly=True)
    try:
        return [dict(row) for row in con.execute(sql, args)]
    finally:
        con.close()


def one(state, sql, args=()):
    found = rows(state, sql, args)
    return found[0] if found else None


class Failure(Exception):
    def __init__(self, stage, message):
        super().__init__(message)
        self.stage = stage


def expect(value, stage, message):
    if not value:
        raise Failure(stage, message)
    return value


def provenance(root, harness, executable, mode, evidence_class):
    source = Path(__file__).resolve().parents[1]
    argv = native_worker.cli_prefix(executable) + ["--version"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=30)
        version = {"argv": argv, "returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
    except Exception as exc:
        version = {"argv": argv, "error": str(exc)}
    save(Path(root) / "version.json", version)
    files = {str(path.relative_to(source)): sha(path) for path in sorted((source / "scripts").glob("*.py"))}
    for name in files:
        destination = Path(root) / "source" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((source / name).read_bytes())
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source, capture_output=True,
                                text=True, timeout=15).stdout.strip()
    except Exception:
        commit = ""
    value = {"evidence_class": evidence_class, "harness": harness, "executable": str(executable),
             "execution_semantics": mode, "os": platform.platform(), "python": sys.version,
             "source_commit": commit, "source_files": files, "herdr_invoked": False,
             "herdr_env_present": "HERDR_ENV" in os.environ, "recorded_at": monitor_task.utc_now(),
             "outer_assignment_orchestration": "Herdr pane outside this validator; not invoked here",
             "model_selection": {"requested": {"provider": None, "model": None},
                                 "source": "cli_default", "observed": []}}
    save(Path(root) / "provenance.json", value)
    return value


def issue(state, actor, epoch, payload, path):
    result = access.issue_token(state, *actor, {
        "tool": "access.issue", "payload": payload,
        "command_id": "v2native-access-" + uuid.uuid4().hex, "expected_epoch": epoch}, str(path))
    return Path(result["token_file"]).read_text(encoding="utf-8").strip()


def call(state, root, token, tool, arguments=None):
    return service.gateway_call(state, str(root), token, tool, arguments or {})


def command(state, actor, epoch, tool, payload):
    return service.execute_command(state, *actor, {
        "tool": tool, "payload": payload,
        "command_id": "v2native-" + uuid.uuid4().hex, "expected_epoch": epoch})


def args_for(epoch, **payload):
    return dict(payload, command_id="v2native-api-" + uuid.uuid4().hex, expected_epoch=epoch)


def workspace_call(state, actor, epoch, tool, payload):
    """Call the controller-side workspace API with the same envelope the CLI uses."""
    handler = getattr(workspace_runtime, tool.split(".", 1)[1])
    return handler(state, actor[0], actor[1], "main", {
        "tool": tool, "payload": payload,
        "command_id": "v2native-ws-" + uuid.uuid4().hex, "expected_epoch": epoch})


def create_managed_workspace(state, root, actor, epoch, assignment_id, delivery, allowed_paths):
    """Bind a real V2 managed directory workspace to the frozen delivery root.

    The native bridge executes with the frozen request cwd, so the workspace is
    created at that exact path: the later evidence, review note and inventory all
    live inside the owned directory and the send gate verifies it before launch.
    """
    delivery = Path(delivery).resolve()
    expect(delivery.is_dir() and not any(delivery.iterdir()), "workspace_create",
           "delivery root must be an existing empty directory before managed workspace creation")
    delivery.rmdir()
    revision = one(state, "SELECT revision FROM v2_assignment WHERE assignment_id=?",
                   (assignment_id,))["revision"]
    response = workspace_call(state, actor, epoch, "workspace.create", {
        "assignment_id": assignment_id, "expected_revision": revision, "kind": "directory",
        "root": str(Path(root).resolve()), "path": str(delivery),
        "allowed_paths": sorted(allowed_paths)})
    expect(response.get("status") == "WORKSPACE_READY", "workspace_create",
           "managed workspace creation failed: " + json.dumps(response)[:800])
    raw = one(state, "SELECT workspace_id FROM v2_workspace WHERE assignment_id=?", (assignment_id,))
    expect(raw is not None, "workspace_create", "managed workspace row is missing")
    con = service._connect(service._db_path(state), readonly=True)
    try:
        ws = workspace_lib.get_workspace(con, raw["workspace_id"])
    finally:
        con.close()
    bound = one(state, "SELECT workspace_id FROM v2_assignment WHERE assignment_id=?", (assignment_id,))
    expect(ws and ws["status"] == "ready" and ws["kind"] == "directory"
           and Path(ws["path"]).resolve() == delivery
           and bound and bound["workspace_id"] == ws["workspace_id"], "workspace_create",
           "Assignment/workspace binding is not the provisioned managed directory")
    spec = json.loads(ws["spec_json"])
    marker = Path(ws["path"]) / workspace_lib.DIRECTORY_MARKER
    expect(marker.is_file() and workspace_lib._ownership(ws), "workspace_create",
           "managed workspace ownership marker is missing or invalid")
    return ws, spec, sha(marker)


def workspace_summary(state, assignment_id):
    row = one(state, "SELECT workspace_id,assignment_id,task_id,host,kind,path,status,"
                     "revision,spec_sha256,quiescence_json,error FROM v2_workspace "
                     "WHERE assignment_id=?", (assignment_id,))
    spec = json.loads(one(state, "SELECT spec_json FROM v2_workspace WHERE assignment_id=?",
                          (assignment_id,))["spec_json"])
    row["base_commit"] = spec.get("base_commit")
    row["allowed_paths"] = spec.get("allowed_paths")
    return row


def review_snapshot(state, assignment_id, event_id):
    return {"reviews": rows(state, "SELECT review_id,verdict,state FROM v2_review WHERE assignment_id=? "
                                   "ORDER BY review_id", (assignment_id,)),
            "workspace_reviews": rows(state, "SELECT workspace_id,inventory_sha256,verdict FROM "
                                            "v2_workspace_review WHERE assignment_id=?", (assignment_id,)),
            "assignment": one(state, "SELECT state,review_status,holds_slot,revision,workspace_id "
                                     "FROM v2_assignment WHERE assignment_id=?", (assignment_id,)),
            "task": one(state, "SELECT task_state,accepted_evidence_id FROM v2_task WHERE task_id='NATIVE-A'"),
            "event": one(state, "SELECT status FROM events WHERE id=?", (event_id,)),
            "evidence": rows(state, "SELECT evidence_id,sealed_sha256,sealed,state FROM v2_evidence "
                                    "WHERE assignment_id=? ORDER BY evidence_id", (assignment_id,))}


def stored_binding(state, binding_id):
    return one(state, "SELECT id,role,version,mode,status,data FROM bindings WHERE id=?", (binding_id,))


def set_binding_record(state, binding_id, mode, data):
    con = service._connect(service._db_path(state))
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute("UPDATE bindings SET mode=?,data=? WHERE id=?", (mode, data, binding_id))
        con.commit()
    finally:
        con.close()



def request_for(task_id, nonce, cwd, state, worker, reviewer):
    return {
        "task_id": task_id, "nonce": nonce, "cwd": str(cwd), "generation": 1,
        "workflow_state": str(state),
        "worker": dict(worker, baseline_completion_seq=0),
        "reviewer": dict(reviewer), "dispatch_confirmed": False,
        "authorization": {"source": "Owner-approved bounded V2 native acceptance fixture",
                          "dispatch_worker": True, "notify_reviewer": False},
        "completion": {"binding_artifact": "HANDOFF.md", "artifacts": ["RESULT.md"]},
        "monitor": {"interval_seconds": 1, "settle_grace_seconds": 3, "command_timeout_seconds": 15},
    }


def prepare(root, harness, executable, mode, timeout, evidence_class, fixture_script=None):
    expect(evidence_class in EVIDENCE_CLASSES, "prepare", "unknown evidence class")
    expect(not (evidence_class == "native" and os.environ.get("HERDR_ENV") == "1"), "prepare",
           "native-only acceptance requires a caller outside Herdr")
    expect(not (fixture_script and evidence_class != "protocol-fixture"), "prepare",
           "a fixture executable is only allowed for labelled protocol-fixture evidence")
    native_worker.selection(harness, None, None)
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    meta = provenance(root, harness, executable, mode, evidence_class)
    meta["timeout_seconds"] = timeout
    save(root / "provenance.json", meta)
    project = root / "project"
    project.mkdir()
    (project / "README.md").write_text("# Disposable V2 native acceptance fixture\n", encoding="utf-8")
    (project / "input.txt").write_text("synthetic fixture data\n", encoding="utf-8")
    cfg = {"roles": {"planner": {}, "executor": {}},
           "workflow": {"reviewer_role": "planner", "dispatch_role": "planner",
                        "transitions": {"accept": "next", "repair": "repair", "reject": "stop"}},
           "scheduler": {"max_concurrency": 1, "resources": {"native_slot": 1}}}
    state = root / "state"
    control.initialize(state, cfg)
    observed_version = ""
    version_file = Path(root) / "version.json"
    if version_file.exists():
        observed_version = str(json.loads(version_file.read_text(encoding="utf-8")).get("stdout", "")).strip()
    observed_version = observed_version or "unknown"
    worker_spec = dict(native_worker.initialize(root / "worker", harness, executable, mode, timeout),
                       role="executor", version=observed_version)
    reviewer_spec = dict(native_worker.initialize(root / "reviewer", harness, executable, "one-shot", timeout),
                         role="planner", context_policy="inbox_only", version=observed_version)
    if fixture_script:
        prefix = [sys.executable, str(Path(fixture_script).resolve())]
        for directory in (root / "worker", root / "reviewer"):
            config = monitor_task.read_json(directory / "config.json")
            config["argv_prefix"] = prefix
            monitor_task.save_json(directory / "config.json", config)

    def identity(spec):
        return monitor_task.Transport({}).status(spec)["identity"]

    planner = control.bind(state, "planner", reviewer_spec, identity(reviewer_spec), "controller", "inbox_only")
    executor = control.bind(state, "executor", worker_spec, identity(worker_spec), "worker")
    actor = (planner["instance_id"], planner["version"], "main")
    service.apply_migration(state, *actor)
    con = service._connect(service._db_path(state))
    try:
        con.execute("BEGIN IMMEDIATE")
        scheduler.create_scheduler_schema(con)
        con.commit()
    finally:
        con.close()
    project_id = service.service_status(state)["project_id"]
    worker_agent = service._agent_id_for_binding(executor["instance_id"])
    planner_agent = service._agent_id_for_binding(planner["instance_id"])
    epoch = service.set_enabled(state, *actor, True)["epoch"]
    capability = root / "capability-evidence.json"
    save(capability, {"kind": "installed-cli-command-bridge" if evidence_class == "native" else "protocol-fixture",
                      "harness": harness, "version": observed_version, "mode": mode,
                      "version_file": "version.json", "version_sha256": sha(version_file) if version_file.exists() else None,
                      "worker_config_sha256": sha(root / "worker" / "config.json"),
                      "bridge_source_sha256": sha(Path(native_worker.__file__)),
                      "run_job_source_sha256": sha(Path(__file__).resolve().parents[1] / "scripts" / "run_job.py")})
    for key in scheduler.DEFAULT_CAPABILITIES:
        agent = service.agent_get(state, *actor, worker_agent)["agent"]
        service.set_agent_capability(state, *actor, {
            "tool": "agent.capability",
            "payload": {"agent_id": agent["agent_id"], "capability_key": key, "level": "supported",
                        "harness_version": observed_version, "os": sys.platform, "host": "local",
                        "checked_at": service._now(), "evidence_path": str(capability),
                        "evidence_sha256": sha(capability),
                        "source": "live_probe" if evidence_class == "native" else "regression_test",
                        "expected_revision": agent["revision"]},
            "command_id": "v2native-cap-" + uuid.uuid4().hex, "expected_epoch": epoch})
    secrets = root / "secrets"
    secrets.mkdir()
    controller_token = issue(state, actor, epoch, {
        "project_root": str(root), "role": "controller", "agent_id": planner_agent,
        "binding_id": planner["instance_id"], "binding_version": planner["version"]},
        secrets / "controller.token")
    worker_token = issue(state, actor, epoch, {
        "project_root": str(root), "role": "worker", "agent_id": worker_agent}, secrets / "worker.token")
    seed = root / "seed"
    seed.mkdir()
    tasks = {}
    for name in ("NATIVE-A", "NATIVE-B"):
        created = call(state, root, controller_token, "awo_task_create", args_for(
            epoch, task_id=name, title="Disposable native acceptance " + name,
            body="Synthetic local fixture; no user data.", priority="P3"))
        expect(created.get("status") == "CREATED", "prepare", "task creation failed for " + name)
        tasks[name] = created
    reviewer_token = issue(state, actor, epoch, {
        "project_root": str(root), "role": "reviewer", "agent_id": planner_agent,
        "binding_id": planner["instance_id"], "binding_version": planner["version"], "task_id": "NATIVE-A"},
        secrets / "reviewer.token")
    secret = uuid.uuid4().hex if mode == "session" else None
    prompts = {
        "NATIVE-A": (("Remember the secret " + secret + ". Compute 17 * 19. Return JSON only with keys "
                      "secret and answer. Do not use tools.") if secret else
                     "Compute 17 * 19. Return only the integer. Do not use tools."),
        "NATIVE-B": (("Using the secret from the earlier turn, compute 17 * 19 + 1. Return JSON only with "
                      "keys secret and answer. Do not use tools.") if secret else
                     "Compute 17 * 19 + 1. Return only the integer. Do not use tools."),
    }
    prepared = {}
    for name in ("NATIVE-A", "NATIVE-B"):
        nonce = uuid.uuid4().hex
        base = request_for("NATIVE-" + name[-1] + "-RUN", nonce, seed, state, worker_spec, reviewer_spec)
        base["completion"]["marker"] = "TASK_DONE " + base["task_id"] + " " + nonce
        base["reviewer"] = reviewer_spec
        delivery = allocate(base, root / "delivery")
        delivery["worker"]["baseline_completion_seq"] = native_worker.status(
            worker_spec["expected_identity"]["launch_cwd"])["completion_seq"]
        prompt = prompts[name]
        message = json.dumps({"task_id": delivery["task_id"], "nonce": delivery["nonce"],
                              "cwd": delivery["cwd"], "prompt": prompt,
                              "marker": delivery["completion"]["marker"],
                              "authorization": {"source": delivery["authorization"]["source"],
                                                "terminate_on_timeout": True}}, ensure_ascii=True)
        prepared[name] = {"request": delivery, "message": message, "prompt": prompt}
    # Enqueue A now; B is enqueued after A is review_ready so its baseline is live.
    queued = command(state, actor, epoch, "scheduler.enqueue", {
        "task_id": "NATIVE-A", "expected_revision": tasks["NATIVE-A"]["revision"],
        "request": prepared["NATIVE-A"]["request"], "message": prepared["NATIVE-A"]["message"],
        "agent_id": worker_agent, "budget": {"tokens.input": 40000, "tokens.output": 8000},
        "resources": {"native_slot": 1}, "workspace_mode": "directory", "task_kind": "artifact",
        "dependencies": []})
    expect(queued.get("status") == "QUEUED", "prepare", "enqueue A failed: " + json.dumps(queued)[:400])
    run = {"state": str(state), "root": str(root), "project_id": project_id, "actor": list(actor),
           "epoch": epoch, "mode": mode, "timeout": timeout, "evidence_class": evidence_class,
           "harness": harness, "worker_agent": worker_agent, "planner_agent": planner_agent,
           "controller_token": str(secrets / "controller.token"),
           "worker_token": str(secrets / "worker.token"),
           "reviewer_token": str(secrets / "reviewer.token"),
           "worker_spec": worker_spec, "reviewer_spec": reviewer_spec,
           "tasks": {"NATIVE-A": tasks["NATIVE-A"], "NATIVE-B": tasks["NATIVE-B"]},
           "prepared": prepared, "queue_a": queued, "secret": secret,
           "started_at": monitor_task.utc_now()}
    save(root / "run.json", run)
    return root


def token(run, name):
    return Path(run[name]).read_text(encoding="utf-8").strip()


def trace(root, event):
    event = dict(event, at=monitor_task.utc_now())
    with (Path(root) / "trace.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=True, sort_keys=True) + "\n")
    return event


def wait_turn(directory, baseline, deadline, stage):
    last = None
    while time.monotonic() < deadline:
        last = native_worker.status(directory)
        if last.get("status") in ("failed", "blocked", "exited"):
            raise Failure(stage, "native worker " + last.get("status", "?") + ": " + json.dumps(last)[:800])
        if last.get("status") == "idle" and monitor_task.sequence(last.get("completion_seq")) >= baseline + 1:
            return last
        time.sleep(0.5)
    raise Failure(stage, "native turn timeout: " + json.dumps(last)[:800])


def parse_json_text(raw):
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    return json.loads(text)


def usage(root):
    jobs = []
    total = {}
    for role in ("worker", "reviewer"):
        for path in sorted((Path(root) / role / "jobs").glob("*/receipt/output.log")):
            values = {}
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get("type") == "turn.completed":
                    found = event.get("usage")
                    if isinstance(found, dict):
                        for key, value in found.items():
                            if type(value) is int:
                                values[key] = values.get(key, 0) + value
            for key, value in values.items():
                total[key] = total.get(key, 0) + value
            jobs.append({"role": role, "job": path.parents[1].name, "usage": values or "unknown",
                         "output_sha256": sha(path)})
    return {"status": "actual" if total else "unknown", "jobs": jobs, "totals": total or "unknown",
            "cost": {"status": "unknown", "reason": "no provider pricing applied by this fixture"}}


def review_native(root, run, request, artifact_text, requirement, expected, deadline):
    directory = Path(root) / "review-runs" / task_key(request)
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "artifact.md").write_bytes((Path(request["cwd"]) / "RESULT.md").read_bytes())
    prompt = ("Review this artifact independently. Task: " + request["task_id"] +
              "\nNonce: " + request["nonce"] + "\nRequirement: " + requirement +
              "\nExpected: " + expected + "\nArtifact SHA-256: " +
              sha(Path(request["cwd"]) / "RESULT.md") +
              "\nThe artifact is also stored byte-for-byte as artifact.md in your current directory." +
              "\nArtifact text:\n<<<\n" + artifact_text + "\n>>>\n"
              "Return JSON only with task_id, nonce, verdict (accept or reject), and reason. "
              "Accept only if the artifact satisfies the requirement. Do not use tools.")
    payload = {"task_id": "REVIEW-" + request["task_id"], "nonce": uuid.uuid4().hex,
               "cwd": str(directory), "prompt": prompt,
               "authorization": {"source": "bounded native reviewer acceptance fixture",
                                 "terminate_on_timeout": True}}
    baseline = native_worker.status(Path(root) / "reviewer")["completion_seq"]
    sent = native_worker.send(Path(root) / "reviewer", payload)
    trace(root, {"stage": "review_send", "task_id": request["task_id"], "response": sent,
                 "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()})
    observed = wait_turn(Path(root) / "reviewer", baseline, deadline, "review_turn")
    raw = (directory / "RESULT.md").read_text(encoding="utf-8")
    value = parse_json_text(raw)
    expect(value.get("task_id") == request["task_id"] and value.get("nonce") == request["nonce"]
           and value.get("verdict") in ("accept", "reject"), "review_parse",
           "review response lacks exact task/nonce/verdict: " + raw[:400])
    trace(root, {"stage": "review_turn", "status": observed, "review": value})
    return value, directory


def execute(root):
    root = Path(root).resolve()
    run = monitor_task.read_json(root / "run.json")
    meta = monitor_task.read_json(root / "provenance.json")
    if meta.get("evidence_class") == "native" and os.environ.get("HERDR_ENV") == "1":
        raise Failure("execute", "native-only acceptance requires a caller outside Herdr")
    source = Path(__file__).resolve().parents[1]
    for name, digest in meta["source_files"].items():
        if not (source / name).is_file() or sha(source / name) != digest:
            raise Failure("execute", "validation source changed: " + name)
    state, epoch, actor = Path(run["state"]), run["epoch"], tuple(run["actor"])
    project, deadline = run["project_id"], time.monotonic() + (run["timeout"] * 4 + 90)
    controller, worker, reviewer = token(run, "controller_token"), token(run, "worker_token"), token(run, "reviewer_token")
    request_a = run["prepared"]["NATIVE-A"]["request"]
    traces = []

    def emit(value):
        traces.append(trace(root, value))
        return value

    # 1. Claim A through the authenticated gateway.
    claim = call(state, root, worker, "awo_assignment_claim",
                 args_for(epoch, task_id="NATIVE-A", expected_revision=run["queue_a"]["revision"]))
    expect(claim.get("status") == "RESERVED", "claim", "claim A failed: " + json.dumps(claim)[:400])
    aid = claim["assignment_id"]
    emit({"stage": "claim", "status": claim["status"], "assignment_id": aid, "run_id": claim["run_id"],
          "reservation_ids": claim.get("reservation_ids")})
    assignment = one(state, "SELECT * FROM v2_assignment WHERE assignment_id=?", (aid,))
    reservations = rows(state, "SELECT b.scope_type,b.scope_id,r.dimension,r.amount,r.state "
                              "FROM v2_budget_reservation r JOIN v2_budget b ON b.budget_id=r.budget_id "
                              "WHERE r.assignment_id=? ORDER BY r.budget_id,r.dimension", (aid,))
    resources = rows(state, "SELECT assignment_id,run_id,attempt_id,send_state,controller_id,controller_epoch "
                            "FROM v2_scheduler_intent WHERE assignment_id=?", (aid,))
    expect(assignment and assignment["holds_slot"] == 1, "claim", "claim did not hold the slot")
    expect(len(reservations) >= 3, "claim", "budget reservation rows are missing")
    emit({"stage": "claim_records", "assignment": assignment, "budget_reservations": reservations,
          "intent": resources, "workspace_mode": "directory", "delivery_cwd": request_a["cwd"]})

    # 1b. Bind a real managed workspace to this exact frozen delivery root.
    allowed_a = ["HANDOFF.md", "RESULT.md", "REVIEW.md"]
    ws_a, spec_a, marker_a = create_managed_workspace(
        state, root, actor, epoch, aid, request_a["cwd"], allowed_a)
    emit({"stage": "workspace_create", "workspace_id": ws_a["workspace_id"], "assignment_id": aid,
          "task_id": ws_a["task_id"], "kind": ws_a["kind"], "status": ws_a["status"],
          "path": ws_a["path"], "allowed_paths": spec_a["allowed_paths"],
          "spec_sha256": ws_a["spec_sha256"], "marker": workspace_lib.DIRECTORY_MARKER,
          "marker_sha256": marker_a,
          "ownership_token_sha256": hashlib.sha256(spec_a["ownership_token"].encode()).hexdigest(),
          "assignment_workspace_id": one(state, "SELECT workspace_id FROM v2_assignment WHERE "
                                               "assignment_id=?", (aid,))["workspace_id"],
          "claims_retained": True})

    # 2. Dispatch through the V2 scheduler send gate to the real bridge.
    dispatch = scheduler.dispatch_assignment(state, aid, *actor, expected_epoch=epoch)
    expect(dispatch.get("status") == "SUBMITTED", "dispatch", "dispatch A failed: " + json.dumps(dispatch)[:400])
    emit({"stage": "dispatch", "status": dispatch["status"], "assignment_id": aid,
          "workspace_id": ws_a["workspace_id"],
          "workspace_status": one(state, "SELECT status FROM v2_workspace WHERE workspace_id=?",
                                  (ws_a["workspace_id"],))["status"]})
    baseline = request_a["worker"]["baseline_completion_seq"]
    observed = wait_turn(Path(run["worker_spec"]["expected_identity"]["launch_cwd"]), baseline,
                         deadline, "worker_turn")
    result_path = Path(request_a["cwd"]) / "RESULT.md"
    handoff_path = Path(request_a["cwd"]) / "HANDOFF.md"
    expect(result_path.is_file() and handoff_path.is_file(), "worker_turn", "native bridge did not write delivery artifacts")
    artifact = result_path.read_text(encoding="utf-8")
    expect(request_a["completion"]["marker"] in handoff_path.read_text(encoding="utf-8"), "worker_turn",
           "delivery marker is missing")
    emit({"stage": "worker_turn", "status": observed, "result_sha256": sha(result_path),
          "handoff_sha256": sha(handoff_path), "result_bytes": result_path.stat().st_size,
          "native_session_id": observed.get("native_session_id"), "artifact": artifact[:2000]})
    job_path = Path(run["worker_spec"]["expected_identity"]["launch_cwd"]) / "jobs" / task_key(request_a) / "job.json"
    job = monitor_task.read_json(job_path)
    expect(Path(job["cwd"]).resolve() == Path(ws_a["path"]).resolve(), "workspace_execution",
           "native job cwd is not the managed workspace: " + json.dumps(job.get("cwd")))
    inspection = workspace_runtime.inspect(state, actor[0], actor[1], "main", ws_a["workspace_id"])
    report = inspection["inspection"]
    expect(report["ok"], "workspace_execution", "managed workspace inspection failed: " + str(report.get("error")))
    expect(sorted(report["inventory"]["changed_paths"]) == ["HANDOFF.md", "RESULT.md"]
           and not report["inventory"]["undeclared_paths"] and report["inventory"]["scope_ok"],
           "workspace_execution", "workspace content is not the declared delivery: "
           + json.dumps(report["inventory"]["changed_paths"]))
    emit({"stage": "workspace_execution", "workspace_id": ws_a["workspace_id"],
          "job_cwd": job["cwd"], "worker_launch_cwd": job["identity"]["launch_cwd"],
          "status": report["status"], "changed_paths": report["inventory"]["changed_paths"],
          "undeclared_paths": report["inventory"]["undeclared_paths"],
          "scope_ok": report["inventory"]["scope_ok"],
          "fingerprint_sha256": hashlib.sha256(json.dumps(report["inventory"]["fingerprint"],
                                                          sort_keys=True).encode()).hexdigest()})
    reconciled = scheduler.reconcile_assignment(state, aid, *actor, expected_epoch=epoch)
    expect(reconciled.get("status") == "REVIEW_READY", "reconcile",
           "controller observation did not reach review_ready: " + json.dumps(reconciled)[:400])
    emit({"stage": "reconcile", "status": reconciled["status"], "event_id": reconciled.get("event_id"),
          "claims_retained": reconciled.get("claims_retained")})

    # 3. Record evidence and request observation through the worker's own credential.
    current = one(state, "SELECT revision FROM v2_assignment WHERE assignment_id=?", (aid,))["revision"]
    recorded = []
    for name in ("RESULT.md", "HANDOFF.md"):
        raw = (Path(request_a["cwd"]) / name).read_bytes()
        value = call(state, root, worker, "awo_evidence_record", args_for(
            epoch, assignment_id=aid, expected_revision=current, kind="artifact", path=name,
            sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw)))
        expect(value.get("status") == "EVIDENCE_RECORDED", "evidence", "evidence record failed: " + json.dumps(value)[:400])
        recorded.append(value)
        current = value["revision"]
    assignment = one(state, "SELECT * FROM v2_assignment WHERE assignment_id=?", (aid,))
    completed = call(state, root, worker, "awo_assignment_complete", args_for(
        epoch, assignment_id=aid, generation=assignment["generation"], expected_revision=current,
        outcome="completed", evidence_ids=[item["evidence_id"] for item in recorded],
        summary="Native turn completed and observed."))
    expect(completed.get("status") == "REVIEW_READY" and completed.get("independently_accepted") is False,
           "complete", "completion did not reach independent review: " + json.dumps(completed)[:400])
    emit({"stage": "complete", "status": completed["status"], "event_id": completed["event_id"],
          "evidence_ids": [item["evidence_id"] for item in recorded], "independently_accepted": False,
          "task_state": one(state, "SELECT task_state FROM v2_task WHERE task_id='NATIVE-A'")["task_state"]})

    # 4. Queue the dependent Task only after A is review_ready, then prove it is still blocked.
    prepared_b = run["prepared"]["NATIVE-B"]
    prepared_b["request"]["worker"]["baseline_completion_seq"] = native_worker.status(
        run["worker_spec"]["expected_identity"]["launch_cwd"])["completion_seq"]
    message_b = json.dumps({"task_id": prepared_b["request"]["task_id"],
                            "nonce": prepared_b["request"]["nonce"], "cwd": prepared_b["request"]["cwd"],
                            "prompt": prepared_b["prompt"], "marker": prepared_b["request"]["completion"]["marker"],
                            "authorization": {"source": prepared_b["request"]["authorization"]["source"],
                                              "terminate_on_timeout": True}}, ensure_ascii=True)
    queued_b = command(state, actor, epoch, "scheduler.enqueue", {
        "task_id": "NATIVE-B", "expected_revision": run["tasks"]["NATIVE-B"]["revision"],
        "request": prepared_b["request"], "message": message_b, "agent_id": run["worker_agent"],
        "budget": {"tokens.input": 40000, "tokens.output": 8000}, "resources": {"native_slot": 1},
        "workspace_mode": "directory", "task_kind": "artifact", "dependencies": ["NATIVE-A"]})
    expect(queued_b.get("status") == "QUEUED", "enqueue_b", "enqueue B failed: " + json.dumps(queued_b)[:400])
    blocked = call(state, root, worker, "awo_assignment_claim", args_for(
        epoch, task_id="NATIVE-B", expected_revision=queued_b["revision"]))
    expect(blocked.get("status") == "WAITING" and
           not one(state, "SELECT 1 FROM v2_assignment WHERE task_id='NATIVE-B'"), "dependency_gate",
           "completed-but-unaccepted A did not block B: " + json.dumps(blocked)[:400])
    emit({"stage": "dependency_gate", "status": blocked["status"], "reason": blocked.get("reason"),
          "queue_b": queued_b})

    # 5. Self-review negative validation with otherwise valid review arguments.
    #    (a) The executing Agent's real worker credential must fail the role gate.
    #    (b) The trusted issuer must refuse a reviewer credential for that binding.
    #    (c) A synthetic, fixture-only mis-issued reviewer credential bound to the
    #        executing Agent must still hit the service same-agent guard, with no
    #        review/acceptance/claim side effect.
    current_revision = one(state, "SELECT revision FROM v2_assignment WHERE assignment_id=?",
                           (aid,))["revision"]
    notes = Path(root) / "notes"
    notes.mkdir(exist_ok=True)
    synthetic_note = notes / "same-agent-review-note.md"
    synthetic_note.write_text(request_a["task_id"] + " " + request_a["nonce"] +
                              "\nVerdict: accept\nSynthetic same-agent negative check.\n",
                              encoding="utf-8", newline="\n")
    valid_review = dict(assignment_id=aid, expected_revision=current_revision, task_id="NATIVE-A",
                        event_id=completed["event_id"],
                        evidence_ids=[item["evidence_id"] for item in recorded], verdict="accept",
                        review_note_path=synthetic_note.relative_to(root).as_posix(),
                        review_note_sha256=sha(synthetic_note))
    before = review_snapshot(state, aid, completed["event_id"])
    worker_args = args_for(epoch, **valid_review)
    worker_denied = None
    try:
        call(state, root, worker, "awo_review_submit", worker_args)
    except Exception as exc:
        worker_denied = {"type": type(exc).__name__, "code": getattr(exc, "code", None),
                         "message": str(exc)}
    expect(worker_denied == {"type": "APIError", "code": "forbidden_role",
                             "message": "This credential may not call awo_review_submit"}, "self_review",
           "worker credential was not rejected by the exact role gate: " + json.dumps(worker_denied))
    rejected = one(state, "SELECT payload_json FROM v2_event WHERE event_type='command.rejected' "
                          "AND entity_id=?", (worker_args["command_id"],))
    expect(rejected and json.loads(rejected["payload_json"]).get("code") == "forbidden_role"
           and json.loads(rejected["payload_json"]).get("tool") == "awo_review_submit", "self_review",
           "role rejection was not recorded as a rejected command")
    after_worker = review_snapshot(state, aid, completed["event_id"])
    expect(after_worker == before, "self_review", "worker self-review attempt changed review/assignment state")
    emit({"stage": "self_review_denied", "actor": "worker-credential", "status": "DENIED", **worker_denied,
          "state_unchanged": True, "reviews": len(before["reviews"]),
          "workspace_reviews": len(before["workspace_reviews"]),
          "holds_slot": before["assignment"]["holds_slot"], "rejection_event": json.loads(rejected["payload_json"])})

    executor_binding = json.loads(one(state, "SELECT contract_json FROM v2_scheduler_intent "
                                            "WHERE assignment_id=?", (aid,))["contract_json"])["worker_binding"]
    tokens_before = one(state, "SELECT count(*) AS n FROM v2_access_token")["n"]
    issuance_denied = None
    try:
        access.issue_token(state, actor[0], actor[1], "main", {
            "tool": "access.issue", "payload": {
                "project_root": str(root), "role": "reviewer", "agent_id": run["worker_agent"],
                "binding_id": executor_binding["instance_id"],
                "binding_version": executor_binding["version"], "task_id": "NATIVE-A"},
            "command_id": "v2native-access-" + uuid.uuid4().hex, "expected_epoch": epoch},
            str(Path(root) / "secrets" / "executor-reviewer-rejected.token"))
    except Exception as exc:
        issuance_denied = {"type": type(exc).__name__, "message": str(exc)}
    expect(issuance_denied == {"type": "ValueError",
                               "message": "actor does not hold controller permission for review"},
           "reviewer_issuance_denied",
           "reviewer issuance for the executing binding was not rejected exactly: " + json.dumps(issuance_denied))
    expect(one(state, "SELECT count(*) AS n FROM v2_access_token")["n"] == tokens_before
           and not (Path(root) / "secrets" / "executor-reviewer-rejected.token").exists(),
           "reviewer_issuance_denied", "rejected issuance left a credential behind")
    emit({"stage": "reviewer_issuance_denied", "actor": "executing-binding", "status": "DENIED",
          **issuance_denied, "tokens_unchanged": True})

    executor_row = stored_binding(state, executor_binding["instance_id"])
    expect(executor_row and executor_row["status"] == "active" and executor_row["mode"] == "worker",
           "self_review_same_agent", "executor binding is not the active worker binding")
    synthetic_token_id = None
    same_agent_denied = None
    try:
        changed = json.loads(executor_row["data"])
        changed["mode"] = "reviewer"
        set_binding_record(state, executor_row["id"], "reviewer", json.dumps(changed, sort_keys=True))
        issued = access.issue_token(state, actor[0], actor[1], "main", {
            "tool": "access.issue", "payload": {
                "project_root": str(root), "role": "reviewer", "agent_id": run["worker_agent"],
                "binding_id": executor_binding["instance_id"],
                "binding_version": executor_binding["version"], "task_id": "NATIVE-A"},
            "command_id": "v2native-access-" + uuid.uuid4().hex, "expected_epoch": epoch},
            str(Path(root) / "secrets" / "executor-reviewer.token"))
        synthetic_token_id = issued["token_id"]
        synthetic = Path(issued["token_file"]).read_text(encoding="utf-8").strip()
        try:
            call(state, root, synthetic, "awo_review_submit", args_for(epoch, **valid_review))
        except Exception as exc:
            same_agent_denied = {"type": type(exc).__name__, "code": getattr(exc, "code", None),
                                 "message": str(exc)}
    finally:
        set_binding_record(state, executor_row["id"], executor_row["mode"], executor_row["data"])
    expect(same_agent_denied == {"type": "APIError", "code": "forbidden_role",
                                 "message": "Executors cannot review their own delivery"},
           "self_review_same_agent",
           "same-agent reviewer credential was not rejected exactly: " + json.dumps(same_agent_denied))
    if synthetic_token_id:
        access.revoke_token(state, actor[0], actor[1], "main", {
            "tool": "access.revoke",
            "payload": {"token_id": synthetic_token_id, "expected_revision": 1},
            "command_id": "v2native-access-" + uuid.uuid4().hex, "expected_epoch": epoch})
    restored = stored_binding(state, executor_row["id"])
    expect(restored == executor_row, "self_review_same_agent",
           "executor binding was not restored verbatim after the synthetic check")
    after_same_agent = review_snapshot(state, aid, completed["event_id"])
    expect(after_same_agent == before, "self_review_same_agent",
           "same-agent review attempt changed review/assignment/claim state")
    self_review = {"worker_role": worker_denied, "issuer": issuance_denied,
                   "same_agent": same_agent_denied,
                   "state_unchanged": True,
                   "executor_binding_restored": True,
                   "synthetic_credential": "fixture-only mis-issued binding mode; reverted and revoked"}
    emit({"stage": "self_review_same_agent_denied", "actor": "synthetic-reviewer-credential",
          "status": "DENIED", **same_agent_denied, "state_unchanged": True,
          "reviews": len(before["reviews"]), "workspace_reviews": len(before["workspace_reviews"]),
          "holds_slot": before["assignment"]["holds_slot"],
          "executor_binding_restored": True, "synthetic_credential_revoked": bool(synthetic_token_id)})

    # 6. A distinct native Reviewer turn examines the artifact.
    expected = ("the JSON must contain secret=" + run["secret"] + " and answer=323") if run["secret"] else \
        "the only content must be the integer 323"
    requirement = ("the artifact is a JSON object whose answer is the product of 17 and 19 and whose secret "
                   "matches the value the worker received earlier") if run["secret"] else \
        "the artifact contains the product of 17 and 19"
    review, review_dir = review_native(root, run, request_a, artifact, requirement, expected, deadline)
    emit({"stage": "review", "review": review, "review_cwd": str(review_dir),
          "artifact_sha256": sha(Path(request_a["cwd"]) / "RESULT.md")})
    if review["verdict"] != "accept":
        return finish(root, {"status": "REVIEW_REJECTED", "stage": "review", "review": review,
                             "model_based_review": meta["evidence_class"] == "native"}, traces)

    # 7. Submit the model verdict through the distinct reviewer credential, then close as Controller.
    note = Path(request_a["review_path"])
    note.write_text(request_a["task_id"] + " " + request_a["nonce"] + "\nVerdict: accept\n" +
                    "Independent native review: " + str(review.get("reason", "")) + "\n",
                    encoding="utf-8", newline="\n")
    reviewed = call(state, root, reviewer, "awo_review_submit", args_for(
        epoch, assignment_id=aid, expected_revision=one(
            state, "SELECT revision FROM v2_assignment WHERE assignment_id=?", (aid,))["revision"],
        task_id="NATIVE-A", event_id=completed["event_id"],
        evidence_ids=[item["evidence_id"] for item in recorded], verdict="accept",
        review_note_path=note.resolve().relative_to(root).as_posix(), review_note_sha256=sha(note)))
    expect(reviewed.get("status") == "REVIEW_RECORDED" and reviewed.get("claims_retained") is True,
           "review_submit", "review submit failed: " + json.dumps(reviewed)[:400])
    sealed_ws = one(state, "SELECT workspace_id,inventory_sha256,verdict,inventory_json FROM "
                           "v2_workspace_review WHERE assignment_id=?", (aid,))
    expect(sealed_ws and sealed_ws["verdict"] == "accept" and sealed_ws["workspace_id"] == ws_a["workspace_id"],
           "workspace_review", "review gate did not seal the managed workspace inventory")
    inventory_a = json.loads(sealed_ws["inventory_json"])
    expect(inventory_a["scope_ok"] and not inventory_a["undeclared_paths"]
           and sorted(inventory_a["changed_paths"]) == ["HANDOFF.md", "RESULT.md", "REVIEW.md"],
           "workspace_review", "sealed workspace inventory is not the declared delivery: "
           + json.dumps(inventory_a["changed_paths"]))
    emit({"stage": "workspace_review", "workspace_id": sealed_ws["workspace_id"],
          "verdict": sealed_ws["verdict"], "inventory_sha256": sealed_ws["inventory_sha256"],
          "changed_paths": inventory_a["changed_paths"], "undeclared_paths": inventory_a["undeclared_paths"],
          "scope_ok": inventory_a["scope_ok"]})
    revision = one(state, "SELECT revision FROM v2_assignment WHERE assignment_id=?", (aid,))["revision"]
    closed = command(state, actor, epoch, "assignment.close", {
        "assignment_id": aid, "expected_revision": revision, "outcome": "accepted",
        "event_id": completed["event_id"]})
    expect(closed.get("status") == "CLOSED" and closed.get("outcome") == "accepted", "close",
           "controller close failed: " + json.dumps(closed)[:400])
    emit({"stage": "review_submit", "status": reviewed["status"], "claims_retained": True})
    emit({"stage": "close", **closed})
    sealed = {"task": one(state, "SELECT task_state,accepted_evidence_id FROM v2_task WHERE task_id='NATIVE-A'"),
              "evidence": rows(state, "SELECT path,sealed_sha256,bytes,sealed,state FROM v2_evidence "
                                      "WHERE task_id='NATIVE-A' ORDER BY path"),
              "review": rows(state, "SELECT verdict,note_path,note_sha256,state FROM v2_review "
                                    "WHERE task_id='NATIVE-A'")}
    expect(sealed["task"]["task_state"] == "accepted" and sealed["review"], "close", "accepted state is incomplete")
    emit({"stage": "sealed_delivery", **sealed})

    # 7b. Retain the accepted workspace: freeze needs closed quiescence, so it must
    #     happen before the dependent turn changes the worker sequence.
    ws_revision = one(state, "SELECT revision FROM v2_workspace WHERE workspace_id=?",
                      (ws_a["workspace_id"],))["revision"]
    frozen = workspace_call(state, actor, epoch, "workspace.freeze", {
        "workspace_id": ws_a["workspace_id"], "expected_revision": ws_revision,
        "evidence_root": str(Path(root) / "quiescence")})
    expect(frozen.get("status") == "WORKSPACE_FROZEN" and frozen["workspace"]["status"] == "frozen",
           "workspace_freeze", "accepted workspace was not frozen: " + json.dumps(frozen)[:600])
    ws_frozen = one(state, "SELECT workspace_id,status,revision,quiescence_json FROM v2_workspace "
                           "WHERE workspace_id=?", (ws_a["workspace_id"],))
    quiescence = json.loads(ws_frozen["quiescence_json"])
    expect(Path(quiescence["evidence_path"]).is_file()
           and sha(quiescence["evidence_path"]) == quiescence["evidence_sha256"]
           and not workspace_lib.paths_overlap(quiescence["evidence_path"], ws_a["path"]),
           "workspace_freeze", "freeze quiescence evidence is missing, changed, or inside the workspace")
    emit({"stage": "workspace_freeze", "workspace_id": ws_frozen["workspace_id"],
          "status": ws_frozen["status"], "revision": ws_frozen["revision"],
          "quiescence_evidence": quiescence["evidence_path"],
          "quiescence_sha256": quiescence["evidence_sha256"],
          "quiescence_reason": quiescence.get("reason"),
          "assignment_holds_slot": one(state, "SELECT holds_slot FROM v2_assignment WHERE "
                                              "assignment_id=?", (aid,))["holds_slot"]})

    # 8. The dependent Task becomes eligible and is dispatched; session reuse is checked when applicable.
    unlocked = call(state, root, worker, "awo_assignment_claim", args_for(
        epoch, task_id="NATIVE-B", expected_revision=queued_b["revision"]))
    expect(unlocked.get("status") == "RESERVED", "unlock", "accepted A did not unlock B: " + json.dumps(unlocked)[:400])
    bid = unlocked["assignment_id"]
    ws_b, spec_b, marker_b = create_managed_workspace(
        state, root, actor, epoch, bid, prepared_b["request"]["cwd"], ["HANDOFF.md", "RESULT.md"])
    emit({"stage": "workspace_create_b", "workspace_id": ws_b["workspace_id"], "assignment_id": bid,
          "kind": ws_b["kind"], "status": ws_b["status"], "path": ws_b["path"],
          "allowed_paths": spec_b["allowed_paths"], "spec_sha256": ws_b["spec_sha256"],
          "marker_sha256": marker_b,
          "assignment_workspace_id": one(state, "SELECT workspace_id FROM v2_assignment WHERE "
                                               "assignment_id=?", (bid,))["workspace_id"],
          "claims_retained": True})
    dispatched_b = scheduler.dispatch_assignment(state, bid, *actor, expected_epoch=epoch)
    expect(dispatched_b.get("status") == "SUBMITTED", "unlock", "dispatch B failed: " + json.dumps(dispatched_b)[:400])
    observed_b = wait_turn(Path(run["worker_spec"]["expected_identity"]["launch_cwd"]),
                           prepared_b["request"]["worker"]["baseline_completion_seq"], deadline, "worker_turn_b")
    text_b = (Path(prepared_b["request"]["cwd"]) / "RESULT.md").read_text(encoding="utf-8")
    job_b = monitor_task.read_json(Path(run["worker_spec"]["expected_identity"]["launch_cwd"]) /
                                   "jobs" / task_key(prepared_b["request"]) / "job.json")
    expect(Path(job_b["cwd"]).resolve() == Path(ws_b["path"]).resolve(), "workspace_execution_b",
           "dependent native job cwd is not its managed workspace")
    inspection_b = workspace_runtime.inspect(state, actor[0], actor[1], "main", ws_b["workspace_id"])
    expect(inspection_b["inspection"]["ok"]
           and sorted(inspection_b["inspection"]["inventory"]["changed_paths"]) == ["HANDOFF.md", "RESULT.md"]
           and inspection_b["inspection"]["inventory"]["scope_ok"], "workspace_execution_b",
           "dependent workspace content is not the declared delivery: "
           + json.dumps(inspection_b["inspection"]["inventory"]["changed_paths"]))
    outcome_a = monitor_task.read_json(Path(run["worker_spec"]["expected_identity"]["launch_cwd"]) /
                                       "jobs" / task_key(request_a) / "outcome.json")
    outcome_b = monitor_task.read_json(Path(run["worker_spec"]["expected_identity"]["launch_cwd"]) /
                                       "jobs" / task_key(prepared_b["request"]) / "outcome.json")
    session = {"a": outcome_a.get("native_session_id"), "b": outcome_b.get("native_session_id"),
               "execution_semantics": run["mode"], "recall": None}
    if run["mode"] == "session":
        expect(session["a"] and session["a"] == session["b"], "session_reuse",
               "native session identifier was not reused")
        value = parse_json_text(text_b)
        session["recall"] = value.get("secret") == run["secret"] and value.get("answer") == 324
        expect(session["recall"], "session_reuse", "session did not recall the earlier secret/result")
    emit({"stage": "dependent_dispatch", "status": dispatched_b["status"], "assignment_id": bid,
          "workspace_id": ws_b["workspace_id"], "job_cwd": job_b["cwd"],
          "worker_status": observed_b, "result_sha256": sha(Path(prepared_b["request"]["cwd"]) / "RESULT.md"),
          "session": session, "artifact": text_b[:2000]})
    final = {"task_a": sealed,
             "assignment_a": one(state, "SELECT state,review_status,holds_slot,revision,workspace_id "
                                        "FROM v2_assignment WHERE assignment_id=?", (aid,)),
             "workspace_a": workspace_summary(state, aid),
             "task_b": one(state, "SELECT task_state FROM v2_task WHERE task_id='NATIVE-B'"),
             "assignment_b": one(state, "SELECT state,review_status,holds_slot,revision,workspace_id "
                                        "FROM v2_assignment WHERE assignment_id=?", (bid,)),
             "workspace_b": workspace_summary(state, bid)}
    emit({"stage": "final_state", **final})
    payload = {"status": "V2_NATIVE_LOOP_PASS", "stage": "complete", "evidence_class": meta["evidence_class"],
               "execution_semantics": run["mode"], "session": session, "self_review": self_review,
               "herdr_invoked": False, "task_a": sealed, "assignment_a": aid, "assignment_b": bid,
               "workspace_a": final["workspace_a"], "workspace_b": final["workspace_b"],
               "final": final}
    return finish(root, payload, traces)


def finish(root, result, traces):
    root = Path(root)
    meta = monitor_task.read_json(root / "provenance.json")
    observed = []
    for role in ("worker", "reviewer"):
        for path in sorted((root / role / "jobs").glob("*/receipt/status.json")):
            receipt = monitor_task.read_json(path)
            turn = receipt.get("native_turn", {})
            observed.append({"role": role, "task_id": receipt.get("task_id"),
                             "provider": turn.get("provider"), "model": turn.get("model"),
                             "turn_status": turn.get("status", "unverified"),
                             "process_status": receipt.get("process_status", "unverified"),
                             "returncode": receipt.get("returncode")})
    meta["model_selection"]["observed"] = observed
    monitor_task.save_json(root / "provenance.json", meta)
    save(root / "trace.json", traces)
    save(root / "usage.json", usage(root))
    result["evidence_class"] = meta["evidence_class"]
    result["model_based_review"] = meta["evidence_class"] == "native"
    if result.get("status") == "V2_NATIVE_LOOP_PASS" and meta["evidence_class"] == "protocol-fixture":
        result["status"] = "PROTOCOL_FIXTURE_PASS"
    save(root / "result.json", result)
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name not in ("manifest.json", "runner.log") and "secrets" not in path.parts:
            files.append({"path": path.relative_to(root).as_posix(), "bytes": path.stat().st_size,
                          "sha256": sha(path)})
    save(root / "manifest.json", {"recorded_at": monitor_task.utc_now(), "status": result["status"],
                                  "files": files, "secrets_excluded": True})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--harness", choices=("codex", "pi", "omp"))
    parser.add_argument("--executable")
    parser.add_argument("--mode", choices=("one-shot", "session"), default="one-shot")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--evidence-class", choices=EVIDENCE_CLASSES, default="native")
    parser.add_argument("--fixture-script", help="only for labelled protocol-fixture evidence")
    args = parser.parse_args()
    root = Path(args.directory).resolve()
    if root.exists():
        parser.error("a new validation directory is required; existing evidence is never overwritten")
    try:
        if not args.harness:
            parser.error("--harness is required")
        candidates = ([args.harness + suffix for suffix in (".exe", ".cmd", "")]
                      if sys.platform == "win32" else [args.harness])
        executable = args.executable or next((found for name in candidates if (found := shutil.which(name))), None)
        if not executable:
            parser.error("native CLI executable not found")
        prepare(root, args.harness, executable, args.mode, args.timeout, args.evidence_class, args.fixture_script)
        result = execute(root)
    except Exception as exc:
        if not root.exists():
            raise
        result = {"status": "V2_NATIVE_LOOP_FAILED", "stage": getattr(exc, "stage", "unknown"),
                  "error": str(exc), "herdr_invoked": False}
        try:
            meta = monitor_task.read_json(root / "provenance.json")
            result["evidence_class"] = meta.get("evidence_class", args.evidence_class)
            finish(root, result, [])
        except Exception as nested:
            result["finish_error"] = str(nested)
        print(json.dumps(result, ensure_ascii=True))
        return 2
    print(json.dumps({key: value for key, value in result.items() if key not in ("task_a",)},
                     ensure_ascii=True))
    return 0 if result["status"] in ("V2_NATIVE_LOOP_PASS", "PROTOCOL_FIXTURE_PASS") else 2


if __name__ == "__main__":
    sys.exit(main())
