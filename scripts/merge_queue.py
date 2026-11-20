"""AWO merge metadata, one-shot Git intents, and reviewed materialization.

SQLite is the only lifecycle authority. Service commands use the caller's
transaction; the executor only consumes committed tickets and records receipts.
No remote writes, shell commands, task acceptance, or automatic effect retries.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import stat
import uuid

import workspace_manager as workspace


class MergeError(ValueError):
    def __init__(self, code, detail):
        self.code = code
        super().__init__(code + ": " + detail)


def _fail(code, detail):
    raise MergeError(code, detail)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode("ascii")).hexdigest()


def _rows(con, sql, args=()):
    cur = con.execute(sql, args)
    names = [col[0] for col in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def _one(con, sql, args=()):
    rows = _rows(con, sql, args)
    return rows[0] if rows else None


def _tx(con):
    if not con.in_transaction:
        _fail("transaction_required", "caller must begin a write transaction")


def _fields(value, allowed, required=()):
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        _fail("invalid_request", "payload must be an object")
    if set(value) - set(allowed) or set(required) - set(value):
        _fail("invalid_request", "unexpected or missing payload fields")


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 4096 or "\x00" in value:
        _fail("invalid_request", label + " must be nonempty text")
    return value


def _oid(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value):
        _fail("invalid_request", "full commit object ID required")
    return value


def _ref(value):
    if (not isinstance(value, str) or not value.startswith("refs/heads/")
            or not re.fullmatch(r"[A-Za-z0-9_./-]+", value) or ".." in value
            or "//" in value or value.endswith(("/", ".", ".lock"))
            or any(part.startswith(".") for part in value.split("/"))):
        _fail("invalid_request", "safe fully qualified local branch ref required")
    return value


def _revision(row, expected):
    if type(expected) is not int or expected < 1 or row["revision"] != expected:
        _fail("revision_conflict", "expected_revision differs from current revision")


def _event(con, project, principal, entity, identity, kind, payload):
    import awo_service as service
    return service._event(con, project, entity, identity, kind, principal["principal_id"],
                          "v2-merge", None, payload)


def _principal(con, project, principal, roles):
    if not isinstance(principal, dict) or any(not isinstance(principal.get(key), str) or not principal[key] for key in
                                              ("agent_id", "role", "principal_id", "project_id")):
        _fail("permission_denied", "service-derived principal required")
    row = _one(con, "SELECT * FROM v2_agent WHERE agent_id=? AND project_id=?",
               (principal["agent_id"], project))
    if (principal["project_id"] != project or principal["role"] not in roles or not row
            or row["lifecycle"] != "active"):
        _fail("permission_denied", "principal project, registry role or lifecycle is invalid")
    if row["role"] != principal["role"] and principal["role"] != "observer":
        if principal["role"] != "reviewer":
            _fail("permission_denied", "credential cannot change the registered Agent role")
        _bound_reviewer(con, row["agent_id"], principal.get("binding_id"), principal.get("binding_version"))
    elif principal["role"] in ("controller", "reviewer") and (
            principal.get("binding_id") is not None or principal.get("binding_version") is not None):
        _bound_reviewer(con, row["agent_id"], principal.get("binding_id"), principal.get("binding_version"))
    return row


def _bound_reviewer(con, agent_id, binding_id, version):
    import awo_service as service
    if (not isinstance(binding_id, str) or not binding_id or type(version) is not int
            or service._agent_id_for_binding(binding_id) != agent_id):
        _fail("permission_denied", "review binding must preserve the authenticated Agent identity")
    try:
        return service.control.guard(con, binding_id, version, "main", "review")
    except (ValueError, KeyError, sqlite3.Error) as exc:
        _fail("permission_denied", "review binding is no longer authorized: " + str(exc))


def _bytes(path):
    try:
        return workspace._read_bytes(Path(path))
    except (OSError, ValueError, TypeError) as exc:
        _fail("EVIDENCE_CHANGED", str(exc))


def _sealed(path, digest, size=None):
    data = _bytes(path)
    if hashlib.sha256(data).hexdigest() != digest or (size is not None and len(data) != size):
        _fail("EVIDENCE_CHANGED", "sealed bytes differ: " + str(path))
    return data


def _pinned_bytes(path, digest=None):
    # Installed Git executables can legitimately have hardlinks. Unlike worker
    # evidence, these are controller-approved inputs and checked by content.
    path = Path(_absolute(str(path), directory=False))
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > 128 * 1024 * 1024:
        _fail("blocked_environment", "pinned executable/input is not a bounded regular file")
    with path.open("rb") as stream:
        data = stream.read(128 * 1024 * 1024 + 1)
        after = os.fstat(stream.fileno())
    final = path.stat()
    signature = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    if signature(before) != signature(after) or signature(after) != signature(final):
        _fail("EVIDENCE_CHANGED", "pinned input changed while reading")
    if digest is not None and hashlib.sha256(data).hexdigest() != digest:
        _fail("EVIDENCE_CHANGED", "pinned input hash changed: " + str(path))
    return data


def _read_json(path, digest):
    try:
        value = json.loads(_sealed(path, digest))
    except (ValueError, UnicodeError) as exc:
        _fail("EVIDENCE_CHANGED", "sealed JSON invalid: " + str(exc))
    if not isinstance(value, dict):
        _fail("EVIDENCE_CHANGED", "sealed JSON must be an object")
    return value


def _absolute(path, directory=True):
    value = Path(_text(path, "path"))
    try:
        workspace._absolute(value)  # Also rejects Windows 3.10 reparse/UNC/ADS paths.
    except (ValueError, OSError) as exc:
        _fail("invalid_request", str(exc))
    if directory and not value.is_dir():
        _fail("blocked_environment", "required directory does not exist")
    return str(value.resolve())


def create_merge_schema(con):
    """Caller-owned additive DDL; never create another Task/Assignment authority."""
    _tx(con)
    statements = (
        """CREATE TABLE IF NOT EXISTS v2_merge_policy (
          policy_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES v2_project(project_id),
          spec_json TEXT NOT NULL, spec_sha256 TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS v2_merge_request (
          merge_request_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES v2_project(project_id),
          task_id TEXT NOT NULL REFERENCES v2_task(task_id),
          assignment_id TEXT NOT NULL REFERENCES v2_assignment(assignment_id),
          workspace_id TEXT NOT NULL REFERENCES v2_workspace(workspace_id),
          policy_id TEXT NOT NULL REFERENCES v2_merge_policy(policy_id),
          repository_id TEXT NOT NULL, target_ref TEXT NOT NULL,
          spec_json TEXT NOT NULL, spec_sha256 TEXT NOT NULL,
          state TEXT NOT NULL CHECK(state IN ('proposed','queued','waiting_blockers','rebasing',
            'verifying','merging','landed','blocked_environment','waiting_remote','failed','cancelled')),
          revision INTEGER NOT NULL DEFAULT 1, attempts INTEGER NOT NULL DEFAULT 0,
          holds_lock INTEGER NOT NULL DEFAULT 0 CHECK(holds_lock IN (0,1)),
          candidate_json TEXT, candidate_sha256 TEXT, review_json TEXT,
          merge_commit TEXT, landed_at TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
        """CREATE UNIQUE INDEX IF NOT EXISTS v2_merge_target_lock
          ON v2_merge_request(project_id,repository_id,target_ref) WHERE holds_lock=1""",
        """CREATE UNIQUE INDEX IF NOT EXISTS v2_merge_live_task ON v2_merge_request(task_id)
          WHERE state NOT IN ('failed','cancelled')""",
        """CREATE TABLE IF NOT EXISTS v2_merge_intent (
          intent_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES v2_project(project_id),
          merge_request_id TEXT REFERENCES v2_merge_request(merge_request_id),
          task_id TEXT NOT NULL REFERENCES v2_task(task_id), kind TEXT NOT NULL
            CHECK(kind IN ('build','land','poll','archive','stack')), operation_token TEXT NOT NULL UNIQUE,
          bundle_json TEXT NOT NULL, bundle_sha256 TEXT NOT NULL,
          state TEXT NOT NULL CHECK(state IN ('ready','started','reported','finished')),
          revision INTEGER NOT NULL DEFAULT 1, executor_pid INTEGER, started_at TEXT,
          report_json TEXT, report_sha256 TEXT, recovery_json TEXT, created_at TEXT NOT NULL)""",
        """CREATE UNIQUE INDEX IF NOT EXISTS v2_merge_active_archive ON v2_merge_intent(task_id)
          WHERE kind IN ('archive','stack') AND state<>'finished'""",
        """CREATE TABLE IF NOT EXISTS v2_merge_materialization (
          materialization_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, task_id TEXT NOT NULL,
          base_commit TEXT NOT NULL, parents_json TEXT NOT NULL, proof_json TEXT NOT NULL,
          proof_sha256 TEXT NOT NULL, created_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS v2_artifact_finalization (
          task_id TEXT PRIMARY KEY REFERENCES v2_task(task_id),
          assignment_id TEXT NOT NULL REFERENCES v2_assignment(assignment_id),
          intent_id TEXT NOT NULL UNIQUE REFERENCES v2_merge_intent(intent_id),
          manifest_path TEXT NOT NULL, manifest_sha256 TEXT NOT NULL, finalized_at TEXT NOT NULL)""",
    )
    for statement in statements:
        con.execute(statement)


def _independent(con, project, task, agent_id, binding=None):
    agent = _one(con, "SELECT * FROM v2_agent WHERE agent_id=? AND project_id=?", (agent_id, project))
    if (not agent or agent["lifecycle"] != "active" or agent["role"] not in ("reviewer", "controller")
            or _one(con, "SELECT 1 FROM v2_assignment WHERE task_id=? AND agent_id=? LIMIT 1", (task, agent_id))):
        _fail("permission_denied", "reviewer must be independent of every competing worker")
    if binding is not None:
        import awo_service as service
        # Native competitor bindings also catch two Agent IDs for one worker.
        if _one(con, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='v2_scheduler_intent'"):
            for row in _rows(con, "SELECT i.contract_json FROM v2_assignment a JOIN v2_scheduler_intent i "
                                  "ON a.assignment_id=i.assignment_id WHERE a.task_id=?", (task,)):
                worker = json.loads(row["contract_json"])["worker_binding"]
                if service.control.same_binding_agent(binding, worker):
                    _fail("permission_denied", "reviewer identity overlaps a competing worker binding")


def _native_acceptance(con, project, identity, assignment, evidence, review, ws, intent, verification_policy):
    """Derive metadata from existing native authorities, never a new review."""
    import awo_service as service
    import priority_scheduler as scheduler
    import monitor_task as monitor
    import workflow_scheduler as legacy
    try:
        contract = json.loads(intent["contract_json"])
        closure = json.loads(intent["closure_json"] or "{}")
        attempt = assignment["legacy_task_id"]
        request = contract["request"]
        if (intent["send_state"] != "closed" or closure.get("outcome") != "accepted"
                or intent["attempt_id"] != attempt or request["task_id"] != attempt
                or request["nonce"] != assignment["nonce"]
                or closure.get("event_id") != review["event_id"]
                or closure.get("review_sha256") != review["note_sha256"]):
            _fail("EVIDENCE_CHANGED", "native accepted closure/attempt binding differs")
        # Legacy helpers require sqlite3.Row; leave the caller's factory intact.
        factory = con.row_factory
        try:
            con.row_factory = sqlite3.Row
            original, deliveries = scheduler._verified_delivery(con, attempt, review["event_id"])
        finally:
            con.row_factory = factory
        actor = original["actor"]
        reviewer_id = service._agent_id_for_binding(actor["instance_id"])
        binding = _bound_reviewer(con, reviewer_id, actor["instance_id"], actor["version"])
        if (actor != binding or actor != json.loads(review["actor_json"])
                or original["verdict"] != "accept" or original["path"] != review["note_path"]
                or original["sha256"] != review["note_sha256"]):
            _fail("EVIDENCE_CHANGED", "original review bytes or actor provenance changed")
        _independent(con, project, identity["task_id"], reviewer_id, binding)
        _sealed(review["note_path"], review["note_sha256"])
        sealed_deliveries = _rows(con, "SELECT * FROM v2_evidence WHERE task_id=? AND assignment_id=? "
                                 "ORDER BY evidence_id", (identity["task_id"], identity["assignment_id"]))
        expected = {(str(Path(p).resolve()), sha, size) for p, sha, current, size, state in deliveries
                    if sha == current and state == "sealed"}
        actual = {(str(Path(e["path"]).resolve()), e["sealed_sha256"], e["bytes"]) for e in sealed_deliveries
                  if e["sealed"] == 1 and e["state"] == "sealed"}
        if (len(expected) != len(deliveries) or actual != expected or len(actual) != len(sealed_deliveries)
                or evidence["evidence_id"] not in {e["evidence_id"] for e in sealed_deliveries}):
            _fail("EVIDENCE_CHANGED", "native per-Assignment delivery set differs")
        for e in sealed_deliveries:
            _sealed(e["path"], e["sealed_sha256"], e["bytes"])
        inventory_row = _one(con, "SELECT * FROM v2_workspace_review WHERE assignment_id=?",
                             (identity["assignment_id"],))
        if (not inventory_row or inventory_row["verdict"] != "accept"
                or inventory_row["workspace_id"] != ws["workspace_id"]):
            _fail("EVIDENCE_CHANGED", "native workspace has no independently sealed inventory")
        inventory = json.loads(inventory_row["inventory_json"])
        if (workspace._digest(inventory) != inventory_row["inventory_sha256"]
                or any(inventory.get(k) != ws[k] for k in
                       ("workspace_id", "assignment_id", "task_id", "host", "kind", "repository_id", "base_commit"))
                or inventory.get("scope_ok") is not True
                or workspace._fingerprint(ws) != inventory["fingerprint"]):
            _fail("EVIDENCE_CHANGED", "native workspace changed after independent review")
        # Commands/finalization only rehash bounded local bytes and Git metadata.
        # Executor validation repeats the canonical full inventory outside the
        # write transaction, including Git's view of the source tree/index.
        if verification_policy is not None:
            if con.in_transaction:
                _fail("transaction_required", "external inventory verification requires no write transaction")
            if ws["kind"] == "git_worktree":
                # Check effective worktree configuration before the native Git
                # inspector, which must never execute a configured fsmonitor.
                _safe_repo(dict(verification_policy, repo_root=ws["path"]))
            import workspace_runtime
            factory = con.row_factory
            try:
                con.row_factory = sqlite3.Row
                if workspace_runtime.verify_reviewed_workspace(con, identity["assignment_id"]) != inventory:
                    _fail("EVIDENCE_CHANGED", "native workspace inventory differs")
            finally:
                con.row_factory = factory
        quiet = json.loads(ws["quiescence_json"])
        proof = _read_json(quiet["evidence_path"], quiet["evidence_sha256"])
        observed, expected_observation = proof.get("observation"), closure.get("observation")
        if (proof.get("assignment_id") != identity["assignment_id"]
                or proof.get("workspace_id") != ws["workspace_id"] or proof.get("closure") != closure
                or proof.get("actor") != quiet.get("controller") or not proof.get("checked_at")
                or quiet.get("host") != ws["host"] or not observed or not expected_observation
                or not monitor.identity_matches(observed.get("identity", {}), request["worker"]["expected_identity"])
                or not legacy._worker_quiescent(request, observed)
                or monitor.sequence(observed.get("completion_seq")) != monitor.sequence(expected_observation.get("completion_seq"))):
            _fail("permission_denied", "native freeze lacks observed closed-worker quiescence")
        manifest = dict(identity, workspace_id=ws["workspace_id"],
                        inventory_sha256=inventory_row["inventory_sha256"],
                        review_event_id=review["event_id"], review_path=review["note_path"],
                        review_sha256=review["note_sha256"])
        if contract["task_kind"] == "code" and ws["kind"] == "git_worktree":
            if inventory["dirty"]:
                _fail("blocked_environment", "reviewed code must have a committed clean source tree")
            manifest.update(kind="git", repository_id=ws["repository_id"], source_ref="refs/heads/" + ws["branch"],
                            source_commit=_oid(inventory["head"]), base_commit=_oid(ws["base_commit"]))
        elif contract["task_kind"] == "artifact" and ws["kind"] == "directory":
            files = [{k: e[k] for k in ("path", "sha256", "bytes")} for e in inventory["entries"]
                     if e["kind"] == "file" and e["path"].startswith("outputs/") and not e.get("unknown")]
            if not files:
                _fail("EVIDENCE_CHANGED", "native artifact inventory has no reviewed outputs/ files")
            manifest.update(kind="artifact", files=files)
        else:
            _fail("state_conflict", "native task classification and owned workspace kind differ")
        return manifest, {"scheduler_intent": intent, "original_review": original,
                          "deliveries": sealed_deliveries, "workspace_review": inventory_row,
                          "reviewer_agent_id": reviewer_id, "binding_id": actor["instance_id"],
                          "binding_version": actor["version"], "binding_sha256": _digest(binding)}
    except MergeError:
        raise
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error, ImportError) as exc:
        _fail("EVIDENCE_CHANGED", "native acceptance cannot be verified: " + str(exc))


def _accepted(con, project, task_id, assignment_id, generation, *, terminal=False, verification_policy=None):
    task = _one(con, "SELECT * FROM v2_task WHERE task_id=? AND project_id=?", (task_id, project))
    assignment = _one(con, "SELECT * FROM v2_assignment WHERE assignment_id=? AND task_id=?", (assignment_id, task_id))
    states = ("accepted", "landed", "finalized") if terminal else ("accepted",)
    if (not task or task["task_state"] not in states or not assignment
            or type(generation) is not int or generation < 1 or generation != assignment["generation"]
            or assignment["state"] != "completed" or assignment["review_status"] != "accepted"
            or assignment["holds_slot"]):
        _fail("state_conflict", "accepted task, closed completed assignment and generation required")
    if _one(con, "SELECT 1 FROM v2_assignment WHERE task_id=? AND slot_id=? AND generation>?",
            (task_id, assignment["slot_id"], generation)):
        _fail("state_conflict", "assignment generation superseded")
    owner = _one(con, "SELECT project_id FROM v2_agent WHERE agent_id=?", (assignment["agent_id"],))
    if not owner or owner["project_id"] != project:
        _fail("permission_denied", "assignment owner belongs to another project")
    evidence = _one(con, "SELECT * FROM v2_evidence WHERE evidence_id=? AND task_id=? AND assignment_id=?",
                    (task["accepted_evidence_id"], task_id, assignment_id))
    if not evidence or evidence["sealed"] != 1 or evidence["state"] != "sealed":
        _fail("EVIDENCE_CHANGED", "accepted per-assignment evidence is not sealed")
    if evidence["bytes"] is None or len(_sealed(evidence["path"], evidence["sealed_sha256"])) != evidence["bytes"]:
        _fail("EVIDENCE_CHANGED", "accepted evidence length differs")
    identity = {"task_id": task_id, "assignment_id": assignment_id, "generation": generation}
    reviews = _rows(con, "SELECT * FROM v2_review WHERE task_id=? AND assignment_id=? "
                    "ORDER BY reviewed_at DESC,review_id DESC LIMIT 1", (task_id, assignment_id))
    if not reviews or reviews[0]["verdict"] != "accept" or reviews[0]["state"] != "sealed":
        _fail("EVIDENCE_CHANGED", "independent sealed review missing")
    review = reviews[0]
    ws = workspace.get_workspace(con, assignment["workspace_id"])
    if (not ws or ws["task_id"] != task_id or ws["assignment_id"] != assignment_id
            or ws["status"] != "frozen" or not ws.get("quiescence_json")):
        _fail("state_conflict", "owned frozen workspace and writer quiescence required")
    quiet = json.loads(ws["quiescence_json"])
    if (quiet.get("verified") is not True or quiet.get("assignment_id") != assignment_id
            or quiet.get("workspace_id") != ws["workspace_id"]):
        _fail("permission_denied", "quiescence is not bound to this workspace")
    _sealed(quiet.get("evidence_path"), quiet.get("evidence_sha256"))
    intent = None
    if _one(con, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='v2_scheduler_intent'"):
        intent = _one(con, "SELECT * FROM v2_scheduler_intent WHERE assignment_id=?", (assignment_id,))
    native = None
    if intent:
        manifest, native = _native_acceptance(con, project, identity, assignment, evidence, review, ws, intent, verification_policy)
    else:
        # Explicit service-approved imports retain the existing JSON contract.
        manifest = _read_json(evidence["path"], evidence["sealed_sha256"])
        if any(manifest.get(key) != value for key, value in identity.items()):
            _fail("EVIDENCE_CHANGED", "manifest is not bound to task/assignment/generation")
        try:
            actor = json.loads(review["actor_json"] or "{}")
            reviewer_id = actor["agent_id"]
        except (ValueError, KeyError, TypeError):
            _fail("permission_denied", "review needs a service-resolved reviewer agent ID")
        binding = None
        if actor.get("binding_id") is not None or actor.get("binding_version") is not None:
            binding = _bound_reviewer(con, reviewer_id, actor.get("binding_id"), actor.get("binding_version"))
        _independent(con, project, task_id, reviewer_id, binding)
        note = _read_json(review["note_path"], review["note_sha256"])
        expected = dict(identity, evidence_id=evidence["evidence_id"], evidence_sha256=evidence["sealed_sha256"],
                        reviewer_agent_id=reviewer_id, verdict="accept")
        if any(note.get(key) != value for key, value in expected.items()):
            _fail("EVIDENCE_CHANGED", "sealed review does not bind the accepted evidence")
    result = {"task_id": task_id, "assignment_id": assignment_id, "generation": generation,
              "assignment_binding": {key: assignment[key] for key in
                                     ("agent_id", "slot_id", "generation", "workspace_id", "nonce", "revision")},
              "evidence": evidence, "review": review, "manifest": manifest, "workspace": ws}
    if native is not None:
        result["native"] = native
    result["seal_sha256"] = _digest(result)
    return result


def _policy(con, project, policy_id):
    row = _one(con, "SELECT * FROM v2_merge_policy WHERE policy_id=? AND project_id=?", (policy_id, project))
    if not row:
        _fail("permission_denied", "approved policy missing")
    spec = json.loads(row["spec_json"])
    if _digest(spec) != row["spec_sha256"]:
        _fail("EVIDENCE_CHANGED", "policy changed")
    return spec


def _new_policy(con, project, principal, payload):
    _fields(payload, ("policy_id", "repository_id", "repo_root", "operation_root", "archive_root",
                      "verify_argv", "pins", "timeout", "max_attempts", "provider_argv", "receipt_key_path"),
            ("repository_id", "repo_root", "operation_root", "archive_root", "verify_argv", "pins"))
    spec = dict(payload)
    policy_id = spec.pop("policy_id", "mp-" + uuid.uuid4().hex)
    _text(policy_id, "policy_id")
    _text(spec["repository_id"], "repository_id")
    for field in ("repo_root", "operation_root", "archive_root"):
        spec[field] = _absolute(spec[field])
    if any(workspace._inside(spec[key], spec["repo_root"]) for key in ("operation_root", "archive_root")):
        _fail("invalid_request", "operation/archive roots must be outside repository")
    if workspace._inside(spec["operation_root"], spec["archive_root"]) or workspace._inside(spec["archive_root"], spec["operation_root"]):
        _fail("invalid_request", "operation/archive roots must be disjoint")
    pins = spec["pins"]
    if not isinstance(pins, dict):
        _fail("invalid_request", "pins must map approved absolute files to SHA-256")
    normalized = {}
    for path, digest in pins.items():
        path = _absolute(path, directory=False)
        _pinned_bytes(path, digest)
        normalized[path] = digest
    spec["pins"] = normalized
    for name in ("verify_argv", "provider_argv"):
        argv = spec.get(name)
        if name == "provider_argv" and argv is None:
            continue
        if not isinstance(argv, list) or not argv or any(not isinstance(arg, str) or "\x00" in arg for arg in argv):
            _fail("invalid_request", "approved argv must be a nonempty string list")
        executable = _absolute(argv[0], directory=False)
        shells = {"cmd", "powershell", "pwsh", "sh", "bash", "dash", "zsh", "ksh", "fish", "csh", "tcsh"}
        if (executable not in normalized or Path(executable).suffix.lower() in (".cmd", ".bat", ".ps1", ".sh")
                or Path(executable).stem.lower() in shells):
            _fail("permission_denied", "approved pinned executable required; no shell scripts")
        spec[name] = [executable] + argv[1:]
        for arg in argv[1:]:
            if Path(arg).is_absolute() and Path(arg).is_file() and str(Path(arg).resolve()) not in normalized:
                _fail("permission_denied", "absolute verifier/provider input files must be hash-pinned")
    spec["timeout"] = spec.get("timeout", 30)
    spec["max_attempts"] = spec.get("max_attempts", 3)
    if type(spec["timeout"]) is not int or not 1 <= spec["timeout"] <= 300:
        _fail("invalid_request", "timeout must be 1..300 seconds")
    if type(spec["max_attempts"]) is not int or not 1 <= spec["max_attempts"] <= 10:
        _fail("invalid_request", "max_attempts must be 1..10")
    if spec.get("provider_argv"):
        key_path = _absolute(spec.get("receipt_key_path"), directory=False)
        key = _bytes(key_path)
        if len(key) < 32:
            _fail("invalid_request", "provider receipt key needs at least 32 bytes")
        spec["receipt_key_path"] = key_path
        spec["receipt_key_sha256"] = hashlib.sha256(key).hexdigest()
    elif spec.get("receipt_key_path"):
        _fail("invalid_request", "receipt key requires provider argv")
    git = shutil.which("git")
    if not git:
        _fail("blocked_environment", "Git is unavailable")
    spec["git_path"] = str(Path(git).resolve())
    spec["git_sha256"] = hashlib.sha256(_pinned_bytes(spec["git_path"])).hexdigest()
    con.execute("INSERT INTO v2_merge_policy VALUES (?,?,?,?,?,?)",
                (policy_id, project, _json(spec), _digest(spec), principal["principal_id"], _now()))
    _event(con, project, principal, "merge_policy", policy_id, "merge.policy.approved", {"spec_sha256": _digest(spec)})
    return {"policy_id": policy_id, "status": "APPROVED"}


def _mr(con, project, identity):
    row = _one(con, "SELECT * FROM v2_merge_request WHERE merge_request_id=? AND project_id=?", (identity, project))
    if not row:
        _fail("permission_denied", "merge request not in this project")
    spec = json.loads(row["spec_json"])
    if _digest(spec) != row["spec_sha256"]:
        _fail("EVIDENCE_CHANGED", "merge specification changed")
    if row["candidate_json"] and _digest(json.loads(row["candidate_json"])) != row["candidate_sha256"]:
        _fail("EVIDENCE_CHANGED", "merge candidate changed")
    return row, spec


def _refresh(con, project, saved, terminal=False, verification_policy=None):
    current = _accepted(con, project, saved["task_id"], saved["assignment_id"], saved["generation"],
                        terminal=terminal, verification_policy=verification_policy)
    if current["seal_sha256"] != saved["seal_sha256"]:
        _fail("EVIDENCE_CHANGED", "accepted artifact/review/workspace binding changed")
    return current


def _git_binding(policy, accepted):
    manifest, ws = accepted["manifest"], accepted["workspace"]
    if (manifest.get("kind") != "git" or ws["kind"] != "git_worktree"
            or manifest.get("repository_id") != policy["repository_id"]
            or ws["repository_id"] != policy["repository_id"]
            or Path(ws["repo_root"]).resolve() != Path(policy["repo_root"])
            or manifest.get("source_ref") != "refs/heads/" + ws["branch"]
            or manifest.get("base_commit") != ws["base_commit"]):
        _fail("state_conflict", "accepted Git repository/ref/base binding differs")
    _oid(manifest.get("source_commit"))
    _oid(manifest.get("base_commit"))
    _ref(manifest.get("source_ref"))


def _stack_task(con, project, task_id):
    task = _one(con, "SELECT * FROM v2_task WHERE task_id=? AND project_id=?", (task_id, project))
    if (not task or task["task_state"] not in ("draft", "ready", "queued", "blocked")
            or _one(con, "SELECT 1 FROM v2_assignment WHERE task_id=? AND holds_slot=1", (task_id,))):
        _fail("state_conflict", "stack requires an undispatched Task without a live slot owner")
    return task


def _materialize(con, project, principal, payload):
    _fields(payload, ("task_id", "policy_id", "target_ref", "parents", "expected_revision"),
            ("task_id", "policy_id", "target_ref", "parents", "expected_revision"))
    task = _stack_task(con, project, payload["task_id"])
    _revision(task, payload["expected_revision"])
    policy = _policy(con, project, payload["policy_id"])
    target = _ref(payload["target_ref"])
    parents = payload["parents"]
    if not isinstance(parents, list) or not 1 <= len(parents) <= 8:
        _fail("invalid_request", "stack requires 1..8 accepted parents")
    sealed, seen = [], {task["task_id"]}
    for item in parents:
        _fields(item, ("task_id", "assignment_id", "generation"), ("task_id", "assignment_id", "generation"))
        if item["task_id"] in seen:
            _fail("invalid_request", "duplicate/self stack parent")
        seen.add(item["task_id"])
        parent = _accepted(con, project, **item, terminal=True)
        manifest, ws = parent["manifest"], parent["workspace"]
        if (manifest.get("kind") != "git" or manifest.get("repository_id") != policy["repository_id"]
                or ws["repository_id"] != policy["repository_id"]
                or Path(ws["repo_root"]).resolve() != Path(policy["repo_root"])
                or manifest.get("source_ref") != "refs/heads/" + ws["branch"]
                or manifest.get("base_commit") != ws["base_commit"]):
            _fail("state_conflict", "stack parent repository/ref/base differs")
        _oid(manifest.get("source_commit"))
        sealed.append(parent)
    ticket = _ticket(con, project, task["task_id"], "stack", {
        "spec": {"accepted": None, "parents": sealed, "target_ref": target, "pr_id": None},
        "policy_id": payload["policy_id"], "policy": policy})
    _event(con, project, principal, "merge_intent", ticket["intent_id"], "merge.materialize.reserved",
           {"task_id": task["task_id"], "parents": sorted(seen - {task["task_id"]})})
    return ticket


def _record_materialization(con, project, task_id, spec, candidate):
    manifest = _read_json(candidate["manifest_path"], candidate["manifest_sha256"])
    core = {k: v for k, v in candidate.items() if k not in ("manifest_path", "manifest_sha256")}
    if manifest != core:
        _fail("EVIDENCE_CHANGED", "materialization manifest binding differs")
    _sealed(candidate["verify_log_path"], candidate["verify_log_sha256"])
    proof = {"candidate": candidate, "parent_sha256": candidate["parent_sha256"],
             "accepted_sha256": candidate["accepted_sha256"]}
    con.execute("INSERT INTO v2_merge_materialization VALUES (?,?,?,?,?,?,?,?)",
                ("mm-" + uuid.uuid4().hex, project, task_id, candidate["stack_base"],
                 _json(spec["parents"]), _json(proof), _digest(proof), _now()))


def _propose(con, project, principal, payload):
    _fields(payload, ("task_id", "assignment_id", "generation", "workspace_id", "policy_id",
                      "target_ref", "parents", "pr_id", "expected_revision"),
            ("task_id", "assignment_id", "generation", "workspace_id", "policy_id", "target_ref", "expected_revision"))
    accepted = _accepted(con, project, payload["task_id"], payload["assignment_id"], payload["generation"])
    task = _one(con, "SELECT revision FROM v2_task WHERE task_id=?", (payload["task_id"],))
    _revision(task, payload["expected_revision"])
    manifest, ws = accepted["manifest"], accepted["workspace"]
    policy = _policy(con, project, payload["policy_id"])
    if (manifest.get("kind") != "git" or ws["kind"] != "git_worktree"
            or ws["workspace_id"] != payload["workspace_id"]
            or manifest.get("repository_id") != policy["repository_id"]
            or ws["repository_id"] != policy["repository_id"]
            or Path(ws["repo_root"]).resolve() != Path(policy["repo_root"])):
        _fail("state_conflict", "git evidence/workspace/repository binding differs")
    _oid(manifest.get("source_commit"))
    _oid(manifest.get("base_commit"))
    _ref(manifest.get("source_ref"))
    if manifest["base_commit"] != ws["base_commit"] or manifest["source_ref"] != "refs/heads/" + ws["branch"]:
        _fail("EVIDENCE_CHANGED", "source ref/base differ from owned workspace")
    target = _ref(payload["target_ref"])
    if target == manifest["source_ref"]:
        _fail("invalid_request", "target cannot equal source")
    parents = payload.get("parents", [])
    if not isinstance(parents, list) or len(parents) > 8:
        _fail("invalid_request", "at most eight accepted parents permitted")
    sealed_parents, seen = [], {payload["task_id"]}
    for item in parents:
        _fields(item, ("task_id", "assignment_id", "generation"), ("task_id", "assignment_id", "generation"))
        if item["task_id"] in seen:
            _fail("invalid_request", "duplicate/self parent")
        seen.add(item["task_id"])
        parent = _accepted(con, project, **item, terminal=True)
        if parent["manifest"].get("kind") != "git" or parent["manifest"].get("repository_id") != policy["repository_id"]:
            _fail("state_conflict", "parent repository or artifact type differs")
        _oid(parent["manifest"].get("source_commit"))
        _git_binding(policy, parent)
        sealed_parents.append(parent)
    if payload.get("pr_id") is not None:
        _text(payload["pr_id"], "pr_id")
        if not policy.get("provider_argv"):
            _fail("permission_denied", "PR requires an approved status adapter")
    identity = "mr-" + uuid.uuid4().hex
    spec = {"accepted": accepted, "parents": sealed_parents, "policy_id": payload["policy_id"],
            "target_ref": target, "pr_id": payload.get("pr_id")}
    at = _now()
    con.execute("INSERT INTO v2_merge_request(merge_request_id,project_id,task_id,assignment_id,workspace_id,"
                "policy_id,repository_id,target_ref,spec_json,spec_sha256,state,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,'proposed',?,?)",
                (identity, project, payload["task_id"], payload["assignment_id"], payload["workspace_id"],
                 payload["policy_id"], policy["repository_id"], target, _json(spec), _digest(spec), at, at))
    _event(con, project, principal, "merge_request", identity, "merge.proposed", {"spec_sha256": _digest(spec)})
    return {"merge_request_id": identity, "state": "proposed", "revision": 1}


def _ticket(con, project, task_id, kind, bundle, merge_request_id=None):
    identity, token = "mi-" + uuid.uuid4().hex, secrets.token_hex(32)
    bundle = dict(bundle, intent_id=identity, project_id=project, task_id=task_id, kind=kind)
    bundle["service_state"] = _service_state(con, project)
    bundle["task_revision"] = _one(con, "SELECT revision FROM v2_task WHERE task_id=?", (task_id,))["revision"]
    con.execute("INSERT INTO v2_merge_intent(intent_id,project_id,merge_request_id,task_id,kind,operation_token,"
                "bundle_json,bundle_sha256,state,created_at) VALUES (?,?,?,?,?,?,?,?,'ready',?)",
                (identity, project, merge_request_id, task_id, kind, token, _json(bundle), _digest(bundle), _now()))
    return {"intent_id": identity, "operation_token": token, "kind": kind, "revision": 1}


def _review_binding(con, project, spec, candidate, review):
    if not review:
        _fail("review_required", "integration candidate needs an independent sealed review")
    binding = None
    if review.get("binding_id") is not None or review.get("binding_version") is not None:
        binding = _bound_reviewer(con, review["reviewer_agent_id"], review.get("binding_id"), review.get("binding_version"))
        if _digest(binding) != review.get("binding_sha256"):
            _fail("permission_denied", "saved integration reviewer binding changed")
    _independent(con, project, spec["accepted"]["task_id"], review["reviewer_agent_id"], binding)
    note = _read_json(review["note_path"], review["note_sha256"])
    expected = {"candidate_sha256": _digest(candidate), "verdict": "accept",
                "task_id": spec["accepted"]["task_id"],
                "assignment_id": spec["accepted"]["assignment_id"],
                "generation": spec["accepted"]["generation"],
                "reviewer_agent_id": review["reviewer_agent_id"]}
    if any(note.get(k) != v for k, v in expected.items()):
        _fail("EVIDENCE_CHANGED", "integration review binding differs")
    _sealed(candidate["manifest_path"], candidate["manifest_sha256"])
    _sealed(candidate["verify_log_path"], candidate["verify_log_sha256"])


def _waiting(con, project, principal, row, reason):
    con.execute("UPDATE v2_merge_request SET state='waiting_blockers',error=?,revision=revision+1,"
                "updated_at=? WHERE merge_request_id=?", (reason, _now(), row["merge_request_id"]))
    _event(con, project, principal, "merge_request", row["merge_request_id"], "merge.waiting_blockers",
           {"reason": reason})
    return {"merge_request_id": row["merge_request_id"], "state": "waiting_blockers",
            "merge_revision": row["revision"] + 1, "reason": reason}


def apply_command(con, project_id, principal, tool, payload):
    """Service command adapter; no external Git/verification process here."""
    _tx(con)
    _principal(con, project_id, principal, ("reviewer", "controller") if tool == "merge.review" else ("controller",))
    if tool == "merge.policy":
        return _new_policy(con, project_id, principal, payload)
    if tool == "merge.propose":
        return _propose(con, project_id, principal, payload)
    if tool == "merge.materialize":
        return _materialize(con, project_id, principal, payload)
    if tool == "merge.finish":
        return _finish(con, project_id, principal, payload)
    if tool == "merge.recover":
        _fields(payload, ("intent_id", "expected_revision", "evidence_path", "evidence_sha256"),
                ("intent_id", "expected_revision", "evidence_path", "evidence_sha256"))
        intent, bundle = _intent(con, project_id, payload["intent_id"])
        _revision(intent, payload["expected_revision"])
        if intent["state"] != "started":
            _fail("state_conflict", "only uncertain started intent can be recovered")
        note = _read_json(payload["evidence_path"], payload["evidence_sha256"])
        if (note.get("intent_id") != intent["intent_id"] or note.get("executor_pid") != intent["executor_pid"]
                or note.get("quiescent") is not True or not note.get("reason")):
            _fail("permission_denied", "controller needs operation-bound executor quiescence evidence")
        con.execute("UPDATE v2_merge_intent SET recovery_json=?,revision=revision+1 WHERE intent_id=?",
                    (_json(payload), intent["intent_id"]))
        _event(con, project_id, principal, "merge_intent", intent["intent_id"], "merge.recovery.authorized",
               {"evidence_sha256": payload["evidence_sha256"]})
        return {"intent_id": intent["intent_id"], "revision": intent["revision"] + 1}
    if tool == "artifact.begin":
        _fields(payload, ("task_id", "assignment_id", "generation", "policy_id", "expected_revision"),
                ("task_id", "assignment_id", "generation", "policy_id", "expected_revision"))
        accepted = _accepted(con, project_id, payload["task_id"], payload["assignment_id"], payload["generation"])
        _revision(_one(con, "SELECT revision FROM v2_task WHERE task_id=?", (payload["task_id"],)), payload["expected_revision"])
        if accepted["manifest"].get("kind") != "artifact" or accepted["workspace"]["kind"] != "directory":
            _fail("state_conflict", "artifact finalization requires noncode evidence/workspace")
        policy = _policy(con, project_id, payload["policy_id"])
        ticket = _ticket(con, project_id, payload["task_id"], "archive",
                       {"accepted": accepted, "policy": policy, "policy_id": payload["policy_id"],
                        "task_revision": payload["expected_revision"]})
        _event(con, project_id, principal, "merge_intent", ticket["intent_id"], "artifact.finalization.reserved",
               {"task_id": payload["task_id"], "assignment_id": payload["assignment_id"]})
        return ticket
    allowed = {"merge.enqueue", "merge.begin", "merge.review", "merge.cancel"}
    if tool not in allowed:
        _fail("invalid_request", "unknown merge command")
    fields = {"merge_request_id", "expected_revision"}
    if tool == "merge.begin":
        fields.add("kind")
    if tool == "merge.review":
        fields.update(("note_path", "note_sha256"))
    _fields(payload, fields, ("merge_request_id", "expected_revision"))
    row, spec = _mr(con, project_id, payload["merge_request_id"])
    _revision(row, payload["expected_revision"])
    policy = _policy(con, project_id, row["policy_id"])
    _refresh(con, project_id, spec["accepted"])
    for parent in spec["parents"]:
        _refresh(con, project_id, parent, terminal=True)
    result = {}
    if tool == "merge.enqueue":
        if row["state"] not in ("proposed", "blocked_environment", "waiting_blockers") or row["holds_lock"]:
            _fail("state_conflict", "request cannot be queued in this state")
        state = "queued"
    elif tool == "merge.cancel":
        if row["holds_lock"] or row["state"] in ("landed", "cancelled"):
            _fail("state_conflict", "in-flight/landed request cannot be cancelled")
        state = "cancelled"
    elif tool == "merge.review":
        if row["state"] != "verifying" or not row["candidate_json"]:
            _fail("state_conflict", "verified candidate required")
        candidate = json.loads(row["candidate_json"])
        review = {"reviewer_agent_id": principal["agent_id"], "note_path": payload.get("note_path"),
                  "note_sha256": payload.get("note_sha256")}
        if principal.get("binding_id") is not None or principal.get("binding_version") is not None:
            binding = _bound_reviewer(con, principal["agent_id"], principal.get("binding_id"), principal.get("binding_version"))
            review.update(binding_id=principal["binding_id"], binding_version=principal["binding_version"],
                          binding_sha256=_digest(binding))
        _review_binding(con, project_id, spec, candidate, review)
        con.execute("UPDATE v2_merge_request SET review_json=? WHERE merge_request_id=?", (_json(review), row["merge_request_id"]))
        state = "verifying"
    else:
        kind = payload.get("kind", "build")
        if kind not in ("build", "land", "poll") or row["holds_lock"]:
            _fail("state_conflict", "unknown or already active merge phase")
        if row["attempts"] >= policy["max_attempts"]:
            _fail("attempts_exhausted", "merge retry limit reached")
        candidate = json.loads(row["candidate_json"]) if row["candidate_json"] else None
        review = json.loads(row["review_json"]) if row["review_json"] else None
        if kind == "build":
            if row["state"] not in ("queued", "waiting_blockers"):
                _fail("state_conflict", "build requires queued request")
            earlier = _one(con, "SELECT merge_request_id FROM v2_merge_request WHERE project_id=? "
                           "AND repository_id=? AND target_ref=? AND state IN ('queued','waiting_blockers') "
                           "AND (created_at<? OR (created_at=? AND merge_request_id<?)) LIMIT 1",
                           (project_id, row["repository_id"], row["target_ref"], row["created_at"], row["created_at"], row["merge_request_id"]))
            if earlier:
                return _waiting(con, project_id, principal, row, "earlier queued merge owns FIFO position")
        else:
            if row["state"] not in ("verifying", "waiting_remote", "waiting_blockers") or not candidate:
                _fail("state_conflict", "reviewed candidate required before landing/polling")
            _review_binding(con, project_id, spec, candidate, review)
            if (kind == "poll") != bool(spec["pr_id"]):
                _fail("permission_denied", "PR requests use status receipts, local requests use CAS")
        state = {"build": "rebasing", "land": "merging", "poll": "waiting_remote"}[kind]
        try:
            con.execute("UPDATE v2_merge_request SET holds_lock=1 WHERE merge_request_id=?", (row["merge_request_id"],))
        except sqlite3.IntegrityError:
            return _waiting(con, project_id, principal, row, "repository target has an unresolved operation")
        result = _ticket(con, project_id, row["task_id"], kind,
                         {"spec": spec, "policy": policy, "policy_id": row["policy_id"],
                          "candidate": candidate, "review": review}, row["merge_request_id"])
    con.execute("UPDATE v2_merge_request SET state=?,revision=revision+1,updated_at=? WHERE merge_request_id=?",
                (state, _now(), row["merge_request_id"]))
    _event(con, project_id, principal, "merge_request", row["merge_request_id"], tool, {"state": state, "revision": row["revision"] + 1})
    return dict(result, merge_request_id=row["merge_request_id"], state=state, merge_revision=row["revision"] + 1)


def _intent(con, project, identity):
    row = _one(con, "SELECT * FROM v2_merge_intent WHERE intent_id=? AND project_id=?", (identity, project))
    if not row:
        _fail("permission_denied", "intent not in this project")
    bundle = json.loads(row["bundle_json"])
    if _digest(bundle) != row["bundle_sha256"]:
        _fail("EVIDENCE_CHANGED", "intent bundle changed")
    return row, bundle


def _service_state(con, project):
    state = _one(con, "SELECT project_id,v2_enabled,frozen,controller_epoch FROM v2_service_state WHERE id=1")
    if not state or state["project_id"] != project or state["frozen"] or not state["v2_enabled"]:
        _fail("blocked_environment", "service is disabled, frozen or belongs to another project")
    return state


def _validate_bundle(con, bundle, *, check_epoch=True):
    project = bundle["project_id"]
    if check_epoch and _service_state(con, project) != bundle["service_state"]:
        _fail("stale_epoch", "committed intent belongs to an older controller epoch")
    if _policy(con, project, bundle["policy_id"]) != bundle["policy"]:
        _fail("EVIDENCE_CHANGED", "approved policy no longer matches ticket")
    task = _one(con, "SELECT revision FROM v2_task WHERE task_id=?", (bundle["task_id"],))
    _revision(task, bundle["task_revision"])
    verification_policy = bundle["policy"] if not con.in_transaction else None
    if verification_policy is not None:
        _check_policy_files(verification_policy)
    if bundle["kind"] == "archive":
        _refresh(con, project, bundle["accepted"], verification_policy=verification_policy)
    else:
        spec = bundle["spec"]
        if bundle["kind"] != "stack":
            _refresh(con, project, spec["accepted"], verification_policy=verification_policy)
        else:
            _stack_task(con, project, bundle["task_id"])
        for parent in spec["parents"]:
            _refresh(con, project, parent, terminal=True, verification_policy=verification_policy)
        if bundle["kind"] in ("land", "poll"):
            _review_binding(con, project, spec, bundle["candidate"], bundle["review"])


def _check_policy_files(policy):
    for key in ("repo_root", "operation_root", "archive_root"):
        _absolute(policy[key])
    _pinned_bytes(policy["git_path"], policy["git_sha256"])
    git = shutil.which("git")
    if not git or Path(git).resolve() != Path(policy["git_path"]):
        _fail("blocked_environment", "workspace inspector Git differs from approved executable")
    for path, digest in policy["pins"].items():
        _pinned_bytes(path, digest)
    if policy.get("provider_argv"):
        _sealed(policy["receipt_key_path"], policy["receipt_key_sha256"])


def _git(policy, cwd, *args, ok=(0,)):
    argv = [policy["git_path"], "--no-replace-objects",
            "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false",
            "-c", "protocol.allow=never", "-c", "submodule.recurse=false",
            "-c", "commit.gpgSign=false", "-c", "tag.gpgSign=false",
            "-c", "user.name=AWO Integration", "-c", "user.email=awo@example.invalid",
            "-C", str(cwd), *args]
    result = workspace.run_argv(argv, cwd=cwd, timeout=policy["timeout"])
    if result["returncode"] not in ok:
        _fail("git_failed", args[0] + ": " + result["stderr"].decode("utf-8", "replace")[:2000])
    return result


def _git_text(policy, cwd, *args):
    return _git(policy, cwd, *args)["stdout"].decode("utf-8", "strict").strip()


def _safe_repo(policy, target=None):
    repo = policy["repo_root"]
    config = _git(policy, repo, "config", "--includes", "--get-regexp",
                  r"^(filter[.]|merge[.].*[.]driver$|core[.](sshcommand|fsmonitor)$)", ok=(0, 1))
    # get-regexp also lists our command-line core.fsmonitor=false override.
    unsafe = [line for line in config["stdout"].splitlines() if line != b"core.fsmonitor false"]
    if unsafe:
        _fail("blocked_environment", "custom filters/merge drivers are unsupported")
    if _git_text(policy, repo, "rev-parse", "--is-shallow-repository") != "false":
        _fail("blocked_environment", "complete local Git history required")
    if _git_text(policy, repo, "rev-parse", "--is-bare-repository") != "true":
        dirty = _git(policy, repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        if dirty["stdout"]:
            _fail("blocked_environment", "repository worktree is dirty")
    listing = _git(policy, repo, "worktree", "list", "--porcelain", "-z")["stdout"].split(b"\0")
    if target and ("branch " + target).encode("utf-8") in listing:
        _fail("blocked_environment", "target is checked out; direct ref update is forbidden")
    if target:
        # Symbolic refs can redirect update-ref to an unrelated branch.
        symbolic = _git(policy, repo, "symbolic-ref", "-q", target, ok=(0, 1))
        if symbolic["returncode"] == 0:
            _fail("blocked_environment", "target must be a direct branch ref")


def _sources_safe(bundle):
    policy, spec = bundle["policy"], bundle["spec"]
    _safe_repo(policy, spec["target_ref"])
    sources = ([spec["accepted"]] if spec.get("accepted") else []) + spec["parents"]
    for accepted in sources:
        ws, manifest = accepted["workspace"], accepted["manifest"]
        inspection = workspace.inspect_workspace(ws)
        if not inspection["ok"]:
            _fail("EVIDENCE_CHANGED", inspection.get("error", "workspace ownership changed"))
        if inspection["dirty"]:
            _fail("blocked_environment", "accepted source workspace is dirty")
        if not inspection["inventory"]["scope_ok"]:
            _fail("EVIDENCE_CHANGED", "source inventory has unresolved scope or file problems")
        head = _git_text(policy, ws["path"], "rev-parse", "--verify", "HEAD")
        ref_tip = _git_text(policy, policy["repo_root"], "rev-parse", "--verify", manifest["source_ref"])
        branch = _git_text(policy, ws["path"], "symbolic-ref", "HEAD")
        if head != manifest["source_commit"] or ref_tip != head or branch != manifest["source_ref"]:
            _fail("EVIDENCE_CHANGED", "source commit or branch changed after acceptance")
        _git(policy, ws["path"], "merge-base", "--is-ancestor", manifest["base_commit"], head)
        modes = _git(policy, ws["path"], "ls-tree", "-r", "-z", head)["stdout"].split(b"\0")
        if any(item.startswith((b"120000 ", b"160000 ")) for item in modes):
            _fail("blocked_environment", "symlinks/submodules require a separate integration policy")


def _publish(path, data):
    path = Path(path)
    _absolute(str(path.parent))
    if os.path.lexists(path):
        if _bytes(path) != data:
            _fail("EVIDENCE_CHANGED", "existing receipt/archive bytes differ")
    else:
        # Exclusive creation never overwrites another writer's evidence.
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        workspace._sync_dir(path.parent)
    return {"path": str(path), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def _operation_dir(bundle):
    root = Path(bundle["policy"]["operation_root"])
    _absolute(str(root))
    path = root / bundle["intent_id"]
    path.mkdir()  # A previous partial operation must go through recovery.
    return path


def _candidate_check(bundle):
    candidate, policy = bundle["candidate"], bundle["policy"]
    manifest = _read_json(candidate["manifest_path"], candidate["manifest_sha256"])
    _sealed(candidate["verify_log_path"], candidate["verify_log_sha256"])
    core = {k: v for k, v in candidate.items() if k not in ("manifest_path", "manifest_sha256")}
    if manifest != core:
        _fail("EVIDENCE_CHANGED", "candidate manifest does not match sealed review")
    path = _absolute(candidate["worktree"])
    if not workspace._inside(path, policy["operation_root"]):
        _fail("permission_denied", "candidate worktree escaped operation root")
    if _git_text(policy, path, "rev-parse", "--verify", "HEAD") != candidate["candidate_commit"]:
        _fail("EVIDENCE_CHANGED", "candidate HEAD changed after verification")
    if _git(policy, path, "status", "--porcelain=v1", "-z", "--untracked-files=all",
            "--ignored=matching")["stdout"]:
        _fail("EVIDENCE_CHANGED", "candidate worktree changed after verification")
    _git(policy, path, "merge-base", "--is-ancestor", candidate["expected_old_tip"],
         candidate["candidate_commit"])


def _build(con, bundle):
    policy, spec = bundle["policy"], bundle["spec"]
    repo = policy["repo_root"]
    _sources_safe(bundle)
    old = _oid(_git_text(policy, repo, "rev-parse", "--verify", spec["target_ref"]))
    operation = _operation_dir(bundle)
    tree = operation / "candidate"
    _git(policy, repo, "worktree", "add", "--detach", str(tree), old)
    for parent in spec["parents"]:
        commit = parent["manifest"]["source_commit"]
        preflight = _git(policy, repo, "merge-tree", "--write-tree",
                         _git_text(policy, tree, "rev-parse", "HEAD"), commit, ok=(0, 1))
        if preflight["returncode"]:
            _fail("conflict", "accepted parent composition conflicts: " +
                  preflight["stdout"].decode("utf-8", "replace")[-2000:])
        merged = _git(policy, tree, "merge", "--no-edit", "--no-ff", commit, ok=(0, 1))
        if merged["returncode"]:
            _git(policy, tree, "merge", "--abort")
            _fail("conflict", "accepted parent merge conflicts")
    stack_base = _oid(_git_text(policy, tree, "rev-parse", "HEAD"))
    if bundle["kind"] != "stack":
        source = spec["accepted"]["manifest"]
        _git(policy, tree, "checkout", "--detach", source["source_commit"])
        # Preserve reviewed merge commits when replaying a source containing merges.
        rebased = _git(policy, tree, "rebase", "--rebase-merges", "--onto", stack_base,
                       source["base_commit"], ok=(0, 1))
        if rebased["returncode"]:
            conflict = _git(policy, tree, "diff", "--name-only", "--diff-filter=U")["stdout"]
            _git(policy, tree, "rebase", "--abort")
            _fail("conflict", "source rebase conflicts: " + conflict.decode("utf-8", "replace")[:2000])
    commit = _oid(_git_text(policy, tree, "rev-parse", "HEAD"))
    _check_policy_files(policy)
    result = workspace.run_argv(policy["verify_argv"], cwd=tree, timeout=policy["timeout"])
    log = _publish(operation / "verify.json", _json({
        "argv": policy["verify_argv"], "returncode": result["returncode"],
        "stdout": result["stdout"].decode("utf-8", "replace"),
        "stderr": result["stderr"].decode("utf-8", "replace"),
        "candidate_commit": commit}).encode("ascii"))
    if result["returncode"]:
        return {"outcome": "failed", "error": "verification_failed", "verify_log_path": log["path"],
                "verify_log_sha256": log["sha256"]}
    candidate = {
        "task_id": bundle["task_id"], "assignment_id": (spec.get("accepted") or {}).get("assignment_id"),
        "generation": (spec.get("accepted") or {}).get("generation"), "intent_id": bundle["intent_id"],
        "candidate_commit": commit, "expected_old_tip": old, "stack_base": stack_base,
        "target_ref": spec["target_ref"], "worktree": str(tree),
        "accepted_sha256": (spec.get("accepted") or {}).get("seal_sha256"),
        "parent_sha256": [parent["seal_sha256"] for parent in spec["parents"]],
        "verify_log_path": log["path"], "verify_log_sha256": log["sha256"]}
    manifest = _publish(operation / "candidate.json", _json(candidate).encode("ascii"))
    candidate.update(manifest_path=manifest["path"], manifest_sha256=manifest["sha256"])
    _validate_bundle(con, bundle)
    _sources_safe(bundle)
    _candidate_check(dict(bundle, candidate=candidate))
    return {"outcome": "materialized" if bundle["kind"] == "stack" else "verified", "candidate": candidate}


def _land(con, bundle):
    _candidate_check(bundle)
    _sources_safe(bundle)
    _validate_bundle(con, bundle)
    candidate, policy = bundle["candidate"], bundle["policy"]
    # Last external safety check precedes the actual expected-old CAS.
    _safe_repo(policy, bundle["spec"]["target_ref"])
    result = _git(policy, policy["repo_root"], "update-ref", "--no-deref",
                  bundle["spec"]["target_ref"], candidate["candidate_commit"],
                  candidate["expected_old_tip"], ok=(0, 1, 128))
    if result["returncode"]:
        actual = _git_text(policy, policy["repo_root"], "rev-parse", "--verify", bundle["spec"]["target_ref"])
        if actual != candidate["expected_old_tip"]:
            return {"outcome": "rebase_required", "error": "CAS_MISMATCH", "observed_tip": actual}
        return {"outcome": "blocked_environment", "error": result["stderr"].decode("utf-8", "replace")[:2000]}
    return {"outcome": "landed", "merge_commit": candidate["candidate_commit"], "method": "local_cas"}


def _archive(con, bundle):
    accepted, policy = bundle["accepted"], bundle["policy"]
    ws = accepted["workspace"]
    inspection = workspace.inspect_workspace(ws)
    if not inspection["ok"] or not inspection["inventory"]["scope_ok"]:
        _fail("EVIDENCE_CHANGED", "artifact workspace ownership/inventory invalid")
    files = accepted["manifest"].get("files")
    if not isinstance(files, list) or not files or len(files) > 4096:
        _fail("invalid_request", "artifact manifest needs 1..4096 files")
    directory = Path(policy["archive_root"]) / accepted["evidence"]["sealed_sha256"]
    if directory.exists():
        _absolute(str(directory))
    else:
        directory.mkdir()
    archived, names = [], set()
    for item in files:
        _fields(item, ("path", "sha256", "bytes"), ("path", "sha256", "bytes"))
        try:
            name = workspace._relative(item["path"])
        except (ValueError, TypeError) as exc:
            _fail("invalid_request", "invalid artifact relative path: " + str(exc))
        if name in names or not name.startswith("outputs/") or type(item["bytes"]) is not int or item["bytes"] < 0:
            _fail("invalid_request", "unique outputs/ file and byte length required")
        names.add(name)
        source = Path(ws["path"]) / name
        data = _sealed(source, item["sha256"], item["bytes"])
        destination = directory / item["sha256"]
        blob = _publish(destination, data)
        archived.append(dict(item, archive_path=blob["path"]))
    _validate_bundle(con, bundle)
    for item in files:
        _sealed(Path(ws["path"]) / item["path"], item["sha256"], item["bytes"])
    manifest = {"task_id": bundle["task_id"], "assignment_id": accepted["assignment_id"],
                "generation": accepted["generation"], "evidence_sha256": accepted["evidence"]["sealed_sha256"],
                "files": archived}
    record = _publish(directory / "manifest.json", _json(manifest).encode("ascii"))
    return {"outcome": "finalized", "manifest_path": record["path"], "manifest_sha256": record["sha256"]}


def _poll(con, bundle):
    """Read-only approved adapter. No built-in credentials, fetch, push or API writes."""
    policy, candidate, spec = bundle["policy"], bundle["candidate"], bundle["spec"]
    _candidate_check(bundle)
    _sources_safe(bundle)
    operation = _operation_dir(bundle)
    request = {"format": 1, "intent_id": bundle["intent_id"], "project_id": bundle["project_id"],
               "repository_id": policy["repository_id"], "pr_id": spec["pr_id"],
               "target_ref": spec["target_ref"], "candidate_commit": candidate["candidate_commit"],
               "expected_old_tip": candidate["expected_old_tip"], "candidate_sha256": _digest(candidate)}
    request_file = _publish(operation / "request.json", _json(request).encode("ascii"))
    receipt_path = operation / "receipt.json"
    argv = policy["provider_argv"] + ["--request", request_file["path"], "--receipt", str(receipt_path)]
    result = workspace.run_argv(argv, cwd=operation, timeout=policy["timeout"])
    _publish(operation / "provider.log", _json({
        "returncode": result["returncode"], "stdout": result["stdout"].decode("utf-8", "replace"),
        "stderr": result["stderr"].decode("utf-8", "replace")}).encode("ascii"))
    if result["returncode"]:
        return {"outcome": "blocked_environment", "error": "approved PR status adapter failed"}
    try:
        receipt = json.loads(_bytes(receipt_path))
        body, signature = receipt["body"], receipt["hmac_sha256"]
        key = _sealed(policy["receipt_key_path"], policy["receipt_key_sha256"])
        calculated = hmac.new(key, _json(body).encode("ascii"), hashlib.sha256).hexdigest()
        if not isinstance(signature, str) or not hmac.compare_digest(calculated, signature):
            _fail("invalid_receipt", "PR receipt authentication failed")
        if any(body.get(k) != v for k, v in request.items()):
            _fail("invalid_receipt", "PR receipt does not bind this exact operation/candidate/base")
        if body.get("state") not in ("pending", "merged", "closed"):
            _fail("invalid_receipt", "unknown PR state")
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, MergeError):
            raise
        _fail("invalid_receipt", "malformed PR receipt")
    if body["state"] == "pending" or (body["state"] == "merged" and body.get("ci") != "passed"):
        return {"outcome": "waiting_remote", "error": "PR/CI gate pending"}
    if body["state"] == "closed":
        return {"outcome": "failed", "error": "PR closed without landing"}
    merge_commit = _oid(body.get("merge_commit"))
    # A signed adapter assertion alone is insufficient: require locally available
    # content and ancestry matching the reviewed candidate. No implicit fetch.
    repo = policy["repo_root"]
    tree = _git_text(policy, repo, "rev-parse", "--verify", merge_commit + "^{tree}")
    expected_tree = _git_text(policy, repo, "rev-parse", "--verify", candidate["candidate_commit"] + "^{tree}")
    if tree != expected_tree:
        _fail("invalid_receipt", "remote merge tree differs from reviewed candidate")
    _git(policy, repo, "merge-base", "--is-ancestor", candidate["expected_old_tip"], merge_commit)
    _validate_bundle(con, bundle)
    _candidate_check(bundle)
    sealed = _publish(operation / "accepted-receipt.json", _json(receipt).encode("ascii"))
    return {"outcome": "landed", "merge_commit": merge_commit, "method": "authenticated_pr_receipt",
            "receipt_path": sealed["path"], "receipt_sha256": sealed["sha256"]}


def _connect(database_path):
    path = Path(database_path).resolve()
    # mode=rw prevents typos from creating a second authority.
    con = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=1)
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _load_ticket(con, identity, token):
    row = _one(con, "SELECT project_id FROM v2_merge_intent WHERE intent_id=?", (identity,))
    if not row:
        _fail("permission_denied", "committed intent not found")
    intent, bundle = _intent(con, row["project_id"], identity)
    if not isinstance(token, str) or not hmac.compare_digest(intent["operation_token"], token):
        _fail("permission_denied", "operation token mismatch")
    return intent, bundle


def _report(intent):
    if not intent["report_json"]:
        _fail("state_conflict", "external outcome has not been recorded")
    report = json.loads(intent["report_json"])
    if _digest(report) != intent["report_sha256"]:
        _fail("EVIDENCE_CHANGED", "external report changed")
    return dict(report, intent_id=intent["intent_id"], intent_revision=intent["revision"])


def _store_report(con, identity, token, report):
    con.execute("BEGIN IMMEDIATE")
    try:
        intent, _ = _load_ticket(con, identity, token)
        if intent["state"] in ("reported", "finished"):
            con.rollback()
            return _report(intent)
        if intent["state"] != "started":
            _fail("state_conflict", "report requires consumed intent")
        con.execute("UPDATE v2_merge_intent SET state='reported',report_json=?,report_sha256=?,"
                    "revision=revision+1 WHERE intent_id=?", (_json(report), _digest(report), identity))
        con.commit()
    except BaseException:
        con.rollback()
        raise
    return dict(report, intent_id=identity, intent_revision=intent["revision"] + 1)


def execute_intent(database_path, intent_id, operation_token):
    """Execute one committed ticket; interrupted work is never automatically replayed."""
    con = _connect(database_path)
    try:
        con.execute("BEGIN IMMEDIATE")
        intent, bundle = _load_ticket(con, intent_id, operation_token)
        if intent["state"] in ("reported", "finished"):
            con.rollback()
            return _report(intent)
        if intent["state"] != "ready":
            _fail("uncertain", "executor already started; reconcile after explicit quiescence")
        con.execute("UPDATE v2_merge_intent SET state='started',executor_pid=?,started_at=?,"
                    "revision=revision+1 WHERE intent_id=?", (os.getpid(), _now(), intent_id))
        con.commit()
        try:
            _validate_bundle(con, bundle)
            report = {"build": _build, "stack": _build, "land": _land, "poll": _poll, "archive": _archive}[bundle["kind"]](con, bundle)
        except MergeError as exc:
            state = "blocked_environment" if exc.code in (
                "blocked_environment", "stale_epoch", "revision_conflict", "git_failed") else "failed"
            report = {"outcome": state, "error": str(exc)}
        # OSError/timeout/unknown exceptions deliberately retain started and target
        # lock: process termination alone does not prove descendants are quiescent.
        return _store_report(con, intent_id, operation_token, report)
    finally:
        con.close()


def recover_intent(database_path, intent_id, operation_token):
    """Inspect a controller-fenced interrupted operation. Never rerun Git effects."""
    con = _connect(database_path)
    try:
        intent, bundle = _load_ticket(con, intent_id, operation_token)
        if intent["state"] in ("reported", "finished"):
            return _report(intent)
        if intent["state"] != "started" or not intent["recovery_json"]:
            _fail("uncertain", "explicit controller quiescence authorization required")
        recovery = json.loads(intent["recovery_json"])
        _read_json(recovery["evidence_path"], recovery["evidence_sha256"])
        _check_policy_files(bundle["policy"])
        if bundle["kind"] == "land":
            candidate, policy = bundle["candidate"], bundle["policy"]
            actual = _git_text(policy, policy["repo_root"], "rev-parse", "--verify", bundle["spec"]["target_ref"])
            if actual == candidate["candidate_commit"]:
                _validate_bundle(con, bundle, check_epoch=False)
                _candidate_check(bundle)
                report = {"outcome": "landed", "merge_commit": actual, "method": "recovered_local_cas"}
            elif actual == candidate["expected_old_tip"]:
                report = {"outcome": "rebase_required", "error": "quiescent operation did not change target"}
            else:
                _fail("uncertain", "target is neither expected old nor reviewed candidate; manual reconciliation required")
        elif bundle["kind"] == "archive":
            accepted = bundle["accepted"]
            path = Path(bundle["policy"]["archive_root"]) / accepted["evidence"]["sealed_sha256"] / "manifest.json"
            if not path.exists():
                _fail("uncertain", "partial archive retained; no complete manifest to finalize")
            data = _bytes(path)
            manifest = json.loads(data)
            _validate_archive(bundle, manifest)
            report = {"outcome": "finalized", "manifest_path": str(path),
                      "manifest_sha256": hashlib.sha256(data).hexdigest(), "recovered": True}
        else:
            report = {"outcome": "blocked_environment",
                      "error": "quiescent interrupted operation retained; reserve a fresh intent"}
        return _store_report(con, intent_id, operation_token, report)
    finally:
        con.close()


def _validate_archive(bundle, manifest):
    accepted, policy = bundle["accepted"], bundle["policy"]
    if (manifest.get("task_id") != bundle["task_id"] or manifest.get("assignment_id") != accepted["assignment_id"]
            or manifest.get("generation") != accepted["generation"]
            or manifest.get("evidence_sha256") != accepted["evidence"]["sealed_sha256"]):
        _fail("EVIDENCE_CHANGED", "archive identity differs")
    original = accepted["manifest"]["files"]
    files = manifest.get("files")
    if not isinstance(files, list) or len(original) != len(files):
        _fail("EVIDENCE_CHANGED", "archive file set differs")
    for source, item in zip(original, files):
        if any(item.get(k) != v for k, v in source.items()):
            _fail("EVIDENCE_CHANGED", "archive entry differs")
        expected = Path(policy["archive_root"]) / accepted["evidence"]["sealed_sha256"] / source["sha256"]
        if Path(item["archive_path"]) != expected:
            _fail("EVIDENCE_CHANGED", "archive escaped content-addressed root")
        _sealed(expected, source["sha256"], source["bytes"])


def _finish(con, project, principal, payload):
    _fields(payload, ("intent_id", "expected_revision"), ("intent_id", "expected_revision"))
    intent, bundle = _intent(con, project, payload["intent_id"])
    _revision(intent, payload["expected_revision"])
    if intent["state"] != "reported":
        _fail("state_conflict", "finish requires a persisted, unfinished execution report")
    report = _report(intent)
    outcome = report["outcome"]
    task = _one(con, "SELECT * FROM v2_task WHERE task_id=? AND project_id=?", (intent["task_id"], project))
    _revision(task, bundle["task_revision"])
    if intent["kind"] != "stack" and task["task_state"] != "accepted":
        _fail("state_conflict", "Task acceptance changed during external operation")
    if outcome in ("verified", "landed", "finalized", "materialized"):
        # Parent authenticates the current controller. Finalizing a recorded
        # effect after epoch handover must not repeat the old external write.
        _validate_bundle(con, bundle, check_epoch=False)
    if intent["kind"] == "stack":
        state = outcome
        if outcome == "materialized":
            _record_materialization(con, project, intent["task_id"], bundle["spec"], report["candidate"])
    elif intent["kind"] == "archive":
        if outcome == "finalized":
            manifest = _read_json(report["manifest_path"], report["manifest_sha256"])
            _validate_archive(bundle, manifest)
            con.execute("INSERT INTO v2_artifact_finalization VALUES (?,?,?,?,?,?)",
                        (intent["task_id"], bundle["accepted"]["assignment_id"], intent["intent_id"],
                         report["manifest_path"], report["manifest_sha256"], _now()))
            con.execute("UPDATE v2_task SET task_state='finalized',revision=revision+1,updated_at=? WHERE task_id=?",
                        (_now(), intent["task_id"]))
        state = outcome
    else:
        row, spec = _mr(con, project, intent["merge_request_id"])
        if not row["holds_lock"]:
            _fail("state_conflict", "intent lost its target reservation")
        state = {"verified": "verifying", "rebase_required": "queued"}.get(outcome, outcome)
        if state not in ("verifying", "queued", "landed", "blocked_environment", "waiting_remote", "failed"):
            _fail("state_conflict", "unexpected execution outcome")
        if outcome == "verified":
            candidate = report["candidate"]
            _read_json(candidate["manifest_path"], candidate["manifest_sha256"])
            _sealed(candidate["verify_log_path"], candidate["verify_log_sha256"])
            con.execute("UPDATE v2_merge_request SET candidate_json=?,candidate_sha256=?,review_json=NULL "
                        "WHERE merge_request_id=?", (_json(candidate), _digest(candidate), row["merge_request_id"]))
            _record_materialization(con, project, row["task_id"], spec, candidate)
        elif outcome == "landed":
            if intent["kind"] not in ("land", "poll"):
                _fail("state_conflict", "only landing intent may land Task")
            if report.get("receipt_path"):
                _sealed(report["receipt_path"], report["receipt_sha256"])
            con.execute("UPDATE v2_task SET task_state='landed',revision=revision+1,updated_at=? WHERE task_id=?",
                        (_now(), row["task_id"]))
        elif outcome == "failed":
            con.execute("UPDATE v2_task SET task_state='repair_required',revision=revision+1,updated_at=? WHERE task_id=?",
                        (_now(), row["task_id"]))
        attempts = row["attempts"] + (1 if outcome in ("failed", "rebase_required") else 0)
        if outcome == "rebase_required" and attempts >= bundle["policy"]["max_attempts"]:
            state = "failed"
            con.execute("UPDATE v2_task SET task_state='repair_required',revision=revision+1,updated_at=? WHERE task_id=?",
                        (_now(), row["task_id"]))
        con.execute("UPDATE v2_merge_request SET state=?,holds_lock=0,attempts=?,merge_commit=?,landed_at=?,error=?,"
                    "revision=revision+1,updated_at=? WHERE merge_request_id=?",
                    (state, attempts, report.get("merge_commit"), _now() if state == "landed" else None,
                     report.get("error"), _now(), row["merge_request_id"]))
    con.execute("UPDATE v2_merge_intent SET state='finished',revision=revision+1 WHERE intent_id=?", (intent["intent_id"],))
    _event(con, project, principal, "merge_intent", intent["intent_id"], "merge.finished",
           {"outcome": outcome, "state": state, "task_id": intent["task_id"], "report_sha256": intent["report_sha256"]})
    return {"intent_id": intent["intent_id"], "revision": intent["revision"] + 1, "state": state,
            "task_state": _one(con, "SELECT task_state FROM v2_task WHERE task_id=?", (intent["task_id"],))["task_state"]}


def read_queue(con, project_id, principal, filters=None):
    """Pure projection. Workers see only requests for their own Assignments."""
    _principal(con, project_id, principal, ("controller", "reviewer", "observer", "worker"))
    filters = {} if filters is None else filters
    _fields(filters, ("task_id", "merge_request_id", "state", "limit"))
    limit = filters.get("limit", 100)
    if type(limit) is not int or not 1 <= limit <= 100:
        _fail("invalid_request", "limit must be 1..100")
    where, args = ["m.project_id=?"], [project_id]
    for field in ("task_id", "merge_request_id", "state"):
        if field in filters:
            where.append("m." + field + "=?")
            args.append(_text(filters[field], field))
    if principal["role"] == "worker":
        where.append("a.agent_id=?")
        args.append(principal["agent_id"])
    rows = _rows(con, "SELECT m.merge_request_id,m.task_id,m.assignment_id,m.repository_id,m.target_ref,"
        "m.state,m.revision,m.attempts,m.holds_lock,m.candidate_sha256,m.merge_commit,m.landed_at,m.error,m.candidate_json,m.spec_json "
                 "FROM v2_merge_request m JOIN v2_assignment a ON a.assignment_id=m.assignment_id WHERE " +
                 " AND ".join(where) + " ORDER BY m.created_at,m.merge_request_id LIMIT ?", tuple(args + [limit]))
    for row in rows:
        row["candidate"] = json.loads(row.pop("candidate_json") or "null")
        accepted = json.loads(row.pop("spec_json"))["accepted"]
        row.update({key: accepted["manifest"][key] for key in ("source_ref", "source_commit", "base_commit")})
        row["accepted_evidence_id"] = accepted["evidence"]["evidence_id"]
        row["intents"] = _rows(con, "SELECT intent_id,kind,state,revision FROM v2_merge_intent "
                              "WHERE merge_request_id=? ORDER BY created_at,intent_id", (row["merge_request_id"],))
    return rows


def read_intents(con, project_id, principal, task_id):
    """Pure recovery projection, including standalone stack/archive operations."""
    _principal(con, project_id, principal, ("controller", "reviewer", "observer", "worker"))
    _text(task_id, "task_id")
    if principal["role"] == "worker" and not _one(con,
            "SELECT 1 FROM v2_assignment WHERE task_id=? AND agent_id=?", (task_id, principal["agent_id"])):
        _fail("permission_denied", "Task is not owned by this worker")
    return _rows(con, "SELECT intent_id,task_id,merge_request_id,kind,state,revision,executor_pid,started_at "
                 "FROM v2_merge_intent WHERE project_id=? AND task_id=? ORDER BY created_at,intent_id",
                 (project_id, task_id))


def materialization_gate(con, project_id, task_id, base_commit):
    """Pure recorded stack proof, not a scheduler, worktree provisioner or Git call."""
    _oid(base_commit)
    if not _one(con, "SELECT 1 FROM v2_task WHERE project_id=? AND task_id=?", (project_id, task_id)):
        return {"ready": False, "reason": "unmaterialized_dependency"}
    rows = _rows(con, "SELECT * FROM v2_merge_materialization WHERE project_id=? AND task_id=? "
                 "AND base_commit=? ORDER BY created_at DESC", (project_id, task_id, base_commit))
    for row in rows:
        try:
            proof = json.loads(row["proof_json"])
            parents = json.loads(row["parents_json"])
            if _digest(proof) != row["proof_sha256"]:
                continue
            if [p["seal_sha256"] for p in parents] != proof["parent_sha256"]:
                continue
            for parent in parents:
                _refresh(con, project_id, parent, terminal=True)
            candidate = proof["candidate"]
            if candidate["stack_base"] != base_commit or candidate["task_id"] != task_id:
                continue
            manifest = _read_json(candidate["manifest_path"], candidate["manifest_sha256"])
            if manifest != {k: v for k, v in candidate.items() if k not in ("manifest_path", "manifest_sha256")}:
                continue
            _sealed(candidate["verify_log_path"], candidate["verify_log_sha256"])
            return {"ready": True, "base_commit": base_commit, "materialization_id": row["materialization_id"],
                    "materialized_from": [p["task_id"] for p in parents], "base_artifact_sha": row["proof_sha256"]}
        except (MergeError, KeyError, ValueError):
            continue
    return {"ready": False, "reason": "unmaterialized_dependency"}
