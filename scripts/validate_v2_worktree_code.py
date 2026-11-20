"""V2 managed-worktree code-loop acceptance with real native Codex turns.

Opt-in and writes a new directory only. It creates a disposable Git repository,
one Task per code worker, binds each Assignment to its own real Git worktree at
an immutable base commit, sends the frozen execution contract through the V2
scheduler to the installed Codex bridge, commits the reviewed patch, seals the
native acceptance, runs the local Merge Queue (build, independent integration
review, CAS landing), materializes a downstream base, and finishes by releasing
both worktrees. No user repository, binding, ledger or monitor is touched.

The evidence classes are explicit:
  * real native turns      -- installed Codex edits and reviews actual bytes
  * real Git/process tests -- worktrees, refs, CAS, test commands, release
  * synthetic protocol     -- same-agent credential and duplicate-command probes
  * reused prior tests     -- cited, not re-run here (merge_queue suite)
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
import merge_queue
import monitor_task
import native_worker
import priority_scheduler as scheduler
import service_access as access
import service_extensions
import workflow_control as control
import workspace_manager as workspace_lib
import workspace_runtime
from validate_v2_native_loop import (Failure, args_for, call, command, expect, issue, one,
                                     parse_json_text, provenance, rows, save, sha, trace,
                                     wait_turn, workspace_call)
from process_util import pid_alive
from workflow_evidence import allocate, task_key

TASK_A = "CODE-A"
TASK_B = "CODE-B"
NATIVE_TASK_A = "CODE-A-RUN"
NATIVE_TASK_B = "CODE-B-RUN"


def git(repo, *args, ok=(0,)):
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=180)
    if proc.returncode not in ok:
        raise Failure("git", "git " + " ".join(args) + ": " + proc.stderr[-2000:])
    return proc.stdout.strip("\r\n")


def run_test(root, label, cwd):
    """Run the module's real correctness test under a clean no-bytecode interpreter."""
    argv = [sys.executable, "-B", "-m", "unittest", "test_calc"]
    proc = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=180)
    value = {"label": label, "argv": argv, "cwd": str(cwd), "returncode": proc.returncode,
             "stdout": proc.stdout, "stderr": proc.stderr}
    save(Path(root) / "evidence" / (label + ".json"), value)
    return value


def write_new(path, data, encoding=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        with path.open("x", encoding=encoding or "utf-8", newline="\n") as stream:
            stream.write(data)
    return path


def request_for(task_id, nonce, cwd, state_path, worker, reviewer):
    return {
        "task_id": task_id, "nonce": nonce, "cwd": str(cwd), "generation": 1,
        "workflow_state": str(state_path),
        "worker": dict(worker, baseline_completion_seq=0),
        "reviewer": dict(reviewer), "dispatch_confirmed": False,
        "authorization": {"source": "Owner-approved bounded V2 worktree code fixture",
                          "dispatch_worker": True, "notify_reviewer": False},
        "completion": {"binding_artifact": "HANDOFF.md", "artifacts": ["RESULT.md"]},
        "monitor": {"interval_seconds": 1, "settle_grace_seconds": 3, "command_timeout_seconds": 15},
    }


def freeze_request(root, request, message):
    delivery = allocate(request, Path(root) / "delivery")
    delivery["worker"]["baseline_completion_seq"] = 0
    body = {"task_id": delivery["task_id"], "nonce": delivery["nonce"], "cwd": delivery["cwd"],
            "prompt": message.pop("prompt"), "marker": delivery["completion"]["marker"],
            "authorization": {"source": delivery["authorization"]["source"],
                              "terminate_on_timeout": True}}
    body.update(message)
    text = json.dumps(body, ensure_ascii=True)
    return delivery, text


def prepare(root, executable, timeout):
    expect(os.environ.get("HERDR_ENV") != "1", "prepare",
           "native worktree acceptance requires a caller outside Herdr")
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    meta = provenance(root, "codex", executable, "one-shot", "native")
    meta["timeout_seconds"] = timeout
    save(root / "provenance.json", meta)
    version_file = root / "version.json"
    observed = "unknown"
    if version_file.exists():
        observed = str(json.loads(version_file.read_text(encoding="utf-8")).get("stdout", "")).strip() or "unknown"

    repo, workspaces, delivery_root = root / "repo", root / "workspaces", root / "delivery"
    for directory in (repo, workspaces, delivery_root, root / "evidence", root / "notes",
                      root / "quiescence", root / "secrets", root / "seed", root / "review-runs",
                      root / "merge-ops", root / "merge-archive"):
        directory.mkdir()
    write_new(repo / "calc.py", "def add(a, b):\n    return a - b\n", encoding="ascii")
    write_new(repo / "test_calc.py",
              "import unittest\nfrom calc import add\n\n\nclass CalcTest(unittest.TestCase):\n"
              "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n\n\n"
              "if __name__ == '__main__':\n    unittest.main()\n", encoding="ascii")
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "AWO Code Fixture")
    git(repo, "config", "user.email", "awo@example.invalid")
    git(repo, "config", "core.autocrlf", "false")
    git(repo, "config", "core.eol", "lf")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "fixture base: add() subtracts")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "update-ref", "refs/heads/integration", base)
    expect(git(repo, "status", "--porcelain") == "", "prepare", "fixture repository is not clean")
    base_test = run_test(root, "base-test", repo)
    expect(base_test["returncode"] != 0 and "FAILED" in (base_test["stdout"] + base_test["stderr"]),
           "prepare", "base fixture must fail the correctness test")

    plan_a = (workspaces / "code-a").resolve()
    plan_b = (workspaces / "code-b").resolve()
    specs = {
        "planner": dict(native_worker.initialize(root / "controller", "codex", executable,
                                                 "one-shot", timeout),
                        role="planner", version=observed),
        "executor": dict(native_worker.initialize(root / "worker-a", "codex", executable,
                                                  "one-shot", timeout, launch_cwd=str(plan_a)),
                         role="executor", version=observed),
        "executor2": dict(native_worker.initialize(root / "worker-b", "codex", executable,
                                                   "one-shot", timeout, launch_cwd=str(plan_b)),
                          role="executor2", version=observed),
        "reviewer": dict(native_worker.initialize(root / "reviewer", "codex", executable,
                                                  "one-shot", timeout),
                         role="reviewer", context_policy="inbox_only", version=observed),
    }
    cfg = {"roles": {name: {} for name in ("planner", "executor", "executor2", "reviewer")},
           "workflow": {"reviewer_role": "reviewer", "dispatch_role": "planner",
                        "transitions": {"accept": "next", "repair": "repair", "reject": "stop"}},
           "scheduler": {"max_concurrency": 1, "resources": {"native_slot": 1}}}
    state = root / "state"
    control.initialize(state, cfg)

    def identity(spec):
        return monitor_task.Transport({}).status(spec)["identity"]

    planner = control.bind(state, "planner", specs["planner"], identity(specs["planner"]),
                           "controller", "inbox_only")
    executor_a = control.bind(state, "executor", specs["executor"], identity(specs["executor"]), "worker")
    executor_b = control.bind(state, "executor2", specs["executor2"], identity(specs["executor2"]), "worker")
    reviewer = control.bind(state, "reviewer", specs["reviewer"], identity(specs["reviewer"]),
                            "reviewer", "inbox_only")
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
    agents = {"planner": service._agent_id_for_binding(planner["instance_id"]),
              "executor": service._agent_id_for_binding(executor_a["instance_id"]),
              "executor2": service._agent_id_for_binding(executor_b["instance_id"]),
              "reviewer": service._agent_id_for_binding(reviewer["instance_id"])}
    epoch = service.set_enabled(state, *actor, True)["epoch"]
    capability = root / "capability-evidence.json"
    save(capability, {"kind": "installed-cli-command-bridge", "harness": "codex",
                      "version": observed, "mode": "one-shot",
                      "worker_config_sha256": {
                          "executor": sha(root / "worker-a" / "config.json"),
                          "executor2": sha(root / "worker-b" / "config.json")},
                      "bridge_source_sha256": sha(Path(native_worker.__file__)),
                      "run_job_source_sha256": sha(Path(native_worker.__file__).with_name("run_job.py"))})
    for role in ("executor", "executor2"):
        for key in scheduler.DEFAULT_CAPABILITIES:
            agent = service.agent_get(state, *actor, agents[role])["agent"]
            service.set_agent_capability(state, *actor, {
                "tool": "agent.capability",
                "payload": {"agent_id": agent["agent_id"], "capability_key": key, "level": "supported",
                            "harness_version": observed, "os": sys.platform, "host": "local",
                            "checked_at": service._now(), "evidence_path": str(capability),
                            "evidence_sha256": sha(capability), "source": "live_probe",
                            "expected_revision": agent["revision"]},
                "command_id": "v2code-cap-" + uuid.uuid4().hex, "expected_epoch": epoch})

    secrets = root / "secrets"
    controller_token = issue(state, actor, epoch, {
        "project_root": str(root), "role": "controller", "agent_id": agents["planner"],
        "binding_id": planner["instance_id"], "binding_version": planner["version"]},
        secrets / "controller.token")
    merge_token = issue(state, actor, epoch, {
        "project_root": str(repo), "role": "controller", "agent_id": agents["planner"],
        "binding_id": planner["instance_id"], "binding_version": planner["version"]},
        secrets / "merge.token")
    worker_a_token = issue(state, actor, epoch, {
        "project_root": str(root), "role": "worker", "agent_id": agents["executor"]},
        secrets / "worker-a.token")
    worker_b_token = issue(state, actor, epoch, {
        "project_root": str(root), "role": "worker", "agent_id": agents["executor2"]},
        secrets / "worker-b.token")

    tasks = {}
    for name, title in ((TASK_A, "Disposable code Task A"), (TASK_B, "Disposable code Task B")):
        created = call(state, root, controller_token, "awo_task_create", args_for(
            epoch, task_id=name, title=title, body="Synthetic local code fixture; no user data.",
            priority="P3"))
        expect(created.get("status") == "CREATED", "prepare", "task creation failed for " + name)
        tasks[name] = created
    reviewer_a_token = issue(state, actor, epoch, {
        "project_root": str(root), "role": "reviewer", "agent_id": agents["reviewer"],
        "binding_id": reviewer["instance_id"], "binding_version": reviewer["version"],
        "task_id": TASK_A}, secrets / "reviewer-a.token")
    reviewer_b_token = issue(state, actor, epoch, {
        "project_root": str(root), "role": "reviewer", "agent_id": agents["reviewer"],
        "binding_id": reviewer["instance_id"], "binding_version": reviewer["version"],
        "task_id": TASK_B}, secrets / "reviewer-b.token")
    merge_reviewer_token = issue(state, actor, epoch, {
        "project_root": str(repo), "role": "reviewer", "agent_id": agents["reviewer"],
        "binding_id": reviewer["instance_id"], "binding_version": reviewer["version"],
        "task_id": TASK_A}, secrets / "merge-reviewer.token")

    prompt_a = ("You are in a small disposable Python repository worktree. Fix calc.py so add(a, b) returns "
                "the sum. Add one unittest case to test_calc.py covering negative numbers. Only modify "
                "calc.py and test_calc.py. Do not run git. Do not run tests. Do not create, rename or "
                "delete any other file. Reply with one short line.")
    prompt_b = ("You are in a small disposable Python repository worktree. Add a function mul(a, b) to "
                "calc.py returning the product. Append one unittest case to test_calc.py covering "
                "mul(3, 4) == 12. Only modify calc.py and test_calc.py. Do not run git. Do not run tests. "
                "Do not create, rename or delete any other file. Reply with one short line.")
    prepared = {}
    for name, prompt, plan, worker in ((TASK_A, prompt_a, plan_a, specs["executor"]),
                                       (TASK_B, prompt_b, plan_b, specs["executor2"])):
        nonce = uuid.uuid4().hex
        native = TASK_A + "-RUN" if name == TASK_A else TASK_B + "-RUN"
        request = request_for(native, nonce, root / "seed", state, worker, specs["reviewer"])
        delivery, message = freeze_request(root, request, {
            "prompt": prompt, "execution_cwd": str(plan), "sandbox": "workspace-write"})
        prepared[name] = {"request": delivery, "message": message, "prompt": prompt, "workspace": str(plan)}
    queued = command(state, actor, epoch, "scheduler.enqueue", {
        "task_id": TASK_A, "expected_revision": tasks[TASK_A]["revision"],
        "request": prepared[TASK_A]["request"], "message": prepared[TASK_A]["message"],
        "agent_id": agents["executor"], "budget": {"tokens.input": 40000, "tokens.output": 8000},
        "resources": {"native_slot": 1}, "workspace_mode": "git_worktree", "task_kind": "code",
        "workspace_base_commit": base, "dependencies": []})
    expect(queued.get("status") == "QUEUED", "prepare", "enqueue A failed: " + json.dumps(queued)[:400])
    run = {"state": str(state), "root": str(root), "repo": str(repo), "base": base,
           "project_id": project_id, "actor": list(actor), "epoch": epoch, "timeout": timeout,
           "agents": agents, "specs": specs, "tasks": tasks, "prepared": prepared,
           "queue_a": queued, "started_at": monitor_task.utc_now(),
           "tokens": {name: str(secrets / (name + ".token")) for name in (
               "controller", "merge", "worker-a", "worker-b", "reviewer-a", "reviewer-b",
               "merge-reviewer")}}
    save(root / "run.json", run)
    save(root / "repo.json", {"base": base, "target_ref": "refs/heads/integration",
                              "base_test": base_test["returncode"]})
    return root


