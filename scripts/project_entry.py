"""User-facing entry for one bounded task in an existing local Git repository.

prepare freezes an operator-authored specification, a disposable clone, a fresh
ledger and the machine bindings without sending a model or moving the target
ref. run performs the explicitly confirmed execution through the existing
scheduler, native bridge, managed worktree, independent reviewer and Merge
Queue. status is read-only. recover diagnoses a persisted attempt and resumes
only safe, non-send steps.

The entry adds orchestration only. It owns no database, scheduler, adapter or
merge authority; all durable state remains the existing SQLite ledger.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

import awo_service as service
import merge_queue
import monitor_task
import native_events
import native_worker
import priority_scheduler as scheduler
import service_access as access
import service_extensions
import workflow_control as control
import workspace_manager as workspace_lib
import workspace_runtime
from process_util import pid_alive
from workflow_evidence import allocate, task_key

PLAN_VERSION = 1
SHELLS = {"cmd", "powershell", "pwsh", "sh", "bash", "dash", "zsh", "ksh", "fish",
          "csh", "tcsh"}
REF = re.compile(r"refs/heads/[A-Za-z0-9._/-]{1,120}\Z")
HEX40 = re.compile(r"[0-9a-f]{40}\Z")


class EntryError(Exception):
    """A refusal or a bounded failure with a stable stage label."""

    def __init__(self, stage, message, code="refused"):
        super().__init__(message)
        self.stage = stage
        self.code = code


def sha_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                      separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode("ascii")).hexdigest()


def save_json(path, value):
    """Atomically replace a convenience cache; a crash never leaves half a file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def save_bytes(path, data):
    """Atomically replace a bounded evidence file with exact bytes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_new(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    return path


def run_argv(argv, cwd, timeout=120):
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x for x in argv):
        raise EntryError("argv", "command must be a nonempty string array")
    try:
        return workspace_lib.run_argv(argv, cwd=str(cwd), timeout=timeout)
    except Exception as exc:
        return {"returncode": 127, "stdout": b"", "stderr": str(exc).encode("utf-8")}


def git(repo, *args, timeout=180, ok=(0,)):
    result = run_argv(["git", "-c", "core.hooksPath=" + os.devnull,
                       "-c", "protocol.allow=never", "-c", "submodule.recurse=false",
                       "-C", str(repo), *args], Path(repo), timeout=timeout)
    if result["returncode"] not in ok:
        raise EntryError("git", args[0] + ": " + result["stderr"].decode("utf-8", "replace")[-2000:],
                         "git_failed")
    return result


def git_text(repo, *args, **kwargs):
    return git(repo, *args, **kwargs)["stdout"].decode("utf-8", "strict").strip("\r\n")


def git_opt(repo, *args):
    result = run_argv(["git", "-C", str(repo), *args], Path(repo), timeout=60)
    if result["returncode"]:
        return None
    return result["stdout"].decode("utf-8", "strict").strip("\r\n") or None


def same_path(first, second):
    return (os.path.normcase(os.path.realpath(str(first))) ==
            os.path.normcase(os.path.realpath(str(second))))


def _fields(raw):
    return raw.decode("utf-8", "surrogateescape").split("\0")


def _parse_name_status(raw):
    fields, result, index = _fields(raw), [], 0
    while index < len(fields):
        token = fields[index]
        if not token:
            index += 1
            continue
        kind = token[0]
        if kind in ("R", "C"):
            result.append((token, fields[index + 1], fields[index + 2]))
            index += 3
        else:
            result.append((token, fields[index + 1]))
            index += 2
    return sorted(result)


def name_status(repo, base, head):
    """Parse `git diff --name-status -z` without quoting or rename ambiguity."""
    raw = git(repo, "-c", "core.quotepath=false", "diff", "--no-ext-diff", "--no-textconv",
              "-z", "--name-status", "--find-renames", "--find-copies", base, head)["stdout"]
    return _parse_name_status(raw)


def staged_paths(repo, base):
    """Every path the index changes against the base, parsed exactly."""
    raw = git(repo, "-c", "core.quotepath=false", "diff", "--no-ext-diff", "--no-textconv",
              "-z", "--name-status", "--find-renames", "--find-copies", "--cached",
              base)["stdout"]
    return sorted({path for item in _parse_name_status(raw) for path in item[1:]})


def status_paths(repo):
    """Parse `git status --porcelain=v1 -z` including quoted names and renames."""
    raw = git(repo, "-c", "core.quotepath=false", "status", "--porcelain=v1", "-z",
              "--untracked-files=all", "--ignore-submodules=none")["stdout"]
    fields, result, index = _fields(raw), [], 0
    while index < len(fields):
        item = fields[index]
        if not item:
            index += 1
            continue
        code = item[:2]
        if "R" in code or "C" in code:
            result.append((code, [item[3:], fields[index + 1]]))
            index += 2
        else:
            result.append((code, [item[3:]]))
            index += 1
    return result


def tracked_worktree_clean(repo):
    def names(*args):
        return git(repo, *args)["stdout"].decode("utf-8", "replace").strip()
    return names("diff", "--name-only") == "" and names("diff", "--cached", "--name-only") == ""


# ---------------------------------------------------------------------------
# Specification validation


def _require(condition, stage, message, code="refused"):
    if not condition:
        raise EntryError(stage, message, code)


def _safe_relative(value, label):
    _require(isinstance(value, str) and value, "spec", label + " must be a nonempty string")
    _require("\\" not in value, "spec", label + " must use forward slashes")
    _require(not value.startswith("/") and not re.match(r"^[A-Za-z]:", value), "spec",
             label + " must be relative")
    parts = value.split("/")
    _require(all(part not in ("", ".", "..") for part in parts), "spec",
             label + " must not contain empty, dot or parent segments")
    _require(".git" not in parts, "spec", label + " must not address repository metadata")
    return value


def _pinned_executable(value, label, allow_launcher=False, stage="spec"):
    _require(isinstance(value, str) and Path(value).is_absolute(), stage,
             label + " must be an absolute path")
    path = Path(value).resolve()
    _require(path.is_file(), stage, label + " does not exist")
    suffix = path.suffix.lower()
    if allow_launcher:
        _require(suffix not in (".ps1", ".sh", ".js", ".mjs"), stage,
                 label + " must be a native or .cmd/.bat launcher")
    else:
        _require(suffix not in (".cmd", ".bat", ".ps1", ".sh", ".js", ".mjs"), stage,
                 label + " must be a native executable, not an interpreter script")
    _require(path.stem.lower() not in SHELLS, stage, label + " must not be a shell")
    return path


def pinned_inputs(plan):
    """Recompute the frozen verification inputs from the plan's own argv."""
    d = plan["derived"]
    argv = d["verify_argv"]
    executable = _pinned_executable(argv[0], "verify_argv[0]", stage="environment")
    pins = {str(executable): sha_file(executable)}
    for item in argv[1:]:
        if Path(item).is_absolute():
            path = Path(item).resolve()
            _require(path.is_file(), "environment",
                     "pinned verification input disappeared: " + str(path), "pins_changed")
            pins[str(path)] = sha_file(path)
    return pins


def validate_pins(plan):
    """Refuse a changed executable or absolute input before any model/command effect."""
    frozen = plan["derived"].get("pins")
    _require(isinstance(frozen, dict) and frozen, "environment",
             "the plan has no frozen verification pins", "pins_missing")
    current = pinned_inputs(plan)
    changed = sorted(path for path in set(frozen) | set(current)
                     if frozen.get(path) != current.get(path))
    _require(not changed, "environment",
             "pinned verification input changed after preparation: " +
             json.dumps(changed), "pins_changed")


def _tree_entries(repository, commit, path):
    raw = git(repository, "-c", "core.quotepath=false", "ls-tree", "-z", commit, "--",
              path)["stdout"]
    entries = []
    for item in _fields(raw):
        if not item:
            continue
        header, name = item.split("\t", 1)
        mode, kind, oid = header.split(" ", 2)
        entries.append({"mode": mode, "kind": kind, "oid": oid, "name": name})
    return entries


def _validate_allowed_entries(repository, commit, paths):
    """Refuse allowed-path semantics this entry cannot review safely."""
    for item in paths:
        parts = item.split("/")
        for stop in range(1, len(parts)):
            parent = "/".join(parts[:stop])
            for entry in _tree_entries(repository, commit, parent):
                _require(entry["name"] == parent, "spec",
                         "allowed path parent is not a plain directory: " + parent)
                _require(entry["kind"] == "tree", "spec",
                         "allowed path parent is not a directory: " + parent)
        for entry in _tree_entries(repository, commit, item):
            _require(entry["name"] == item, "spec", "unexpected tree entry for " + item)
            _require(entry["kind"] == "blob" and entry["mode"] in ("100644", "100755"), "spec",
                     "allowed path is not a regular file: " + item)


def validate_spec(spec):
    allowed = {"version", "repository", "goal", "allowed_paths", "verify_argv",
               "worker", "reviewer", "authorization", "budget", "target_ref",
               "work_root", "change_name", "max_real_turns", "timeout_seconds"}
    _require(isinstance(spec, dict), "spec", "spec must be an object")
    _require(set(spec) <= allowed, "spec", "unknown spec keys: " + ", ".join(sorted(set(spec) - allowed)))
    for key in ("version", "repository", "goal", "allowed_paths", "verify_argv",
                "worker", "reviewer", "authorization", "budget", "target_ref",
                "work_root", "change_name"):
        _require(key in spec, "spec", "missing spec key: " + key)
    _require(spec["version"] == PLAN_VERSION, "spec", "unsupported spec version")
    _require(isinstance(spec["goal"], str) and spec["goal"].strip(), "spec", "goal is empty")
    _require(isinstance(spec["change_name"], str)
             and re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", spec["change_name"]) is not None,
             "spec", "change_name must be a lowercase slug")
    paths = spec["allowed_paths"]
    _require(isinstance(paths, list) and paths, "spec", "allowed_paths must be nonempty")
    spec["allowed_paths"] = [_safe_relative(item, "allowed path") for item in paths]
    _require(len(set(spec["allowed_paths"])) == len(spec["allowed_paths"]), "spec",
             "allowed_paths must be unique")

    repository = spec["repository"]
    _require(isinstance(repository, str) and Path(repository).is_absolute(), "spec",
             "repository must be absolute")
    repository = Path(repository).resolve()
    _require(repository.is_dir(), "spec", "repository directory does not exist")
    top = git_opt(repository, "rev-parse", "--show-toplevel")
    _require(top and Path(top).resolve() == repository, "spec",
             "repository must be the root of a local Git working tree")
    head = git_opt(repository, "rev-parse", "--verify", "HEAD")
    _require(head and HEX40.fullmatch(head.lower()), "spec", "repository has no commit")
    _validate_allowed_entries(repository, head, spec["allowed_paths"])

    work_root = Path(spec["work_root"])
    _require(work_root.is_absolute(), "spec", "work_root must be absolute")
    work_root = work_root.resolve()
    _require(not work_root.exists(), "spec", "work_root must be a new directory")
    _require(not workspace_lib._inside(work_root, repository)
             and not workspace_lib._inside(repository, work_root), "spec",
             "work_root must be disjoint from the source repository")

    argv = spec["verify_argv"]
    _require(isinstance(argv, list) and argv and all(isinstance(x, str) and x for x in argv),
             "spec", "verify_argv must be a nonempty string array")
    executable = _pinned_executable(argv[0], "verify_argv[0]")
    pins = {str(executable): sha_file(executable)}
    for item in argv[1:]:
        if Path(item).is_absolute():
            _require(Path(item).is_file(), "spec", "absolute verify input does not exist: " + item)
            pins[str(Path(item).resolve())] = sha_file(Path(item).resolve())
    spec["verify_argv"] = [str(executable)] + argv[1:]

    for name in ("worker", "reviewer"):
        bridge = spec[name]
        _require(isinstance(bridge, dict), "spec", name + " must be an object")
        _require(set(bridge) <= {"harness", "executable", "timeout_seconds"}, "spec",
                 name + " has unknown keys")
        _require(bridge.get("harness") == "codex", "spec",
                 name + " harness must be codex for the verified one-shot path")
        _pinned_executable(bridge.get("executable"), name + ".executable", allow_launcher=True)
        timeout = bridge.get("timeout_seconds", 300)
        _require(type(timeout) is int and 1 <= timeout <= 3600, "spec",
                 name + ".timeout_seconds must be 1..3600")
        bridge["timeout_seconds"] = timeout

    auth = spec["authorization"]
    _require(isinstance(auth, dict) and set(auth) <= {"source", "dispatch_worker", "allow_local_land"},
             "spec", "authorization has unknown keys")
    _require(isinstance(auth.get("source"), str) and auth["source"].strip(), "spec",
             "authorization.source is required")
    _require(auth.get("dispatch_worker") is True, "spec",
             "authorization.dispatch_worker must be true in the prepared plan")
    _require(type(auth.get("allow_local_land")) is bool, "spec",
             "authorization.allow_local_land must be boolean")

    budget = spec["budget"]
    _require(isinstance(budget, dict) and budget, "spec", "budget must be a nonempty object")
    _require(all(isinstance(k, str) and k.startswith("tokens.") and type(v) is int and v > 0
                 for k, v in budget.items()), "spec",
             "budget must map tokens.* names to positive integers")

    target = spec["target_ref"]
    _require(isinstance(target, str) and REF.fullmatch(target), "spec",
             "target_ref must be refs/heads/...")
    _require(target not in ("refs/heads/main", "refs/heads/master"), "spec",
             "target_ref must not be a protected default branch")
    _require(target != "refs/heads/awo" and not target.startswith("refs/heads/awo/"),
             "spec", "target_ref must not share the managed worktree ref namespace")
    branch = git_opt(repository, "symbolic-ref", "-q", "--short", "HEAD")
    _require(branch is None or target != "refs/heads/" + branch, "spec",
             "target_ref must not equal the source branch")

    turns = spec.get("max_real_turns", 12)
    _require(type(turns) is int and 1 <= turns <= 12, "spec", "max_real_turns must be 1..12")
    spec["max_real_turns"] = turns
    timeout = spec.get("timeout_seconds", 300)
    _require(type(timeout) is int and 1 <= timeout <= 300, "spec",
             "timeout_seconds must be 1..300")
    spec["timeout_seconds"] = timeout
    spec["_pins"] = pins
    return spec


# ---------------------------------------------------------------------------
# Plan helpers


def plan_hash(plan):
    value = dict(plan)
    value.pop("plan_sha256", None)
    return digest(value)


def load_plan(path):
    path = Path(path).resolve()
    _require(path.is_file(), "plan", "plan file does not exist")
    plan = read_json(path)
    _require(plan.get("kind") == "awo-project-entry-plan", "plan", "not a project-entry plan")
    _require(plan.get("version") == PLAN_VERSION, "plan", "unsupported plan version")
    stored = plan.get("plan_sha256")
    _require(stored == plan_hash(plan), "plan", "plan hash changed after preparation")
    return plan


def _paths(plan):
    return {key: Path(value) for key, value in plan["derived"]["paths"].items()}


def _actor(plan):
    derived = plan["derived"]
    return derived["controller"]["instance_id"], derived["controller"]["version"], "main"


def _token(plan, name):
    return Path(plan["derived"]["tokens"][name]).read_text(encoding="utf-8").strip()


