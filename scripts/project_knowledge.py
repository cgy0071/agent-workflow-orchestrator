"""Local knowledge and document proposals behind the AWO service boundary.

Only SQLite is authoritative. The caller owns authentication, envelopes,
transactions, replay, epoch fencing and error journaling. This module performs
no network operations, external writes, commits, or implicit migrations.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import uuid


SCHEMA_VERSION = 1
KINDS = ("preference", "convention", "constraint", "hazard", "decision", "fact", "reference")
SCOPES = ("personal", "project", "team")
ROLES = ("worker", "reviewer", "controller", "observer", "advisor")
MAX_CONTENT_BYTES = 256 * 1024
MAX_INLINE_EVIDENCE_BYTES = 64 * 1024
MAX_PAYLOAD_BYTES = 1024 * 1024
MAX_REFS = 16
DEFAULT_VERIFY_SECONDS = 30 * 24 * 60 * 60
SYNC_FORMAT = "awo.knowledge.proposal.v1"
COMMANDS = frozenset((
    "knowledge_propose", "knowledge_revise", "knowledge_review", "knowledge_promote",
    "knowledge_retire", "knowledge_source_approve", "knowledge_source_revoke",
    "knowledge_sync_import", "doc_import", "doc_revise", "doc_review", "doc_publish", "doc_retire",
))
QUERIES = frozenset((
    "knowledge_get", "knowledge_list", "knowledge_history", "knowledge_sync_export",
    "doc_get", "doc_list", "doc_history",
))


class KnowledgeError(ValueError):
    """A domain rejection with a stable, service-mappable code."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _fail(code, message):
    raise KnowledgeError(code, message)


def _now():
    return datetime.now(timezone.utc)