def token_of(run, name):
    return Path(run["tokens"][name]).read_text(encoding="utf-8").strip()


def stage(root, traces, name, **values):
    event = trace(root, dict(values, stage=name))
    traces.append(event)
    return event


def reviewer_turn(root, label, prompt, files, timeout, reviewer_spec, worktree=None):
    """One real read-only Codex turn over controller-prepared bytes or a real worktree."""
    if worktree is not None:
        directory = Path(worktree)
    else:
        directory = Path(root) / "review-runs" / (label + "-" + uuid.uuid4().hex[:8])
        directory.mkdir(parents=True, exist_ok=False)
    for name, data in files.items():
        write_new(directory / name, data, encoding="ascii" if name.endswith(".txt") or name.endswith(".md")
                  else "utf-8")
    payload = {"task_id": "REVIEW-" + label, "nonce": uuid.uuid4().hex, "cwd": str(directory),
               "prompt": prompt,
               "authorization": {"source": "bounded real Codex reviewer for worktree code acceptance",
                                 "terminate_on_timeout": True}}
    state_root = Path(reviewer_spec["expected_identity"]["launch_cwd"])
    baseline = native_worker.status(state_root)["completion_seq"]
    sent = native_worker.send(state_root, payload)
    trace(root, {"stage": "reviewer_send", "label": label, "response": sent,
                 "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest()})
    observed = wait_turn(state_root, baseline, time.monotonic() + timeout * 2 + 120, "review_" + label)
    raw = (directory / "RESULT.md").read_text(encoding="utf-8")
    value = parse_json_text(raw)
    trace(root, {"stage": "reviewer_turn", "label": label, "status": observed, "review": value,
                 "result_sha256": sha(directory / "RESULT.md")})
    return value, directory, raw


def code_change_view(worktree, base):
    patch = subprocess.run(["git", "-C", str(worktree), "diff", "--binary", base, "--"],
                           capture_output=True, timeout=120)
    if patch.returncode:
        raise Failure("evidence", "diff failed: " + patch.stderr.decode("utf-8", "replace")[-1000:])
    return {"patch": patch.stdout,
            "calc": (Path(worktree) / "calc.py").read_bytes(),
            "test": (Path(worktree) / "test_calc.py").read_bytes(),
            "calc_sha": git(worktree, "hash-object", "calc.py"),
            "test_sha": git(worktree, "hash-object", "test_calc.py")}


def workspace_row(state, assignment_id):
    return one(state, "SELECT * FROM v2_workspace WHERE assignment_id=?", (assignment_id,))


def ws_row(state, workspace_id):
    con = service._connect(service._db_path(state), readonly=True)
    try:
        return workspace_lib.get_workspace(con, workspace_id)
    finally:
        con.close()


def assignment_row(state, assignment_id):
    return one(state, "SELECT * FROM v2_assignment WHERE assignment_id=?", (assignment_id,))


def evidence_ids(recorded):
    return [item["evidence_id"] for item in recorded]


def record_delivery(root, state, run, worker_token, assignment_id, delivery_cwd):
    current = one(state, "SELECT revision FROM v2_assignment WHERE assignment_id=?",
                  (assignment_id,))["revision"]
    recorded = []
    for name in ("RESULT.md", "HANDOFF.md"):
        raw = (Path(delivery_cwd) / name).read_bytes()
        value = call(state, root, worker_token, "awo_evidence_record", args_for(
            run["epoch"], assignment_id=assignment_id, expected_revision=current, kind="artifact",
            path=name, sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw)))
        expect(value.get("status") == "EVIDENCE_RECORDED", "evidence",
               "evidence record failed: " + json.dumps(value)[:400])
        recorded.append(value)
        current = value["revision"]
    return recorded


def complete_assignment(state, root, run, worker_token, assignment_id, recorded, summary):
    assignment = assignment_row(state, assignment_id)
    return call(state, root, worker_token, "awo_assignment_complete", args_for(
        run["epoch"], assignment_id=assignment_id, generation=assignment["generation"],
        expected_revision=assignment["revision"], outcome="completed",
        evidence_ids=evidence_ids(recorded), summary=summary))


def review_submit(state, root, run, token, assignment_id, task_id, event_id, recorded, note):
    return call(state, root, token, "awo_review_submit", args_for(
        run["epoch"], assignment_id=assignment_id,
        expected_revision=assignment_row(state, assignment_id)["revision"], task_id=task_id,
        event_id=event_id, evidence_ids=evidence_ids(recorded), verdict="accept",
        review_note_path=Path(note).resolve().relative_to(Path(root)).as_posix(),
        review_note_sha256=sha(note)))


def close_assignment(state, actor, run, assignment_id, event_id):
    return command(state, actor, run["epoch"], "assignment.close", {
        "assignment_id": assignment_id,
        "expected_revision": assignment_row(state, assignment_id)["revision"],
        "outcome": "accepted", "event_id": event_id})


def freeze_workspace(state, actor, run, root, workspace_id, label):
    revision = one(state, "SELECT revision FROM v2_workspace WHERE workspace_id=?",
                   (workspace_id,))["revision"]
    value = workspace_call(state, actor, run["epoch"], "workspace.freeze", {
        "workspace_id": workspace_id, "expected_revision": revision,
        "evidence_root": str(Path(root) / "quiescence" / label)})
    expect(value.get("status") == "WORKSPACE_FROZEN", "freeze",
           "workspace was not frozen: " + json.dumps(value)[:600])
    return value


def release_workspace(state, actor, run, root, workspace_id, label):
    revision = one(state, "SELECT revision FROM v2_workspace WHERE workspace_id=?",
                   (workspace_id,))["revision"]
    value = workspace_call(state, actor, run["epoch"], "workspace.release", {
        "workspace_id": workspace_id, "expected_revision": revision,
        "evidence_root": str(Path(root) / "quiescence" / label)})
    expect(value.get("status") == "WORKSPACE_RELEASED", "release",
           "workspace was not released: " + json.dumps(value)[:600])
    return value


def merge_call(state, repo, merge_token, tool, payload, epoch):
    return service_extensions.call(state, repo, merge_token, tool, {
        "payload": payload, "command_id": "v2code-merge-" + uuid.uuid4().hex,
        "expected_epoch": epoch})


def intent_revision(state, intent_id):
    return one(state, "SELECT revision FROM v2_merge_intent WHERE intent_id=?", (intent_id,))["revision"]


def merge_revision(state, merge_request_id):
    return one(state, "SELECT revision FROM v2_merge_request WHERE merge_request_id=?",
               (merge_request_id,))["revision"]


def finish_intent(state, repo, merge_token, run, intent_id):
    report = merge_queue.execute_intent(service._db_path(Path(run["state"])), intent_id,
                                        one(state, "SELECT operation_token FROM v2_merge_intent "
                                                   "WHERE intent_id=?", (intent_id,))["operation_token"])
    save(Path(run["root"]) / "evidence" / ("report-" + intent_id[:16] + ".json"), report)
    return report


def execute(root):
    root = Path(root).resolve()
    run = monitor_task.read_json(root / "run.json")
    meta = monitor_task.read_json(root / "provenance.json")
    expect(not (meta.get("evidence_class") == "native" and os.environ.get("HERDR_ENV") == "1"),
           "execute", "native-only acceptance requires a caller outside Herdr")
    source = Path(__file__).resolve().parents[1]
    for name, digest in meta["source_files"].items():
        expect((source / name).is_file() and sha(source / name) == digest, "execute",
               "validation source changed: " + name)
    state, epoch, actor = Path(run["state"]), run["epoch"], tuple(run["actor"])
    repo, base, project = Path(run["repo"]), run["base"], run["project_id"]
    deadline = time.monotonic() + run["timeout"] * 2 + 120
    controller = token_of(run, "controller")
    merge_token = token_of(run, "merge")
    worker_a = token_of(run, "worker-a")
    worker_b = token_of(run, "worker-b")
    reviewer_a = token_of(run, "reviewer-a")
    reviewer_b = token_of(run, "reviewer-b")
    merge_reviewer = token_of(run, "merge-reviewer")
    traces = []

    def state_rows(sql, args=()):
        return rows(state, sql, args)

    prepared = run["prepared"]
    repo_meta = monitor_task.read_json(root / "repo.json")

    # 1. Claim A through the authenticated gateway; the slot and resources are held.
    claim = call(state, root, worker_a, "awo_assignment_claim", args_for(
        epoch, task_id=TASK_A, expected_revision=run["queue_a"]["revision"]))
    expect(claim.get("status") == "RESERVED", "claim", "claim A failed: " + json.dumps(claim)[:400])
    aid = claim["assignment_id"]
    assignment = assignment_row(state, aid)
    expect(assignment["holds_slot"] == 1, "claim", "claim did not hold the slot")
    reservations = state_rows("SELECT b.scope_type,r.dimension,r.amount,r.state FROM v2_budget_reservation r "
                              "JOIN v2_budget b ON b.budget_id=r.budget_id WHERE r.assignment_id=? "
                              "ORDER BY r.budget_id,r.dimension", (aid,))
    stage(root, traces, "claim", assignment_id=aid, status=claim["status"],
          generation=assignment["generation"], reservations=reservations,
          workspace_mode="git_worktree", delivery_cwd=prepared[TASK_A]["request"]["cwd"])

    # 2. Refuse a workspace that is not the frozen launch directory or not the frozen base.
    plan_a = prepared[TASK_A]["workspace"]
    common = {"assignment_id": aid, "expected_revision": assignment["revision"], "kind": "git_worktree",
              "root": str((root / "workspaces").resolve()), "allowed_paths": ["calc.py", "test_calc.py"],
              "repository_id": "fixture-code-repo", "repo_root": str(repo)}
    refusals = {}
    for label, extra in (("wrong_path", {"path": str(root / "workspaces" / "other-source"),
                                         "base_commit": base}),
                         ("wrong_base", {"path": plan_a, "base_commit": "b" * 40})):
        try:
            workspace_call(state, actor, epoch, "workspace.create", dict(common, **extra))
            refusals[label] = None
        except Exception as exc:
            refusals[label] = {"type": type(exc).__name__, "message": str(exc)}
    expect(refusals["wrong_path"] and "frozen worker launch directory" in refusals["wrong_path"]["message"],
           "workspace_create_guard", "wrong planned path was not refused: " + json.dumps(refusals))
    expect(refusals["wrong_base"] and "frozen materialized base commit" in refusals["wrong_base"]["message"],
           "workspace_create_guard", "wrong base was not refused: " + json.dumps(refusals))
    expect(not state_rows("SELECT 1 FROM v2_workspace WHERE assignment_id=?", (aid,)),
           "workspace_create_guard", "refused workspace creation left a workspace row")
    stage(root, traces, "workspace_create_guard", refusals=refusals,
          workspace_rows=0, assignment_revision=assignment_row(state, aid)["revision"])

    # 3. Provision the actual Git worktree at the frozen path and base.
    created = workspace_call(state, actor, epoch, "workspace.create", dict(
        common, path=plan_a, base_commit=base))
    expect(created.get("status") == "WORKSPACE_READY", "workspace_create",
           "workspace creation failed: " + json.dumps(created)[:800])
    ws_a = ws_row(state, created["workspace"]["workspace_id"])
    expect(ws_a and ws_a["kind"] == "git_worktree" and Path(ws_a["path"]).resolve() == Path(plan_a).resolve()
           and ws_a["base_commit"] == base and ws_a["status"] == "ready"
           and assignment_row(state, aid)["workspace_id"] == ws_a["workspace_id"], "workspace_create",
           "Assignment/worktree binding is not the provisioned worktree")
    expect(git(ws_a["path"], "rev-parse", "HEAD") == base, "workspace_create",
           "worktree HEAD is not the frozen base")
    expect(git(ws_a["path"], "symbolic-ref", "HEAD") == "refs/heads/" + ws_a["branch"], "workspace_create",
           "worktree branch differs from the durable record")
    listing = git(repo, "worktree", "list", "--porcelain")
    expect(str(Path(ws_a["path"]).resolve()).replace("\\", "/") in listing.replace("\\", "/"), "workspace_create",
           "worktree is not registered with the source repository")
    marker = Path(ws_a["path"]) / ".git"
    expect(marker.is_file() and "worktrees" in marker.read_text(encoding="utf-8"), "workspace_create",
           "worktree-private git directory is missing")
    stage(root, traces, "workspace_create", workspace_id=ws_a["workspace_id"], task_id=TASK_A,
          kind=ws_a["kind"], status=ws_a["status"], path=ws_a["path"], base_commit=ws_a["base_commit"],
          branch=ws_a["branch"], head=base, marker=str(marker),
          common_git_dir=ws_a["common_git_dir"], worktree_registered=True,
          assignment_workspace_id=assignment_row(state, aid)["workspace_id"])

    # 4. Single-attempt dispatch through the V2 send gate to the real bridge.
    dispatched = scheduler.dispatch_assignment(state, aid, *actor, expected_epoch=epoch)
    expect(dispatched.get("status") == "SUBMITTED", "dispatch",
           "dispatch A failed: " + json.dumps(dispatched)[:400])
    again = scheduler.dispatch_assignment(state, aid, *actor, expected_epoch=epoch)
    expect(again.get("status") == "ALREADY_ATTEMPTED" and again.get("sent") is False, "dispatch",
           "an attempted send was retried: " + json.dumps(again)[:400])
    jobs = list((root / "worker-a" / "jobs").iterdir())
    expect(len(jobs) == 1, "dispatch", "the bridge did not create exactly one job")
    job = monitor_task.read_json(jobs[0] / "job.json")
    expect(Path(job["execution_cwd"]).resolve() == Path(ws_a["path"]).resolve(), "dispatch",
           "job execution directory is not the worktree: " + str(job.get("execution_cwd")))
    expect(job["cwd"] != job["execution_cwd"], "dispatch", "delivery and execution directories collapsed")
    sandbox = job["argv"][job["argv"].index("--sandbox") + 1]
    expect(sandbox == "workspace-write", "dispatch", "native job was not sent with workspace-write")
    stage(root, traces, "dispatch", status=dispatched["status"], retry_status=again["status"],
          retry_sent=again.get("sent"), job_cwd=job["cwd"], execution_cwd=job["execution_cwd"],
          sandbox=sandbox, job_sha256=sha(jobs[0] / "job.json"), jobs=len(jobs),
          argv_sha256=hashlib.sha256(json.dumps(job["argv"]).encode()).hexdigest())

    # 5. The installed Codex edits the real worktree; delivery bytes stay outside it.
    observed = wait_turn(root / "worker-a", 0, deadline, "worker_turn_a")
    expect(observed.get("status") == "idle" and observed.get("completion_seq") == 1, "worker_turn_a",
           "unexpected bridge status: " + json.dumps(observed)[:600])
    receipt = monitor_task.read_json(jobs[0] / "receipt" / "status.json")
    expect(receipt["status"] == "done" and receipt["returncode"] == 0
           and receipt["process_status"] == "clean_exit", "worker_turn_a",
           "native receipt is not a clean completion: " + json.dumps(receipt)[:600])
    expect(Path(receipt["execution_cwd"]).resolve() == Path(ws_a["path"]).resolve()
           and Path(receipt["cwd"]).resolve() == Path(job["cwd"]).resolve(), "worker_turn_a",
           "receipt directories do not match the frozen contract")
    delivery_a = Path(job["cwd"])
    expect((delivery_a / "RESULT.md").is_file() and (delivery_a / "HANDOFF.md").is_file(),
           "worker_turn_a", "delivery artifacts are missing")
    expect(not (Path(ws_a["path"]) / "RESULT.md").exists(), "worker_turn_a",
           "delivery artifact leaked into the source worktree")
    status = git(ws_a["path"], "status", "--porcelain=v1", "--untracked-files=all").splitlines()
    changed = sorted(line[3:] for line in status if line.strip())
    expect(changed == ["calc.py", "test_calc.py"], "worker_turn_a",
           "harness changed unexpected paths: " + json.dumps(changed))
    view = code_change_view(ws_a["path"], base)
    expect(b"return a + b" in view["calc"], "worker_turn_a", "calc.py does not return a sum")
    patch_path = write_new(root / "evidence" / "a.patch", view["patch"])
    after_test = run_test(root, "patched-test-a", ws_a["path"])
    expect(after_test["returncode"] == 0, "worker_turn_a",
           "patched worktree failed its correctness test: " + after_test["stderr"][-800:])
    stage(root, traces, "worker_turn", status=observed, receipt={
        "status": receipt["status"], "returncode": receipt["returncode"],
        "process_status": receipt["process_status"], "cwd": receipt["cwd"],
        "execution_cwd": receipt["execution_cwd"], "sha256": sha(jobs[0] / "receipt" / "status.json")},
        changed_paths=changed, patch_path=str(patch_path), patch_sha256=sha(patch_path),
        calc_blob=view["calc_sha"], test_blob=view["test_sha"],
        result_sha256=sha(delivery_a / "RESULT.md"), handoff_sha256=sha(delivery_a / "HANDOFF.md"),
        test_returncode=after_test["returncode"], base_test_returncode=repo_meta["base_test"])

    # 6. Reconcile the observed native turn, commit the reviewed patch, and inspect inventory.
    reconciled = scheduler.reconcile_assignment(state, aid, *actor, expected_epoch=epoch)
    expect(reconciled.get("status") == "REVIEW_READY", "reconcile",
           "reconcile did not reach review_ready: " + json.dumps(reconciled)[:400])
    event_a = reconciled["event_id"]
    git(ws_a["path"], "-c", "user.name=AWO Code Fixture", "-c", "user.email=awo@example.invalid",
        "add", "--", "calc.py", "test_calc.py")
    git(ws_a["path"], "-c", "user.name=AWO Code Fixture", "-c", "user.email=awo@example.invalid",
        "commit", "-m", "fix add() and cover negative numbers")
    source_commit_a = git(ws_a["path"], "rev-parse", "HEAD")
    expect(git(ws_a["path"], "status", "--porcelain=v1", "--untracked-files=all") == "", "commit",
           "worktree is not clean after the controller commit")
    expect(git(repo, "rev-parse", "refs/heads/" + ws_a["branch"]) == source_commit_a, "commit",
           "worktree branch ref does not point at the committed source")
    inspection = workspace_runtime.inspect(state, actor[0], actor[1], "main", ws_a["workspace_id"])
    report = inspection["inspection"]
    expect(report["ok"] and report["inventory"]["scope_ok"] and not report["inventory"]["dirty"]
           and report["inventory"]["changed_paths"] == ["calc.py", "test_calc.py"]
           and report["inventory"]["head"] == source_commit_a, "inventory",
           "committed inventory is not the declared patch: " + json.dumps(report)[:800])
    stage(root, traces, "commit", event_id=event_a, source_commit=source_commit_a,
          base=base, source_ref="refs/heads/" + ws_a["branch"],
          changed_paths=report["inventory"]["changed_paths"],
          undeclared_paths=report["inventory"]["undeclared_paths"],
          dirty=report["inventory"]["dirty"], scope_ok=report["inventory"]["scope_ok"],
          inventory_sha256=hashlib.sha256(json.dumps(report["inventory"], sort_keys=True).encode()).hexdigest())

    # 7. Worker evidence and completion; the controller still does not accept.
    recorded_a = record_delivery(root, state, run, worker_a, aid, delivery_a)
    completed_a = complete_assignment(state, root, run, worker_a, aid, recorded_a,
                                      "Codex patched the bound worktree; controller committed it.")
    expect(completed_a.get("status") == "REVIEW_READY" and completed_a.get("independently_accepted") is False,
           "complete", "completion self-accepted: " + json.dumps(completed_a)[:400])
    # A repeated completion request must deduplicate to the first recorded receipt,
    # and no other worker identity may complete this Assignment.
    events_before = one(state, "SELECT count(*) AS n FROM events")["n"]
    revision_before = assignment_row(state, aid)["revision"]
    duplicate = call(state, root, worker_a, "awo_assignment_complete", args_for(
        epoch, assignment_id=aid, generation=assignment["generation"],
        expected_revision=revision_before, outcome="completed",
        evidence_ids=evidence_ids(recorded_a), summary="duplicate completion"))
    expect(duplicate.get("status") == "REVIEW_READY"
           and duplicate.get("event_id") == completed_a["event_id"], "complete",
           "duplicate completion did not deduplicate: " + json.dumps(duplicate)[:400])
    expect(one(state, "SELECT count(*) AS n FROM events")["n"] == events_before
           and assignment_row(state, aid)["revision"] == revision_before, "complete",
           "duplicate completion changed event or Assignment state")
    foreign = None
    try:
        call(state, root, worker_b, "awo_assignment_complete", args_for(
            epoch, assignment_id=aid, generation=assignment["generation"],
            expected_revision=revision_before, outcome="completed",
            evidence_ids=evidence_ids(recorded_a), summary="foreign completion"))
    except Exception as exc:
        foreign = {"type": type(exc).__name__, "code": getattr(exc, "code", None), "message": str(exc)}
    expect(foreign is not None and one(state, "SELECT count(*) AS n FROM events")["n"] == events_before
           and assignment_row(state, aid)["revision"] == revision_before, "complete",
           "another worker completed this Assignment: " + json.dumps(foreign))
    stage(root, traces, "complete", status=completed_a["status"], event_id=completed_a["event_id"],
          evidence_ids=evidence_ids(recorded_a), independently_accepted=False,
          duplicate_deduplicated=True, duplicate_event_id=duplicate["event_id"],
          foreign_denied=foreign,
          task_state=one(state, "SELECT task_state FROM v2_task WHERE task_id=?", (TASK_A,))["task_state"])

    # 7b. Self-approval negatives: the executing worker role may not submit a
    #     review, and the issuer refuses a reviewer credential bound to it.
    synthetic_note = write_new(root / "notes" / "self-review-note.md",
                               TASK_A + "-RUN " + prepared[TASK_A]["request"]["nonce"] +
                               "\nVerdict: accept\nSynthetic self-approval negative.\n", encoding="ascii")
    valid_review = dict(assignment_id=aid, expected_revision=assignment_row(state, aid)["revision"],
                        task_id=TASK_A, event_id=completed_a["event_id"],
                        evidence_ids=evidence_ids(recorded_a), verdict="accept",
                        review_note_path=synthetic_note.relative_to(root).as_posix(),
                        review_note_sha256=sha(synthetic_note))
    reviews_before = one(state, "SELECT count(*) AS n FROM v2_review")["n"]
    worker_denied = None
    try:
        call(state, root, worker_a, "awo_review_submit", args_for(epoch, **valid_review))
    except Exception as exc:
        worker_denied = {"type": type(exc).__name__, "code": getattr(exc, "code", None), "message": str(exc)}
    expect(worker_denied and worker_denied.get("code") == "forbidden_role", "self_review",
           "worker credential was not denied review submission: " + json.dumps(worker_denied))
    executor_binding = json.loads(one(state, "SELECT contract_json FROM v2_scheduler_intent "
                                            "WHERE assignment_id=?", (aid,))["contract_json"])["worker_binding"]
    tokens_before = one(state, "SELECT count(*) AS n FROM v2_access_token")["n"]
    issuance_denied = None
    rejected_path = Path(root) / "secrets" / "executor-reviewer-rejected.token"
    try:
        access.issue_token(state, actor[0], actor[1], "main", {
            "tool": "access.issue", "payload": {
                "project_root": str(root), "role": "reviewer", "agent_id": run["agents"]["executor"],
                "binding_id": executor_binding["instance_id"],
                "binding_version": executor_binding["version"], "task_id": TASK_A},
            "command_id": "v2code-access-" + uuid.uuid4().hex, "expected_epoch": epoch},
            str(rejected_path))
    except Exception as exc:
        issuance_denied = {"type": type(exc).__name__, "message": str(exc)}
    expect(issuance_denied and "controller permission for review" in issuance_denied["message"],
           "self_review", "reviewer issuance for the executing binding was not denied: " + json.dumps(issuance_denied))
    expect(one(state, "SELECT count(*) AS n FROM v2_access_token")["n"] == tokens_before
           and not rejected_path.exists()
           and one(state, "SELECT count(*) AS n FROM v2_review")["n"] == reviews_before,
           "self_review", "denied self-review left a credential or review row")
    stage(root, traces, "self_review_denied", worker=worker_denied, issuer=issuance_denied,
          reviews_unchanged=True, tokens_unchanged=True)

    # 8. Real out-of-scope write: the independent review gate must refuse it.
    unexpected = Path(ws_a["path"]) / "outside-scope.txt"
    write_new(unexpected, "not declared\n", encoding="ascii")
    dirty_report = workspace_runtime.inspect(state, actor[0], actor[1], "main", ws_a["workspace_id"])
    expect(dirty_report["inspection"]["inventory"]["undeclared_paths"] == ["outside-scope.txt"]
           and not dirty_report["inspection"]["inventory"]["scope_ok"], "scope_negative",
           "undeclared write was not detected: " + json.dumps(dirty_report)[:600])
    placeholder = Path(prepared[TASK_A]["request"]["review_path"])
    placeholder.write_text(TASK_A + "-RUN " + prepared[TASK_A]["request"]["nonce"] +
                           "\nVerdict: accept\nScope negative placeholder.\n",
                           encoding="utf-8", newline="\n")
    scope_denied = None
    try:
        review_submit(state, root, run, reviewer_a, aid, TASK_A, event_a, recorded_a, placeholder)
    except Exception as exc:
        scope_denied = {"type": type(exc).__name__, "code": getattr(exc, "code", None), "message": str(exc)}
    expect(scope_denied and "scope violations" in scope_denied["message"], "scope_negative",
           "out-of-scope review was not refused: " + json.dumps(scope_denied))
    expect(not state_rows("SELECT 1 FROM v2_review WHERE assignment_id=?", (aid,))
           and not state_rows("SELECT 1 FROM v2_workspace_review WHERE assignment_id=?", (aid,)),
           "scope_negative", "refused review left review/sealed rows")
    unexpected.unlink()
    restored = workspace_runtime.inspect(state, actor[0], actor[1], "main", ws_a["workspace_id"])
    expect(restored["inspection"]["inventory"]["scope_ok"]
           and restored["inspection"]["inventory"]["changed_paths"] == ["calc.py", "test_calc.py"],
           "scope_negative", "restoring the untracked file did not restore the declared inventory")
    stage(root, traces, "scope_negative", undeclared=["outside-scope.txt"], denied=scope_denied,
          review_rows=0, sealed_rows=0, restored_scope_ok=True)

    # 9. A real independent Codex reviewer reads the changed code and test evidence.
    view = code_change_view(ws_a["path"], base)
    requirement = ("calc.py add(a, b) must return the arithmetic sum, and test_calc.py must contain a "
                   "unittest case covering negative numbers. Only these two files may change.")
    evidence_text = json.dumps({"base_test_returncode": 0, "base_expected_failure": True,
                                "patched_test_argv": ["python", "-B", "-m", "unittest", "test_calc"],
                                "patched_test_returncode": after_test["returncode"],
                                "patched_stdout": after_test["stdout"],
                                "patched_stderr": after_test["stderr"]}, indent=2)
    review_prompt_a = (
        "Review a small code change independently. Read every file in your current directory with your "
        "tools before deciding: request.md, calc.py, test_calc.py, patch.diff, tests.json.\n"
        "Requirement: " + requirement + "\n"
        "Task: " + TASK_A + "\nNonce: " + prepared[TASK_A]["request"]["nonce"] + "\n"
        "Base commit: " + base + "\nCommitted source commit: " + source_commit_a + "\n"
        "Return JSON only with exactly the keys task_id, nonce, verdict (accept or reject), reason. "
        "Accept only if the requirement is met by the actual code and the tests really pass. "
        "Do not use a sandbox escape and do not modify anything.")
    review_a, review_dir_a, _ = reviewer_turn(root, "task-a", review_prompt_a, {
        "request.md": requirement + "\n", "calc.py": view["calc"].decode("utf-8"),
        "test_calc.py": view["test"].decode("utf-8"), "patch.diff": view["patch"].decode("utf-8"),
        "tests.json": evidence_text}, run["timeout"], run["specs"]["reviewer"])
    expect(review_a.get("task_id") == TASK_A
           and review_a.get("nonce") == prepared[TASK_A]["request"]["nonce"]
           and review_a.get("verdict") in ("accept", "reject"), "review_a",
           "reviewer output lacks exact task/nonce/verdict: " + json.dumps(review_a)[:500])
    if review_a["verdict"] != "accept":
        return finish(root, {"status": "WORKTREE_CODE_REVIEW_REJECTED", "stage": "review_a",
                             "review": review_a}, traces)
    note_a = Path(prepared[TASK_A]["request"]["review_path"])
    note_a.write_text(TASK_A + "-RUN " + prepared[TASK_A]["request"]["nonce"] + "\nVerdict: accept\n" +
                      "Independent Codex review: " + str(review_a.get("reason", "")) + "\n",
                      encoding="utf-8", newline="\n")
    submitted_a = review_submit(state, root, run, reviewer_a, aid, TASK_A, event_a, recorded_a, note_a)
    expect(submitted_a.get("status") == "REVIEW_RECORDED" and submitted_a.get("claims_retained") is True,
           "review_a", "review submit failed: " + json.dumps(submitted_a)[:400])
    sealed_a = one(state, "SELECT * FROM v2_workspace_review WHERE assignment_id=?", (aid,))
    inventory_a = json.loads(sealed_a["inventory_json"])
    expect(sealed_a["verdict"] == "accept" and inventory_a["scope_ok"]
           and inventory_a["changed_paths"] == ["calc.py", "test_calc.py"]
           and inventory_a["head"] == source_commit_a, "review_a",
           "sealed worktree inventory is not the reviewed patch: " + json.dumps(inventory_a)[:600])
    stage(root, traces, "review_a", review=review_a, review_dir=str(review_dir_a),
          result_sha256=sha(review_dir_a / "RESULT.md"), note_sha256=sha(note_a),
          inventory_sha256=sealed_a["inventory_sha256"],
          changed_paths=inventory_a["changed_paths"], head=inventory_a["head"])

    # 10. Post-review tamper is refused; the exact reviewed bytes let the same close proceed.
    #     The sealed inventory binds raw bytes AND the worktree index, so restore both the
    #     content and its file times instead of running a Git checkout (which rewrites the
    #     index stat cache and would itself be a post-review change).
    calc_path = Path(ws_a["path"]) / "calc.py"
    original_calc = calc_path.read_bytes()
    original_stat = calc_path.stat()
    calc_path.write_bytes(original_calc + b"\n# tampered after review\n")
    tamper_denied = None
    try:
        close_assignment(state, actor, run, aid, event_a)
    except Exception as exc:
        tamper_denied = {"type": type(exc).__name__, "code": getattr(exc, "code", None), "message": str(exc)}
    expect(tamper_denied and "EVIDENCE_CHANGED" in tamper_denied["message"], "tamper",
           "post-review tamper was not refused: " + json.dumps(tamper_denied))
    expect(assignment_row(state, aid)["holds_slot"] == 1, "tamper", "tamper refusal released the slot")
    calc_path.write_bytes(original_calc)
    os.utime(calc_path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    restored_calc = calc_path.read_bytes()
    restored_inventory = workspace_runtime.inspect(
        state, actor[0], actor[1], "main", ws_a["workspace_id"])["inspection"]["inventory"]
    expect(restored_calc == original_calc
           and restored_inventory == inventory_a, "tamper",
           "restore did not recover the reviewed bytes and inventory")
    stage(root, traces, "tamper", denied=tamper_denied, holds_slot=1,
          restored_sha256=hashlib.sha256(restored_calc).hexdigest(),
          restored_inventory_sha256=workspace_lib._digest(restored_inventory))

    # 11. Close accepted and freeze the actual worktree with observed quiescence.
    closed_a = close_assignment(state, actor, run, aid, event_a)
    expect(closed_a.get("status") == "CLOSED" and closed_a.get("outcome") == "accepted", "close_a",
           "close A failed: " + json.dumps(closed_a)[:400])
    frozen_a = freeze_workspace(state, actor, run, root, ws_a["workspace_id"], "code-a")
    quiet_a = json.loads(one(state, "SELECT quiescence_json FROM v2_workspace WHERE workspace_id=?",
                             (ws_a["workspace_id"],))["quiescence_json"])
    expect(Path(quiet_a["evidence_path"]).is_file() and sha(quiet_a["evidence_path"]) == quiet_a["evidence_sha256"],
           "freeze_a", "freeze quiescence evidence is missing or changed")
    stage(root, traces, "close_freeze_a", close=closed_a, freeze=frozen_a["status"],
          quiescence_sha256=quiet_a["evidence_sha256"], quiescence_path=quiet_a["evidence_path"],
          task_state=one(state, "SELECT task_state FROM v2_task WHERE task_id=?", (TASK_A,))["task_state"],
          assignment=assignment_row(state, aid))

    # 12. Accepted is not landed: the target ref still points at the base commit.
    expect(git(repo, "rev-parse", "refs/heads/integration") == base, "landing",
           "target moved before any merge command")
    policy = merge_call(state, repo, merge_token, "merge.policy", {
        "repository_id": "fixture-code-repo", "repo_root": str(repo),
        "operation_root": str((root / "merge-ops").resolve()),
        "archive_root": str((root / "merge-archive").resolve()),
        "verify_argv": [sys.executable, "-B", "-m", "unittest", "test_calc"],
        "pins": {str(Path(sys.executable).resolve()): sha(sys.executable)},
        "timeout": 60}, epoch)
    policy_id = policy["policy_id"]
    task_a = one(state, "SELECT * FROM v2_task WHERE task_id=?", (TASK_A,))
    proposed = merge_call(state, repo, merge_token, "merge.propose", {
        "task_id": TASK_A, "assignment_id": aid, "generation": assignment["generation"],
        "workspace_id": ws_a["workspace_id"], "policy_id": policy_id,
        "target_ref": "refs/heads/integration", "expected_revision": task_a["revision"]}, epoch)
    mr = proposed["merge_request_id"]
    expect(proposed["state"] == "proposed" and git(repo, "rev-parse", "refs/heads/integration") == base,
           "merge_propose", "proposal landed or failed: " + json.dumps(proposed)[:400])
    queued_mr = merge_call(state, repo, merge_token, "merge.enqueue", {
        "merge_request_id": mr, "expected_revision": proposed["revision"]}, epoch)
    started = merge_call(state, repo, merge_token, "merge.begin", {
        "merge_request_id": mr, "expected_revision": queued_mr["merge_revision"], "kind": "build"}, epoch)
    build_report = finish_intent(state, repo, merge_token, run, started["intent_id"])
    expect(build_report.get("outcome") == "verified", "merge_build",
           "merge build did not verify: " + json.dumps(build_report)[:600])
    finished = merge_call(state, repo, merge_token, "merge.finish", {
        "intent_id": started["intent_id"],
        "expected_revision": intent_revision(state, started["intent_id"])}, epoch)
    expect(finished.get("state") == "verifying", "merge_build",
           "verified candidate did not enter review: " + json.dumps(finished)[:400])
    candidate = one(state, "SELECT candidate_json FROM v2_merge_request WHERE merge_request_id=?",
                    (mr,))["candidate_json"]
    candidate = json.loads(candidate)
    expect(git(repo, "rev-parse", "refs/heads/integration") == base, "merge_build",
           "target moved during build/review")
    stage(root, traces, "merge_build", policy_id=policy_id, merge_request_id=mr,
          state=finished["state"], candidate=candidate, target_tip=base,
          build_report=build_report, policy=json.loads(one(
              state, "SELECT spec_json FROM v2_merge_policy WHERE policy_id=?", (policy_id,))["spec_json"]))

    # 13. Independent integration review in a real detached worktree at the candidate commit,
    #     so the reviewer can verify the claimed change set from actual Git state.
    verify_log = json.loads(Path(candidate["verify_log_path"]).read_text(encoding="ascii"))
    candidate_digest = merge_queue._digest(candidate)
    review_tree = Path(root) / "review-runs" / ("integration-tree-" + uuid.uuid4().hex[:8])
    git(repo, "worktree", "add", "--detach", str(review_tree), candidate["candidate_commit"])
    scope_view = git(repo, "diff", "--name-status", candidate["stack_base"], candidate["candidate_commit"])
    expect(scope_view.splitlines() == ["M\tcalc.py", "M\ttest_calc.py"], "integration_review",
           "candidate change set is not the reviewed patch: " + json.dumps(scope_view))
    review_prompt_i = (
        "Your current directory is a real detached Git worktree at the candidate commit. Read the files and "
        "use git read-only commands to verify the candidate independently; do not modify tracked files.\n"
        "Requirement: " + requirement + "\n"
        "Task: " + TASK_A + "\nAssignment: " + aid + "\nGeneration: " + str(assignment["generation"]) + "\n"
        "Candidate commit: " + candidate["candidate_commit"] + "\nBase commit: " + candidate["stack_base"] + "\n"
        "Expected changed paths: calc.py and test_calc.py only.\n"
        "Candidate SHA-256: " + candidate_digest + "\n"
        "Verify with git diff --name-status " + candidate["stack_base"] + " " +
        candidate["candidate_commit"] + ", git rev-parse HEAD, reading the code, and running the unittest.\n"
        "Return JSON only with exactly the keys verdict (accept or reject), reason, candidate_sha256, "
        "task_id, assignment_id, generation, reviewer_agent_id. Copy the candidate_sha256, task_id, "
        "assignment_id, generation and reviewer_agent_id values exactly as given here: "
        + json.dumps({"candidate_sha256": candidate_digest, "task_id": TASK_A, "assignment_id": aid,
                      "generation": assignment["generation"],
                      "reviewer_agent_id": run["agents"]["reviewer"]}, sort_keys=True)
        + ". Do not modify anything.")
    review_i, review_dir_i, _ = reviewer_turn(root, "integration", review_prompt_i, {},
                                              run["timeout"], run["specs"]["reviewer"],
                                              worktree=review_tree)
    expect(git(review_tree, "rev-parse", "HEAD") == candidate["candidate_commit"]
           and git(review_tree, "diff", "--name-only") == "", "integration_review",
           "the reviewed worktree no longer matches the candidate commit")
    expected_binding = {"candidate_sha256": candidate_digest, "task_id": TASK_A,
                        "assignment_id": aid, "generation": assignment["generation"],
                        "reviewer_agent_id": run["agents"]["reviewer"]}
    expect(all(review_i.get(k) == v for k, v in expected_binding.items())
           and review_i.get("verdict") in ("accept", "reject"), "integration_review",
           "integration review is not bound to the candidate: " + json.dumps(review_i)[:600])
    if review_i["verdict"] != "accept":
        return finish(root, {"status": "WORKTREE_CODE_INTEGRATION_REJECTED", "stage": "integration_review",
                             "review": review_i}, traces)
    saved_i = write_new(root / "evidence" / "integration-review-result.md",
                        (review_dir_i / "RESULT.md").read_bytes())
    git(repo, "worktree", "remove", "--force", str(review_tree))
    note_i = write_new(root / "notes" / "integration-review.json",
                       json.dumps(dict(expected_binding, verdict="accept",
                                       reason=str(review_i.get("reason", ""))), indent=2,
                                  sort_keys=True) + "\n", encoding="ascii")
    reviewed = merge_call(state, repo, merge_reviewer, "merge.review", {
        "merge_request_id": mr, "expected_revision": merge_revision(state, mr),
        "note_path": str(note_i.resolve()), "note_sha256": sha(note_i)}, epoch)
    expect(reviewed.get("state") == "verifying", "integration_review",
           "merge review failed: " + json.dumps(reviewed)[:400])
    expect(git(repo, "rev-parse", "refs/heads/integration") == base, "integration_review",
           "target moved before landing")
    stage(root, traces, "integration_review", review=review_i, note_sha256=sha(note_i),
          review_tree=str(review_tree), review_scope=scope_view,
          result_path=str(saved_i), result_sha256=sha(saved_i),
          candidate_sha256=candidate_digest, target_tip=base,
          verify_log=candidate["verify_log_path"], verify_returncode=verify_log["returncode"])

    # 14. Local CAS landing; the destination ref must equal the reviewed candidate.
    landed = merge_call(state, repo, merge_token, "merge.begin", {
        "merge_request_id": mr, "expected_revision": merge_revision(state, mr), "kind": "land"}, epoch)
    land_report = finish_intent(state, repo, merge_token, run, landed["intent_id"])
    expect(land_report.get("outcome") == "landed", "merge_land",
           "landing failed: " + json.dumps(land_report)[:600])
    finalized = merge_call(state, repo, merge_token, "merge.finish", {
        "intent_id": landed["intent_id"],
        "expected_revision": intent_revision(state, landed["intent_id"])}, epoch)
    tip = git(repo, "rev-parse", "refs/heads/integration")
    expect(tip == candidate["candidate_commit"] and land_report["merge_commit"] == tip, "merge_land",
           "landed ref is not the reviewed candidate")
    expect(finalized.get("task_state") == "landed", "merge_land",
           "accepted task did not become landed: " + json.dumps(finalized)[:400])
    blob_calc = git(repo, "rev-parse", "refs/heads/integration:calc.py")
    blob_test = git(repo, "rev-parse", "refs/heads/integration:test_calc.py")
    expect(blob_calc == git(ws_a["path"], "rev-parse", source_commit_a + ":calc.py")
           and blob_test == git(ws_a["path"], "rev-parse", source_commit_a + ":test_calc.py"),
           "merge_land", "landed blobs differ from the reviewed source commit")
    candidate_test = run_test(root, "candidate-test", candidate["worktree"])
    expect(candidate_test["returncode"] == 0, "merge_land", "landed candidate fails the correctness test")
    expect(git(repo, "status", "--porcelain") == "" and git(repo, "rev-parse", "HEAD") == base,
           "merge_land", "the source repository worktree changed")
    stage(root, traces, "merge_land", merge_request_id=mr, intent_id=landed["intent_id"],
          outcome=land_report["outcome"], merge_commit=land_report["merge_commit"],
          target_tip=tip, candidate_commit=candidate["candidate_commit"],
          calc_blob=blob_calc, test_blob=blob_test, source_commit=source_commit_a,
          candidate_test_returncode=candidate_test["returncode"],
          source_repo_head=base, source_repo_clean=True,
          merge_state=finalized["state"], task_state=finalized["task_state"])

    # 15. The downstream scheduler gate rejects an unbacked base, then a recorded
    #     materialization of A unlocks exactly that base commit.
    con = service._connect(service._db_path(state), readonly=True)
    try:
        unbacked = merge_queue.materialization_gate(con, project, TASK_B, tip)
        gate_error = None
        try:
            scheduler._dependencies(con, project, {"task_id": TASK_B, "dependencies": [TASK_A],
                                                   "workspace_mode": "git_worktree",
                                                   "workspace_base_commit": tip, "dag_run": None})
        except Exception as exc:
            gate_error = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        con.close()
    expect(unbacked.get("ready") is False and gate_error
           and "materialization proof" in gate_error["message"], "downstream_gate",
           "unbacked base was not gated: " + json.dumps({"gate": unbacked, "error": gate_error}))
    ticket = merge_call(state, repo, merge_token, "merge.materialize", {
        "task_id": TASK_B, "expected_revision": one(
            state, "SELECT revision FROM v2_task WHERE task_id=?", (TASK_B,))["revision"],
        "policy_id": policy_id, "target_ref": "refs/heads/integration",
        "parents": [{"task_id": TASK_A, "assignment_id": aid, "generation": assignment["generation"]}]},
        epoch)
    materialized = finish_intent(state, repo, merge_token, run, ticket["intent_id"])
    expect(materialized.get("outcome") == "materialized", "materialize",
           "stack materialization failed: " + json.dumps(materialized)[:600])
    materialize_finish = merge_call(state, repo, merge_token, "merge.finish", {
        "intent_id": ticket["intent_id"],
        "expected_revision": intent_revision(state, ticket["intent_id"])}, epoch)
    stack_base = materialized["candidate"]["stack_base"]
    expect(git(repo, "rev-parse", "refs/heads/integration") == tip, "materialize",
           "materialization moved the integration target")
    expect(one(state, "SELECT task_state FROM v2_task WHERE task_id=?", (TASK_B,))["task_state"] == "draft",
           "materialize", "materialization changed the downstream Task state")
    con = service._connect(service._db_path(state), readonly=True)
    try:
        gate_ready = merge_queue.materialization_gate(con, project, TASK_B, stack_base)
        # A different, unrecorded base commit must stay closed for this Task.
        gate_changed = merge_queue.materialization_gate(con, project, TASK_B, base)
        scheduler._dependencies(con, project, {"task_id": TASK_B, "dependencies": [TASK_A],
                                               "workspace_mode": "git_worktree",
                                               "workspace_base_commit": stack_base, "dag_run": None})
    finally:
        con.close()
    expect(gate_ready.get("ready") is True and gate_ready.get("materialized_from") == [TASK_A]
           and gate_changed.get("ready") is False, "materialize",
           "materialization gate is not bound to the recorded stack base: " + json.dumps(
               {"ready": gate_ready, "changed": gate_changed})[:800])
    expect(git(repo, "rev-parse", stack_base + "^{commit}") == stack_base, "materialize",
           "stack base is not a persisted commit")
    stage(root, traces, "materialize", unbacked=unbacked, gate_error=gate_error,
          materialized_from=gate_ready.get("materialized_from"),
          materialization_id=gate_ready.get("materialization_id"),
          stack_base=stack_base, target_tip=tip, finish_state=materialize_finish.get("state"),
          stack_equals_landed_tip=stack_base == tip,
          downstream_task_state="draft", candidate=materialized["candidate"],
          changed_base_gate=gate_changed)

    # 16. Task B binds its own worktree at the exact materialized base.
    task_b = one(state, "SELECT revision FROM v2_task WHERE task_id=?", (TASK_B,))
    queued_b = command(state, actor, epoch, "scheduler.enqueue", {
        "task_id": TASK_B, "expected_revision": task_b["revision"],
        "request": prepared[TASK_B]["request"], "message": prepared[TASK_B]["message"],
        "agent_id": run["agents"]["executor2"], "budget": {"tokens.input": 40000, "tokens.output": 8000},
        "resources": {"native_slot": 1}, "workspace_mode": "git_worktree", "task_kind": "code",
        "workspace_base_commit": stack_base, "dependencies": [TASK_A]})
    expect(queued_b.get("status") == "QUEUED", "enqueue_b", "enqueue B failed: " + json.dumps(queued_b)[:400])
    claimed_b = call(state, root, worker_b, "awo_assignment_claim", args_for(
        epoch, task_id=TASK_B, expected_revision=queued_b["revision"]))
    expect(claimed_b.get("status") == "RESERVED", "claim_b",
           "materialized dependency did not unlock B: " + json.dumps(claimed_b)[:400])
    bid = claimed_b["assignment_id"]
    assignment_b = assignment_row(state, bid)
    plan_b = prepared[TASK_B]["workspace"]
    common_b = {"assignment_id": bid, "expected_revision": assignment_b["revision"],
                "kind": "git_worktree", "root": str((root / "workspaces").resolve()),
                "allowed_paths": ["calc.py", "test_calc.py"], "repository_id": "fixture-code-repo",
                "repo_root": str(repo)}
    try:
        workspace_call(state, actor, epoch, "workspace.create",
                       dict(common_b, path=plan_b, base_commit=base))
        changed_base_denied = None
    except Exception as exc:
        changed_base_denied = {"type": type(exc).__name__, "message": str(exc)}
    expect(changed_base_denied and "frozen materialized base commit" in changed_base_denied["message"],
           "workspace_create_b", "changed base was not refused: " + json.dumps(changed_base_denied))
    created_b = workspace_call(state, actor, epoch, "workspace.create", dict(
        common_b, path=plan_b, base_commit=stack_base))
    expect(created_b.get("status") == "WORKSPACE_READY", "workspace_create_b",
           "workspace B creation failed: " + json.dumps(created_b)[:800])
    ws_b = ws_row(state, created_b["workspace"]["workspace_id"])
    expect(ws_b and Path(ws_b["path"]).resolve() == Path(plan_b).resolve()
           and ws_b["base_commit"] == stack_base, "workspace_create_b",
           "workspace B is not the frozen materialized worktree")
    expect(git(ws_b["path"], "rev-parse", "HEAD") == stack_base, "workspace_create_b",
           "worktree B HEAD is not the materialized base")
    expect(b"return a + b" in (Path(ws_b["path"]) / "calc.py").read_bytes(), "workspace_create_b",
           "materialized worktree does not contain the accepted dependency patch")
    base_b_test = run_test(root, "materialized-base-test-b", ws_b["path"])
    expect(base_b_test["returncode"] == 0, "workspace_create_b",
           "materialized base does not pass the dependency's correctness test")
    worktrees = git(repo, "worktree", "list", "--porcelain")
    expect(str(Path(ws_a["path"]).resolve()).replace("\\", "/") in worktrees.replace("\\", "/")
           and str(Path(ws_b["path"]).resolve()).replace("\\", "/") in worktrees.replace("\\", "/"),
           "workspace_create_b", "independent worktrees are not concurrently registered")
    stage(root, traces, "workspace_create_b", assignment_id=bid, workspace_id=ws_b["workspace_id"],
          path=ws_b["path"], base_commit=stack_base, branch=ws_b["branch"],
          changed_base_denied=changed_base_denied,
          dependency_patch_present=True, base_test_returncode=base_b_test["returncode"],
          concurrent_worktrees=[ws_a["path"], ws_b["path"]],
          distinct_branches=sorted([ws_a["branch"], ws_b["branch"]]))

    # 17. The downstream worker really executes in its own worktree.
    dispatched_b = scheduler.dispatch_assignment(state, bid, *actor, expected_epoch=epoch)
    expect(dispatched_b.get("status") == "SUBMITTED", "dispatch_b",
           "dispatch B failed: " + json.dumps(dispatched_b)[:400])
    jobs_b = list((root / "worker-b" / "jobs").iterdir())
    expect(len(jobs_b) == 1, "dispatch_b", "bridge B did not create exactly one job")
    job_b = monitor_task.read_json(jobs_b[0] / "job.json")
    expect(Path(job_b["execution_cwd"]).resolve() == Path(ws_b["path"]).resolve(), "dispatch_b",
           "job B execution directory is not worktree B")
    observed_b = wait_turn(root / "worker-b", 0, deadline, "worker_turn_b")
    expect(observed_b.get("status") == "idle" and observed_b.get("completion_seq") == 1, "worker_turn_b",
           "unexpected bridge B status: " + json.dumps(observed_b)[:600])
    receipt_b = monitor_task.read_json(jobs_b[0] / "receipt" / "status.json")
    expect(receipt_b["status"] == "done" and receipt_b["returncode"] == 0
           and Path(receipt_b["execution_cwd"]).resolve() == Path(ws_b["path"]).resolve(), "worker_turn_b",
           "receipt B is not a clean worktree completion: " + json.dumps(receipt_b)[:600])
    status_b = git(ws_b["path"], "status", "--porcelain=v1", "--untracked-files=all").splitlines()
    changed_b = sorted(line[3:] for line in status_b if line.strip())
    expect(changed_b == ["calc.py", "test_calc.py"], "worker_turn_b",
           "harness B changed unexpected paths: " + json.dumps(changed_b))
    view_b = code_change_view(ws_b["path"], stack_base)
    expect(b"def mul(" in view_b["calc"] and b"return a + b" in view_b["calc"], "worker_turn_b",
           "worktree B does not keep the dependency fix and the new function")
    patch_b = write_new(root / "evidence" / "b.patch", view_b["patch"])
    test_b = run_test(root, "patched-test-b", ws_b["path"])
    expect(test_b["returncode"] == 0, "worker_turn_b",
           "patched worktree B failed its correctness test: " + test_b["stderr"][-800:])
    stage(root, traces, "worker_turn_b", status=observed_b, changed_paths=changed_b,
          receipt={"status": receipt_b["status"], "returncode": receipt_b["returncode"],
                   "cwd": receipt_b["cwd"], "execution_cwd": receipt_b["execution_cwd"],
                   "sha256": sha(jobs_b[0] / "receipt" / "status.json")},
          patch_path=str(patch_b), patch_sha256=sha(patch_b),
          calc_blob=view_b["calc_sha"], test_blob=view_b["test_sha"],
          test_returncode=test_b["returncode"],
          result_sha256=sha(Path(job_b["cwd"]) / "RESULT.md"))

    # 18. Full downstream acceptance so no claim or workspace leaks.
    reconciled_b = scheduler.reconcile_assignment(state, bid, *actor, expected_epoch=epoch)
    expect(reconciled_b.get("status") == "REVIEW_READY", "reconcile_b",
           "reconcile B failed: " + json.dumps(reconciled_b)[:400])
    event_b = reconciled_b["event_id"]
    git(ws_b["path"], "-c", "user.name=AWO Code Fixture", "-c", "user.email=awo@example.invalid",
        "add", "--", "calc.py", "test_calc.py")
    git(ws_b["path"], "-c", "user.name=AWO Code Fixture", "-c", "user.email=awo@example.invalid",
        "commit", "-m", "add mul() and cover it")
    source_commit_b = git(ws_b["path"], "rev-parse", "HEAD")
    expect(git(ws_b["path"], "status", "--porcelain=v1", "--untracked-files=all") == "", "commit_b",
           "worktree B is not clean after commit")
    inspection_b = workspace_runtime.inspect(state, actor[0], actor[1], "main", ws_b["workspace_id"])
    expect(inspection_b["inspection"]["inventory"]["scope_ok"]
           and not inspection_b["inspection"]["inventory"]["dirty"]
           and inspection_b["inspection"]["inventory"]["changed_paths"] == ["calc.py", "test_calc.py"],
           "commit_b", "worktree B inventory is not the declared patch")
    recorded_b = record_delivery(root, state, run, worker_b, bid, job_b["cwd"])
    completed_b = complete_assignment(state, root, run, worker_b, bid, recorded_b,
                                      "Codex added mul() on the materialized base.")
    expect(completed_b.get("status") == "REVIEW_READY", "complete_b",
           "complete B failed: " + json.dumps(completed_b)[:400])
    view_b = code_change_view(ws_b["path"], stack_base)
    review_prompt_b = (
        "Review a small code change independently. Read every file in your current directory with your "
        "tools before deciding: request.md, calc.py, test_calc.py, patch.diff, tests.json.\n"
        "Requirement: add mul(a, b) returning the product and one unittest case for mul(3, 4) == 12; "
        "keep the existing add() fix; only calc.py and test_calc.py may change.\n"
        "Task: " + TASK_B + "\nNonce: " + prepared[TASK_B]["request"]["nonce"] + "\n"
        "Materialized base: " + stack_base + "\nCommitted source commit: " + source_commit_b + "\n"
        "Return JSON only with exactly the keys task_id, nonce, verdict (accept or reject), reason.")
    review_b, review_dir_b, _ = reviewer_turn(root, "task-b", review_prompt_b, {
        "request.md": "add mul(a, b) and one unittest case; keep add() correct.\n",
        "calc.py": view_b["calc"].decode("utf-8"),
        "test_calc.py": view_b["test"].decode("utf-8"),
        "patch.diff": view_b["patch"].decode("utf-8"),
        "tests.json": json.dumps({"materialized_base_test_returncode": base_b_test["returncode"],
                                  "patched_test_returncode": test_b["returncode"],
                                  "patched_stdout": test_b["stdout"],
                                  "patched_stderr": test_b["stderr"]}, indent=2)},
        run["timeout"], run["specs"]["reviewer"])
    expect(review_b.get("task_id") == TASK_B
           and review_b.get("nonce") == prepared[TASK_B]["request"]["nonce"]
           and review_b.get("verdict") in ("accept", "reject"), "review_b",
           "reviewer B output lacks exact task/nonce/verdict")
    if review_b["verdict"] != "accept":
        return finish(root, {"status": "WORKTREE_CODE_REVIEW_REJECTED", "stage": "review_b",
                             "review": review_b}, traces)
    note_b = Path(prepared[TASK_B]["request"]["review_path"])
    note_b.write_text(TASK_B + "-RUN " + prepared[TASK_B]["request"]["nonce"] + "\nVerdict: accept\n" +
                      "Independent Codex review: " + str(review_b.get("reason", "")) + "\n",
                      encoding="utf-8", newline="\n")
    submitted_b = review_submit(state, root, run, reviewer_b, bid, TASK_B, event_b, recorded_b, note_b)
    expect(submitted_b.get("status") == "REVIEW_RECORDED", "review_b",
           "review submit B failed: " + json.dumps(submitted_b)[:400])
    sealed_b = one(state, "SELECT * FROM v2_workspace_review WHERE assignment_id=?", (bid,))
    inventory_b = json.loads(sealed_b["inventory_json"])
    expect(sealed_b["verdict"] == "accept" and inventory_b["scope_ok"]
           and inventory_b["changed_paths"] == ["calc.py", "test_calc.py"]
           and inventory_b["head"] == source_commit_b, "review_b",
           "sealed worktree B inventory is not the reviewed patch")
    closed_b = close_assignment(state, actor, run, bid, event_b)
    expect(closed_b.get("status") == "CLOSED" and closed_b.get("outcome") == "accepted", "close_b",
           "close B failed: " + json.dumps(closed_b)[:400])
    frozen_b = freeze_workspace(state, actor, run, root, ws_b["workspace_id"], "code-b")
    stage(root, traces, "accept_b", review=review_b, note_sha256=sha(note_b),
          inventory_sha256=sealed_b["inventory_sha256"], source_commit=source_commit_b,
          close=closed_b, freeze=frozen_b["status"], task_state=closed_b["task_state"])

    # 19. Release both worktrees: no live process, registration or held claim remains.
    released_b = release_workspace(state, actor, run, root, ws_b["workspace_id"], "release-b")
    released_a = release_workspace(state, actor, run, root, ws_a["workspace_id"], "release-a")
    listing = git(repo, "worktree", "list", "--porcelain")
    expect(str(Path(ws_a["path"]).resolve()).replace("\\", "/") not in listing.replace("\\", "/")
           and str(Path(ws_b["path"]).resolve()).replace("\\", "/") not in listing.replace("\\", "/"),
           "release", "a worktree remains registered after release")
    expect(not Path(ws_a["path"]).exists() and not Path(ws_b["path"]).exists(), "release",
           "a released worktree path still exists")
    for label, spec in (("worker-a", run["specs"]["executor"]), ("worker-b", run["specs"]["executor2"]),
                        ("reviewer", run["specs"]["reviewer"])):
        current = native_worker.status(root / label)
        expect(current["status"] == "idle", "release", label + " is not idle after release")
        active = root / label / "active.json"
        if active.exists():
            pid = monitor_task.read_json(active).get("runner_pid")
            expect(pid is None or not pid_alive(pid), "release",
                   label + " still has a live runner")
    expect(assignment_row(state, aid)["holds_slot"] == 0 and assignment_row(state, bid)["holds_slot"] == 0,
           "release", "an accepted Assignment still holds its slot")
    branches = {"a": git(repo, "rev-parse", "refs/heads/" + ws_a["branch"]),
                "b": git(repo, "rev-parse", "refs/heads/" + ws_b["branch"]),
                "integration": git(repo, "rev-parse", "refs/heads/integration")}
    expect(branches["a"] == source_commit_a and branches["b"] == source_commit_b
           and branches["integration"] == candidate["candidate_commit"], "release",
           "released worktree branches/refs do not retain the accepted commits")
    stage(root, traces, "release", released=[released_a["status"], released_b["status"]],
          paths_removed=True, worktrees_registered=False, worker_status="idle",
          runner_processes="exited", holds_slot={"a": assignment_row(state, aid)["holds_slot"],
                                                 "b": assignment_row(state, bid)["holds_slot"]},
          retained_refs=branches)

    final = {
        "task_a": one(state, "SELECT task_state,accepted_evidence_id FROM v2_task WHERE task_id=?", (TASK_A,)),
        "task_b": one(state, "SELECT task_state,accepted_evidence_id FROM v2_task WHERE task_id=?", (TASK_B,)),
        "assignment_a": {k: assignment_row(state, aid)[k] for k in
                         ("state", "review_status", "holds_slot", "workspace_id")},
        "assignment_b": {k: assignment_row(state, bid)[k] for k in
                         ("state", "review_status", "holds_slot", "workspace_id")},
        "reviews": state_rows("SELECT task_id,verdict,state FROM v2_review ORDER BY task_id"),
        "evidence": state_rows("SELECT task_id,path,sealed_sha256,bytes,sealed,state FROM v2_evidence "
                               "ORDER BY task_id,path"),
        "workspaces": [{"workspace_id": w["workspace_id"], "task_id": w["task_id"], "kind": w["kind"],
                        "status": w["status"], "base_commit": w["base_commit"],
                        "branch": w.get("branch"), "path": w["path"],
                        "repository_id": w.get("repository_id")}
                       for w in (ws_row(state, r["workspace_id"]) for r in state_rows(
                           "SELECT workspace_id FROM v2_workspace ORDER BY task_id"))],
        "merge_requests": state_rows("SELECT merge_request_id,state,merge_commit FROM v2_merge_request"),
        "materializations": state_rows("SELECT task_id,base_commit,parents_json FROM v2_merge_materialization "
                                       "WHERE task_id=?", (TASK_B,)),
        "intents": state_rows("SELECT intent_id,kind,state FROM v2_merge_intent ORDER BY created_at,intent_id"),
    }
    stage(root, traces, "final_state", **final)
    payload = {"status": "V2_WORKTREE_CODE_LOOP_PASS", "stage": "complete",
               "evidence_class": meta["evidence_class"], "base": base,
               "source_commit_a": source_commit_a, "source_commit_b": source_commit_b,
               "candidate_commit": candidate["candidate_commit"], "stack_base": stack_base,
               "target_ref": "refs/heads/integration",
               "task_states": {"a": final["task_a"]["task_state"], "b": final["task_b"]["task_state"]},
               "synthetic": {"same_agent_credential": "checked separately; see trace",
                             "duplicate_completion": duplicate},
               "final": final}
    return finish(root, payload, traces)


def usage(root):
    jobs, total = [], {}
    for role in ("worker-a", "worker-b", "reviewer"):
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


def finish(root, result, traces):
    root = Path(root)
    meta = monitor_task.read_json(root / "provenance.json")
    observed = []
    for role in ("worker-a", "worker-b", "reviewer"):
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
    if not traces and (root / "trace.jsonl").exists():
        traces = [json.loads(line) for line in (root / "trace.jsonl").read_text(
            encoding="utf-8").splitlines() if line.strip()]
    save(root / "trace.json", traces)
    save(root / "usage.json", usage(root))
    result["evidence_class"] = meta["evidence_class"]
    result["model_based_review"] = True
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
    parser.add_argument("--directory", required=True,
                        help="new validation directory; existing evidence is never overwritten")
    parser.add_argument("--executable", help="installed Codex launcher")
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args()
    root = Path(args.directory).resolve()
    if root.exists():
        parser.error("a new validation directory is required; existing evidence is never overwritten")
    try:
        candidates = ["codex.exe", "codex.cmd", "codex"] if sys.platform == "win32" else ["codex"]
        executable = args.executable or next((found for name in candidates if (found := shutil.which(name))), None)
        if not executable:
            parser.error("installed Codex executable not found")
        prepare(root, executable, args.timeout)
        result = execute(root)
    except Exception as exc:
        if not root.exists():
            raise
        result = {"status": "V2_WORKTREE_CODE_LOOP_FAILED", "stage": getattr(exc, "stage", "unknown"),
                  "error": str(exc), "herdr_invoked": False}
        try:
            meta = monitor_task.read_json(root / "provenance.json")
            result["evidence_class"] = meta.get("evidence_class", "native")
            finish(root, result, [])
        except Exception as nested:
            result["finish_error"] = str(nested)
        print(json.dumps(result, ensure_ascii=True))
        return 2
    print(json.dumps({key: value for key, value in result.items() if key not in ("final",)},
                     ensure_ascii=True))
    return 0 if result["status"] == "V2_WORKTREE_CODE_LOOP_PASS" else 2


if __name__ == "__main__":
    sys.exit(main())