def _db(state):
    return service._db_path(Path(state))


def _query(state, sql, args=()):
    con = service._connect(_db(state), readonly=True)
    try:
        return [dict(row) for row in con.execute(sql, args)]
    finally:
        con.close()


def _one(state, sql, args=()):
    found = _query(state, sql, args)
    return found[0] if found else None


def _workspace_row(state, workspace_id):
    """Read a workspace as the runtime does: stored columns plus hashed spec."""
    row = _one(state, "SELECT * FROM v2_workspace WHERE workspace_id=?", (workspace_id,))
    _require(row, "workspace", "bound workspace row is missing")
    spec = json.loads(row["spec_json"])
    _require(digest(spec) == row["spec_sha256"], "workspace",
             "workspace specification hash mismatch")
    return dict(spec, **row)


def gateway(state, project_root, token, tool, arguments=None):
    return service.gateway_call(state, str(Path(project_root).resolve()), token, tool,
                                arguments or {})


def api_args(epoch, **payload):
    return dict(payload, command_id="awo-entry-api-" + uuid.uuid4().hex, expected_epoch=epoch)


def command(state, actor, epoch, tool, payload):
    return service.execute_command(state, actor[0], actor[1], "main", {
        "tool": tool, "payload": payload,
        "command_id": "awo-entry-cmd-" + uuid.uuid4().hex, "expected_epoch": epoch})


def workspace_call(state, actor, epoch, tool, payload):
    handler = getattr(workspace_runtime, tool.split(".", 1)[1])
    return handler(state, actor[0], actor[1], "main", {
        "tool": tool, "payload": payload,
        "command_id": "awo-entry-ws-" + uuid.uuid4().hex, "expected_epoch": epoch})


def merge_call(state, repo, token, tool, payload, epoch):
    return service_extensions.call(state, str(repo), token, tool, {
        "payload": payload, "command_id": "awo-entry-merge-" + uuid.uuid4().hex,
        "expected_epoch": epoch})


def issue(state, actor, epoch, payload, path):
    result = access.issue_token(state, *actor, {
        "tool": "access.issue", "payload": payload,
        "command_id": "awo-entry-access-" + uuid.uuid4().hex, "expected_epoch": epoch},
        str(path))
    return Path(result["token_file"]).read_text(encoding="utf-8").strip()


def bridge_identity(root):
    return native_worker.status(Path(root))["identity"]


def cli_version(executable):
    argv = native_worker.cli_prefix(str(executable)) + ["--version"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60)
        return (proc.stdout or proc.stderr).strip() or "unknown"
    except Exception:
        return "unknown"


def _turn_count(plan):
    worker = Path(plan["derived"]["paths"]["worker"])
    reviewer = Path(plan["derived"]["paths"]["reviewer"])
    total = 0
    for root in (worker, reviewer):
        jobs = root / "jobs"
        if jobs.is_dir():
            total += sum(1 for item in jobs.iterdir() if item.is_dir())
    return total


def _enforce_turn_budget(plan):
    used = _turn_count(plan)
    cap = plan["derived"]["max_real_turns"]
    if used >= cap:
        raise EntryError("turn_budget", "real turn cap reached (" + str(used) + "/" + str(cap) + ")",
                         "turn_budget")


# ---------------------------------------------------------------------------
# prepare


def _resolve_output(spec, out_dir):
    """Bind the declared work_root to the actual --out before any write."""
    raw = Path(out_dir)
    _require(raw.is_absolute(), "prepare", "--out must be an absolute path")
    try:
        out = raw.resolve()
    except OSError as exc:
        raise EntryError("prepare", "output path cannot be resolved safely: " + str(exc))
    declared = Path(spec["work_root"])
    _require(same_path(out, declared), "prepare",
             "work_root must name the same directory as --out")
    source = Path(os.path.realpath(str(Path(spec["repository"]).resolve())))
    for path in (out, declared):
        _require(not workspace_lib._inside(path, source)
                 and not workspace_lib._inside(source, path), "prepare",
                 "output root must be disjoint from the source repository")
    _require(not out.exists(), "prepare", "output directory must be new")
    ancestor = out.parent
    while True:
        if ancestor.exists():
            linked = ancestor.is_symlink() or bool(
                getattr(ancestor, "is_junction", lambda: False)())
            _require(ancestor.is_dir() and not linked, "prepare",
                     "output ancestor is not a real directory: " + str(ancestor))
            break
        if ancestor.parent == ancestor:
            break
        ancestor = ancestor.parent
    return out


def prepare(spec_path, out_dir):
    spec = validate_spec(read_json(Path(spec_path)))
    out = _resolve_output(spec, out_dir)
    out.mkdir(parents=True)
    try:
        return _prepare_into(spec, spec_path, out)
    except BaseException:
        # A refusal or crash must not leave a partial output root behind.
        shutil.rmtree(out, ignore_errors=True)
        raise


def _prepare_into(spec, spec_path, out):
    (out / "spec.json").write_bytes(Path(spec_path).read_bytes())

    repository = Path(spec["repository"]).resolve()
    head = git_opt(repository, "rev-parse", "--verify", "HEAD").lower()
    base = head
    source_branch = git_opt(repository, "symbolic-ref", "-q", "--short", "HEAD")
    remote = git_opt(repository, "config", "--get", "remote.origin.url") or ""
    dirty = git_text(repository, "status", "--porcelain=v1", "--untracked-files=all")
    repository_id = "awo-" + hashlib.sha256(
        (str(repository) + "\n" + remote + "\n" + base).encode("utf-8")).hexdigest()[:20]

    clone = out / "repo"
    cloned = run_argv(["git", "clone", "--no-hardlinks", "--no-checkout", "--",
                       str(repository), str(clone)], out, timeout=300)
    _require(cloned["returncode"] == 0, "prepare",
             "clone failed: " + cloned["stderr"].decode("utf-8", "replace")[-1200:])
    checked = run_argv(["git", "-C", str(clone), "-c", "core.autocrlf=false",
                        "-c", "core.eol=lf", "checkout", "--detach", base], clone, timeout=180)
    _require(checked["returncode"] == 0, "prepare",
             "checkout failed: " + checked["stderr"].decode("utf-8", "replace")[-1200:])
    _require(git_text(clone, "rev-parse", "HEAD").lower() == base, "prepare",
             "clone HEAD is not the frozen base")
    _require(git_text(clone, "status", "--porcelain=v1", "--untracked-files=all") == "",
             "prepare", "clone is not clean after checkout")
    for key, value in (("core.autocrlf", "false"), ("core.eol", "lf"),
                       ("core.hooksPath", os.devnull)):
        run_argv(["git", "-C", str(clone), "config", key, value], clone, timeout=30)
    target = spec["target_ref"]
    existing = git_opt(clone, "rev-parse", "--verify", "--quiet", target)
    if existing:
        _require(existing.lower() == base, "prepare", "target ref already points elsewhere")
    _require(git_opt(clone, "symbolic-ref", "-q", target) is None, "prepare",
             "target ref must be a direct branch ref")

    for name in ("workspaces", "delivery", "evidence", "notes", "quiescence",
                 "review-runs", "secrets", "seed", "merge-ops", "merge-archive"):
        (out / name).mkdir()
    state = out / "state"
    workspace_path = (out / "workspaces" / "change").resolve()
    _require(not workspace_path.exists(), "prepare", "frozen worktree path must be new")

    cfg = {
        "roles": {"planner": {}, "executor": {}, "reviewer": {}},
        "workflow": {"reviewer_role": "reviewer", "dispatch_role": "planner",
                     "execution_role": "executor",
                     "transitions": {"accept": "next", "repair": "repair", "reject": "stop"}},
        "scheduler": {"max_concurrency": 1, "resources": {"native_slot": 1}},
    }
    control.initialize(state, cfg)
    version = cli_version(spec["worker"]["executable"])
    controller_spec = dict(native_worker.initialize(
        out / "controller", "codex", spec["worker"]["executable"], "one-shot",
        spec["worker"]["timeout_seconds"]), role="planner", version=version)
    worker_spec = dict(native_worker.initialize(
        out / "worker", "codex", spec["worker"]["executable"], "one-shot",
        spec["worker"]["timeout_seconds"], launch_cwd=str(workspace_path)),
        role="executor", version=version)
    reviewer_spec = dict(native_worker.initialize(
        out / "reviewer", "codex", spec["reviewer"]["executable"], "one-shot",
        spec["reviewer"]["timeout_seconds"]), role="reviewer",
        context_policy="inbox_only", version=version)

    controller = control.bind(state, "planner", controller_spec,
                              bridge_identity(out / "controller"), "controller", "inbox_only")
    worker = control.bind(state, "executor", worker_spec,
                          bridge_identity(out / "worker"), "worker")
    reviewer = control.bind(state, "reviewer", reviewer_spec,
                            bridge_identity(out / "reviewer"), "reviewer", "inbox_only")
    actor = (controller["instance_id"], controller["version"], "main")
    service.apply_migration(state, *actor)
    con = service._connect(_db(state))
    try:
        con.execute("BEGIN IMMEDIATE")
        scheduler.create_scheduler_schema(con)
        con.commit()
    finally:
        con.close()
    enabled = service.set_enabled(state, *actor, True)
    epoch = enabled["epoch"]
    project_id = service.service_status(state)["project_id"]
    agents = {"controller": service._agent_id_for_binding(controller["instance_id"]),
              "worker": service._agent_id_for_binding(worker["instance_id"]),
              "reviewer": service._agent_id_for_binding(reviewer["instance_id"])}

    capability = out / "evidence" / "capability.json"
    save_json(capability, {
        "kind": "installed-cli-command-bridge", "harness": "codex", "version": version,
        "mode": "one-shot",
        "worker_config_sha256": sha_file(out / "worker" / "config.json"),
        "reviewer_config_sha256": sha_file(out / "reviewer" / "config.json"),
        "bridge_source_sha256": sha_file(Path(native_worker.__file__)),
        "run_job_source_sha256": sha_file(Path(native_worker.__file__).with_name("run_job.py"))})
    for key in scheduler.DEFAULT_CAPABILITIES:
        worker_agent = service.agent_get(state, *actor, agents["worker"])["agent"]
        service.set_agent_capability(state, *actor, {
            "tool": "agent.capability",
            "payload": {"agent_id": worker_agent["agent_id"], "capability_key": key,
                        "level": "supported", "harness_version": version, "os": sys.platform,
                        "host": "local", "checked_at": service._now(),
                        "evidence_path": str(capability), "evidence_sha256": sha_file(capability),
                        "source": "live_probe", "expected_revision": worker_agent["revision"]},
            "command_id": "awo-entry-cap-" + uuid.uuid4().hex, "expected_epoch": epoch})

    controller_token = issue(state, actor, epoch, {
        "project_root": str(out), "role": "controller", "agent_id": agents["controller"],
        "binding_id": controller["instance_id"], "binding_version": controller["version"]},
        out / "secrets" / "controller.token")
    merge_token = issue(state, actor, epoch, {
        "project_root": str(clone), "role": "controller", "agent_id": agents["controller"],
        "binding_id": controller["instance_id"], "binding_version": controller["version"]},
        out / "secrets" / "merge.token")
    worker_token = issue(state, actor, epoch, {
        "project_root": str(out), "role": "worker", "agent_id": agents["worker"]},
        out / "secrets" / "worker.token")
    task_id = "ENTRY-" + spec["change_name"].upper().replace(".", "-")[:48]
    created = gateway(state, out, controller_token, "awo_task_create", api_args(
        epoch, task_id=task_id, title="Project entry: " + spec["change_name"],
        body="Operator-authorized bounded change in an existing local repository.", priority="P3"))
    _require(created.get("status") == "CREATED", "prepare",
             "task creation failed: " + json.dumps(created)[:500])
    reviewer_token = issue(state, actor, epoch, {
        "project_root": str(out), "role": "reviewer", "agent_id": agents["reviewer"],
        "binding_id": reviewer["instance_id"], "binding_version": reviewer["version"],
        "task_id": task_id}, out / "secrets" / "reviewer.token")
    merge_reviewer_token = issue(state, actor, epoch, {
        "project_root": str(clone), "role": "reviewer", "agent_id": agents["reviewer"],
        "binding_id": reviewer["instance_id"], "binding_version": reviewer["version"],
        "task_id": task_id}, out / "secrets" / "merge-reviewer.token")

    attempt_id = task_id + "-RUN"
    nonce = uuid.uuid4().hex
    request = {
        "task_id": attempt_id, "nonce": nonce, "cwd": str((out / "seed").resolve()),
        "generation": 1, "workflow_state": str(state),
        "worker": dict(worker_spec, baseline_completion_seq=0),
        "reviewer": dict(reviewer_spec), "dispatch_confirmed": False,
        "authorization": {"source": spec["authorization"]["source"], "dispatch_worker": True,
                          "notify_reviewer": False},
        "completion": {"binding_artifact": "HANDOFF.md", "artifacts": ["RESULT.md"]},
        "monitor": {"interval_seconds": 1, "settle_grace_seconds": 3,
                    "command_timeout_seconds": 15},
    }
    delivery = allocate(request, out / "delivery")
    prompt = ("You are working in a disposable Git worktree of an existing local project.\n"
              "Change requirement:\n" + spec["goal"].strip() + "\n\n"
              "Allowed paths (only these may change):\n" +
              "".join("- " + item + "\n" for item in spec["allowed_paths"]) +
              "\nDo not run git. Do not run the project's tests. Do not create, rename or "
              "delete any file outside the allowed paths. When finished, reply with one "
              "short line.")
    message = json.dumps({
        "task_id": delivery["task_id"], "nonce": delivery["nonce"], "cwd": delivery["cwd"],
        "prompt": prompt, "marker": delivery["completion"]["marker"],
        "execution_cwd": str(workspace_path), "sandbox": "workspace-write",
        "authorization": {"source": spec["authorization"]["source"],
                          "terminate_on_timeout": True}}, ensure_ascii=True)

    derived = {
        "created_at": monitor_task.utc_now(),
        "work_root": str(out),
        "source": {"path": str(repository), "realpath": str(Path(repository).resolve()),
                   "head": head, "branch": source_branch, "remote": remote,
                   "dirty_preserved": bool(dirty), "repository_id": repository_id},
        "base": base, "target_ref": target, "project_id": project_id, "epoch": epoch,
        "task_id": task_id, "attempt_id": delivery["task_id"], "nonce": delivery["nonce"],
        "controller": {"instance_id": controller["instance_id"], "version": controller["version"],
                       "agent_id": agents["controller"]},
        "worker": {"instance_id": worker["instance_id"], "version": worker["version"],
                   "agent_id": agents["worker"]},
        "reviewer": {"instance_id": reviewer["instance_id"], "version": reviewer["version"],
                     "agent_id": agents["reviewer"]},
        "worker_spec": worker_spec, "reviewer_spec": reviewer_spec,
        "controller_spec": controller_spec,
        "request": delivery, "message": message,
        "message_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "paths": {"root": str(out), "state": str(state), "repo": str(clone),
                  "workspace": str(workspace_path), "delivery": delivery["cwd"],
                  "evidence": str(out / "evidence"), "notes": str(out / "notes"),
                  "quiescence": str(out / "quiescence"), "review_runs": str(out / "review-runs"),
                  "merge_ops": str(out / "merge-ops"),
                  "merge_archive": str(out / "merge-archive"),
                  "worker": str(out / "worker"), "reviewer": str(out / "reviewer"),
                  "controller": str(out / "controller")},
        "tokens": {"controller": str(out / "secrets" / "controller.token"),
                   "merge": str(out / "secrets" / "merge.token"),
                   "worker": str(out / "secrets" / "worker.token"),
                   "reviewer": str(out / "secrets" / "reviewer.token"),
                   "merge_reviewer": str(out / "secrets" / "merge-reviewer.token")},
        "verify_argv": spec["verify_argv"], "pins": spec["_pins"],
        "budget": spec["budget"], "allowed_paths": spec["allowed_paths"],
        "authorization": spec["authorization"], "max_real_turns": spec["max_real_turns"],
        "timeout_seconds": spec["timeout_seconds"], "goal": spec["goal"],
        "change_name": spec["change_name"],
    }
    plan = {"kind": "awo-project-entry-plan", "version": PLAN_VERSION, "spec": spec,
            "derived": derived}
    plan["plan_sha256"] = plan_hash(plan)
    save_json(out / "plan.json", plan)
    write_new(out / "plan.sha256", plan["plan_sha256"] + "\n")
    write_new(out / "PLAN.md", render_plan(plan))
    return plan