def _json(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        _fail("invalid_payload", "JSON values must be finite and serializable")


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _digest(value):
    return _sha(_json(value).encode("ascii"))


def _text(value, name, limit=1024, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()) or "\x00" in value:
        _fail("invalid_payload", name + " must be text")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        _fail("invalid_payload", name + " must be valid UTF-8")
    if size > limit:
        _fail("invalid_payload", name + " exceeds its byte limit")
    return value


def _hash(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        _fail("invalid_payload", "expected a lowercase SHA-256 digest")
    return value


def _integer(value, name, low=1, high=2147483647):
    if type(value) is not int or not low <= value <= high:
        _fail("invalid_payload", name + " must be an integer in range")
    return value


def _keys(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        _fail("invalid_payload", "missing or unsupported fields")


def _time(value):
    _text(value, "timestamp", 64)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            _fail("invalid_payload", "timestamp must include a timezone")
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        _fail("invalid_payload", "invalid or out-of-range ISO timestamp")


def _rows(con, sql, args=()):
    cur = con.execute(sql, args)
    names = [item[0] for item in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def _one(con, sql, args=()):
    rows = _rows(con, sql, args)
    return rows[0] if rows else None


def _transaction(con):
    if not con.in_transaction:
        _fail("transaction_required", "caller must begin a transaction; use BEGIN IMMEDIATE for writes")


def create_knowledge_schema(con):
    """Add only module tables in the caller's transaction; never commit it."""
    _transaction(con)
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='v2_knowledge_meta'").fetchone():
        version = con.execute("SELECT version FROM v2_knowledge_meta WHERE id=1").fetchone()
        if version and version[0] != SCHEMA_VERSION:
            _fail("schema_conflict", "unsupported knowledge schema version")
    statements = (
        "CREATE TABLE IF NOT EXISTS v2_knowledge_meta (id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL)",
        """CREATE TABLE IF NOT EXISTS v2_knowledge (
          entity_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES v2_project(project_id),
          entity_type TEXT NOT NULL CHECK(entity_type IN ('knowledge','doc')),
          owner_principal_id TEXT NOT NULL, owner_agent_id TEXT,
          scope TEXT NOT NULL CHECK(scope IN ('personal','project','team')),
          revision INTEGER NOT NULL CHECK(revision>0), content_revision INTEGER NOT NULL CHECK(content_revision>0),
          state TEXT NOT NULL CHECK(state IN ('proposed','reviewed','rejected','active','published','retired')),
          review_id TEXT, last_verified_at TEXT, retired_reason TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS v2_knowledge_project ON v2_knowledge(project_id,entity_type,entity_id)",
        """CREATE TABLE IF NOT EXISTS v2_knowledge_reader (
          entity_id TEXT NOT NULL REFERENCES v2_knowledge(entity_id), principal_id TEXT NOT NULL,
          PRIMARY KEY(entity_id,principal_id))""",
        """CREATE TABLE IF NOT EXISTS v2_knowledge_revision (
          entity_id TEXT NOT NULL REFERENCES v2_knowledge(entity_id), content_revision INTEGER NOT NULL,
          author_principal_id TEXT NOT NULL, author_agent_id TEXT, created_at TEXT NOT NULL,
          manifest_json TEXT NOT NULL, revision_sha256 TEXT NOT NULL, content_sha256 TEXT NOT NULL,
          provenance_json TEXT NOT NULL, PRIMARY KEY(entity_id,content_revision))""",
        """CREATE TABLE IF NOT EXISTS v2_knowledge_review (
          review_id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES v2_knowledge(entity_id),
          content_revision INTEGER NOT NULL, reviewer_principal_id TEXT NOT NULL, reviewer_agent_id TEXT,
          reviewer_role TEXT NOT NULL, verdict TEXT NOT NULL CHECK(verdict IN ('accept','reject')),
          revision_sha256 TEXT NOT NULL, evidence_sha256 TEXT NOT NULL,
          evidence_reviewed INTEGER NOT NULL CHECK(evidence_reviewed IN (0,1)),
          comment TEXT NOT NULL, reviewed_at TEXT NOT NULL,
          FOREIGN KEY(entity_id,content_revision) REFERENCES v2_knowledge_revision(entity_id,content_revision))""",
        """CREATE TABLE IF NOT EXISTS v2_knowledge_source (
          source_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES v2_project(project_id),
          relative_path TEXT NOT NULL, sha256 TEXT NOT NULL, approved_by TEXT NOT NULL,
          state TEXT NOT NULL CHECK(state IN ('approved','revoked')), revision INTEGER NOT NULL,
          created_at TEXT NOT NULL, revoked_reason TEXT,
          UNIQUE(project_id,relative_path,sha256))""",
        """CREATE TABLE IF NOT EXISTS v2_knowledge_audit (
          seq INTEGER PRIMARY KEY, project_id TEXT NOT NULL REFERENCES v2_project(project_id),
          entity_id TEXT NOT NULL, action TEXT NOT NULL, revision INTEGER NOT NULL,
          principal_id TEXT NOT NULL, agent_id TEXT, at TEXT NOT NULL, data_json TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS v2_knowledge_sync (
          project_id TEXT NOT NULL REFERENCES v2_project(project_id), envelope_sha256 TEXT NOT NULL,
          entity_id TEXT NOT NULL REFERENCES v2_knowledge(entity_id), envelope_json TEXT NOT NULL,
          imported_by TEXT NOT NULL, imported_at TEXT NOT NULL,
          PRIMARY KEY(project_id,envelope_sha256))""",
        """CREATE TRIGGER IF NOT EXISTS v2_knowledge_identity BEFORE UPDATE OF
          entity_id,project_id,entity_type,owner_principal_id,owner_agent_id,scope,created_at
          ON v2_knowledge BEGIN SELECT RAISE(ABORT,'immutable knowledge identity'); END""",
        """CREATE TRIGGER IF NOT EXISTS v2_knowledge_source_identity BEFORE UPDATE OF
          source_id,project_id,relative_path,sha256,approved_by,created_at ON v2_knowledge_source
          BEGIN SELECT RAISE(ABORT,'immutable source approval'); END""",
    )
    for sql in statements:
        con.execute(sql)
    review_columns = {row[1] for row in con.execute('PRAGMA table_info(v2_knowledge_review)')}
    for name, definition in (('reviewer_binding_id', 'TEXT'), ('reviewer_binding_version', 'INTEGER')):
        if name not in review_columns:
            con.execute('ALTER TABLE v2_knowledge_review ADD COLUMN ' + name + ' ' + definition)
    for table in ("v2_knowledge_revision", "v2_knowledge_review", "v2_knowledge_audit", "v2_knowledge_sync"):
        for operation in ("UPDATE", "DELETE"):
            con.execute("CREATE TRIGGER IF NOT EXISTS " + table + "_no_" + operation.lower() +
                        " BEFORE " + operation + " ON " + table +
                        " BEGIN SELECT RAISE(ABORT,'append-only knowledge history'); END")
    con.execute("INSERT OR IGNORE INTO v2_knowledge_meta VALUES (1,?)", (SCHEMA_VERSION,))


def _principal(con, project_id, principal):
    _text(project_id, "project_id", 256)
    if not isinstance(principal, dict):
        _fail("invalid_principal", "trusted principal mapping required")
    if principal.get("role") not in ROLES:
        _fail("invalid_principal", "unknown principal role")
    pid = principal.get("principal_id")
    aid = principal.get("agent_id")
    if not isinstance(pid, str) or not pid.strip() or len(pid) > 256 or "\x00" in pid:
        _fail("invalid_principal", "principal_id required")
    if aid is not None and (not isinstance(aid, str) or not aid.strip() or len(aid) > 256):
        _fail("invalid_principal", "invalid agent_id")
    if aid is None and principal["role"] == "worker":
        _fail("invalid_principal", "worker requires a registered agent identity")
    for value in (pid, aid):
        if value is not None:
            try:
                if len(value.encode("utf-8")) > 256 or any(ord(c) < 32 for c in value):
                    _fail("invalid_principal", "invalid principal identity text")
            except UnicodeError:
                _fail("invalid_principal", "principal identity must be valid UTF-8")
    if principal.get("project_id", project_id) != project_id:
        _fail("wrong_project", "principal belongs to another project")
    if not con.execute("SELECT 1 FROM v2_project WHERE project_id=?", (project_id,)).fetchone():
        _fail("wrong_project", "project does not exist")
    if aid is not None:
        agent = _one(con, "SELECT project_id,role,lifecycle FROM v2_agent WHERE agent_id=?", (aid,))
        if not agent or agent["project_id"] != project_id or agent["lifecycle"] != "active":
            _fail("invalid_principal", "agent is not active in this project")
        if principal.get('binding_id') is not None:
            import awo_service as service
            binding = principal['binding_id']
            version = principal.get('binding_version')
            if (principal['role'] not in ('controller', 'reviewer') or not isinstance(binding, str)
                    or type(version) is not int or service._agent_id_for_binding(binding) != aid):
                _fail('invalid_principal', 'invalid privileged binding identity')
            service.control.guard(con, binding, version, 'main',
                                  'review' if principal['role'] == 'reviewer' else 'dispatch')
        # The parent may narrow an active agent credential to read-only observer.
        if agent["role"] != principal["role"] and principal["role"] != "observer":
            # A current review-capable binding can narrow a controller Agent to
            # a scoped reviewer credential. Preserve its Agent identity for
            # independence; never replace it with an anonymous human principal.
            import awo_service as service
            binding = principal.get('binding_id')
            version = principal.get('binding_version')
            if (principal['role'] != 'reviewer' or not isinstance(binding, str)
                    or type(version) is not int or service._agent_id_for_binding(binding) != aid):
                _fail("invalid_principal", "agent role does not match the trusted principal")
            service.control.guard(con, binding, version, 'main', 'review')
    return {"principal_id": pid, "agent_id": aid, "role": principal["role"],
            "binding_id": principal.get('binding_id'), "binding_version": principal.get('binding_version')}


def _role(principal, roles):
    if principal["role"] not in roles:
        _fail("forbidden_role", "role cannot perform this operation")


def _same(principal, pid, aid):
    return principal["principal_id"] == pid or (aid is not None and principal["agent_id"] == aid)


def _version(con, item, number=None):
    row = _one(con, "SELECT * FROM v2_knowledge_revision WHERE entity_id=? AND content_revision=?",
               (item["entity_id"], number or item["content_revision"]))
    if not row:
        _fail("not_found", "content revision not found")
    manifest = json.loads(row["manifest_json"])
    provenance = json.loads(row["provenance_json"])
    if (_digest({"manifest": manifest, "provenance": provenance}) != row["revision_sha256"] or
            _sha(manifest["body"].encode("utf-8")) != row["content_sha256"]):
        _fail("integrity_error", "stored content hash mismatch")
    row["manifest"] = manifest
    row["provenance"] = provenance
    return row


def _visible(item, manifest, principal):
    return (item["scope"] != "personal" or
            _same(principal, item["owner_principal_id"], item["owner_agent_id"]) or
            (principal["role"] in ("reviewer", "controller") and
             principal["principal_id"] in manifest["reviewer_principal_ids"]))


def _item(con, project_id, principal, entity_id, entity_type):
    _text(entity_id, "entity_id", 256)
    item = _one(con, "SELECT * FROM v2_knowledge WHERE project_id=? AND entity_id=? AND entity_type=?",
                (project_id, entity_id, entity_type))
    if not item:
        _fail("not_found", "item not found in this project")
    version = _version(con, item)
    if not _visible(item, version["manifest"], principal):
        _fail("not_found", "item not visible to principal")
    return item, version


def _owner(item, principal):
    if not _same(principal, item["owner_principal_id"], item["owner_agent_id"]):
        if item["scope"] == "personal" or principal["role"] != "controller":
            _fail("forbidden_owner", "only the owner or a project controller can change this item")


def _independent(con, item, principal):
    authors = _rows(con, "SELECT author_principal_id,author_agent_id FROM v2_knowledge_revision WHERE entity_id=?",
                    (item["entity_id"],))
    if any(_same(principal, a["author_principal_id"], a["author_agent_id"]) for a in authors):
        _fail("independent_review_required", "reviewer/promoter must differ from every revision author")


def _expected(item, payload):
    expected = _integer(payload.get("expected_revision"), "expected_revision")
    if expected != item["revision"]:
        _fail("revision_conflict", "expected_revision is stale")
    if item.get("state") == "retired":
        _fail("state_conflict", "retired items cannot be changed or restored")


def _audit(con, project_id, entity_id, revision, principal, action, data):
    con.execute("INSERT INTO v2_knowledge_audit(project_id,entity_id,action,revision,principal_id,agent_id,at,data_json) "
                "VALUES (?,?,?,?,?,?,?,?)", (project_id, entity_id, action, revision,
                principal["principal_id"], principal["agent_id"], _now().isoformat(), _json(data)))


def _relative(value):
    _text(value, "relative_path", 2048)
    path = PurePosixPath(value)
    if (path.is_absolute() or "\\" in value or ":" in value or "//" in value or
            any(p.casefold() in ("", ".", "..", ".git") for p in value.split("/"))):
        _fail("unsafe_source", "source must be a normal project-relative POSIX path")
    for part in path.parts:
        if (part.endswith((".", " ")) or any(ord(c) < 32 for c in part) or
                re.fullmatch(r"(?i)(con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?", part)):
            _fail("unsafe_source", "device and ambiguous filenames are forbidden")
    return path


def _no_links(path):
    for part in reversed((path,) + tuple(path.parents)):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            _fail("unsafe_source", "symlink/junction/reparse sources are forbidden")


def _source_root(con, project_id):
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='v2_project_content_root'").fetchone():
        row = con.execute('SELECT root_path FROM v2_project_content_root WHERE project_id=?', (project_id,)).fetchone()
        if row:
            return row[0]
    row = con.execute('SELECT root_path FROM v2_project WHERE project_id=?', (project_id,)).fetchone()
    if not row:
        _fail('wrong_project', 'source project does not exist')
    return row[0]


def _file_bytes(con, project_id, relative_path):
    """Read a bounded approved local path; never follow links or device paths."""
    rel = _relative(relative_path)
    root = Path(_source_root(con, project_id))
    if not root.is_absolute() or str(root).startswith(("\\", "//")):
        _fail("unsafe_source", "project root must be an approved absolute local directory")
    path = root.joinpath(*rel.parts)
    try:
        _no_links(path)
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_CONTENT_BYTES:
            _fail("unsafe_source", "source must be a bounded regular file with one hardlink")
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                _fail("evidence_changed", "source changed during open")
            data = stream.read(MAX_CONTENT_BYTES + 1)
            after = os.fstat(stream.fileno())
        _no_links(path)
        final = path.lstat()
        # Windows lstat and fstat can expose different ctime semantics. Compare
        # ctime within the same API, while comparing identity/mtime across both.
        signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_nlink)
        if (len(data) > MAX_CONTENT_BYTES or
                not signature(before) == signature(opened) == signature(after) == signature(final) or
                before.st_ctime_ns != final.st_ctime_ns or opened.st_ctime_ns != after.st_ctime_ns):
            _fail("evidence_changed", "source changed during read")
        return data
    except OSError:
        _fail("evidence_unavailable", "approved source is unavailable")


def _source(con, project_id, source_id, sha256):
    row = _one(con, "SELECT * FROM v2_knowledge_source WHERE project_id=? AND source_id=?",
               (project_id, source_id))
    if not row or row["state"] != "approved" or row["sha256"] != sha256:
        _fail("evidence_unavailable", "source approval is missing, revoked, or for a different hash")
    data = _file_bytes(con, project_id, row["relative_path"])
    if _sha(data) != sha256:
        _fail("evidence_changed", "approved source bytes no longer match their hash")
    return data


def _refs(value):
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_REFS:
        _fail("invalid_payload", "source_refs requires 1..16 references")
    result = []
    for ref in value:
        if not isinstance(ref, dict):
            _fail("invalid_payload", "invalid source reference")
        kind = ref.get("kind")
        if not isinstance(kind, str):
            _fail("invalid_payload", "reference kind must be text")
        fields = {"inline": ("content", "label"), "file": ("source_id",),
                  "external": ("uri",), "evidence": ("evidence_id", "source_id"),
                  "doc": ("doc_id", "content_revision")}.get(kind)
        if fields is None:
            _fail("invalid_payload", "unsupported reference kind")
        _keys(ref, ("kind", "sha256") + fields, ("kind", "sha256") + fields)
        _hash(ref["sha256"])
        for key in fields:
            if key == "content_revision":
                _integer(ref[key], key)
            else:
                _text(ref[key], key, MAX_INLINE_EVIDENCE_BYTES if key == "content" else 2048)
        if kind == "inline" and _sha(ref["content"].encode("utf-8")) != ref["sha256"]:
            _fail("evidence_changed", "inline evidence hash mismatch")
        if kind == "external" and not re.match(r"^(https?://|urn:|awo-local:)", ref["uri"]):
            _fail("invalid_payload", "external references require an http(s), urn, or awo-local URI")
        result.append(dict(ref))
    if len({_digest(ref) for ref in result}) != len(result):
        _fail("invalid_payload", "duplicate source references")
    return result


def _manifest(con, project_id, data, entity_type):
    common = ("title", "scope", "expires_at", "verify_after_seconds", "reviewer_principal_ids")
    specific = (("kind", "body", "source", "source_refs", "confidence", "rationale", "alternatives")
                if entity_type == "knowledge" else ("content", "content_sha256", "source_id"))
    _keys(data, common + specific, ("title", "scope"))
    title = _text(data["title"], "title", 1024)
    scope = data["scope"]
    if scope not in SCOPES:
        _fail("invalid_payload", "invalid knowledge scope")
    reviewers = data.get("reviewer_principal_ids", [])
    if (not isinstance(reviewers, list) or len(reviewers) > 8 or
            any(not isinstance(p, str) or not p.strip() or len(p) > 256 for p in reviewers)):
        _fail("invalid_payload", "reviewer_principal_ids must contain at most eight principals")
    if len(set(reviewers)) != len(reviewers):
        _fail("invalid_payload", "duplicate personal reviewers")
    for reviewer in reviewers:
        _text(reviewer, "reviewer principal", 256)
    expiry = data.get("expires_at")
    if expiry is not None:
        expiry = _time(expiry).isoformat()
    seconds = _integer(data.get("verify_after_seconds", DEFAULT_VERIFY_SECONDS),
                       "verify_after_seconds", 1, 365 * 24 * 60 * 60)
    base = {"title": title, "scope": scope, "expires_at": expiry, "verify_after_seconds": seconds,
            "reviewer_principal_ids": reviewers}
    if entity_type == "doc":
        if ("content" in data) == ("source_id" in data):
            _fail("invalid_payload", "provide exactly one of content or approved source_id")
        if "source_id" in data:
            source_id = _text(data["source_id"], "source_id", 256)
            sha = _hash(data.get("content_sha256"))
            try:
                body = _source(con, project_id, source_id, sha).decode("utf-8")
            except UnicodeError:
                _fail("invalid_payload", "document source must be UTF-8 text")
            refs = [{"kind": "file", "source_id": source_id, "sha256": sha}]
        else:
            body = data["content"]
            refs = []
        _text(body, "content", MAX_CONTENT_BYTES)
        sha = _sha(body.encode("utf-8"))
        if "content_sha256" in data and _hash(data["content_sha256"]) != sha:
            _fail("evidence_changed", "document content hash mismatch")
        return dict(base, kind="document", body=body, source="doc", source_refs=refs,
                    confidence="unknown", rationale=None, alternatives=[])
    kind = data.get("kind")
    if kind not in KINDS or (kind == "convention" and scope == "personal"):
        _fail("invalid_payload", "invalid kind or personal convention")
    source = data.get("source")
    if source not in ("human", "agent", "doc", "review", "evidence", "external"):
        _fail("invalid_payload", "invalid provenance source")
    confidence = data.get("confidence", "unknown")
    if confidence not in ("high", "medium", "low", "unknown") or (kind == "fact" and confidence == "unknown"):
        _fail("invalid_payload", "fact requires a stated confidence; confidence is not proof")
    body = _text(data.get("body"), "body", MAX_INLINE_EVIDENCE_BYTES)
    refs = _refs(data.get("source_refs"))
    rationale = data.get("rationale")
    alternatives = data.get("alternatives", [])
    if rationale is not None:
        _text(rationale, "rationale", 4096)
    if not isinstance(alternatives, list) or len(alternatives) > 16:
        _fail("invalid_payload", "alternatives must be a bounded list")
    for alternative in alternatives:
        _text(alternative, "alternative", 1024)
    if kind == "decision" and (not rationale or not alternatives):
        _fail("invalid_payload", "decision requires rationale and alternatives")
    # A fact must be backed by at least one reference replayable from this
    # project's bytes. `source` is a client-declared label, never authenticated
    # authorship: no label, including "human", substitutes for evidence. Inline
    # text and external hash claims are assertions, not replayable bytes, so the
    # rule applies to every declared source rather than only to "agent".
    if kind == "fact" and not any(ref["kind"] in ("file", "evidence", "doc") for ref in refs):
        _fail("evidence_required",
              "a fact needs replayable local evidence (file, evidence, or doc); "
              "the declared source label is not proof")
    return dict(base, kind=kind, body=body, source=source, source_refs=refs,
                confidence=confidence, rationale=rationale, alternatives=alternatives)


def _evidence(con, project_id, principal, item, version, require_proof=False):
    """Revalidate every local reference, including bytes, at each review gate."""
    manifest = version["manifest"]
    proofs = 1 if item["entity_type"] == "doc" else 0
    refs = manifest["source_refs"]
    for ref in refs:
        kind = ref["kind"]
        if kind == "inline":
            if _sha(ref["content"].encode("utf-8")) != ref["sha256"]:
                _fail("evidence_changed", "inline evidence hash mismatch")
        elif kind in ("file", "evidence"):
            _source(con, project_id, ref["source_id"], ref["sha256"])
            if kind == "evidence":
                evidence = _one(con, "SELECT e.*,t.project_id,a.agent_id AS producer,a.task_id AS producer_task,"
                                "g.project_id AS producer_project FROM v2_evidence e "
                                "JOIN v2_task t ON t.task_id=e.task_id LEFT JOIN v2_assignment a "
                                "ON a.assignment_id=e.assignment_id LEFT JOIN v2_agent g "
                                "ON g.agent_id=a.agent_id WHERE e.evidence_id=?", (ref["evidence_id"],))
                source = _one(con, "SELECT relative_path FROM v2_knowledge_source WHERE source_id=?", (ref["source_id"],))
                root = _source_root(con, project_id)
                expected_path = os.path.normcase(os.path.abspath(str(Path(root) / source["relative_path"])))
                actual_path = os.path.normcase(os.path.abspath(str(Path(root) / evidence["path"]))) if evidence else None
                if (not evidence or evidence["project_id"] != project_id or evidence["state"] != "sealed" or
                        evidence["sealed"] != 1 or evidence["sealed_sha256"] != ref["sha256"] or
                        evidence["current_sha256"] != ref["sha256"] or actual_path != expected_path or
                        (evidence["assignment_id"] is not None and
                         (evidence["producer_task"] != evidence["task_id"] or evidence["producer_project"] != project_id))):
                    _fail("evidence_changed", "registered evidence is not sealed for this project/source/hash")
                if require_proof and evidence["producer"] and principal["agent_id"] == evidence["producer"]:
                    _fail("independent_review_required", "evidence producer cannot review its own evidence")
        elif kind == "doc":
            doc, doc_version = _item(con, project_id, principal, ref["doc_id"], "doc")
            if (doc["state"] != "published" or doc["content_revision"] != ref["content_revision"] or
                    doc_version["content_sha256"] != ref["sha256"] or
                    _projection(con, project_id, principal, doc, doc_version)["decay"] != "fresh"):
                _fail("evidence_changed", "document evidence is not the current fresh published hash")
            if require_proof:
                _independent(con, doc, principal)
        # Only bytes this project can replay count as proof for a Fact. Inline
        # text supplied by the submitter and external hash claims are
        # assertions; they never satisfy the fact evidence requirement.
        if kind in ("file", "evidence", "doc"):
            proofs += 1
    if require_proof and manifest["kind"] == "fact" and not proofs:
        _fail("evidence_required", "fact needs locally verifiable evidence, not external hash claims alone")
    return _digest({"content_sha256": version["content_sha256"], "source_refs": refs})


def _result(item, version):
    key = "knowledge_id" if item["entity_type"] == "knowledge" else "doc_id"
    return {key: item["entity_id"], "entity_id": item["entity_id"], "project_id": item["project_id"],
            "revision": item["revision"], "content_revision": item["content_revision"], "state": item["state"],
            "revision_sha256": version["revision_sha256"], "content_sha256": version["content_sha256"],
            "review_id": item["review_id"]}


def _projection(con, project_id, principal, item, version, full=False):
    manifest = version["manifest"]
    result = _result(item, version)
    reasons = []
    try:
        evidence_sha = _evidence(con, project_id, principal, item, version)
    except KnowledgeError as exc:
        evidence_sha = None
        reasons.append(exc.code)
    now = _now()
    expired = manifest["expires_at"] is not None and now >= _time(manifest["expires_at"])
    if expired:
        reasons.append("expired")
    if item["last_verified_at"] and now >= _time(item["last_verified_at"]) + timedelta(seconds=manifest["verify_after_seconds"]):
        reasons.append("verification_due")
    decay = "expired" if expired else ("stale" if reasons else "fresh")
    usable = item["state"] in ("active", "published") and decay == "fresh"
    review = None
    if item["review_id"]:
        review = _one(con, "SELECT reviewer_principal_id,reviewer_agent_id,reviewer_role,verdict,"
                           "reviewed_at FROM v2_knowledge_review WHERE review_id=? AND entity_id=?",
                      (item["review_id"], item["entity_id"]))
    provenance = version["provenance"]
    submitted = provenance.get("submitted_by")
    result.update({"kind": manifest["kind"], "title": manifest["title"], "scope": item["scope"],
                   "source": manifest["source"], "confidence": manifest["confidence"],
                   "confidence_is_proof": False, "expires_at": manifest["expires_at"],
                   "verify_after_seconds": manifest["verify_after_seconds"],
                   # `source` is declared, never authenticated: no verified
                   # mechanism binds a human author to a submission.
                   "source_authenticated": False,
                   "source_assertion": provenance.get("source_assertion"),
                   "submitted_by": submitted,
                   "provenance_class": ("authenticated-submission" if submitted
                                        else "legacy-unknown"),
                   "author_principal_id": version["author_principal_id"],
                   "author_agent_id": version["author_agent_id"], "review": review,
                   "decay": decay, "stale_reasons": reasons, "usable": usable,
                   "effective_state": "stale" if reasons and item["state"] in ("active", "published") else item["state"],
                   "trust": "confirmed" if usable else ("human" if item["owner_agent_id"] is None else "agent"),
                   "source_trust": "untrusted" if manifest["source"] == "external" or
                   version["provenance"].get("untrusted") or
                   any(ref["kind"] == "external" for ref in manifest["source_refs"]) else "local",
                   "last_verified_at": item["last_verified_at"], "evidence_sha256": evidence_sha,
                   "owner_principal_id": item["owner_principal_id"], "created_at": item["created_at"],
                   "updated_at": item["updated_at"], "retired_reason": item["retired_reason"]})
    if full:
        result.update({"manifest": manifest, "provenance": version["provenance"]})
    return result


def _insert_version(con, item, principal, manifest, provenance):
    sha = _digest({"manifest": manifest, "provenance": provenance})
    content_sha = _sha(manifest["body"].encode("utf-8"))
    con.execute("INSERT INTO v2_knowledge_revision VALUES (?,?,?,?,?,?,?,?,?)",
                (item["entity_id"], item["content_revision"], principal["principal_id"], principal["agent_id"],
                 _now().isoformat(), _json(manifest), sha, content_sha, _json(provenance)))
    con.execute("DELETE FROM v2_knowledge_reader WHERE entity_id=?", (item["entity_id"],))
    for pid in manifest["reviewer_principal_ids"]:
        con.execute("INSERT INTO v2_knowledge_reader VALUES (?,?)", (item["entity_id"], pid))
    return _version(con, item)


def _submission(principal, manifest, record=None, **extra):
    """Server-derived record of who submitted this revision and what they asserted.

    ``submitted_by`` comes from the authenticated principal mapping, never from
    request fields. ``source`` is a client-declared label: no verified mechanism
    establishes human authorship, so it is recorded as an unverified assertion
    that cannot bypass the replayable-evidence requirement for facts.
    """
    record = dict(record or {})
    record.pop("submitted_by", None)
    record.pop("source_assertion", None)
    record.update({
        "submitted_by": {"principal_id": principal["principal_id"],
                         "agent_id": principal["agent_id"],
                         "role": principal["role"],
                         "binding_id": principal.get("binding_id"),
                         "binding_version": principal.get("binding_version")},
        "source_assertion": {"declared": manifest["source"], "authenticated": False}})
    record.update(extra)
    return record


def _create(con, project_id, principal, entity_type, manifest, provenance):
    entity_id = ("kn-" if entity_type == "knowledge" else "doc-") + uuid.uuid4().hex
    now = _now().isoformat()
    # All validation precedes writes; the parent still must roll back any failure.
    item = {"entity_id": entity_id, "project_id": project_id, "entity_type": entity_type,
            "owner_principal_id": principal["principal_id"], "owner_agent_id": principal["agent_id"],
            "scope": manifest["scope"], "revision": 1, "content_revision": 1,
            "state": "proposed", "review_id": None, "last_verified_at": None,
            "retired_reason": None, "created_at": now, "updated_at": now}
    tentative = {"manifest": manifest, "content_sha256": _sha(manifest["body"].encode("utf-8"))}
    _evidence(con, project_id, principal, item, tentative)
    columns = tuple(item)
    con.execute("INSERT INTO v2_knowledge (" + ",".join(columns) + ") VALUES (" +
                ",".join("?" for _ in columns) + ")", tuple(item[c] for c in columns))
    version = _insert_version(con, item, principal, manifest, provenance)
    _audit(con, project_id, entity_id, 1, principal, entity_type + ".proposed", _result(item, version))
    return _result(item, version)


def _advance(con, item, **changes):
    changes.update(revision=item["revision"] + 1, updated_at=_now().isoformat())
    cur = con.execute("UPDATE v2_knowledge SET " + ",".join(k + "=?" for k in changes) +
                      " WHERE entity_id=? AND project_id=? AND revision=?",
                      tuple(changes.values()) + (item["entity_id"], item["project_id"], item["revision"]))
    if cur.rowcount != 1:
        _fail("revision_conflict", "item changed concurrently")
    item.update(changes)


def _source_command(con, project_id, principal, tool, payload):
    _role(principal, ("controller",))
    if tool == "knowledge_source_approve":
        _keys(payload, ("relative_path", "sha256", "expected_revision"), ("relative_path", "sha256"))
        _integer(payload.get("expected_revision", 0), "expected_revision", 0, 0)
        relative = str(_relative(payload["relative_path"]))
        sha = _hash(payload["sha256"])
        if _sha(_file_bytes(con, project_id, relative)) != sha:
            _fail("evidence_changed", "source approval does not match file bytes")
        if con.execute("SELECT 1 FROM v2_knowledge_source WHERE project_id=? AND relative_path=? AND sha256=?",
                       (project_id, relative, sha)).fetchone():
            _fail("state_conflict", "this source hash already has an approval record")
        source_id = "src-" + uuid.uuid4().hex
        con.execute("INSERT INTO v2_knowledge_source VALUES (?,?,?,?,?,'approved',1,?,NULL)",
                    (source_id, project_id, relative, sha, principal["principal_id"], _now().isoformat()))
        result = {"source_id": source_id, "revision": 1, "state": "approved", "sha256": sha}
    else:
        _keys(payload, ("source_id", "expected_revision", "reason"), ("source_id", "expected_revision", "reason"))
        reason = _text(payload["reason"], "reason", 4096)
        row = _one(con, "SELECT * FROM v2_knowledge_source WHERE project_id=? AND source_id=?",
                   (project_id, _text(payload["source_id"], "source_id", 256)))
        if not row:
            _fail("not_found", "source not found in this project")
        _expected(row, payload)
        if row["state"] != "approved":
            _fail("state_conflict", "source already revoked")
        con.execute("UPDATE v2_knowledge_source SET state='revoked',revision=revision+1,revoked_reason=? WHERE source_id=?",
                    (reason, row["source_id"]))
        result = {"source_id": row["source_id"], "revision": row["revision"] + 1, "state": "revoked", "reason": reason}
    _audit(con, project_id, result["source_id"], result["revision"], principal, tool, result)
    return result


def _review(con, project_id, principal, item, version, payload):
    _role(principal, ("reviewer", "controller"))
    _independent(con, item, principal)
    if item["state"] not in ("proposed", "reviewed", "rejected", "active", "published"):
        _fail("state_conflict", "item cannot be reviewed")
    verdict = payload.get("verdict")
    if verdict not in ("accept", "reject"):
        _fail("invalid_payload", "review verdict must be accept or reject")
    comment = _text(payload.get("comment"), "comment", 4096)
    checked = payload.get("evidence_reviewed", False)
    if type(checked) is not bool or (verdict == "accept" and not checked):
        _fail("evidence_required", "acceptance requires explicit evidence_reviewed=true")
    if _hash(payload.get("revision_sha256")) != version["revision_sha256"]:
        _fail("revision_conflict", "reviewed revision hash differs from current content")
    evidence_sha = _evidence(con, project_id, principal, item, version, require_proof=verdict == "accept")
    if _hash(payload.get("evidence_sha256")) != evidence_sha:
        _fail("evidence_changed", "reviewed evidence hash differs from current evidence")
    if verdict == "accept" and _projection(con, project_id, principal, item, version)["decay"] == "expired":
        _fail("stale_knowledge", "expired content requires a new revision")
    review_id = "kr-" + uuid.uuid4().hex
    now = _now().isoformat()
    con.execute("INSERT INTO v2_knowledge_review VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (review_id, item["entity_id"], item["content_revision"], principal["principal_id"],
                 principal["agent_id"], principal["role"], verdict, version["revision_sha256"],
                 evidence_sha, int(checked), comment, now,
                 principal.get('binding_id'), principal.get('binding_version')))
    _advance(con, item, state="reviewed" if verdict == "accept" else "rejected", review_id=review_id,
             last_verified_at=now if verdict == "accept" else None)


def _promote(con, project_id, principal, item, version, payload):
    is_doc = item["entity_type"] == "doc"
    _role(principal, ("controller",) if is_doc else ("controller", "reviewer"))
    if not is_doc:
        _independent(con, item, principal)
    review = _one(con, "SELECT * FROM v2_knowledge_review WHERE review_id=? AND entity_id=?",
                  (_text(payload.get("review_id"), "review_id", 256), item["entity_id"]))
    if (item["state"] != "reviewed" or not review or review["review_id"] != item["review_id"] or
            review["content_revision"] != item["content_revision"] or review["verdict"] != "accept" or
            review["evidence_reviewed"] != 1):
        _fail("independent_review_required", "current content needs an accepted independent review")
    reviewer = _principal(con, project_id, {"principal_id": review["reviewer_principal_id"],
                          "agent_id": review["reviewer_agent_id"], "role": review["reviewer_role"],
                          "binding_id": review.get('reviewer_binding_id'),
                          "binding_version": review.get('reviewer_binding_version')})
    _independent(con, item, reviewer)
    if (_hash(payload.get("revision_sha256")) != version["revision_sha256"] or
            review["revision_sha256"] != version["revision_sha256"]):
        _fail("revision_conflict", "promotion targets a different content revision")
    if is_doc and _hash(payload.get("content_sha256")) != version["content_sha256"]:
        _fail("evidence_changed", "publish must name the reviewed document content hash")
    if _evidence(con, project_id, reviewer, item, version, require_proof=True) != review["evidence_sha256"]:
        _fail("evidence_changed", "evidence changed since review")
    if _projection(con, project_id, principal, item, version)["decay"] != "fresh":
        _fail("stale_knowledge", "content or verification has expired")
    _advance(con, item, state="published" if is_doc else "active")


def _sync_import(con, project_id, principal, payload):
    _keys(payload, ("proposal", "expected_revision"), ("proposal",))
    _integer(payload.get("expected_revision", 0), "expected_revision", 0, 0)
    envelope = payload["proposal"]
    fields = ("format", "project_id", "origin", "entry", "sha256")
    _keys(envelope, fields, fields)
    if envelope["format"] != SYNC_FORMAT or envelope["project_id"] != project_id:
        _fail("wrong_project", "sync format/project does not match")
    sha = _hash(envelope["sha256"])
    if _digest({k: envelope[k] for k in fields if k != "sha256"}) != sha:
        _fail("integrity_error", "sync proposal envelope hash mismatch")
    origin = envelope["origin"]
    origin_keys = ("entity_id", "content_revision", "revision_sha256", "exported_by", "exported_at")
    _keys(origin, origin_keys, origin_keys)
    _text(origin["entity_id"], "origin entity", 256)
    _integer(origin["content_revision"], "origin revision")
    _hash(origin["revision_sha256"])
    _text(origin["exported_by"], "origin exporter", 256)
    _time(origin["exported_at"])
    entry = envelope["entry"]
    if not isinstance(entry, dict) or entry.get("scope") != "team" or entry.get("source") != "external":
        _fail("invalid_payload", "sync only accepts untrusted team proposals")
    manifest = _manifest(con, project_id, entry, "knowledge")
    if any(ref["kind"] not in ("inline", "external") for ref in manifest["source_refs"]):
        _fail("unsafe_source", "sync cannot import local source, evidence, or document identifiers")
    if manifest["reviewer_principal_ids"]:
        _fail("invalid_payload", "remote proposals cannot assign local reviewers")
    if con.execute("SELECT 1 FROM v2_knowledge_sync WHERE project_id=? AND envelope_sha256=?", (project_id, sha)).fetchone():
        _fail("state_conflict", "sync proposal already imported; use parent command replay")
    # The same origin revision arriving with different bytes is an unresolved
    # conflict, not an authenticated update. Record it visibly; both copies stay
    # separate untrusted proposals and this module performs no resolution.
    origin_conflict = False
    for (packet_json,) in con.execute("SELECT envelope_json FROM v2_knowledge_sync WHERE project_id=?",
                                      (project_id,)).fetchall():
        try:
            prior_origin = json.loads(packet_json).get("origin") or {}
        except (TypeError, ValueError, AttributeError):
            continue
        if (prior_origin.get("entity_id") == origin["entity_id"] and
                prior_origin.get("content_revision") == origin["content_revision"]):
            origin_conflict = True
            break
    result = _create(con, project_id, principal, "knowledge", manifest,
                     _submission(principal, manifest,
                                 {"type": "sync", "untrusted": True,
                                  "envelope_sha256": sha, "origin": origin}))
    con.execute("INSERT INTO v2_knowledge_sync VALUES (?,?,?,?,?,?)",
                (project_id, sha, result["entity_id"], _json(envelope), principal["principal_id"], _now().isoformat()))
    result["sync"] = {"envelope_sha256": sha, "origin": origin, "origin_conflict": origin_conflict}
    _audit(con, project_id, result["entity_id"], result["revision"], principal, "knowledge.sync_imported",
           {"envelope_sha256": sha, "origin": origin, "untrusted": True,
            "origin_conflict": origin_conflict})
    return result


def apply_command(con, project_id, principal, tool, payload):
    """Apply a domain command. Caller must roll back the transaction on error."""
    _transaction(con)
    principal = _principal(con, project_id, principal)
    _role(principal, ("worker", "reviewer", "controller"))
    tool = _text(tool, "tool", 80).removeprefix("awo_")
    if tool not in COMMANDS:
        _fail("unknown_command", "unknown knowledge command")
    if not isinstance(payload, dict) or len(_json(payload).encode("ascii")) > MAX_PAYLOAD_BYTES:
        _fail("invalid_payload", "payload exceeds the bounded JSON contract")
    if tool in ("knowledge_source_approve", "knowledge_source_revoke"):
        return _source_command(con, project_id, principal, tool, payload)
    if tool == "knowledge_sync_import":
        return _sync_import(con, project_id, principal, payload)
    entity_type = "doc" if tool.startswith("doc_") else "knowledge"
    key = "doc_id" if entity_type == "doc" else "knowledge_id"
    action = tool.split("_", 1)[1]
    if action in ("propose", "import"):
        _integer(payload.get("expected_revision", 0), "expected_revision", 0, 0)
        data = {k: v for k, v in payload.items() if k != "expected_revision"}
        manifest = _manifest(con, project_id, data, entity_type)
        return _create(con, project_id, principal, entity_type, manifest,
                       _submission(principal, manifest, {"type": "local"}))
    item, version = _item(con, project_id, principal, payload.get(key), entity_type)
    _expected(item, payload)
    if action == "revise":
        _owner(item, principal)
        data = {k: v for k, v in payload.items() if k not in (key, "expected_revision")}
        manifest = _manifest(con, project_id, data, entity_type)
        if manifest["scope"] != item["scope"]:
            _fail("forbidden_owner", "scope is immutable; create a separately reviewed proposal")
        tentative = {"manifest": manifest, "content_sha256": _sha(manifest["body"].encode("utf-8"))}
        _evidence(con, project_id, principal, item, tentative)
        provenance = _submission(principal, manifest, version["provenance"],
                                 supersedes_sha256=version["revision_sha256"])
        _advance(con, item, content_revision=item["content_revision"] + 1, state="proposed",
                 review_id=None, last_verified_at=None)
        version = _insert_version(con, item, principal, manifest, provenance)
    elif action == "review":
        _keys(payload, (key, "expected_revision", "verdict", "comment", "evidence_reviewed",
                        "revision_sha256", "evidence_sha256"))
        _review(con, project_id, principal, item, version, payload)
    elif action in ("promote", "publish"):
        fields = (key, "expected_revision", "review_id", "revision_sha256")
        _keys(payload, fields + (("content_sha256",) if entity_type == "doc" else ()))
        _promote(con, project_id, principal, item, version, payload)
    else:
        _keys(payload, (key, "expected_revision", "reason"), (key, "expected_revision", "reason"))
        _owner(item, principal)
        _advance(con, item, state="retired", retired_reason=_text(payload["reason"], "reason", 4096))
    result = _result(item, version)
    _audit(con, project_id, item["entity_id"], item["revision"], principal, tool, result)
    return result


def _sync_export(con, project_id, principal, item, version):
    if item["scope"] != "team":
        _fail("forbidden_owner", "only team proposals can be exported")
    manifest = version["manifest"]
    entry = {k: manifest[k] for k in ("kind", "title", "body", "scope", "confidence", "expires_at",
                                    "verify_after_seconds", "rationale", "alternatives")}
    refs = []
    for ref in manifest["source_refs"]:
        if ref["kind"] in ("inline", "external"):
            refs.append(dict(ref))
        else:
            ref_id = ref.get("source_id", ref.get("doc_id"))
            refs.append({"kind": "external", "uri": "awo-local:" + project_id + ":" + ref_id, "sha256": ref["sha256"]})
    # Different local references may reduce to one external pointer.
    entry.update(source="external", source_refs=list({_digest(r): r for r in refs}.values()))
    envelope = {"format": SYNC_FORMAT, "project_id": project_id, "entry": entry,
                "origin": {"entity_id": item["entity_id"], "content_revision": item["content_revision"],
                           "revision_sha256": version["revision_sha256"],
                           "exported_by": principal["principal_id"], "exported_at": _now().isoformat()}}
    envelope["sha256"] = _digest(envelope)
    return {"proposal": envelope, "authenticated_remote": False, "auto_promote": False}


def query(con, project_id, principal, query, payload):
    """Read-only projection: no schema creation, recall counters, or expiry writes."""
    principal = _principal(con, project_id, principal)
    query = _text(query, "query", 80).removeprefix("awo_")
    if query not in QUERIES:
        _fail("unknown_query", "unknown knowledge query")
    if not isinstance(payload, dict) or len(_json(payload).encode("ascii")) > MAX_PAYLOAD_BYTES:
        _fail("invalid_payload", "invalid query payload")
    entity_type = "doc" if query.startswith("doc_") else "knowledge"
    key = "doc_id" if entity_type == "doc" else "knowledge_id"
    if query.endswith("_list"):
        _keys(payload, ("limit", "after_id", "scope", "kind"))
        limit = _integer(payload.get("limit", 50), "limit", 1, 100)
        after = _text(payload.get("after_id", ""), "after_id", 256, empty=True)
        if "scope" in payload and payload["scope"] not in SCOPES:
            _fail("invalid_payload", "invalid scope filter")
        if "kind" in payload and payload["kind"] not in KINDS:
            _fail("invalid_payload", "invalid kind filter")
        # Apply privacy before pagination; even cursors must not disclose private IDs.
        rows = _rows(con, "SELECT k.* FROM v2_knowledge k WHERE project_id=? AND entity_type=? AND entity_id>? "
                     "AND (scope!='personal' OR owner_principal_id=? OR owner_agent_id=? OR "
                     "(? IN ('reviewer','controller') AND EXISTS (SELECT 1 FROM v2_knowledge_reader r "
                     "WHERE r.entity_id=k.entity_id AND r.principal_id=?))) ORDER BY entity_id LIMIT ?",
                     (project_id, entity_type, after, principal["principal_id"], principal["agent_id"],
                      principal["role"], principal["principal_id"], limit))
        result = []
        for item in rows:
            version = _version(con, item)
            manifest = version["manifest"]
            if (_visible(item, manifest, principal) and payload.get("scope", item["scope"]) == item["scope"] and
                    payload.get("kind", manifest["kind"]) == manifest["kind"]):
                result.append(_projection(con, project_id, principal, item, version))
        return {"items": result, "next_after_id": rows[-1]["entity_id"] if len(rows) == limit else None}
    if query.endswith("_history"):
        _keys(payload, (key, "limit", "after_seq"), (key,))
    elif query.endswith("_get"):
        _keys(payload, (key, "content_revision"), (key,))
    else:
        _keys(payload, (key,), (key,))
    item, version = _item(con, project_id, principal, payload[key], entity_type)
    if query == "knowledge_sync_export":
        return _sync_export(con, project_id, principal, item, version)
    if query.endswith("_history"):
        limit = _integer(payload.get("limit", 50), "limit", 1, 100)
        after = _integer(payload.get("after_seq", 0), "after_seq", 0)
        events = _rows(con, "SELECT * FROM v2_knowledge_audit WHERE project_id=? AND entity_id=? AND seq>? "
                       "ORDER BY seq LIMIT ?", (project_id, item["entity_id"], after, limit))
        for event in events:
            event["data"] = json.loads(event.pop("data_json"))
            rid = event["data"].get("review_id")
            if rid:
                event["review"] = _one(con, "SELECT * FROM v2_knowledge_review WHERE review_id=? AND entity_id=?",
                                       (rid, item["entity_id"]))
        return {"events": events, "next_after_seq": events[-1]["seq"] if len(events) == limit else None}
    requested = _integer(payload.get("content_revision", item["content_revision"]), "content_revision")
    if requested != item["content_revision"]:
        version = _version(con, item, requested)
        historical = dict(item, content_revision=requested, state="proposed", review_id=None, last_verified_at=None)
        result = _projection(con, project_id, principal, historical, version, full=True)
        result.update(state="historical", effective_state="historical", usable=False,
                      current_content_revision=item["content_revision"])
        return result
    return _projection(con, project_id, principal, item, version, full=True)