def render_plan(plan):
    d, spec = plan["derived"], plan["spec"]
    return ("# Prepared plan: " + d["change_name"] + "\n\n"
            "- Plan SHA-256: `" + plan["plan_sha256"] + "`\n"
            "- Repository: `" + d["source"]["path"] + "`\n"
            "- Remote: `" + (d["source"]["remote"] or "(none)") + "`\n"
            "- Base commit: `" + d["base"] + "`\n"
            "- Disposable clone: `" + d["paths"]["repo"] + "`\n"
            "- Target ref: `" + d["target_ref"] + "` (created only by `run`)\n"
            "- Workspace: `" + d["paths"]["workspace"] + "`\n"
            "- Allowed paths: " + ", ".join("`" + x + "`" for x in spec["allowed_paths"]) + "\n"
            "- Verify argv: `" + json.dumps(spec["verify_argv"]) + "` (shell=False)\n"
            "- Budget: `" + json.dumps(spec["budget"], sort_keys=True) + "`\n"
            "- Max real turns: " + str(spec["max_real_turns"]) + "\n"
            "- Authorization: " + spec["authorization"]["source"] + "\n"
            "- Local landing: " + ("authorized" if spec["authorization"]["allow_local_land"]
                                   else "not authorized") + "\n"
            "- Worker binding: `" + d["worker"]["instance_id"] + "`\n"
            "- Reviewer binding: `" + d["reviewer"]["instance_id"] + "` (independent)\n"
            "\n## Goal\n\n" + spec["goal"].strip() + "\n\n"
            "Preparation sent no model turn and did not create or move the target ref. "
            "Execution requires `run --plan " + d["paths"]["root"] +
            "/plan.json --confirm-execute`.\n")


# ---------------------------------------------------------------------------
# Target and environment checks


def _workspace_identity(plan, journal):
    """Resolve the Assignment and workspace from the ledger, not the cache."""
    d, state = plan["derived"], Path(plan["derived"]["paths"]["state"])
    assignment_id = (journal or {}).get("assignment_id")
    if assignment_id:
        assignment = _one(state, "SELECT * FROM v2_assignment WHERE assignment_id=?",
                          (assignment_id,))
        _require(assignment and assignment["task_id"] == d["task_id"], "environment",
                 "journal Assignment does not belong to this task")
    else:
        assignment = _one(state, "SELECT * FROM v2_assignment WHERE task_id=? "
                                 "ORDER BY generation DESC LIMIT 1", (d["task_id"],))
        assignment_id = assignment["assignment_id"] if assignment else None
    workspace_id = (journal or {}).get("workspace_id")
    if workspace_id:
        workspace = _workspace_row(state, workspace_id)
        _require(workspace.get("assignment_id") == assignment_id, "environment",
                 "journal workspace belongs to another Assignment")
    elif assignment_id:
        workspace_id = assignment["workspace_id"]
        workspace = _workspace_row(state, workspace_id) if workspace_id else None
    else:
        workspace = None
    return assignment, workspace, assignment_id, workspace_id


def _latest_assignment(state, d):
    return _one(state, "SELECT * FROM v2_assignment WHERE task_id=? "
                       "ORDER BY generation DESC LIMIT 1", (d["task_id"],))


def _merge_request_row(state, assignment_id):
    """The ledger's merge request for this Assignment, independent of the cache."""
    if not assignment_id:
        return None
    return _one(state, "SELECT * FROM v2_merge_request WHERE assignment_id=? "
                       "ORDER BY created_at DESC, merge_request_id DESC LIMIT 1",
                (assignment_id,))


def validate_credentials(plan, state):
    d = plan["derived"]
    roles = {"controller": ("controller", "controller"), "worker": ("worker", "worker"),
             "reviewer": ("reviewer", "reviewer"), "merge": ("controller", "controller"),
             "merge_reviewer": ("reviewer", "reviewer")}
    for name, (role, owner) in roles.items():
        path = Path(d["tokens"][name])
        _require(path.is_file(), "environment", "missing token file: " + name)
        secret = path.read_text(encoding="utf-8").strip()
        _require(secret, "environment", "empty token file: " + name)
        row = _one(state, "SELECT role,agent_id,project_id,revoked_at FROM v2_access_token "
                          "WHERE secret_hash=?", (service._secret_hash(secret),))
        _require(row and row["role"] == role and row["project_id"] == d["project_id"]
                 and row["agent_id"] == d[owner]["agent_id"] and not row["revoked_at"],
                 "environment", "token file does not match the frozen credential: " + name)


def validate_bindings(plan, state):
    d = plan["derived"]
    roles = {"controller": ("planner", "controller"), "worker": ("executor", "worker"),
             "reviewer": ("reviewer", "reviewer")}
    for key, (role, mode) in roles.items():
        row = _one(state, "SELECT data FROM bindings WHERE id=? AND version=? AND status='active'",
                   (d[key]["instance_id"], d[key]["version"]))
        _require(row, "environment", key + " binding is no longer active")
        value = json.loads(row["data"])
        _require(value.get("role") == role and value.get("mode") == mode
                 and value.get("version") == d[key]["version"], "environment",
                 key + " binding identity changed")


def verify_environment(plan, journal=None):
    d = plan["derived"]
    validate_pins(plan)
    state = Path(d["paths"]["state"])
    source = Path(d["source"]["realpath"])
    _require(source.is_dir(), "environment", "source repository disappeared")
    head = git_opt(source, "rev-parse", "--verify", "HEAD")
    _require(head and head.lower() == d["base"], "environment",
             "source HEAD moved since preparation")
    remote = git_opt(source, "config", "--get", "remote.origin.url") or ""
    _require(remote == d["source"]["remote"], "environment", "source remote changed since preparation")
    repository_id = "awo-" + hashlib.sha256(
        (str(source) + "\n" + remote + "\n" + d["base"]).encode("utf-8")).hexdigest()[:20]
    _require(repository_id == d["source"]["repository_id"], "environment",
             "repository identity changed")
    root = Path(d["paths"]["root"])
    _require(root.is_dir(), "environment", "prepared output root is missing")
    _require(same_path(root, d.get("work_root", str(root))), "environment",
             "prepared output root differs from work_root")
    _require(not workspace_lib._inside(root, source)
             and not workspace_lib._inside(source, root), "environment",
             "prepared output root overlaps the source repository")
    clone = Path(d["paths"]["repo"])
    _require(clone.is_dir(), "environment", "disposable clone is missing")
    _require(git_text(clone, "rev-parse", "HEAD").lower() == d["base"], "environment",
             "clone HEAD is not the frozen base")
    _require(git_text(clone, "status", "--porcelain=v1", "--untracked-files=all") == "",
             "environment", "clone is not clean")
    assignment, workspace, _, _ = _workspace_identity(plan, journal)
    workspace_path = Path(d["paths"]["workspace"])
    if workspace is None:
        _require(not workspace_path.exists(), "environment",
                 "frozen worktree path already exists before provisioning")
    else:
        _require(same_path(workspace["path"], workspace_path), "environment",
                 "bound workspace path differs from the frozen path")
        _require(workspace["base_commit"].lower() == d["base"], "environment",
                 "bound workspace base differs from the frozen base")
        if workspace["status"] != "released":
            _require(workspace_path.is_dir(), "environment",
                     "recorded worktree path disappeared")
    _require(git_text(clone, "symbolic-ref", "-q", "HEAD", ok=(0, 1)) == "", "environment",
             "clone must be detached at the frozen base")
    validate_credentials(plan, state)
    validate_bindings(plan, state)


def ensure_target(plan, journal):
    d = plan["derived"]
    state = Path(d["paths"]["state"])
    clone, target, base = Path(d["paths"]["repo"]), d["target_ref"], d["base"]
    _require(git_opt(clone, "symbolic-ref", "-q", target) is None, "target",
             "target ref became symbolic")
    current = git_opt(clone, "rev-parse", "--verify", "--quiet", target)
    if current is None:
        git(clone, "update-ref", target, base)
        return base
    current = current.lower()
    landed = None
    row = _merge_request_row(state, (journal or {}).get("assignment_id"))
    if row and row["state"] == "landed" and row.get("merge_commit"):
        landed = row["merge_commit"].lower()
    if not landed:
        landed = (journal.get("candidate_commit")
                  or (journal.get("candidate") or {}).get("candidate_commit"))
    if current == base or (landed and current == landed):
        return current
    raise EntryError("target", "target ref moved to an unexpected commit", "target_changed")


# ---------------------------------------------------------------------------
# Journal


def load_journal(path):
    return read_json(path) if Path(path).is_file() else None


def save_journal(path, journal):
    save_json(path, journal)


# ---------------------------------------------------------------------------
# Single execution ownership


def _lock_value(path):
    try:
        value = read_json(path)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def acquire_lock(plan):
    """Bounded mutual exclusion for run/recover on one prepared plan."""
    path = Path(plan["derived"]["paths"]["root"]) / "run.lock"
    token = uuid.uuid4().hex
    for _ in range(2):
        try:
            with path.open("x", encoding="ascii") as stream:
                stream.write(json.dumps({"pid": os.getpid(), "token": token,
                                         "created_at": monitor_task.utc_now()},
                                        sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            return path, token
        except FileExistsError:
            owner = _lock_value(path)
            pid = owner.get("pid") if owner else None
            if type(pid) is int and pid > 0 and pid_alive(pid):
                raise EntryError("lock", "another run or recover owns this plan (pid " +
                                 str(pid) + ")", "concurrent") from None
            if owner is None:
                # A crash during creation can leave an unreadable lock. Only a
                # clearly aged file is cleared; anything recent stays blocking.
                try:
                    age = time.time() - path.stat().st_mtime
                except OSError:
                    raise EntryError("lock", "run lock is unreadable", "concurrent") from None
                _require(age > 60, "lock", "run lock is being created or cannot be verified",
                         "concurrent")
            path.unlink(missing_ok=True)
    raise EntryError("lock", "could not acquire the plan execution lock", "concurrent")


def release_lock(path, token):
    value = _lock_value(path)
    if value and value.get("token") == token:
        Path(path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Main execution


def execute_plan(plan_path, *, resume=False, locked=False):
    plan = load_plan(plan_path)
    d = plan["derived"]
    paths = _paths(plan)
    state, actor, epoch = Path(d["paths"]["state"]), _actor(plan), d["epoch"]
    journal_path = paths["root"] / "run.json"
    journal = load_journal(journal_path)
    if journal is not None and not resume:
        raise EntryError("already_run", "this plan already has a run journal; use status/recover",
                         "already_run")
    if journal is None:
        journal = {"plan_sha256": plan["plan_sha256"], "started_at": monitor_task.utc_now(),
                   "phase": "starting", "turns": _turn_count(plan), "events": []}
        write_new(journal_path, json.dumps(journal, indent=2, sort_keys=True) + "\n")
    elif journal.get("plan_sha256") != plan["plan_sha256"]:
        raise EntryError("journal", "run journal belongs to another plan", "journal_mismatch")
    journal.setdefault("events", [])

    def note(name, **values):
        journal["phase"] = name
        journal["updated_at"] = monitor_task.utc_now()
        journal["turns"] = _turn_count(plan)
        journal["events"].append({"phase": name, "at": journal["updated_at"],
                                  **{k: v for k, v in values.items() if k in (
                                      "assignment_id", "workspace_id", "intent_id",
                                      "merge_request_id", "candidate_commit", "event_id",
                                      "status")}})
        save_journal(journal_path, journal)

    lock_path = lock_token = None
    if not locked:
        lock_path, lock_token = acquire_lock(plan)
    try:
        verify_environment(plan, journal)
        ensure_target(plan, journal)

        initial = _latest_assignment(state, d)
        assignment_id = initial["assignment_id"] if initial else None
        merge_row = _merge_request_row(state, assignment_id)
        target_tip = git_opt(paths["repo"], "rev-parse", "--verify", "--quiet", d["target_ref"])
        if merge_row and merge_row["state"] == "landed":
            # A landed merge request is final; never repeat an external effect.
            _require(target_tip and target_tip.lower() == merge_row["merge_commit"].lower(),
                     "landing", "landed merge commit differs from the target ref")
            if initial and initial["workspace_id"]:
                release_workspace(plan, state, epoch, initial["workspace_id"])
            journal["merge_request_id"] = merge_row["merge_request_id"]
            journal["candidate_commit"] = merge_row["merge_commit"]
            journal["status"] = "LANDED"
            journal.setdefault("finished_at", monitor_task.utc_now())
            journal["usage"] = usage(plan)
            note("done", status="LANDED", candidate_commit=merge_row["merge_commit"])
            save_json(paths["evidence"] / "run-result.json", journal)
            return journal
        sealed = _query(state, "SELECT verdict FROM v2_review WHERE assignment_id=?",
                        (assignment_id,)) if assignment_id else []
        if sealed:
            _require(len(sealed) == 1, "code_review",
                     "ambiguous sealed reviews for this Assignment")
            verdict = sealed[0]["verdict"]
            if verdict != "accept":
                journal["status"] = ("CODE_REVIEW_REJECTED" if verdict == "reject"
                                     else "REPAIR_REQUIRED")
                journal["finished_at"] = monitor_task.utc_now()
                note("review_refused", status=verdict)
                raise EntryError("code_review", "the authoritative review verdict is " +
                                 verdict + "; this attempt cannot be promoted", "review_rejected")

        controller_token = _token(plan, "controller")
        worker_token = _token(plan, "worker")
        reviewer_token = _token(plan, "reviewer")
        merge_token = _token(plan, "merge")
        merge_reviewer_token = _token(plan, "merge_reviewer")

        # 1. Enqueue and claim once.
        task = _one(state, "SELECT * FROM v2_task WHERE task_id=?", (d["task_id"],))
        _require(task, "task", "task is missing from the ledger")
        assignment_id = journal.get("assignment_id")
        if not assignment_id:
            open_rows = _query(state, "SELECT assignment_id FROM v2_assignment WHERE task_id=? "
                                      "AND holds_slot=1 ORDER BY generation DESC", (d["task_id"],))
            if open_rows:
                assignment_id = open_rows[0]["assignment_id"]
            else:
                queued = _query(state, "SELECT task_revision AS revision FROM v2_scheduler_queue "
                                       "WHERE task_id=?", (d["task_id"],))
                if not queued:
                    queued_result = command(state, actor, epoch, "scheduler.enqueue", {
                        "task_id": d["task_id"], "expected_revision": task["revision"],
                        "request": d["request"], "message": d["message"],
                        "agent_id": d["worker"]["agent_id"], "budget": d["budget"],
                        "resources": {"native_slot": 1}, "workspace_mode": "git_worktree",
                        "task_kind": "code", "workspace_base_commit": d["base"],
                        "dependencies": []})
                    _require(queued_result.get("status") == "QUEUED", "enqueue",
                             "enqueue failed: " + json.dumps(queued_result)[:500])
                    note("enqueued", status=queued_result["status"])
                    task = _one(state, "SELECT * FROM v2_task WHERE task_id=?", (d["task_id"],))
                else:
                    task = _one(state, "SELECT * FROM v2_task WHERE task_id=?", (d["task_id"],))
                claimed = gateway(state, paths["root"], worker_token, "awo_assignment_claim", api_args(
                    epoch, task_id=d["task_id"], expected_revision=task["revision"]))
                _require(claimed.get("status") == "RESERVED", "claim",
                         "claim failed: " + json.dumps(claimed)[:500])
                assignment_id = claimed["assignment_id"]
            journal["assignment_id"] = assignment_id
            note("claimed", assignment_id=assignment_id, status="RESERVED")
        assignment = _one(state, "SELECT * FROM v2_assignment WHERE assignment_id=?", (assignment_id,))
        _require(assignment, "assignment", "Assignment is missing")
        intent = _one(state, "SELECT * FROM v2_scheduler_intent WHERE assignment_id=?", (assignment_id,))
        _require(intent, "assignment", "scheduler intent is missing")

        # 2. Provision the frozen worktree when this Assignment has none.
        workspace_id = assignment["workspace_id"]
        workspace = None
        if workspace_id:
            workspace = _workspace_row(state, workspace_id)
            _require(Path(workspace["path"]).resolve() == Path(d["paths"]["workspace"]),
                     "workspace", "bound workspace path differs from the frozen path")
            _require(workspace["base_commit"].lower() == d["base"], "workspace",
                     "bound workspace base differs from the frozen base")
        else:
            created = workspace_call(state, actor, epoch, "workspace.create", {
                "assignment_id": assignment_id, "expected_revision": assignment["revision"],
                "kind": "git_worktree", "root": str(paths["root"]),
                "path": d["paths"]["workspace"], "allowed_paths": d["allowed_paths"],
                "repository_id": d["source"]["repository_id"], "repo_root": d["paths"]["repo"],
                "base_commit": d["base"]})
            _require(created.get("status") == "WORKSPACE_READY", "workspace",
                     "workspace creation failed: " + json.dumps(created)[:800])
            workspace_id = created["workspace"]["workspace_id"]
            workspace = _workspace_row(state, workspace_id)
            journal["workspace_id"] = workspace_id
            journal["workspace_branch"] = workspace["branch"]
            note("workspace_ready", assignment_id=assignment_id, workspace_id=workspace_id,
                 status=created["status"])
        journal["workspace_id"] = workspace_id
        journal["workspace_branch"] = workspace["branch"]
        save_journal(journal_path, journal)

        # 3. Dispatch once. An attempted intent is never sent again.
        intent = _one(state, "SELECT * FROM v2_scheduler_intent WHERE assignment_id=?", (assignment_id,))
        if intent["send_state"] == "ready":
            _enforce_turn_budget(plan)
            dispatched = scheduler.dispatch_assignment(state, assignment_id, *actor, expected_epoch=epoch)
            if dispatched.get("status") != "SUBMITTED":
                journal["dispatch_response"] = dispatched
                journal["status"] = "DISPATCH_UNCERTAIN"
                note("dispatch_uncertain", assignment_id=assignment_id,
                     status=str(dispatched.get("status")))
                raise EntryError("dispatch",
                                 "send outcome is uncertain; inspect the persisted attempt with "
                                 "status/recover and do not resend: " + json.dumps(dispatched)[:600],
                                 "dispatch_uncertain")
            journal["dispatched_at"] = monitor_task.utc_now()
            note("dispatched", assignment_id=assignment_id, status=dispatched["status"])
        elif intent["send_state"] not in ("attempted", "submitted", "review_ready", "closed"):
            raise EntryError("dispatch", "unexpected send state: " + str(intent["send_state"]),
                             "send_state")

        # 4. Collect the native turn, verify scope and acceptance argv, then commit.
        workspace_path = Path(d["paths"]["workspace"])
        intent_path = paths["evidence"] / "source-commit-intent.json"
        if not journal.get("source_commit"):
            head = git_opt(workspace_path, "rev-parse", "--verify", "HEAD")
            if head and head.lower() != d["base"]:
                # A crash after the controller commit is adopted only when the
                # commit, the verified change set and the recorded receipt agree.
                journal["source_commit"], adopted = _adopt_controller_commit(
                    plan, workspace_path, intent_path)
                journal["changed_paths"] = adopted
                verify_record = read_json(paths["evidence"] / "verify-1.json")
                journal["verify"] = {k: verify_record.get(k) for k in ("argv", "returncode", "cwd")}
                receipt = read_json(paths["evidence"] / "worker-receipt.json")
                _require(receipt.get("status") == "done" and receipt.get("returncode") == 0
                         and receipt.get("process_status") == "clean_exit"
                         and Path(receipt.get("execution_cwd", "")).resolve() ==
                         workspace_path.resolve(), "worker_turn",
                         "recorded worker receipt is missing or changed")
                journal["receipt"] = {k: receipt.get(k) for k in
                                      ("status", "returncode", "process_status", "cwd",
                                       "execution_cwd")}
                note("collected", assignment_id=assignment_id, status=receipt.get("status"))
                save_journal(journal_path, journal)
            else:
                receipt, job_dir = collect_turn(plan, journal, workspace_path)
                changed = changed_paths(workspace_path)
                _require(changed, "scope", "the worker made no change")
                undeclared = sorted(set(changed) - set(d["allowed_paths"]))
                _require(not undeclared, "scope",
                         "out-of-scope paths changed: " + json.dumps(undeclared))
                verify_result = run_verify(plan, workspace_path)
                save_json(paths["evidence"] / "verify-1.json", verify_result)
                _require(verify_result["returncode"] == 0, "verify",
                         "acceptance argv failed with " + str(verify_result["returncode"]) +
                         ": " + verify_result["stderr"][-1200:])
                git(workspace_path, "add", "-A")
                staged_tree = git_text(workspace_path, "write-tree").lower()
                _require(HEX40.fullmatch(staged_tree), "commit",
                         "staged content has no exact tree object")
                _require(staged_paths(workspace_path, d["base"]) == changed, "commit",
                         "staged content differs from the verified change set")
                if intent_path.exists():
                    previous = read_json(intent_path)
                    _require(previous.get("base") == d["base"]
                             and previous.get("changed_paths") == changed
                             and previous.get("verify_sha256") ==
                             sha_file(paths["evidence"] / "verify-1.json")
                             and previous.get("staged_tree") in (None, staged_tree), "commit",
                             "an earlier commit intent disagrees with this attempt")
                save_json(intent_path, {"base": d["base"], "changed_paths": changed,
                                        "staged_tree": staged_tree,
                                        "verify_sha256": sha_file(
                                            paths["evidence"] / "verify-1.json"),
                                        "receipt_sha256": sha_file(
                                            paths["evidence"] / "worker-receipt.json"),
                                        "created_at": monitor_task.utc_now()})
                commit = commit_change(workspace_path, d["change_name"], staged_tree)
                journal["source_commit"] = commit
                journal["changed_paths"] = changed
                journal["verify"] = {k: verify_result[k] for k in
                                     ("argv", "returncode", "cwd")}
                journal["receipt"] = {k: receipt.get(k) for k in
                                      ("status", "returncode", "process_status", "cwd",
                                       "execution_cwd")}
                note("collected", assignment_id=assignment_id, status=receipt.get("status"))
        source_commit = journal["source_commit"]
        _validate_source_evidence(plan, workspace_path, journal)
        recorded = name_status(workspace_path, d["base"], source_commit)
        changed_now = sorted({path for item in recorded for path in item[1:]})
        _require(changed_now == sorted(journal.get("changed_paths") or changed_now), "scope",
                 "committed change set differs from the verified change set")
        _require(not set(changed_now) - set(d["allowed_paths"]), "scope",
                 "committed change set includes out-of-scope paths")

        # 5. Reconcile the observed native turn into the sealed completion event.
        intent = _one(state, "SELECT * FROM v2_scheduler_intent WHERE assignment_id=?",
                      (assignment_id,))
        if intent["send_state"] != "closed" and not journal.get("event_id"):
            reconciled = scheduler.reconcile_assignment(state, assignment_id, *actor,
                                                        expected_epoch=epoch)
            _require(reconciled.get("status") == "REVIEW_READY", "reconcile",
                     "reconcile failed: " + json.dumps(reconciled)[:500])
            journal["event_id"] = reconciled["event_id"]
            note("review_ready", assignment_id=assignment_id, event_id=reconciled["event_id"])
        event_id = journal.get("event_id") or _find_event(state, d)
        journal["event_id"] = event_id

        # 6. Worker evidence and completion; independent acceptance stays false.
        assignment = _one(state, "SELECT * FROM v2_assignment WHERE assignment_id=?",
                          (assignment_id,))
        evidence_ids = _evidence_ids(state, assignment_id)
        if not evidence_ids:
            _require(assignment["state"] != "closed", "complete",
                     "Assignment closed before its evidence was sealed")
            recorded_evidence = record_delivery(plan, state, worker_token, assignment_id)
            evidence_ids = [item["evidence_id"] for item in recorded_evidence]
            journal["evidence_ids"] = evidence_ids
            save_journal(journal_path, journal)
            assignment = _one(state, "SELECT * FROM v2_assignment WHERE assignment_id=?",
                              (assignment_id,))
        if assignment["holds_slot"]:
            completed = gateway(state, paths["root"], worker_token, "awo_assignment_complete",
                                api_args(
                epoch, assignment_id=assignment_id, generation=assignment["generation"],
                expected_revision=assignment["revision"],
                outcome="completed", evidence_ids=evidence_ids,
                summary="Worker changed the reviewed paths; controller verified and committed."))
            _require(completed.get("status") == "REVIEW_READY"
                     and completed.get("independently_accepted") is False, "complete",
                     "completion failed: " + json.dumps(completed)[:500])

        # 7. Independent code review over the committed change. The ledger
        # `reviews` record is authoritative; a cached journal verdict never is.
        assignment = _one(state, "SELECT * FROM v2_assignment WHERE assignment_id=?",
                          (assignment_id,))
        records = _ledger_reviews(state, event_id)
        if not records:
            cached = journal.get("code_review")
            attempt_file = paths["evidence"] / "code-attempt.json"
            _require(not cached or attempt_file.is_file(), "code_review",
                     "a cached review decision has no durable reviewer receipt; refusing to send "
                     "another review")
            review, attempt = code_review(plan, workspace_path, source_commit)
            verdict = review["verdict"]
            _require(verdict in ("accept", "reject", "repair"), "code_review",
                     "reviewer returned an unsupported verdict: " + str(verdict))
            if cached:
                _require(cached.get("verdict") == verdict, "code_review",
                         "cached decision disagrees with the durable reviewer receipt")
            note_path = _write_review_note(plan, verdict, review.get("reason", ""))
            evidence_ids = journal.get("evidence_ids") or _evidence_ids(state, assignment_id)
            current = _one(state, "SELECT revision FROM v2_assignment WHERE assignment_id=?",
                           (assignment_id,))["revision"]
            submitted = gateway(state, paths["root"], reviewer_token, "awo_review_submit",
                                api_args(
                epoch, assignment_id=assignment_id, expected_revision=current,
                task_id=d["task_id"], event_id=event_id, evidence_ids=evidence_ids,
                verdict=verdict, review_note_path=note_path.resolve().relative_to(
                    Path(d["paths"]["root"])).as_posix(),
                review_note_sha256=sha_file(note_path)))
            _require(submitted.get("status") == "REVIEW_RECORDED", "review_submit",
                     "review submit failed: " + json.dumps(submitted)[:600])
            records = _ledger_reviews(state, event_id)
        _require(len(records) == 1, "code_review",
                 "the completion event has no unambiguous independent review")
        record = validate_ledger_review(plan, state, event_id, assignment, records[0],
                                        source_commit)
        journal["code_review"] = _review_summary(record)
        save_journal(journal_path, journal)
        if record["verdict"] != "accept":
            _close_reviewed(plan, state, epoch, assignment_id, event_id, record["verdict"])
            journal["status"] = ("CODE_REVIEW_REJECTED" if record["verdict"] == "reject"
                                 else "REPAIR_REQUIRED")
            journal["finished_at"] = monitor_task.utc_now()
            note("review_refused", assignment_id=assignment_id, status=record["verdict"])
            raise EntryError("code_review", "independent review recorded " +
                             record["verdict"] + "; this attempt cannot be promoted",
                             "review_rejected")

        # 8. Close accepted and freeze the reviewed workspace.
        assignment = _one(state, "SELECT * FROM v2_assignment WHERE assignment_id=?",
                          (assignment_id,))
        if assignment["state"] != "closed" and assignment["holds_slot"]:
            closed = command(state, actor, epoch, "assignment.close", {
                "assignment_id": assignment_id,
                "expected_revision": assignment["revision"],
                "outcome": "accepted", "event_id": event_id})
            _require(closed.get("status") == "CLOSED", "close",
                     "close failed: " + json.dumps(closed)[:600])
            note("closed", assignment_id=assignment_id, status=closed["status"])
        workspace = _one(state, "SELECT * FROM v2_workspace WHERE workspace_id=?",
                         (workspace_id,))
        if workspace["status"] == "active":
            frozen = workspace_call(state, actor, epoch, "workspace.freeze", {
                "workspace_id": workspace_id, "expected_revision": workspace["revision"],
                "evidence_root": str(Path(d["paths"]["quiescence"]) / "freeze")})
            _require(frozen.get("status") == "WORKSPACE_FROZEN", "freeze",
                     "freeze failed: " + json.dumps(frozen)[:600])
            note("frozen", workspace_id=workspace_id, status=frozen["status"])
        elif workspace["status"] not in ("frozen", "released"):
            raise EntryError("freeze", "workspace is " + workspace["status"], "workspace_state")

        # 9. Local landing through the existing Merge Queue.
        if not d["authorization"]["allow_local_land"]:
            release_workspace(plan, state, epoch, workspace_id)
            journal["status"] = "ACCEPTED_NOT_LANDED"
            journal["finished_at"] = monitor_task.utc_now()
            note("accepted_not_landed", workspace_id=workspace_id)
            return journal
        candidate = merge_and_land(plan, state, epoch, assignment_id, workspace_id,
                                   source_commit, merge_token, merge_reviewer_token, journal,
                                   journal_path, note)

        release_workspace(plan, state, epoch, workspace_id)
        target_tip = git_text(Path(d["paths"]["repo"]), "rev-parse", d["target_ref"]).lower()
        _require(target_tip == candidate["candidate_commit"], "landing",
                 "target tip differs from the reviewed candidate")
        _require(git_text(Path(d["paths"]["repo"]), "status", "--porcelain") == "", "landing",
                 "disposable clone is dirty after landing")
        journal["candidate_commit"] = candidate["candidate_commit"]
        journal["status"] = "LANDED"
        journal["finished_at"] = monitor_task.utc_now()
        journal["usage"] = usage(plan)
        note("done", status="LANDED", candidate_commit=candidate["candidate_commit"])
        save_json(paths["evidence"] / "run-result.json", journal)
        return journal
    finally:
        if lock_token:
            release_lock(lock_path, lock_token)


def _find_event(state, plan):
    rows = _query(state, "SELECT id FROM events WHERE task=? AND nonce=?",
                  (plan["derived"]["attempt_id"], plan["derived"]["nonce"]))
    _require(len(rows) == 1, "reconcile", "completion event is missing or ambiguous")
    return rows[0]["id"]


def _evidence_ids(state, assignment_id):
    return [row["evidence_id"] for row in _query(
        state, "SELECT evidence_id FROM v2_evidence WHERE assignment_id=? ORDER BY evidence_id",
        (assignment_id,))]


def collect_turn(plan, journal, workspace_path):
    d = plan["derived"]
    worker_root = Path(d["paths"]["worker"])
    baseline = 0
    deadline = time.monotonic() + max(120, d["timeout_seconds"] * 2 + 60)
    last = None
    while time.monotonic() < deadline:
        last = native_worker.status(worker_root)
        if last.get("status") in ("failed", "blocked"):
            raise EntryError("worker_turn", "native worker " + last.get("status") + ": " +
                             json.dumps(last)[:800], "worker_failed")
        if last.get("status") == "exited":
            raise EntryError("worker_turn", "native runner exited without a receipt; "
                             "inspect before any resend", "uncertain_send")
        if last.get("status") == "idle" and monitor_task.sequence(
                last.get("completion_seq")) >= baseline + 1:
            break
        time.sleep(0.5)
    else:
        raise EntryError("worker_turn", "native turn still in progress; recover after it ends",
                         "turn_in_progress")
    active = Path(worker_root) / "active.json"
    _require(active.is_file(), "worker_turn", "native bridge has no active record")
    job_dir = Path(read_json(active)["directory"])
    receipt = read_json(job_dir / "receipt" / "status.json")
    _require(receipt.get("status") == "done" and receipt.get("returncode") == 0
             and receipt.get("process_status") == "clean_exit", "worker_turn",
             "native receipt is not a clean completion: " + json.dumps(receipt)[:600])
    _require(Path(receipt["execution_cwd"]).resolve() == workspace_path.resolve(), "worker_turn",
             "receipt execution directory differs from the frozen workspace")
    _require(Path(receipt["cwd"]).resolve() == Path(d["paths"]["delivery"]).resolve(), "worker_turn",
             "receipt delivery directory differs from the frozen delivery")
    job = read_json(job_dir / "job.json")
    _require(Path(job["execution_cwd"]).resolve() == workspace_path.resolve(), "worker_turn",
             "job execution directory differs from the frozen workspace")
    sandbox = job["argv"][job["argv"].index("--sandbox") + 1]
    _require(sandbox == "workspace-write", "worker_turn", "job sandbox is not workspace-write")
    save_json(Path(d["paths"]["evidence"]) / "worker-receipt.json", receipt)
    save_json(Path(d["paths"]["evidence"]) / "worker-job.json",
              {k: job.get(k) for k in ("task_id", "nonce", "cwd", "execution_cwd",
                                       "sandbox", "argv")})
    return receipt, job_dir


def changed_paths(workspace_path):
    """Every path Git reports, including renames, deletions and untracked files."""
    result = set()
    for code, paths in status_paths(workspace_path):
        for item in paths:
            result.add(item.replace("\\", "/"))
    return sorted(result)


def run_verify(plan, workspace_path):
    d = plan["derived"]
    validate_pins(plan)
    result = run_argv(d["verify_argv"], workspace_path, timeout=d["timeout_seconds"])
    return {"argv": d["verify_argv"], "cwd": str(workspace_path),
            "returncode": result["returncode"],
            "stdout": result["stdout"].decode("utf-8", "replace"),
            "stderr": result["stderr"].decode("utf-8", "replace")}


def _validate_retained_evidence(plan, journal):
    """Re-check durable intent, acceptance and receipt bytes without a worktree."""
    d = plan["derived"]
    evidence = Path(d["paths"]["evidence"])
    intent_path = evidence / "source-commit-intent.json"
    verify_path = evidence / "verify-1.json"
    receipt_path = evidence / "worker-receipt.json"
    _require(intent_path.is_file() and verify_path.is_file() and receipt_path.is_file(),
             "worker_turn", "recorded worker evidence is missing")
    intent = read_json(intent_path)
    _require(intent.get("base") == d["base"], "worker_turn", "commit intent base changed")
    _require(intent.get("verify_sha256") == sha_file(verify_path), "worker_turn",
             "recorded acceptance evidence changed after the commit")
    _require(intent.get("receipt_sha256") == sha_file(receipt_path), "worker_turn",
             "recorded worker receipt changed after the commit")
    _require(sorted(intent.get("changed_paths") or []) ==
             sorted(journal.get("changed_paths") or []), "scope",
             "commit intent and recorded change set disagree")
    verify_record = read_json(verify_path)
    _require(verify_record.get("returncode") == 0
             and verify_record.get("argv") == d["verify_argv"], "worker_turn",
             "recorded acceptance is not a pass of the frozen command")
    receipt = read_json(receipt_path)
    _require(receipt.get("status") == "done" and receipt.get("returncode") == 0
             and receipt.get("process_status") == "clean_exit", "worker_turn",
             "recorded worker receipt is not a clean completion")
    return intent


def _validate_source_evidence(plan, workspace_path, journal):
    """Re-check the retained evidence and the exact committed content."""
    d = plan["derived"]
    intent = _validate_retained_evidence(plan, journal)
    source_commit = journal["source_commit"]
    if intent.get("staged_tree"):
        _require(git_text(workspace_path, "rev-parse",
                          source_commit + "^{tree}").lower() ==
                 intent["staged_tree"].lower(), "worker_turn",
                 "committed tree differs from the verified staged content")
    recorded = sorted({path for item in
                       name_status(workspace_path, d["base"], source_commit)
                       for path in item[1:]})
    _require(recorded == sorted(journal.get("changed_paths") or []), "scope",
             "committed change set differs from the recorded change set")


def commit_change(workspace_path, name, expected_tree):
    """Commit the exact index the caller staged and verified; stage nothing new."""
    _require(git_text(workspace_path, "write-tree").lower() == expected_tree.lower(), "commit",
             "staged content changed before the controller commit")
    git(workspace_path, "-c", "user.name=AWO Project Entry",
        "-c", "user.email=awo@example.invalid", "commit", "-m", "entry: " + name)
    commit = git_text(workspace_path, "rev-parse", "HEAD").lower()
    _require(git_text(workspace_path, "rev-parse", "HEAD^{tree}").lower() ==
             expected_tree.lower(), "commit",
             "committed tree differs from the verified staged content")
    _require(git_text(workspace_path, "status", "--porcelain=v1", "--untracked-files=all") == "",
             "commit", "worktree is not clean after the controller commit")
    return commit


def _adopt_controller_commit(plan, workspace_path, intent_path):
    """Adopt a commit only when it is provably the verified controller commit."""
    d = plan["derived"]
    _require(intent_path.is_file(), "commit",
             "a commit exists without a recorded commit intent; refusing to adopt it")
    intent = read_json(intent_path)
    _require(intent.get("base") == d["base"], "commit", "commit intent base differs")
    tree = str(intent.get("staged_tree") or "").lower()
    _require(HEX40.fullmatch(tree), "commit",
             "commit intent has no exact staged tree; refusing to adopt")
    head = git_text(workspace_path, "rev-parse", "HEAD").lower()
    _require(git_text(workspace_path, "rev-parse", "HEAD^").lower() == d["base"], "commit",
             "controller commit does not sit on the frozen base")
    _require(git_text(workspace_path, "log", "-1", "--format=%s") == "entry: " + d["change_name"],
             "commit", "unexpected commit message")
    _require(git_text(workspace_path, "rev-parse", head + "^{tree}").lower() == tree, "commit",
             "committed tree differs from the verified staged content")
    _require(tracked_worktree_clean(workspace_path)
             and git_text(workspace_path, "status", "--porcelain=v1", "--untracked-files=all") == "",
             "commit", "worktree is not clean after the controller commit")
    recorded = name_status(workspace_path, d["base"], head)
    changed = sorted({path for item in recorded for path in item[1:]})
    _require(changed == sorted(intent.get("changed_paths") or []), "commit",
             "committed change set differs from the recorded commit intent")
    verify_path = Path(plan["derived"]["paths"]["evidence"]) / "verify-1.json"
    receipt_path = Path(plan["derived"]["paths"]["evidence"]) / "worker-receipt.json"
    _require(verify_path.is_file() and intent.get("verify_sha256") == sha_file(verify_path),
             "commit", "recorded acceptance evidence is missing or changed")
    _require(receipt_path.is_file() and intent.get("receipt_sha256") == sha_file(receipt_path),
             "commit", "recorded worker receipt is missing or changed")
    return head, changed


def record_delivery(plan, state, worker_token, assignment_id):
    d = plan["derived"]
    recorded = []
    current = _one(state, "SELECT revision FROM v2_assignment WHERE assignment_id=?",
                   (assignment_id,))["revision"]
    for name in ("RESULT.md", "HANDOFF.md"):
        raw = (Path(d["paths"]["delivery"]) / name).read_bytes()
        value = gateway(state, plan["derived"]["paths"]["root"], worker_token,
                        "awo_evidence_record", api_args(
            d["epoch"], assignment_id=assignment_id, expected_revision=current,
            kind="artifact", path=name, sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw)))
        _require(value.get("status") == "EVIDENCE_RECORDED", "evidence",
                 "evidence record failed: " + json.dumps(value)[:500])
        recorded.append(value)
        current = value["revision"]
    return recorded


def _review_attempt_path(plan, label):
    return Path(plan["derived"]["paths"]["evidence"]) / (label + "-attempt.json")


def _review_out_dir(plan, label):
    return Path(plan["derived"]["paths"]["review_runs"]) / (label + "-out")


def _review_job(plan, attempt):
    return (Path(plan["derived"]["paths"]["reviewer"]) / "jobs" /
            task_key({"task_id": attempt["task_id"], "nonce": attempt["nonce"]}))


def _record_decision(plan, label, value, attempt):
    """Persist the raw decision with the bridge receipt hashes, before any use."""
    d = plan["derived"]
    record = {"label": label, "plan_sha256": plan["plan_sha256"],
              "task_id": attempt["task_id"], "nonce": attempt["nonce"],
              "prompt_sha256": attempt["prompt_sha256"], "verdict": value.get("verdict"),
              "value": value,
              "result_sha256": sha_file(_review_out_dir(plan, label) / "RESULT.md"),
              "recorded_at": monitor_task.utc_now()}
    save_json(Path(d["paths"]["evidence"]) / (label + "-decision.json"), record)
    return record


def reviewer_turn(plan, label, prompt, files, worktree=None, sandbox=None):
    """Run or reconcile exactly one durable independent-reviewer attempt.

    The attempt record is written before the send. On any restart the recorded
    bridge job is reconciled; another send can never happen.
    """
    d = plan["derived"]
    reviewer_root = Path(d["paths"]["reviewer"])
    out_dir = _review_out_dir(plan, label)
    attempt_path = _review_attempt_path(plan, label)
    attempt = load_journal(attempt_path)
    if attempt is None:
        _require(not out_dir.exists(), label,
                 "review output directory exists without an attempt record; refusing to reuse it")
        out_dir.mkdir(parents=True)
        for name, content in files.items():
            (out_dir / name).write_bytes(content)
        _enforce_turn_budget(plan)
        attempt = {
            "label": label, "plan_sha256": plan["plan_sha256"],
            "task_id": "REVIEW-" + label, "nonce": uuid.uuid4().hex,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "out_dir": str(out_dir), "worktree": str(worktree) if worktree else None,
            "files": sorted((name, hashlib.sha256(content).hexdigest())
                            for name, content in files.items()),
            "baseline": native_worker.status(reviewer_root).get("completion_seq"),
            "created_at": monitor_task.utc_now()}
        save_json(attempt_path, attempt)
        message = {"task_id": attempt["task_id"], "nonce": attempt["nonce"],
                   "cwd": str(out_dir), "prompt": prompt,
                   "authorization": {"source": "independent reviewer turn for the frozen entry plan",
                                     "terminate_on_timeout": True}}
        if worktree is not None:
            message["sandbox"] = sandbox or "workspace-write"
            message["execution_cwd"] = str(worktree)
        sent = native_worker.send(reviewer_root, message)
        attempt["send_response"] = sent
        save_json(attempt_path, attempt)
    else:
        _require(attempt.get("label") == label
                 and attempt.get("plan_sha256") == plan["plan_sha256"], label,
                 "recorded reviewer attempt belongs to another plan")
        _require(attempt.get("prompt_sha256") ==
                 hashlib.sha256(prompt.encode("utf-8")).hexdigest(), label,
                 "review prompt changed; refusing to reuse or resend the attempt")
        _require(same_path(attempt["out_dir"], out_dir), label,
                 "review output directory changed")
        _require((attempt.get("worktree") is None) == (worktree is None)
                 and (worktree is None or same_path(attempt["worktree"], worktree)), label,
                 "review execution directory changed")
        for name, digest in attempt.get("files") or []:
            path = out_dir / name
            _require(path.is_file() and sha_file(path) == digest, label,
                     "review input file changed after the attempt was recorded: " + name)
    value = _await_review_result(plan, label, attempt)
    return value


def _await_review_result(plan, label, attempt):
    """Reconcile the recorded bridge job; never send another turn."""
    d = plan["derived"]
    reviewer_root = Path(d["paths"]["reviewer"])
    out_dir = Path(attempt["out_dir"])
    job = _review_job(plan, attempt)
    _require(job.is_dir(), label,
             "recorded review attempt has no bridge job; refusing to resend", "review_uncertain")
    deadline = time.monotonic() + max(120, d["timeout_seconds"] * 2 + 60)
    while True:
        current = native_worker.status(reviewer_root)
        if current.get("status") == "working":
            _require(current.get("task_id") == attempt["task_id"], label,
                     "a different reviewer turn is active", "review_uncertain")
            _require(time.monotonic() < deadline, label,
                     "recorded reviewer turn is still in progress; recover after it ends",
                     "turn_in_progress")
            time.sleep(0.5)
            continue
        break
    outcome_path = job / "outcome.json"
    _require(outcome_path.is_file(), label,
             "recorded reviewer attempt has no outcome (runner died); bounded stop, no resend",
             "review_uncertain")
    outcome = read_json(outcome_path)
    _require(outcome.get("status") == "done", label,
             "reviewer turn did not complete cleanly: " + json.dumps(outcome)[:400],
             "review_uncertain")
    output = job / "receipt" / "output.log"
    _require(output.is_file(), label, "reviewer output log is missing", "review_uncertain")
    turn = native_events.inspect_turn("codex", output.read_bytes())
    _require(turn.get("status") == "completed", label,
             "reviewer output has no completed native turn", "review_uncertain")
    result_path = out_dir / "RESULT.md"
    _require(result_path.is_file(), label, "reviewer result file is missing", "review_uncertain")
    raw = result_path.read_text(encoding="utf-8")
    _require(raw == turn.get("text", "") + "\n", label,
             "reviewer result bytes differ from the bridge output", "review_uncertain")
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise EntryError(label, "reviewer output is not JSON: " + str(exc), "review_parse")
    save_json(Path(d["paths"]["evidence"]) / (label + "-result.json"), {
        "label": label, "task_id": attempt["task_id"], "nonce": attempt["nonce"],
        "job": str(job), "out_dir": str(out_dir), "prompt_sha256": attempt["prompt_sha256"],
        "result_sha256": sha_file(result_path), "output_sha256": sha_file(output),
        "value": value, "observed_at": monitor_task.utc_now()})
    return value


def code_review(plan, workspace_path, source_commit):
    d = plan["derived"]
    changed = name_status(workspace_path, d["base"], source_commit)
    paths = sorted({path for item in changed for path in item[1:]})
    files = {}
    for name in paths:
        path = workspace_path / name
        if path.is_file():
            files[name.replace("/", "__")] = path.read_bytes()
    patch = git(workspace_path, "diff", "--binary", d["base"], source_commit, "--")["stdout"]
    verify = read_json(Path(d["paths"]["evidence"]) / "verify-1.json")
    files["request.md"] = ("Requirement:\n" + d["goal"].strip() + "\n\nAllowed paths: " +
                           ", ".join(d["allowed_paths"]) + "\n").encode("utf-8")
    files["patch.diff"] = patch
    files["tests.json"] = json.dumps({
        "base_commit": d["base"], "source_commit": source_commit,
        "changed": changed, "verify_argv": verify["argv"],
        "verify_returncode": verify["returncode"], "verify_stdout": verify["stdout"],
        "verify_stderr": verify["stderr"]}, indent=2).encode("utf-8")
    prompt = (
        "Review a small code change independently. Read every file in your current directory "
        "before deciding: request.md, the changed source files, patch.diff and tests.json.\n"
        "Task: " + d["attempt_id"] + "\nNonce: " + d["nonce"] + "\n"
        "Base commit: " + d["base"] + "\nCommitted source commit: " + source_commit + "\n"
        "Requirement:\n" + d["goal"].strip() + "\n"
        "Changed paths: " + json.dumps(changed) + "\n"
        "Acceptance argv: " + json.dumps(verify["argv"]) +
        " returned " + str(verify["returncode"]) + ".\n"
        "Return JSON only with exactly the keys task_id, nonce, verdict (accept or reject), "
        "reason. Accept only if the requirement is met by the actual changed code and the "
        "recorded acceptance result is credible. Do not modify anything.")
    value = reviewer_turn(plan, "code", prompt, files)
    _require(isinstance(value, dict) and value.get("task_id") == d["attempt_id"]
             and value.get("nonce") == d["nonce"]
             and value.get("verdict") in ("accept", "reject", "repair"), "code_review",
             "reviewer output lacks the exact task/nonce/verdict: " + json.dumps(value)[:500])
    attempt = read_json(_review_attempt_path(plan, "code"))
    _record_decision(plan, "code", value, attempt)
    return value, attempt


def _ledger_reviews(state, event_id):
    return [json.loads(row["data"]) for row in
            _query(state, "SELECT data FROM reviews WHERE event=?", (event_id,))]


def _review_summary(record):
    return {key: record.get(key) for key in
            ("event_id", "task_id", "nonce", "verdict", "path", "sha256", "reviewed_at")}


def _write_review_note(plan, verdict, reason):
    d = plan["derived"]
    path = Path(d["request"]["review_path"])
    text = (d["attempt_id"] + " " + d["nonce"] + "\nVerdict: " + verdict + "\n" +
            "Independent reviewer: " + str(reason) + "\n")
    save_bytes(path, text.encode("utf-8"))
    return path


def validate_ledger_review(plan, state, event_id, assignment, record, source_commit):
    """Every acceptance must match the actual reviewer, attempt and reviewed code."""
    d = plan["derived"]
    _require(isinstance(record, dict), "code_review", "review record is not an object")
    _require(record.get("event_id") == event_id and record.get("task_id") == d["attempt_id"]
             and record.get("nonce") == d["nonce"], "code_review",
             "recorded review is not bound to this attempt")
    _require(record.get("verdict") in ("accept", "reject", "repair"), "code_review",
             "recorded review verdict is unsupported")
    actor = record.get("actor")
    _require(isinstance(actor, dict) and actor.get("role") == "reviewer"
             and actor.get("instance_id") == d["reviewer"]["instance_id"]
             and actor.get("version") == d["reviewer"]["version"], "code_review",
             "recorded review was not written by the frozen independent reviewer")
    note = Path(str(record.get("path", ""))).resolve()
    frozen = Path(d["request"]["review_path"]).resolve()
    _require(note == frozen, "code_review", "review note path differs from the frozen request")
    _require(note.is_file() and sha_file(note) == record.get("sha256"), "code_review",
             "review note bytes changed or disappeared")
    lines = note.read_text(encoding="utf-8").splitlines()
    _require(bool(lines) and d["attempt_id"] in lines[0] and d["nonce"] in lines[0],
             "code_review", "review note is not bound to this attempt")
    _require(any(line.strip() == "Verdict: " + record["verdict"] for line in lines),
             "code_review", "review note verdict differs from the ledger")
    mapping = _one(state, "SELECT v2_task_id,v2_assignment_id FROM v2_legacy_map "
                          "WHERE legacy_task_id=?", (d["attempt_id"],))
    _require(mapping and mapping["v2_task_id"] == d["task_id"]
             and mapping["v2_assignment_id"] == assignment["assignment_id"], "code_review",
             "review event does not map to this Assignment")
    sealed = _one(state, "SELECT * FROM v2_workspace_review WHERE assignment_id=?",
                  (assignment["assignment_id"],))
    _require(sealed and sealed["workspace_id"] == assignment["workspace_id"]
             and sealed["verdict"] == record["verdict"], "code_review",
             "recorded review has no matching sealed workspace inventory")
    inventory = json.loads(sealed["inventory_json"])
    _require(workspace_lib._digest(inventory) == sealed["inventory_sha256"], "code_review",
             "sealed workspace inventory hash changed")
    _require(str(inventory.get("head", "")).lower() == str(source_commit).lower(),
             "code_review", "reviewed inventory differs from the committed change")
    _require((record.get("workspace_inventory") or {}).get("inventory_sha256")
             == sealed["inventory_sha256"], "code_review",
             "review record inventory hash differs from the sealed inventory")
    if record["verdict"] == "accept":
        _require(inventory.get("scope_ok") is True, "code_review",
                 "an accepted review cannot cover an out-of-scope workspace")
    return record


def _close_reviewed(plan, state, epoch, assignment_id, event_id, verdict):
    outcome = {"accept": "accepted", "reject": "rejected",
               "repair": "repair_required"}[verdict]
    assignment = _one(state, "SELECT * FROM v2_assignment WHERE assignment_id=?", (assignment_id,))
    intent = _one(state, "SELECT send_state FROM v2_scheduler_intent WHERE assignment_id=?",
                  (assignment_id,))
    if assignment["state"] == "closed" or (intent and intent["send_state"] == "closed"):
        sealed = _one(state, "SELECT verdict FROM v2_review WHERE assignment_id=?",
                      (assignment_id,))
        _require(sealed and sealed["verdict"] == verdict, "close",
                 "Assignment closed without the recorded review verdict")
        return
    _require(assignment["holds_slot"], "close", "Assignment holds no slot to close")
    closed = command(state, _actor(plan), epoch, "assignment.close", {
        "assignment_id": assignment_id, "expected_revision": assignment["revision"],
        "outcome": outcome, "event_id": event_id})
    _require(closed.get("status") == "CLOSED", "close",
             "closing with the recorded verdict failed: " + json.dumps(closed)[:600])


def _validate_merge_candidate(state, merge_request_id):
    """Re-check a stored candidate's digest and sealed evidence before use."""
    row = _merge_row(state, merge_request_id)
    if not row.get("candidate_json"):
        return None
    candidate = json.loads(row["candidate_json"])
    _require(merge_queue._digest(candidate) == row["candidate_sha256"], "merge",
             "stored merge candidate hash changed")
    for key, digest_key in (("manifest_path", "manifest_sha256"),
                            ("verify_log_path", "verify_log_sha256")):
        path = Path(str(candidate.get(key, "")))
        _require(path.is_file() and sha_file(path) == candidate.get(digest_key), "merge",
                 "candidate " + key + " changed or disappeared")
    return candidate


def _ensure_review_worktree(clone, path, commit):
    if path.exists():
        head = git_opt(path, "rev-parse", "--verify", "HEAD")
        _require(head and head.lower() == commit.lower(), "integration_review",
                 "existing integration review worktree is not at the candidate")
        _require(git_text(path, "status", "--porcelain=v1", "--untracked-files=all") == "",
                 "integration_review",
                 "existing integration review worktree has uncommitted changes")
        return
    git(clone, "worktree", "add", "--detach", str(path), commit)


def _integration_setting(plan, state, assignment_id, candidate):
    d = plan["derived"]
    clone = Path(d["paths"]["repo"])
    changed = name_status(clone, d["base"], candidate["candidate_commit"])
    verify_record = read_json(candidate["verify_log_path"])
    generation = _one(state, "SELECT generation FROM v2_assignment WHERE assignment_id=?",
                      (assignment_id,))["generation"]
    binding = {"candidate_sha256": merge_queue._digest(candidate), "task_id": d["task_id"],
               "assignment_id": assignment_id, "generation": generation,
               "reviewer_agent_id": d["reviewer"]["agent_id"]}
    prompt = (
        "Your current directory is a real detached Git worktree at the candidate commit. "
        "Read the files and use read-only git commands to verify the candidate; do not "
        "modify tracked files.\n"
        "Task: " + d["task_id"] + "\nAssignment: " + assignment_id + "\nGeneration: " +
        str(generation) + "\nCandidate commit: " + candidate["candidate_commit"] +
        "\nBase commit: " + candidate["stack_base"] + "\nActual changed paths: " +
        json.dumps(changed) + "\nCandidate SHA-256: " + binding["candidate_sha256"] + "\n"
        "Verify with git diff --name-status " + candidate["stack_base"] + " " +
        candidate["candidate_commit"] + ", git rev-parse HEAD, reading the code, and the "
        "recorded acceptance command. The Merge Queue already ran the frozen acceptance "
        "command in this exact candidate and recorded: " + json.dumps(verify_record,
                                                                    sort_keys=True) + "\n"
        "You may re-run the acceptance command listed above in this worktree; do not "
        "modify tracked files.\n"
        "Return JSON only with exactly the keys verdict (accept or reject), reason, "
        "candidate_sha256, task_id, assignment_id, generation, reviewer_agent_id. Copy "
        "these values exactly as given here: " + json.dumps(binding, sort_keys=True) +
        ". Do not modify anything.")
    return binding, prompt


def _validate_integration_review(plan, state, candidate, review):
    d = plan["derived"]
    _require(isinstance(review, dict), "integration_review", "integration review is missing")
    _require(isinstance(review.get("note_path"), str)
             and isinstance(review.get("note_sha256"), str), "integration_review",
             "integration review note is missing")
    note = Path(review["note_path"])
    _require(note.is_file() and sha_file(note) == review["note_sha256"], "integration_review",
             "integration review note bytes changed or disappeared")
    value = read_json(note)
    generation = _one(state, "SELECT generation FROM v2_assignment WHERE assignment_id=?",
                      (candidate["assignment_id"],))["generation"]
    expected = {"candidate_sha256": merge_queue._digest(candidate), "verdict": "accept",
                "task_id": candidate["task_id"],
                "assignment_id": candidate["assignment_id"], "generation": generation,
                "reviewer_agent_id": d["reviewer"]["agent_id"]}
    _require(isinstance(value, dict)
             and all(value.get(k) == v for k, v in expected.items()), "integration_review",
             "recorded integration review differs from the candidate or reviewer")
    return value


def merge_and_land(plan, state, epoch, assignment_id, workspace_id, source_commit,
                   merge_token, merge_reviewer_token, journal, journal_path, note):
    d = plan["derived"]
    validate_pins(plan)
    clone = Path(d["paths"]["repo"])
    target = d["target_ref"]
    tip = git_opt(clone, "rev-parse", "--verify", "--quiet", target)
    row = _merge_request_row(state, assignment_id)
    if row and row["state"] == "landed":
        _require(tip and tip.lower() == row["merge_commit"].lower(), "landing",
                 "landed merge request differs from the target ref")
        journal["merge_request_id"] = row["merge_request_id"]
        return {"candidate_commit": row["merge_commit"]}

    if row is None:
        policy = merge_call(state, clone, merge_token, "merge.policy", {
            "repository_id": d["source"]["repository_id"], "repo_root": str(clone),
            "operation_root": d["paths"]["merge_ops"],
            "archive_root": d["paths"]["merge_archive"],
            "verify_argv": d["verify_argv"], "pins": d["pins"],
            "timeout": d["timeout_seconds"]}, epoch)
        task = _one(state, "SELECT * FROM v2_task WHERE task_id=?", (d["task_id"],))
        generation = _one(state, "SELECT generation FROM v2_assignment WHERE assignment_id=?",
                          (assignment_id,))["generation"]
        proposed = merge_call(state, clone, merge_token, "merge.propose", {
            "task_id": d["task_id"], "assignment_id": assignment_id,
            "generation": generation, "workspace_id": workspace_id,
            "policy_id": policy["policy_id"], "target_ref": target,
            "expected_revision": task["revision"]}, epoch)
        journal["merge_request_id"] = proposed["merge_request_id"]
        journal["policy_id"] = policy["policy_id"]
        save_journal(journal_path, journal)
        row = _merge_row(state, proposed["merge_request_id"])
    else:
        journal["merge_request_id"] = row["merge_request_id"]
        journal.setdefault("policy_id", row["policy_id"])
    merge_request_id = row["merge_request_id"]
    _require(row["state"] != "cancelled", "integration_review",
             "the merge request was cancelled after a recorded rejection", "review_rejected")

    if row["state"] == "proposed":
        merge_call(state, clone, merge_token, "merge.enqueue", {
            "merge_request_id": merge_request_id,
            "expected_revision": row["revision"]}, epoch)
        row = _merge_row(state, merge_request_id)
    if row["state"] == "queued":
        pending = _one(state, "SELECT intent_id FROM v2_merge_intent WHERE merge_request_id=? "
                              "AND kind='build' AND state IN ('ready','started','reported') "
                              "ORDER BY created_at DESC LIMIT 1", (merge_request_id,))
        _require(not pending, "merge",
                 "a build intent is pending; run recover --confirm-execute", "merge_pending")
        started = merge_call(state, clone, merge_token, "merge.begin", {
            "merge_request_id": merge_request_id,
            "expected_revision": row["revision"], "kind": "build"}, epoch)
        report = merge_queue.execute_intent(_db(Path(d["paths"]["state"])),
                                            started["intent_id"],
                                            _intent_token(state, started["intent_id"]))
        save_json(Path(d["paths"]["evidence"]) / "build-report.json", report)
        _require(report.get("outcome") == "verified", "merge_build",
                 "candidate build failed: " + json.dumps(report)[:800])
        finished = merge_call(state, clone, merge_token, "merge.finish", {
            "intent_id": started["intent_id"],
            "expected_revision": _intent_revision(state, started["intent_id"])}, epoch)
        _require(finished.get("state") == "verifying", "merge_build",
                 "candidate did not enter independent review")
        row = _merge_row(state, merge_request_id)
    if row["state"] in ("rebasing", "merging"):
        pending = _one(state, "SELECT intent_id,state FROM v2_merge_intent "
                              "WHERE merge_request_id=? AND state IN ('reported','started') "
                              "ORDER BY created_at DESC LIMIT 1", (merge_request_id,))
        _require(pending and pending["state"] == "reported", "merge",
                 "a merge intent is in flight; run recover --confirm-execute", "merge_pending")
        merge_call(state, clone, merge_token, "merge.finish", {
            "intent_id": pending["intent_id"],
            "expected_revision": _intent_revision(state, pending["intent_id"])}, epoch)
        row = _merge_row(state, merge_request_id)
    _require(row["state"] in ("verifying", "landed"), "merge",
             "merge request is " + row["state"] + ": " + str(row.get("error")))
    if row["state"] == "landed":
        journal["merge_request_id"] = merge_request_id
        return {"candidate_commit": row["merge_commit"]}
    candidate = _validate_merge_candidate(state, merge_request_id)
    _require(candidate, "merge", "verified merge request has no candidate")
    journal["candidate"] = candidate
    save_journal(journal_path, journal)
    note("candidate", merge_request_id=merge_request_id,
         candidate_commit=candidate["candidate_commit"], status=row["state"])

    # The candidate must reproduce the exact accepted and reviewed change.
    _require(str(candidate["stack_base"]).lower() == d["base"], "integration_review",
             "candidate stack base differs from the frozen base")
    _require(str(candidate["expected_old_tip"]).lower() == d["base"], "integration_review",
             "candidate expected old tip differs from the frozen base")
    source_diff = name_status(clone, d["base"], source_commit)
    candidate_diff = name_status(clone, d["base"], candidate["candidate_commit"])
    _require(candidate_diff == source_diff, "integration_review",
             "candidate change set differs from the accepted change set")
    changed = sorted({path for item in candidate_diff for path in item[1:]})
    undeclared = sorted(set(changed) - set(d["allowed_paths"]))
    _require(not undeclared, "integration_review",
             "candidate changes undeclared paths: " + json.dumps(undeclared))
    _require(git_text(clone, "rev-parse", source_commit + "^{tree}") ==
             git_text(clone, "rev-parse", candidate["candidate_commit"] + "^{tree}"),
             "integration_review", "candidate tree differs from the accepted source tree")
    verify_path = candidate.get("verify_log_path")
    _require(isinstance(verify_path, str) and Path(verify_path).is_file()
             and candidate.get("verify_log_sha256") == sha_file(verify_path),
             "integration_review", "recorded acceptance evidence is missing or changed")
    verify_record = read_json(verify_path)
    _require(verify_record.get("returncode") == 0
             and verify_record.get("candidate_commit") == candidate["candidate_commit"],
             "integration_review", "recorded acceptance result is not this candidate")

    if row.get("review_json"):
        _validate_integration_review(plan, state, candidate, json.loads(row["review_json"]))
        journal["integration_verdict"] = "accept"
        save_journal(journal_path, journal)
    else:
        review_tree = Path(d["paths"]["review_runs"]) / "integration"
        _ensure_review_worktree(clone, review_tree, candidate["candidate_commit"])
        binding, prompt = _integration_setting(plan, state, assignment_id, candidate)
        value = reviewer_turn(plan, "integration", prompt, {}, worktree=review_tree,
                              sandbox="workspace-write")
        attempt = read_json(_review_attempt_path(plan, "integration"))
        _record_decision(plan, "integration", value, attempt)
        _require(isinstance(value, dict)
                 and all(value.get(key) == item for key, item in binding.items())
                 and value.get("verdict") in ("accept", "reject"), "integration_review",
                 "integration output is not bound to the candidate: " +
                 json.dumps(value)[:600])
        _require(git_text(review_tree, "rev-parse", "HEAD").lower() ==
                 candidate["candidate_commit"].lower()
                 and git_text(review_tree, "status", "--porcelain=v1",
                              "--untracked-files=all") == "", "integration_review",
                 "reviewed worktree no longer matches the candidate")
        journal["integration_verdict"] = value["verdict"]
        journal["integration_review"] = dict(binding, verdict=value["verdict"],
                                             reason=str(value.get("reason", "")))
        save_journal(journal_path, journal)
        if value["verdict"] != "accept":
            current = _merge_row(state, merge_request_id)
            merge_call(state, clone, merge_token, "merge.cancel", {
                "merge_request_id": merge_request_id,
                "expected_revision": current["revision"]}, epoch)
            journal["status"] = "INTEGRATION_REJECTED"
            journal["finished_at"] = monitor_task.utc_now()
            note("integration_rejected", merge_request_id=merge_request_id, status="reject")
            raise EntryError("integration_review",
                             "independent integration review rejected the candidate",
                             "review_rejected")
        merge_note = _merge_note(d, journal["integration_review"], merge_request_id)
        merge_call(state, clone, merge_reviewer_token, "merge.review", {
            "merge_request_id": merge_request_id,
            "expected_revision": _merge_revision(state, merge_request_id),
            "note_path": str(merge_note.resolve()), "note_sha256": sha_file(merge_note)}, epoch)
        note("reviewed", merge_request_id=merge_request_id, status="accept")

    row = _merge_row(state, merge_request_id)
    if row["state"] == "landed":
        return {"candidate_commit": row["merge_commit"]}
    _require(row["state"] == "verifying", "landing",
             "merge request is " + row["state"] + " before landing")
    pending = _one(state, "SELECT intent_id,state FROM v2_merge_intent "
                          "WHERE merge_request_id=? AND kind='land' "
                          "AND state IN ('ready','started','reported') "
                          "ORDER BY created_at DESC LIMIT 1", (merge_request_id,))
    if pending:
        _require(pending["state"] == "reported", "landing",
                 "a land intent is pending; run recover --confirm-execute", "merge_pending")
        merge_call(state, clone, merge_token, "merge.finish", {
            "intent_id": pending["intent_id"],
            "expected_revision": _intent_revision(state, pending["intent_id"])}, epoch)
        row = _merge_row(state, merge_request_id)
        if row["state"] == "landed":
            return {"candidate_commit": row["merge_commit"]}
    _require(git_text(clone, "rev-parse", target).lower() == d["base"], "landing",
             "target moved before landing")
    landed = merge_call(state, clone, merge_token, "merge.begin", {
        "merge_request_id": merge_request_id,
        "expected_revision": _merge_revision(state, merge_request_id), "kind": "land"}, epoch)
    report = merge_queue.execute_intent(_db(Path(d["paths"]["state"])),
                                        landed["intent_id"],
                                        _intent_token(state, landed["intent_id"]))
    save_json(Path(d["paths"]["evidence"]) / "land-report.json", report)
    _require(report.get("outcome") == "landed", "landing",
             "landing failed: " + json.dumps(report)[:800])
    merge_call(state, clone, merge_token, "merge.finish", {
        "intent_id": landed["intent_id"],
        "expected_revision": _intent_revision(state, landed["intent_id"])}, epoch)
    tip = git_text(clone, "rev-parse", target).lower()
    _require(tip == candidate["candidate_commit"].lower()
             and report.get("merge_commit") == tip, "landing",
             "landed target differs from the reviewed candidate")
    journal["landed_at"] = monitor_task.utc_now()
    note("landed", merge_request_id=merge_request_id, status="landed",
         candidate_commit=tip)
    return {"candidate_commit": candidate["candidate_commit"]}


def _merge_row(state, merge_request_id):
    row = _one(state, "SELECT * FROM v2_merge_request WHERE merge_request_id=?", (merge_request_id,))
    _require(row, "merge", "merge request is missing")
    return row


def _merge_note(d, review, merge_request_id):
    path = Path(d["paths"]["notes"]) / ("integration-" + merge_request_id + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(review, indent=2, sort_keys=True) + "\n",
                    encoding="ascii", newline="\n")
    return path


def _intent_revision(state, intent_id):
    return _one(state, "SELECT revision FROM v2_merge_intent WHERE intent_id=?",
                (intent_id,))["revision"]


def _intent_token(state, intent_id):
    return _one(state, "SELECT operation_token FROM v2_merge_intent WHERE intent_id=?",
                (intent_id,))["operation_token"]


def _merge_revision(state, merge_request_id):
    return _one(state, "SELECT revision FROM v2_merge_request WHERE merge_request_id=?",
                (merge_request_id,))["revision"]


def release_workspace(plan, state, epoch, workspace_id):
    d = plan["derived"]
    workspace = _one(state, "SELECT * FROM v2_workspace WHERE workspace_id=?", (workspace_id,))
    if not workspace or workspace["status"] == "released":
        return
    result = workspace_call(state, _actor(plan), epoch, "workspace.release", {
        "workspace_id": workspace_id, "expected_revision": workspace["revision"],
        "evidence_root": str(Path(d["paths"]["quiescence"]) / "release")})
    _require(result.get("status") == "WORKSPACE_RELEASED", "release",
             "workspace release failed: " + json.dumps(result)[:700])


def usage(plan):
    d = plan["derived"]
    jobs, total = [], {}
    for role in ("worker", "reviewer"):
        root = Path(d["paths"][role])
        for path in sorted(root.glob("jobs/*/receipt/output.log")):
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
            jobs.append({"role": role, "job": path.parents[1].name,
                         "usage": values or "unknown", "output_sha256": sha_file(path)})
    return {"status": "actual" if total else "unknown", "jobs": jobs,
            "totals": total or "unknown",
            "cost": {"status": "unknown", "reason": "no provider pricing applied"}}


# ---------------------------------------------------------------------------
# status / recover


def status(plan_path):
    plan = load_plan(plan_path)
    d = plan["derived"]
    state = Path(d["paths"]["state"])
    journal = load_journal(Path(d["paths"]["root"]) / "run.json")
    task = _one(state, "SELECT task_state,revision,accepted_evidence_id FROM v2_task "
                       "WHERE task_id=?", (d["task_id"],))
    assignment = _one(state, "SELECT assignment_id,state,review_status,holds_slot,revision,"
                             "workspace_id FROM v2_assignment WHERE task_id=? "
                             "ORDER BY generation DESC LIMIT 1", (d["task_id"],))
    workspace = None
    if assignment and assignment["workspace_id"]:
        try:
            record = _workspace_row(state, assignment["workspace_id"])
        except EntryError as exc:
            record = {"workspace_id": assignment["workspace_id"], "error": str(exc)}
        workspace = {key: record.get(key) for key in
                     ("workspace_id", "status", "path", "base_commit", "branch", "error")}
    intents = _query(state, "SELECT intent_id,kind,state FROM v2_merge_intent "
                            "ORDER BY created_at,intent_id")
    merge_requests = _query(state, "SELECT merge_request_id,state,merge_commit "
                                   "FROM v2_merge_request")
    reviews = _query(state, "SELECT verdict,state,event_id,reviewed_at FROM v2_review "
                            "WHERE assignment_id=? ORDER BY reviewed_at,review_id",
                     (assignment["assignment_id"],)) if assignment else []
    merge_row = _merge_request_row(state, assignment["assignment_id"]) if assignment else None
    target_tip = git_opt(Path(d["paths"]["repo"]), "rev-parse", "--verify", "--quiet",
                         d["target_ref"])
    source_head = git_opt(Path(d["source"]["realpath"]), "rev-parse", "--verify", "HEAD")
    bridges = {}
    for name in ("worker", "reviewer"):
        try:
            current = native_worker.status(Path(d["paths"][name]))
            bridges[name] = {key: current.get(key) for key in
                             ("status", "completion_seq", "task_id", "nonce")}
        except Exception as exc:
            bridges[name] = {"status": "unavailable", "error": str(exc)}
    return {
        "status": "STATUS", "plan_sha256": plan["plan_sha256"],
        "journal": journal and {key: journal.get(key) for key in
                                ("phase", "status", "started_at", "updated_at",
                                 "source_commit", "merge_request_id", "candidate_commit")},
        "task": task, "assignment": assignment, "workspace": workspace,
        "reviews": reviews,
        "merge_request": merge_row and {key: merge_row.get(key) for key in
                                        ("merge_request_id", "state", "merge_commit",
                                         "candidate_sha256", "error")},
        "merge_requests": merge_requests, "merge_intents": intents,
        "target_tip": target_tip, "target_base": d["base"],
        "source_head": source_head, "source_unchanged": source_head == d["base"],
        "bridges": bridges, "turns": _turn_count(plan),
        "next_action": _next_action(journal, assignment, intents, target_tip, d,
                                    reviews, merge_row),
    }


def _next_action(journal, assignment, intents, target_tip, d, reviews=(), merge_row=None):
    landed = bool(merge_row and merge_row["state"] in ("landed",)
                  and target_tip and target_tip.lower() == merge_row["merge_commit"].lower())
    if landed:
        return "complete; inspect evidence"
    if journal is None:
        return "run --plan <plan.json> --confirm-execute"
    if reviews and reviews[-1]["verdict"] in ("reject", "repair"):
        return ("terminal: the sealed review verdict is " + reviews[-1]["verdict"] +
                "; this attempt cannot be promoted; prepare a fresh attempt")
    if merge_row and merge_row["state"] == "cancelled":
        return "terminal: the merge request was cancelled; prepare a fresh attempt"
    if journal.get("status") == "ACCEPTED_NOT_LANDED" and reviews \
            and reviews[-1]["verdict"] == "accept":
        return "complete; accepted without landing"
    if (d["authorization"].get("allow_local_land") is False and assignment
            and assignment["state"] == "completed" and assignment["review_status"] == "accepted"
            and not assignment["holds_slot"] and merge_row is None and reviews
            and reviews[-1]["state"] == "sealed" and reviews[-1]["verdict"] == "accept"):
        return "complete; accepted without landing"
    if any(intent["state"] == "started" for intent in intents):
        return "recover --plan <plan.json> --confirm-execute (a merge intent was interrupted)"
    if journal.get("phase") in ("starting", "enqueued", "claimed", "workspace_ready", "dispatched"):
        return "recover --plan <plan.json> --confirm-execute (never resend an uncertain attempt)"
    return "recover --plan <plan.json> --confirm-execute"


def _sealed_verdicts(state, assignment_id):
    return [row["verdict"] for row in
            _query(state, "SELECT verdict FROM v2_review WHERE assignment_id=?",
                   (assignment_id,))]


def _worker_attempt_state(plan, journal):
    """Classify the dispatched worker attempt without ever resending it."""
    d = plan["derived"]
    worker_root = Path(d["paths"]["worker"])
    current = native_worker.status(worker_root)
    status = current.get("status")
    if status == "working":
        return "running", "the dispatched worker turn is still in progress"
    if status in ("failed", "blocked"):
        return "blocked", "native worker is " + str(status) + "; inspect before any resend"
    if status == "exited":
        return "blocked", "the native runner exited without a completion receipt"
    receipt = Path(d["paths"]["evidence"]) / "worker-receipt.json"
    if receipt.is_file():
        return "recoverable", None
    job = (worker_root / "jobs" /
           task_key({"task_id": d["attempt_id"], "nonce": d["nonce"]}))
    if (job / "receipt" / "status.json").is_file():
        return "recoverable", None
    active = worker_root / "active.json"
    if active.is_file():
        try:
            directory = Path(read_json(active).get("directory", ""))
        except (OSError, ValueError):
            directory = None
        if directory and (directory / "receipt" / "status.json").is_file():
            return "recoverable", None
    return "blocked", ("the send was attempted but no durable native receipt exists; "
                       "do not resend")


def _quiescence_evidence(plan, row):
    """Reuse recorded quiescence for this intent; never overwrite or collide."""
    d = plan["derived"]
    if row.get("recovery_json"):
        stored = json.loads(row["recovery_json"])
        path = Path(stored["evidence_path"])
        _require(path.is_file() and sha_file(path) == stored["evidence_sha256"], "recover",
                 "recorded merge recovery evidence changed; refusing another recovery")
        return path, stored["evidence_sha256"]
    path = Path(d["paths"]["quiescence"]) / ("merge-" + row["intent_id"] + ".json")
    if path.is_file():
        value = read_json(path)
        _require(value.get("intent_id") == row["intent_id"]
                 and value.get("executor_pid") == row["executor_pid"]
                 and value.get("quiescent") is True, "recover",
                 "existing quiescence evidence does not bind this intent")
        return path, sha_file(path)
    write_new(path, json.dumps({
        "intent_id": row["intent_id"], "executor_pid": row["executor_pid"],
        "quiescent": True,
        "reason": "controller observed the recorded executor pid is not alive; no resend",
        "observed_at": monitor_task.utc_now()}, sort_keys=True) + "\n")
    return path, sha_file(path)


def _accepted_without_land(plan, state, journal):
    """Derive the terminal accepted-without-land branch from the ledger.

    Nothing here touches the worktree: a released workspace no longer has one.
    """
    d = plan["derived"]
    if d["authorization"].get("allow_local_land") is not False:
        return None
    assignment = _latest_assignment(state, d)
    if (not assignment or assignment["state"] != "completed"
            or assignment["review_status"] != "accepted" or assignment["holds_slot"]
            or not assignment["workspace_id"]):
        return None
    task = _one(state, "SELECT task_state FROM v2_task WHERE task_id=?", (d["task_id"],))
    if not task or task["task_state"] != "accepted":
        return None
    workspace = _workspace_row(state, assignment["workspace_id"])
    if workspace["status"] not in ("frozen", "released"):
        return None
    if _merge_request_row(state, assignment["assignment_id"]) is not None:
        return None
    event_id = journal.get("event_id")
    source_commit = journal.get("source_commit")
    _require(event_id and source_commit, "recover",
             "the accepted task is not bound to a completion event and source commit")
    records = _ledger_reviews(state, event_id)
    _require(len(records) == 1, "recover",
             "the accepted task has no unambiguous independent review")
    record = validate_ledger_review(plan, state, event_id, assignment, records[0],
                                    source_commit)
    _require(record.get("verdict") == "accept", "recover",
             "the terminal task was not accepted by its independent review")
    _validate_retained_evidence(plan, journal)
    return {"assignment_id": assignment["assignment_id"],
            "workspace_id": workspace["workspace_id"],
            "workspace_status": workspace["status"],
            "source_commit": source_commit}


def recover(plan_path, confirm):
    plan = load_plan(plan_path)
    d = plan["derived"]
    paths = _paths(plan)
    state = Path(d["paths"]["state"])
    journal_path = paths["root"] / "run.json"
    journal = load_journal(journal_path)
    _require(journal is None or journal.get("plan_sha256") == plan["plan_sha256"], "recover",
             "run journal belongs to another plan", "journal_mismatch")
    initial = _latest_assignment(state, d)
    assignment_id = (initial["assignment_id"] if initial
                     else journal and journal.get("assignment_id"))
    scheduler_intent = _one(state, "SELECT send_state,attempted_at FROM v2_scheduler_intent "
                                   "WHERE assignment_id=?", (assignment_id,)) if assignment_id else None
    sealed = _sealed_verdicts(state, assignment_id) if assignment_id else []
    merge_row = _merge_request_row(state, assignment_id) if assignment_id else None
    merge_intents = _query(state, "SELECT * FROM v2_merge_intent "
                                 "WHERE merge_request_id=? AND state IN ('ready','started','reported')",
                           (merge_row["merge_request_id"],)) if merge_row else []
    target_tip = git_opt(paths["repo"], "rev-parse", "--verify", "--quiet", d["target_ref"])
    report = {"status": "RECOVERY_DIAGNOSED", "plan_sha256": plan["plan_sha256"],
              "journal_phase": journal and journal.get("phase"),
              "journal_status": journal and journal.get("status"),
              "assignment_id": assignment_id,
              "send_state": scheduler_intent and scheduler_intent["send_state"],
              "sealed_reviews": sealed,
              "merge_request": merge_row and {"merge_request_id": merge_row["merge_request_id"],
                                              "state": merge_row["state"],
                                              "merge_commit": merge_row["merge_commit"]},
              "merge_intents": [
                  {"intent_id": row["intent_id"], "kind": row["kind"], "state": row["state"],
                   "executor_pid": row["executor_pid"], "pid_alive": bool(
                       row["executor_pid"] and pid_alive(row["executor_pid"]))}
                  for row in merge_intents],
              "target_tip": target_tip, "target_base": d["base"],
              "resumable": False, "reason": None}
    landed = (merge_row and merge_row["state"] == "landed"
              and target_tip and target_tip.lower() == merge_row["merge_commit"].lower())
    accepted_no_land = (d["authorization"].get("allow_local_land") is False and initial
                        and initial["state"] == "completed"
                        and initial["review_status"] == "accepted"
                        and not initial["holds_slot"] and merge_row is None
                        and sealed == ["accept"])
    if not confirm:
        report["resumable"] = (journal is not None and not landed and not accepted_no_land
                               and not (sealed and sealed[-1] != "accept")
                               and not (merge_row and merge_row["state"] == "cancelled")
                               and not any(row["state"] == "started" for row in merge_intents
                                           if row["executor_pid"] and pid_alive(row["executor_pid"])))
        report["reason"] = ("completed; recovery would be idempotent"
                            if landed or accepted_no_land else
                            "diagnosis only; pass --confirm-execute to resume safe steps")
        save_json(paths["root"] / "recover.json", report)
        return report
    if journal is None:
        raise EntryError("recover", "no run journal to recover", "no_journal")

    lock_path, lock_token = acquire_lock(plan)
    try:
        # Same frozen-environment and evidence checks as a normal run, before
        # any recovery effect.
        verify_environment(plan, journal)
        ensure_target(plan, journal)
        if landed:
            journal["merge_request_id"] = merge_row["merge_request_id"]
            journal["candidate_commit"] = merge_row["merge_commit"]
            result = execute_plan(plan_path, resume=True, locked=True)
            report["status"] = "RECOVERY_COMPLETED"
            report["result_status"] = result.get("status")
            report["reason"] = "completed run reconciled from the ledger; no effect repeated"
            save_json(paths["root"] / "recover.json", report)
            return report
        terminal = _accepted_without_land(plan, state, journal)
        if terminal is not None:
            # The accepted-without-land branch is final. Only a still-frozen
            # workspace has one safe controller step left; a released one is
            # never reopened and no worktree is read.
            if terminal["workspace_status"] != "released":
                release_workspace(plan, state, d["epoch"], terminal["workspace_id"])
            journal["status"] = "ACCEPTED_NOT_LANDED"
            journal["phase"] = "accepted_not_landed"
            journal.setdefault("finished_at", monitor_task.utc_now())
            journal["usage"] = journal.get("usage") or usage(plan)
            save_journal(journal_path, journal)
            report["status"] = "RECOVERY_COMPLETED"
            report["result_status"] = "ACCEPTED_NOT_LANDED"
            report["reason"] = "completed; accepted without landing; no effect repeated"
            report["turns"] = journal.get("turns")
            report["usage"] = journal["usage"]
            save_json(paths["root"] / "recover.json", report)
            return report
        if merge_row and merge_row["state"] == "cancelled":
            report["status"] = "RECOVERY_REFUSED"
            report["reason"] = ("the merge request was cancelled after a recorded rejection; "
                                "this attempt is terminal")
            save_json(paths["root"] / "recover.json", report)
            raise EntryError("recover", report["reason"], "review_rejected")
        if sealed:
            _require(len(sealed) == 1, "recover", "ambiguous sealed reviews for this Assignment")
            if sealed[0] != "accept":
                report["status"] = "RECOVERY_REFUSED"
                report["reason"] = ("the authoritative sealed review verdict is " + sealed[0] +
                                    "; this attempt cannot be promoted")
                save_json(paths["root"] / "recover.json", report)
                raise EntryError("recover", report["reason"], "review_rejected")

        blocked = None
        if journal.get("source_commit"):
            workspace_row = _one(state, "SELECT status FROM v2_workspace WHERE workspace_id=?",
                                 (journal.get("workspace_id"),)) if journal.get("workspace_id") else None
            workspace_path = Path(d["paths"]["workspace"])
            if workspace_path.is_dir() and (not workspace_row
                                            or workspace_row["status"] != "released"):
                _validate_source_evidence(plan, workspace_path, journal)
        if scheduler_intent and scheduler_intent["send_state"] == "attempted":
            worker_state, reason = _worker_attempt_state(plan, journal)
            report["worker_attempt"] = worker_state
            if worker_state == "running":
                blocked = reason
            elif worker_state == "blocked":
                blocked = reason
        for row in merge_intents:
            if row["state"] == "started" and (not row["executor_pid"]
                                              or pid_alive(row["executor_pid"])):
                blocked = ("merge intent executor is still alive or unproven; wait for "
                           "quiescence")
        if blocked:
            report["status"] = "RECOVERY_BLOCKED"
            report["reason"] = blocked
            save_json(paths["root"] / "recover.json", report)
            raise EntryError("recover", blocked, "recovery_blocked")

        # Every ready ticket is the controller step the dead process had not
        # taken; a started one is only touched after bound quiescence.
        for row in merge_intents:
            if row["merge_request_id"]:
                _validate_merge_candidate(state, row["merge_request_id"])
        for row in merge_intents:
            validate_pins(plan)
            if row["state"] == "reported":
                merge_call(state, paths["repo"], _token(plan, "merge"), "merge.finish", {
                    "intent_id": row["intent_id"],
                    "expected_revision": _intent_revision(state, row["intent_id"])}, d["epoch"])
                continue
            if row["state"] == "ready":
                try:
                    result = merge_queue.execute_intent(_db(state), row["intent_id"],
                                                        row["operation_token"])
                except Exception as exc:
                    report["status"] = "RECOVERY_BLOCKED"
                    report["reason"] = "ready merge intent could not run: " + str(exc)
                    save_json(paths["root"] / "recover.json", report)
                    raise EntryError("recover", report["reason"], "merge_recovery_failed")
            else:
                evidence_path, evidence_sha = _quiescence_evidence(plan, row)
                try:
                    merge_call(state, paths["repo"], _token(plan, "merge"), "merge.recover", {
                        "intent_id": row["intent_id"], "expected_revision": row["revision"],
                        "evidence_path": str(evidence_path.resolve()),
                        "evidence_sha256": evidence_sha}, d["epoch"])
                    result = merge_queue.recover_intent(_db(state), row["intent_id"],
                                                        row["operation_token"])
                except Exception as exc:
                    report["status"] = "RECOVERY_BLOCKED"
                    report["reason"] = "merge intent recovery failed: " + str(exc)
                    save_json(paths["root"] / "recover.json", report)
                    raise EntryError("recover", report["reason"], "merge_recovery_failed")
            save_json(paths["evidence"] / ("recover-" + row["intent_id"] + ".json"), result)
            if result.get("outcome") in ("landed", "rebase_required", "finalized"):
                merge_call(state, paths["repo"], _token(plan, "merge"), "merge.finish", {
                    "intent_id": row["intent_id"],
                    "expected_revision": _intent_revision(state, row["intent_id"])}, d["epoch"])
            elif result.get("outcome") in ("blocked_environment", "failed"):
                report["status"] = "RECOVERY_BLOCKED"
                report["reason"] = ("merge intent " + row["intent_id"] + " ended " +
                                    str(result.get("outcome")) + ": " +
                                    str(result.get("error"))[:400])
                save_json(paths["root"] / "recover.json", report)
                raise EntryError("recover", report["reason"], "merge_recovery_failed")

        report["resumable"] = True
        report["reason"] = "safe controller steps remain; resuming without a resend"
        save_json(paths["root"] / "recover.json", report)
        result = execute_plan(plan_path, resume=True, locked=True)
        report["status"] = "RECOVERY_COMPLETED"
        report["result_status"] = result.get("status")
        save_json(paths["root"] / "recover.json", report)
        return report
    finally:
        release_lock(lock_path, lock_token)


# ---------------------------------------------------------------------------
# CLI


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare_parser = sub.add_parser("prepare", help="freeze a spec, clone and ledger")
    prepare_parser.add_argument("--spec", required=True)
    prepare_parser.add_argument("--out", required=True)
    run_parser = sub.add_parser("run", help="execute a prepared plan")
    run_parser.add_argument("--plan", required=True)
    run_parser.add_argument("--confirm-execute", action="store_true")
    status_parser = sub.add_parser("status", help="read-only plan and ledger status")
    status_parser.add_argument("--plan", required=True)
    recover_parser = sub.add_parser("recover", help="diagnose or resume a persisted run")
    recover_parser.add_argument("--plan", required=True)
    recover_parser.add_argument("--confirm-execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare(args.spec, args.out)
            output = {"status": "PREPARED", "plan": str(Path(args.out).resolve() / "plan.json"),
                      "plan_sha256": result["plan_sha256"],
                      "turns": 0, "models_launched": False, "target_ref_created": False}
            code = 0
        elif args.command == "run":
            if not args.confirm_execute:
                raise EntryError("run", "execution requires --confirm-execute", "not_confirmed")
            result = execute_plan(args.plan)
            output = {"status": result.get("status", "RUN_COMPLETED"),
                      "phase": result.get("phase"), "journal": str(
                          Path(args.plan).resolve().parent / "run.json"),
                      "turns": result.get("turns"),
                      "source_commit": result.get("source_commit"),
                      "candidate_commit": result.get("candidate_commit"),
                      "usage": result.get("usage")}
            code = 0 if result.get("status") in ("LANDED", "ACCEPTED_NOT_LANDED") else 2
        elif args.command == "status":
            output = status(args.plan)
            code = 0
        else:
            output = recover(args.plan, args.confirm_execute)
            code = 0
    except EntryError as exc:
        output = {"status": "REFUSED", "stage": exc.stage, "code": exc.code,
                  "error": str(exc)}
        code = 2
    except Exception as exc:
        output = {"status": "FAILED", "stage": getattr(exc, "stage", "unexpected"),
                  "error": str(exc)}
        code = 2
    print(json.dumps(output, ensure_ascii=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
