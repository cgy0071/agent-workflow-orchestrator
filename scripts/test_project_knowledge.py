"""Offline real-SQLite tests, separated into P1 minimum and P2 extras.

These exercise the sidecar domain contract, not parent-service acceptance.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import project_knowledge as knowledge


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def packet_hash(packet):
    data = {k: v for k, v in packet.items() if k != "sha256"}
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=True, allow_nan=False).encode("ascii")).hexdigest()


class KnowledgeFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="awo-knowledge-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = "project-one"
        self.other = "project-two"
        self.db = self.root / "state.sqlite3"
        self.con = sqlite3.connect(self.db, isolation_level=None)
        self.con.execute("PRAGMA foreign_keys=ON")
        self.addCleanup(self.con.close)
        self.con.execute("BEGIN IMMEDIATE")
        # Use the real accepted service DDL, without invoking migration or routing.
        import awo_service
        for statement in awo_service.DDL:
            self.con.execute(statement)
        knowledge.create_knowledge_schema(self.con)
        self.con.executemany("INSERT INTO v2_project VALUES (?,?,?)", [
            (self.project, str(self.root), "2026-11-10T00:00:00+00:00"),
            (self.other, str(self.root / "other"), "2026-11-10T00:00:00+00:00")])
        self.worker = self.agent("writer", "worker")
        self.worker2 = self.agent("writer-two", "worker")
        self.reviewer = self.agent("reviewer", "reviewer")
        self.reviewer2 = self.agent("reviewer-two", "reviewer")
        self.controller = self.agent("controller", "controller")
        self.observer = self.agent("observer", "observer")
        self.human = {"role": "controller", "principal_id": "human-controller", "agent_id": None}
        self.con.commit()
        # Every Fact needs replayable local bytes. Pre-approve one fixture file
        # through the real controller path so the default Fact cites it.
        self._default_source = None
        self._default_source = self.approve_file()

    def agent(self, name, role, project_id=None):
        project_id = project_id or self.project
        self.con.execute("INSERT INTO v2_agent(agent_id,project_id,handle,role,harness,harness_version,host,os,"
                         "lifecycle,source,created_at) VALUES (?,?,?,?,?,?,?,?,'active','test',?)",
                         (name, project_id, name, role, "offline", "test", "local", "test",
                          "2026-11-10T00:00:00+00:00"))
        return {"role": role, "principal_id": "principal:" + name, "agent_id": name}

    def command(self, tool, payload, principal=None, project_id=None):
        self.con.execute("BEGIN IMMEDIATE")
        try:
            result = knowledge.apply_command(self.con, project_id or self.project,
                                             principal or self.worker, tool, payload)
            self.assertTrue(self.con.in_transaction)
            self.con.commit()
            return result
        except BaseException:
            self.con.rollback()
            raise

    def read(self, item, principal=None, project_id=None):
        is_doc = "doc_id" in item
        key = "doc_id" if is_doc else "knowledge_id"
        return knowledge.query(self.con, project_id or self.project, principal or self.worker,
                               "doc_get" if is_doc else "knowledge_get", {key: item[key]})

    def fails(self, code, callback, *args, **kwargs):
        before = self.con.execute("SELECT COUNT(*) FROM v2_knowledge_audit").fetchone()[0]
        with self.assertRaises(knowledge.KnowledgeError) as caught:
            callback(*args, **kwargs)
        self.assertEqual(code, caught.exception.code, str(caught.exception))
        self.assertEqual(before, self.con.execute("SELECT COUNT(*) FROM v2_knowledge_audit").fetchone()[0])

    def inline(self, content="Observed output: 2 + 2 = 4"):
        return {"kind": "inline", "content": content, "label": "offline observation", "sha256": sha(content)}

    def evidence_ref(self):
        """A replayable, project-approved file reference for the fixture Fact.

        Every fact must be backed by local bytes; the default fixture therefore
        cites one approved file rather than inline text.
        """
        if self._default_source is None:
            self._default_source = self.approve_file()
        return self.file_ref(self._default_source)

    def data(self, kind="fact", **changes):
        data = {"kind": kind, "title": "Measured result", "body": "Two plus two equals four.",
                "scope": "project", "source": "evidence", "source_refs": [self.evidence_ref()],
                "confidence": "high"}
        if kind == "decision":
            data.update(rationale="Bounded offline operation", alternatives=["Network-dependent operation"])
        data.update(changes)
        return data

    def propose(self, principal=None, **changes):
        return self.command("knowledge_propose", self.data(**changes), principal)

    def doc(self, principal=None, **changes):
        data = {"title": "Local specification", "scope": "project", "content": "Reviewed document bytes.\n"}
        data.update(changes)
        if "source_id" in changes:
            data.pop("content")
        return self.command("doc_import", data, principal)

    def review_payload(self, item, principal=None, **changes):
        current = self.read(item, principal or self.reviewer)
        key = "doc_id" if "doc_id" in item else "knowledge_id"
        payload = {key: item[key], "expected_revision": current["revision"], "verdict": "accept",
                   "comment": "Independently inspected the exact evidence and assertion.",
                   "evidence_reviewed": True, "revision_sha256": current["revision_sha256"],
                   "evidence_sha256": current["evidence_sha256"]}
        payload.update(changes)
        return payload

    def review(self, item, principal=None, **changes):
        principal = principal or self.reviewer
        tool = "doc_review" if "doc_id" in item else "knowledge_review"
        return self.command(tool, self.review_payload(item, principal, **changes), principal)

    def promotion_payload(self, item, **changes):
        key = "doc_id" if "doc_id" in item else "knowledge_id"
        payload = {key: item[key], "expected_revision": item["revision"], "review_id": item["review_id"],
                   "revision_sha256": item["revision_sha256"]}
        if key == "doc_id":
            payload["content_sha256"] = item["content_sha256"]
        payload.update(changes)
        return payload

    def promote(self, item, principal=None, **changes):
        return self.command("doc_publish" if "doc_id" in item else "knowledge_promote",
                            self.promotion_payload(item, **changes), principal or self.controller)

    def approve_file(self, name="proof.txt", text="Evidence version one", principal=None):
        (self.root / name).write_text(text, encoding="utf-8", newline="")
        digest = sha(text)
        # The default fixture Fact and explicit evidence tests may cite the same
        # file; re-approval returns the existing immutable approval record.
        existing = self.con.execute(
            "SELECT source_id,revision,state,sha256 FROM v2_knowledge_source "
            "WHERE project_id=? AND relative_path=? AND sha256=?",
            (self.project, name, digest)).fetchone()
        if existing:
            return {"source_id": existing[0], "revision": existing[1],
                    "state": existing[2], "sha256": existing[3]}
        result = self.command("knowledge_source_approve", {"relative_path": name, "sha256": digest},
                              principal or self.controller)
        return result

    def file_ref(self, approved):
        return {"kind": "file", "source_id": approved["source_id"], "sha256": approved["sha256"]}

    def export(self, item):
        return knowledge.query(self.con, self.project, self.worker, "knowledge_sync_export",
                               {"knowledge_id": item["knowledge_id"]})["proposal"]

    def registered_evidence(self, source, project_id=None, producer=None, evidence_id="ev-test", path="proof.txt"):
        project_id = project_id or self.project
        task = "task-" + evidence_id
        assignment = "assignment-" + evidence_id
        self.con.execute("INSERT INTO v2_task(task_id,project_id,task_state,created_at,updated_at) VALUES (?,?,'draft','now','now')",
                         (task, project_id))
        self.con.execute("INSERT INTO v2_assignment(assignment_id,task_id,agent_id,generation,nonce,"
                         "state,created_at,updated_at) VALUES (?,?,?,1,'nonce','reserved','now','now')",
                         (assignment, task, producer or self.worker["agent_id"]))
        self.con.execute("INSERT INTO v2_evidence(evidence_id,task_id,assignment_id,kind,path,sealed_sha256,"
                         "current_sha256,sealed,state,source,created_at) VALUES (?,?,?,'test',?,?,?,1,'sealed','test','now')",
                         (evidence_id, task, assignment, path, source["sha256"], source["sha256"]))
        return {"kind": "evidence", "evidence_id": evidence_id,
                "source_id": source["source_id"], "sha256": source["sha256"]}


class P1KnowledgeTests(KnowledgeFixture):
    def test_fact_missing_sources_and_unknown_confidence_fail(self):
        for data in (self.data(source_refs=[]), self.data(confidence="unknown"), self.data(source="invented")):
            with self.subTest(data=data):
                self.fails("invalid_payload", self.command, "knowledge_propose", data)

    def test_high_confidence_cannot_promote_without_review(self):
        item = self.propose()
        self.assertEqual("proposed", item["state"])
        self.assertFalse(self.read(item)["usable"])
        self.fails("independent_review_required", self.promote, item, review_id="nonexistent")

    def test_independent_review_and_explicit_promotion(self):
        item = self.propose()
        reviewed = self.review(item)
        self.assertEqual("reviewed", reviewed["state"])
        self.assertFalse(self.read(reviewed)["usable"])
        active = self.promote(reviewed)
        result = self.read(active)
        self.assertEqual("active", active["state"])
        self.assertEqual(3, active["revision"])
        self.assertEqual(1, active["content_revision"])
        self.assertTrue(result["usable"])
        self.assertFalse(result["confidence_is_proof"])

    def test_external_hash_is_not_locally_verified_proof(self):
        external = [{"kind": "external", "uri": "https://example.test/result", "sha256": "a" * 64}]
        # A fact citing only an external hash claim is rejected at submission,
        # before any reviewer could accept it as locally verified.
        self.fails("evidence_required", self.propose, source="external", source_refs=external)
        # Non-fact kinds may retain the claim, but it stays untrusted and unusable
        # until an independent review accepts the proposal.
        item = self.propose(kind="reference", source="external", source_refs=external)
        self.assertEqual("untrusted", self.read(item)["source_trust"])
        self.assertFalse(self.read(item)["usable"])

    def test_author_controller_cannot_self_review_or_self_promote(self):
        item = self.propose(self.controller)
        self.fails("independent_review_required", self.review, item, self.controller)
        reviewed = self.review(item)
        self.fails("independent_review_required", self.promote, reviewed, self.controller)
        self.assertEqual("active", self.promote(reviewed, self.reviewer)["state"])

    def test_alias_principal_cannot_hide_same_agent(self):
        item = self.propose(self.reviewer)
        alias = dict(self.reviewer, principal_id="other-session-name")
        self.fails("independent_review_required", self.review, item, alias)

    def test_every_prior_author_is_excluded(self):
        item = self.propose(self.reviewer)
        revised = self.command("knowledge_revise", dict(self.data(body="Changed statement."),
                               knowledge_id=item["knowledge_id"], expected_revision=1), self.controller)
        self.fails("independent_review_required", self.review, revised, self.reviewer)
        self.fails("independent_review_required", self.review, revised, self.controller)
        self.assertEqual("reviewed", self.review(revised, self.reviewer2)["state"])

    def test_changed_evidence_rejected_before_review(self):
        source = self.approve_file()
        item = self.propose(source_refs=[self.file_ref(source)])
        payload = self.review_payload(item)
        (self.root / "proof.txt").write_text("tampered", encoding="utf-8")
        self.fails("evidence_changed", self.command, "knowledge_review", payload, self.reviewer)
        self.assertEqual("proposed", self.read(item)["state"])

    def test_changed_evidence_rejected_after_review(self):
        source = self.approve_file()
        item = self.review(self.propose(source_refs=[self.file_ref(source)]))
        (self.root / "proof.txt").write_text("tampered", encoding="utf-8")
        self.fails("evidence_changed", self.promote, item)

    def test_revision_invalidates_review_even_if_body_unchanged(self):
        item = self.review(self.propose())
        revised = self.command("knowledge_revise", dict(self.data(confidence="medium"),
                               knowledge_id=item["knowledge_id"], expected_revision=item["revision"]))
        self.assertNotEqual(item["revision_sha256"], revised["revision_sha256"])
        self.assertEqual(item["content_sha256"], revised["content_sha256"])
        self.fails("revision_conflict", self.promote, item)
        self.fails("independent_review_required", self.promote, revised, review_id=item["review_id"])

    def test_review_requires_explicit_evidence_attestation_and_exact_hashes(self):
        item = self.propose()
        for values, code in (({"evidence_reviewed": False}, "evidence_required"),
                             ({"evidence_reviewed": 1}, "evidence_required"),
                             ({"revision_sha256": "a" * 64}, "revision_conflict"),
                             ({"evidence_sha256": "b" * 64}, "evidence_changed")):
            with self.subTest(values=values):
                self.fails(code, self.review, item, **values)

    def test_decision_requires_rationale_and_alternatives(self):
        for changes in ({"rationale": None}, {"alternatives": []}):
            self.fails("invalid_payload", self.command, "knowledge_propose", self.data("decision", **changes))
        self.assertEqual("proposed", self.propose(kind="decision")["state"])

    def test_minimum_kinds_all_require_review_to_activate(self):
        for kind in ("fact", "convention", "hazard", "decision"):
            with self.subTest(kind=kind):
                item = self.propose(kind=kind)
                self.assertFalse(self.read(item)["usable"])
                self.assertTrue(self.read(self.promote(self.review(item)))["usable"])

    def test_registered_evidence_rechecks_authority_and_real_bytes(self):
        source = self.approve_file()
        ref = self.registered_evidence(source)
        item = self.review(self.propose(source_refs=[ref]))
        self.con.execute("UPDATE v2_evidence SET current_sha256=?,state='changed' WHERE evidence_id='ev-test'", ("b" * 64,))
        self.fails("evidence_changed", self.promote, item)

    def test_registered_evidence_wrong_project_and_path_rejected(self):
        source = self.approve_file()
        for ref in (self.registered_evidence(source, self.other, evidence_id="foreign"),
                    self.registered_evidence(source, path="other.txt", evidence_id="different-path")):
            self.fails("evidence_changed", self.propose, source_refs=[ref])

    def test_evidence_producer_is_not_independent_reviewer(self):
        source = self.approve_file()
        ref = self.registered_evidence(source, producer=self.reviewer["agent_id"])
        item = self.propose(source_refs=[ref])
        self.fails("independent_review_required", self.review, item)

    def test_registered_evidence_assignment_producer_must_belong_to_project(self):
        source = self.approve_file()
        foreign = self.agent("foreign-producer", "worker", self.other)
        ref = self.registered_evidence(source, producer=foreign["agent_id"])
        self.fails("evidence_changed", self.propose, source_refs=[ref])

    def test_read_only_stale_decay_and_reverification(self):
        start = datetime(2026, 11, 10, tzinfo=timezone.utc)
        with patch.object(knowledge, "_now", return_value=start):
            item = self.promote(self.review(self.propose(verify_after_seconds=10)))
        before = self.con.total_changes
        with patch.object(knowledge, "_now", return_value=start + timedelta(seconds=11)):
            result = self.read(item)
            self.assertEqual("active", result["state"])
            self.assertEqual("stale", result["effective_state"])
            self.assertFalse(result["usable"])
            self.assertEqual(before, self.con.total_changes)
            fresh = self.promote(self.review(item))
            self.assertTrue(self.read(fresh)["usable"])

    def test_expired_review_cannot_promote_and_expired_content_needs_revision(self):
        start = datetime(2026, 11, 10, tzinfo=timezone.utc)
        with patch.object(knowledge, "_now", return_value=start):
            item = self.review(self.propose(verify_after_seconds=10))
            expired = self.propose(expires_at=(start + timedelta(seconds=10)).isoformat())
        with patch.object(knowledge, "_now", return_value=start + timedelta(seconds=11)):
            self.fails("stale_knowledge", self.promote, item)
            self.fails("stale_knowledge", self.review, expired)
            self.assertEqual("expired", self.read(expired)["decay"])

    def test_missing_and_revoked_sources_project_stale_without_writes(self):
        source = self.approve_file()
        item = self.promote(self.review(self.propose(source_refs=[self.file_ref(source)])))
        self.command("knowledge_source_revoke", {"source_id": source["source_id"], "expected_revision": 1,
                     "reason": "Withdrawn evidence"}, self.controller)
        before = self.con.total_changes
        self.assertEqual("stale", self.read(item)["decay"])
        self.assertEqual(before, self.con.total_changes)

    def test_deleted_evidence_is_stale_and_queries_never_reactivate_it(self):
        source = self.approve_file()
        item = self.promote(self.review(self.propose(source_refs=[self.file_ref(source)])))
        (self.root / "proof.txt").unlink()
        before = self.con.total_changes
        result = self.read(item)
        self.assertEqual("stale", result["decay"])
        self.assertFalse(result["usable"])
        self.assertEqual(before, self.con.total_changes)

    def test_retirement_preserves_history_and_cannot_restore(self):
        item = self.promote(self.review(self.propose()))
        retired = self.command("knowledge_retire", {"knowledge_id": item["knowledge_id"],
                               "expected_revision": item["revision"], "reason": "Superseded observation"})
        self.assertFalse(self.read(retired)["usable"])
        history = knowledge.query(self.con, self.project, self.worker, "knowledge_history",
                                  {"knowledge_id": item["knowledge_id"]})
        self.assertEqual(4, len(history["events"]))
        self.assertEqual("Superseded observation", self.read(retired)["retired_reason"])
        self.fails("state_conflict", self.command, "knowledge_revise", dict(self.data(),
                   knowledge_id=item["knowledge_id"], expected_revision=retired["revision"]))


class P2DocumentTests(KnowledgeFixture):
    def test_import_hash_review_and_explicit_controller_publish(self):
        doc = self.doc()
        self.assertEqual(sha("Reviewed document bytes.\n"), doc["content_sha256"])
        self.fails("independent_review_required", self.promote, doc, review_id="absent")
        reviewed = self.review(doc)
        self.fails("forbidden_role", self.promote, reviewed, self.reviewer)
        self.fails("forbidden_role", self.promote, reviewed, self.worker)
        published = self.promote(reviewed)
        self.assertEqual("published", published["state"])
        self.assertTrue(self.read(published)["usable"])

    def test_controller_can_publish_own_doc_only_after_independent_review(self):
        doc = self.doc(self.controller)
        self.fails("independent_review_required", self.review, doc, self.controller)
        self.assertEqual("published", self.promote(self.review(doc))["state"])

    def test_publish_hash_must_match_reviewed_content(self):
        doc = self.review(self.doc())
        self.fails("evidence_changed", self.promote, doc, content_sha256=sha("wrong content"))

    def test_mutable_file_after_review_never_publishes_changed_bytes(self):
        source = self.approve_file("spec.md", "First revision\n")
        doc = self.review(self.doc(source_id=source["source_id"], content_sha256=source["sha256"]))
        (self.root / "spec.md").write_text("Unreviewed second revision\n", encoding="utf-8")
        self.fails("evidence_changed", self.promote, doc)
        self.assertEqual("First revision\n", self.read(doc)["manifest"]["body"])

    def test_revised_doc_requires_new_review_even_with_old_review_id(self):
        doc = self.review(self.doc())
        revised = self.command("doc_revise", {"doc_id": doc["doc_id"], "expected_revision": doc["revision"],
                               "title": "Local specification", "scope": "project", "content": "New draft"})
        self.assertEqual(2, revised["content_revision"])
        self.fails("independent_review_required", self.promote, revised, review_id=doc["review_id"])
        self.assertEqual("published", self.promote(self.review(revised))["state"])
        old = knowledge.query(self.con, self.project, self.worker, "doc_get",
                              {"doc_id": doc["doc_id"], "content_revision": 1})
        self.assertEqual("historical", old["state"])
        self.assertEqual("Reviewed document bytes.\n", old["manifest"]["body"])
        self.assertEqual(doc["content_sha256"], old["content_sha256"])
        self.assertFalse(old["usable"])

    def test_documents_can_be_fact_evidence_but_revisions_invalidate_it(self):
        doc = self.promote(self.review(self.doc()))
        ref = {"kind": "doc", "doc_id": doc["doc_id"], "content_revision": 1, "sha256": doc["content_sha256"]}
        item = self.promote(self.review(self.propose(source_refs=[ref])))
        self.assertTrue(self.read(item)["usable"])
        self.command("doc_revise", {"doc_id": doc["doc_id"], "expected_revision": doc["revision"],
                     "title": "New document", "scope": "project", "content": "Different bytes"})
        self.assertEqual("stale", self.read(item)["decay"])

    def test_doc_author_cannot_review_fact_supported_by_their_doc(self):
        doc = self.doc(self.reviewer)
        doc = self.promote(self.review(doc, self.reviewer2))
        item = self.propose(source_refs=[{"kind": "doc", "doc_id": doc["doc_id"],
                            "content_revision": 1, "sha256": doc["content_sha256"]}])
        self.fails("independent_review_required", self.review, item, self.reviewer)

    def test_no_arbitrary_read_and_only_controller_approves_file(self):
        self.fails("invalid_payload", self.command, "doc_import",
                   {"title": "No", "scope": "project", "path": str(self.root / "state.sqlite3")})
        self.fails("forbidden_role", self.command, "knowledge_source_approve",
                   {"relative_path": "not-read.txt", "sha256": "a" * 64}, self.worker)
        for path in ("../outside.txt", "C:/outside.txt", "\\\\host\\file", "/etc/passwd", "a\\b",
                     ".git/config", ".GIT/config", "nested/.Git/config", "nul.txt", "a:stream"):
            with self.subTest(path=path):
                self.fails("unsafe_source", self.command, "knowledge_source_approve",
                           {"relative_path": path, "sha256": "a" * 64}, self.controller)

    def test_bounded_inline_doc_and_file_sources(self):
        self.fails("invalid_payload", self.doc, content="a" * (knowledge.MAX_CONTENT_BYTES + 1))
        self.fails("evidence_changed", self.doc, content="text", content_sha256="f" * 64)
        self.fails("invalid_payload", self.command, "doc_import",
                   {"title": "Two sources", "scope": "project", "content": "text", "source_id": "src"})
        (self.root / "large.txt").write_bytes(b"a" * (knowledge.MAX_CONTENT_BYTES + 1))
        self.fails("unsafe_source", self.command, "knowledge_source_approve",
                   {"relative_path": "large.txt", "sha256": "a" * 64}, self.controller)

    def test_distinct_stat_and_fstat_ctime_semantics_do_not_reject_unchanged_file(self):
        original = os.fstat

        def different_ctime(fd):
            info = original(fd)
            names = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_nlink")
            fields = {name: getattr(info, name) for name in names}
            fields["st_ctime_ns"] += 1000000000
            return SimpleNamespace(**fields)

        with patch.object(knowledge.os, "fstat", side_effect=different_ctime):
            source = self.approve_file()
            self.assertEqual("proposed", self.doc(source_id=source["source_id"], content_sha256=source["sha256"])["state"])

    def test_changed_handle_metadata_during_read_is_rejected(self):
        original = os.fstat
        calls = []

        def changed_mtime(fd):
            info = original(fd)
            names = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_nlink")
            fields = {name: getattr(info, name) for name in names}
            calls.append(1)
            fields["st_mtime_ns"] += len(calls)
            return SimpleNamespace(**fields)

        with patch.object(knowledge.os, "fstat", side_effect=changed_mtime):
            self.fails("evidence_changed", self.approve_file, "changing.txt", "Metadata changes while read")

    def test_hardlinked_source_rejected(self):
        (self.root / "original.txt").write_text("same bytes", encoding="utf-8")
        try:
            os.link(self.root / "original.txt", self.root / "link.txt")
        except OSError as exc:
            self.skipTest("host does not permit hardlinks: " + str(exc))
        self.fails("unsafe_source", self.command, "knowledge_source_approve",
                   {"relative_path": "link.txt", "sha256": sha("same bytes")}, self.controller)

    def test_symlink_source_rejected_when_host_supports_it(self):
        (self.root / "original.txt").write_text("same bytes", encoding="utf-8")
        try:
            (self.root / "link.txt").symlink_to(self.root / "original.txt")
        except OSError as exc:
            self.skipTest("host does not permit symlinks: " + str(exc))
        self.fails("unsafe_source", self.command, "knowledge_source_approve",
                   {"relative_path": "link.txt", "sha256": sha("same bytes")}, self.controller)

    def test_binary_file_cannot_be_imported_as_document(self):
        (self.root / "binary.bin").write_bytes(b"\xff\xfe")
        digest = hashlib.sha256(b"\xff\xfe").hexdigest()
        source = self.command("knowledge_source_approve", {"relative_path": "binary.bin", "sha256": digest}, self.controller)
        self.fails("invalid_payload", self.doc, source_id=source["source_id"], content_sha256=digest)

    def test_publish_has_no_external_writes(self):
        source = self.approve_file("spec.md", "Content remains unchanged\n")
        before = {p.name: p.read_bytes() for p in self.root.iterdir() if p.name != self.db.name}
        doc = self.doc(source_id=source["source_id"], content_sha256=source["sha256"])
        self.promote(self.review(doc))
        after = {p.name: p.read_bytes() for p in self.root.iterdir() if p.name != self.db.name}
        self.assertEqual(before, after)


class P2MemoryTests(KnowledgeFixture):
    def test_external_references_stay_untrusted_with_local_source_label(self):
        refs = [self.inline(),
                {"kind": "external", "uri": "urn:unverified:claim", "sha256": "a" * 64}]
        # A human label never upgrades inline/external assertions into local proof.
        self.fails("evidence_required", self.propose, source="human", source_refs=refs)
        # A non-fact proposal keeps the claim and its untrusted classification.
        item = self.propose(kind="reference", source="human", source_refs=refs)
        active = self.promote(self.review(item))
        self.assertTrue(self.read(active)["usable"])
        self.assertEqual("untrusted", self.read(active)["source_trust"])

    def test_all_seven_kinds_and_invalid_kind(self):
        self.assertEqual(7, len(knowledge.KINDS))
        for kind in knowledge.KINDS:
            with self.subTest(kind=kind):
                item = self.propose(kind=kind)
                self.assertEqual(kind, self.read(item)["kind"])
        self.fails("invalid_payload", self.propose, kind="hypothesis")
        self.fails("invalid_payload", self.propose, kind="convention", scope="personal")

    def test_personal_privacy_including_list_cursor(self):
        item = self.propose(scope="personal")
        for principal in (self.worker2, self.controller, self.observer):
            self.fails("not_found", self.read, item, principal)
            listed = knowledge.query(self.con, self.project, principal, "knowledge_list", {"limit": 1})
            self.assertEqual([], listed["items"])
            self.assertIsNone(listed["next_after_id"])
        self.assertEqual("personal", self.read(item)["scope"])

    def test_personal_explicit_reviewer_can_review_but_not_edit(self):
        item = self.propose(scope="personal", reviewer_principal_ids=[self.reviewer["principal_id"]])
        self.fails("forbidden_owner", self.command, "knowledge_revise", dict(self.data(scope="personal"),
                   knowledge_id=item["knowledge_id"], expected_revision=item["revision"]), self.reviewer)
        active = self.promote(self.review(item), self.reviewer)
        self.assertTrue(self.read(active)["usable"])
        self.fails("not_found", self.read, active, self.reviewer2)

    def test_scope_is_immutable_and_other_worker_cannot_revise_or_retire(self):
        item = self.propose()
        self.fails("forbidden_owner", self.command, "knowledge_revise", dict(self.data(scope="team"),
                   knowledge_id=item["knowledge_id"], expected_revision=1))
        self.fails("forbidden_owner", self.command, "knowledge_revise", dict(self.data(),
                   knowledge_id=item["knowledge_id"], expected_revision=1), self.worker2)
        self.fails("forbidden_owner", self.command, "knowledge_retire",
                   {"knowledge_id": item["knowledge_id"], "expected_revision": 1, "reason": "No"}, self.worker2)

    def test_team_export_import_is_auditable_proposal_only(self):
        source = self.promote(self.review(self.propose(kind="convention", scope="team")))
        exported = knowledge.query(self.con, self.project, self.worker, "knowledge_sync_export", {"knowledge_id": source["knowledge_id"]})
        self.assertFalse(exported["authenticated_remote"])
        self.assertFalse(exported["auto_promote"])
        imported = self.command("knowledge_sync_import", {"proposal": exported["proposal"]}, self.worker2)
        result = self.read(imported, self.worker2)
        self.assertEqual("proposed", result["state"])
        self.assertEqual("untrusted", result["source_trust"])
        self.assertEqual(self.worker2["principal_id"], result["owner_principal_id"])
        self.assertFalse(result["usable"])
        self.assertIsNone(result["review_id"])
        self.assertEqual(1, self.con.execute("SELECT COUNT(*) FROM v2_knowledge_sync").fetchone()[0])
        self.assertTrue(self.read(self.promote(self.review(imported)), self.worker2)["usable"])

    def test_tampered_sync_is_rejected(self):
        source = self.propose(kind="convention", scope="team")
        packet = knowledge.query(self.con, self.project, self.worker, "knowledge_sync_export", {"knowledge_id": source["knowledge_id"]})["proposal"]
        tampered = copy.deepcopy(packet)
        tampered["entry"]["body"] = "Tampered remote fact"
        self.fails("integrity_error", self.command, "knowledge_sync_import", {"proposal": tampered})

    def test_recomputed_sync_hash_is_not_authentication_or_promotion(self):
        source = self.propose(kind="convention", scope="team")
        packet = knowledge.query(self.con, self.project, self.worker, "knowledge_sync_export", {"knowledge_id": source["knowledge_id"]})["proposal"]
        packet["entry"]["body"] = "Attacker can recompute an unsigned digest"
        packet["sha256"] = packet_hash(packet)
        imported = self.command("knowledge_sync_import", {"proposal": packet})
        self.assertEqual("proposed", imported["state"])
        self.assertFalse(self.read(imported)["usable"])
        self.assertEqual("untrusted", self.read(imported)["source_trust"])

    def test_sync_cannot_import_status_author_review_or_local_source_ids(self):
        source = self.propose(kind="convention", scope="team")
        packet = knowledge.query(self.con, self.project, self.worker, "knowledge_sync_export", {"knowledge_id": source["knowledge_id"]})["proposal"]
        for key in ("status", "author", "review_id", "trust"):
            tampered = copy.deepcopy(packet)
            tampered["entry"][key] = "active"
            tampered["sha256"] = packet_hash(tampered)
            self.fails("invalid_payload", self.command, "knowledge_sync_import", {"proposal": tampered})
        packet["entry"]["source_refs"] = [{"kind": "file", "source_id": "local", "sha256": "a" * 64}]
        packet["sha256"] = packet_hash(packet)
        self.fails("unsafe_source", self.command, "knowledge_sync_import", {"proposal": packet})

    def test_sync_wrong_project_duplicate_and_private_export_rejected(self):
        private = self.propose(scope="personal")
        self.fails("forbidden_owner", knowledge.query, self.con, self.project, self.worker,
                   "knowledge_sync_export", {"knowledge_id": private["knowledge_id"]})
        team = self.propose(kind="convention", scope="team")
        packet = knowledge.query(self.con, self.project, self.worker, "knowledge_sync_export", {"knowledge_id": team["knowledge_id"]})["proposal"]
        wrong = copy.deepcopy(packet)
        wrong["project_id"] = self.other
        wrong["sha256"] = packet_hash(wrong)
        self.fails("wrong_project", self.command, "knowledge_sync_import", {"proposal": wrong})
        self.command("knowledge_sync_import", {"proposal": packet})
        self.fails("state_conflict", self.command, "knowledge_sync_import", {"proposal": packet})

    def test_agent_inference_needs_replayable_evidence_to_become_fact(self):
        # An agent's own inline text is an inference record, not a project fact.
        inference = self.data(source="agent", source_refs=[self.inline()])
        self.fails("evidence_required", self.command, "knowledge_propose", inference)
        # The same inference may be recorded as a decision proposal.
        decision = self.command("knowledge_propose", self.data("decision", source="agent",
                                                                 source_refs=[self.inline()]))
        self.assertEqual(("proposed", "decision"), (decision["state"], self.read(decision)["kind"]))
        # Revising that inference into an inline-only fact hits the same gate.
        self.fails("evidence_required", self.command, "knowledge_revise",
                   dict(self.data(source="agent", source_refs=[self.inline()]),
                        knowledge_id=decision["knowledge_id"], expected_revision=decision["revision"]))
        # A replayable local reference makes only the reviewed fact usable.
        source = self.approve_file()
        fact = self.propose(source="agent", source_refs=[self.file_ref(source)])
        self.assertEqual("proposed", fact["state"])
        self.assertFalse(self.read(fact)["usable"])
        self.assertEqual("active", self.promote(self.review(fact))["state"])

    def test_projection_retains_source_author_scope_review_and_verification(self):
        # A far-future expiry keeps this test non-default without running the
        # real clock into the expiry guard; expiry itself is covered separately.
        item = self.propose(kind="convention", source="review", expires_at="2099-01-01T00:00:00+00:00",
                            verify_after_seconds=120)
        revised = self.command("knowledge_revise", dict(
            self.data(kind="convention", source="review", expires_at="2099-01-01T00:00:00+00:00",
                      verify_after_seconds=120, body="Controller revision of the convention."),
            knowledge_id=item["knowledge_id"], expected_revision=item["revision"]), self.controller)
        result = self.read(self.promote(self.review(revised), self.reviewer))
        self.assertEqual("review", result["source"])
        self.assertEqual("project", result["scope"])
        # The latest revision author is the controller; ownership stays with the worker.
        self.assertEqual(self.controller["principal_id"], result["author_principal_id"])
        self.assertEqual(self.controller["agent_id"], result["author_agent_id"])
        self.assertEqual(self.worker["principal_id"], result["owner_principal_id"])
        self.assertEqual("2099-01-01T00:00:00+00:00", result["expires_at"])
        self.assertEqual(120, result["verify_after_seconds"])
        self.assertIsNotNone(result["last_verified_at"])
        self.assertEqual("accept", result["review"]["verdict"])
        self.assertEqual(self.reviewer["principal_id"], result["review"]["reviewer_principal_id"])
        self.assertEqual(self.reviewer["agent_id"], result["review"]["reviewer_agent_id"])
        self.assertEqual("reviewer", result["review"]["reviewer_role"])
        self.assertIsNotNone(result["review"]["reviewed_at"])
        # A rejection is still the current review record, never an acceptance.
        rejected = self.review(self.propose(kind="convention", source="human"),
                               verdict="reject", evidence_reviewed=False)
        self.assertEqual("reject", self.read(rejected)["review"]["verdict"])

    def test_sync_origin_conflict_is_a_distinct_proposal_and_expiry_blocks_review(self):
        source = self.propose(kind="convention", scope="team")
        packet = self.export(source)
        first = self.command("knowledge_sync_import", {"proposal": packet})
        self.assertFalse(first["sync"]["origin_conflict"])
        conflict = copy.deepcopy(packet)
        conflict["entry"]["body"] = "A conflicting claim for the same origin revision"
        conflict["sha256"] = packet_hash(conflict)
        second = self.command("knowledge_sync_import", {"proposal": conflict})
        self.assertTrue(second["sync"]["origin_conflict"])
        self.assertEqual(first["sync"]["origin"], second["sync"]["origin"])
        # The conflict is a separate local proposal; nothing is merged or revised.
        self.assertNotEqual(first["knowledge_id"], second["knowledge_id"])
        synced = self.con.execute("SELECT entity_id FROM v2_knowledge_sync WHERE project_id=?",
                                  (self.project,)).fetchall()
        self.assertEqual(2, len(synced))
        self.assertEqual({first["knowledge_id"], second["knowledge_id"]},
                         {row[0] for row in synced})
        entities = self.con.execute("SELECT COUNT(*) FROM v2_knowledge WHERE entity_id IN (?,?)",
                                    (first["knowledge_id"], second["knowledge_id"])).fetchone()[0]
        self.assertEqual(2, entities)
        for imported in (first, second):
            self.assertEqual("proposed", self.read(imported)["state"])
            self.assertEqual("untrusted", self.read(imported)["source_trust"])
            self.assertFalse(self.read(imported)["usable"])
        # Negative controls: another entity and the next content revision are not conflicts.
        unrelated = self.command("knowledge_sync_import", {"proposal": self.export(
            self.propose(kind="convention", scope="team", body="An unrelated team claim."))})
        self.assertFalse(unrelated["sync"]["origin_conflict"])
        revised = self.command("knowledge_revise", dict(
            self.data(kind="convention", scope="team", body="Source content revision two."),
            knowledge_id=source["knowledge_id"], expected_revision=source["revision"]))
        next_revision = self.command("knowledge_sync_import", {"proposal": self.export(revised)})
        self.assertFalse(next_revision["sync"]["origin_conflict"])
        self.fails("state_conflict", self.command, "knowledge_sync_import", {"proposal": packet})
        with patch.object(knowledge, "_now", return_value=datetime(2026, 11, 10, tzinfo=timezone.utc)):
            expiring = self.propose(kind="convention", scope="team",
                                    expires_at="2026-11-10T00:00:10+00:00")
            expired = self.command("knowledge_sync_import", {"proposal": self.export(expiring)})
        with patch.object(knowledge, "_now",
                          return_value=datetime(2026, 11, 10, 0, 0, 11, tzinfo=timezone.utc)):
            self.fails("stale_knowledge", self.review, expired)
            self.assertFalse(self.read(expired)["usable"])

    def test_sync_imported_item_is_project_bound_and_retirement_is_terminal(self):
        source = self.propose(kind="convention", scope="team")
        packet = knowledge.query(self.con, self.project, self.worker, "knowledge_sync_export",
                                 {"knowledge_id": source["knowledge_id"]})["proposal"]
        imported = self.command("knowledge_sync_import", {"proposal": packet})
        foreign = self.agent("foreign-controller-2", "controller", self.other)
        self.fails("not_found", self.read, imported, foreign, self.other)
        revised = self.command("knowledge_revise", dict(self.data(scope="team"),
                               knowledge_id=imported["knowledge_id"], expected_revision=1))
        self.fails("revision_conflict", self.command, "knowledge_revise",
                   dict(self.data(scope="team"), knowledge_id=imported["knowledge_id"], expected_revision=1))
        retired = self.command("knowledge_retire", {"knowledge_id": imported["knowledge_id"],
                               "expected_revision": revised["revision"], "reason": "Withdrawn remote proposal"})
        self.assertFalse(self.read(retired)["usable"])
        self.assertEqual("Withdrawn remote proposal", self.read(retired)["retired_reason"])
        self.fails("state_conflict", self.command, "knowledge_revise",
                   dict(self.data(scope="team"), knowledge_id=imported["knowledge_id"],
                        expected_revision=retired["revision"]))
        history = knowledge.query(self.con, self.project, self.worker, "knowledge_history",
                                  {"knowledge_id": imported["knowledge_id"]})
        self.assertEqual(4, len(history["events"]))

    def test_exported_local_refs_cannot_claim_verified_remote_fact(self):
        source = self.approve_file()
        item = self.promote(self.review(self.propose(scope="team", source_refs=[self.file_ref(source)])))
        packet = knowledge.query(self.con, self.project, self.worker, "knowledge_sync_export", {"knowledge_id": item["knowledge_id"]})["proposal"]
        self.assertEqual("external", packet["entry"]["source_refs"][0]["kind"])
        # Export reduces local bytes to an external pointer; importing that as a
        # fact would launder local evidence into an unverified remote claim, so
        # the fact evidence gate rejects the whole proposal.
        self.fails("evidence_required", self.command, "knowledge_sync_import", {"proposal": packet})
        self.assertEqual(0, self.con.execute("SELECT COUNT(*) FROM v2_knowledge_sync").fetchone()[0])
        self.assertEqual(1, self.con.execute("SELECT COUNT(*) FROM v2_knowledge").fetchone()[0])

    def test_rejected_review_disables_active_knowledge(self):
        item = self.promote(self.review(self.propose()))
        rejected = self.review(item, verdict="reject", evidence_reviewed=False, comment="Contradicted by evidence.")
        self.assertEqual("rejected", rejected["state"])
        self.assertFalse(self.read(rejected)["usable"])

    def test_queries_are_readonly_on_readonly_connection(self):
        item = self.propose()
        con = sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True)
        try:
            result = knowledge.query(con, self.project, self.worker, "knowledge_get", {"knowledge_id": item["knowledge_id"]})
            self.assertEqual("proposed", result["state"])
            knowledge.query(con, self.project, self.worker, "knowledge_list", {})
            knowledge.query(con, self.project, self.worker, "knowledge_history", {"knowledge_id": item["knowledge_id"]})
            self.assertEqual(0, con.total_changes)
        finally:
            con.close()


class ProvenanceTests(KnowledgeFixture):
    """Stage-3 regressions: declared source labels never authenticate authorship."""

    def full(self, item, principal=None, project_id=None):
        return knowledge.query(self.con, project_id or self.project, principal or self.worker,
                               "knowledge_get", {"knowledge_id": item["knowledge_id"]})

    def test_every_declared_source_label_still_needs_replayable_fact_evidence(self):
        claims = [self.inline(),
                  {"kind": "external", "uri": "urn:unverified:claim", "sha256": "a" * 64}]
        for source in ("human", "agent", "doc", "review", "evidence", "external"):
            with self.subTest(source=source):
                self.fails("evidence_required", self.propose, source=source, source_refs=claims)
        self.assertEqual(0, self.con.execute("SELECT COUNT(*) FROM v2_knowledge").fetchone()[0])
        self.assertEqual(0, self.con.execute("SELECT COUNT(*) FROM v2_knowledge_review").fetchone()[0])
        # A human label with replayable local bytes is still recordable, but the
        # label itself is never authenticated authorship.
        item = self.propose(source="human")
        view = self.read(self.promote(self.review(item)))
        self.assertTrue(view["usable"])
        self.assertEqual("human", view["source"])
        self.assertFalse(view["source_authenticated"])
        self.assertEqual({"declared": "human", "authenticated": False},
                         view["source_assertion"])

    def test_submission_principal_is_server_derived_and_assertions_unverified(self):
        item = self.propose(source="agent")
        view = self.full(item)
        self.assertEqual(self.worker["principal_id"], view["submitted_by"]["principal_id"])
        self.assertEqual(self.worker["agent_id"], view["submitted_by"]["agent_id"])
        self.assertEqual("worker", view["submitted_by"]["role"])
        self.assertIsNone(view["submitted_by"]["binding_id"])
        self.assertEqual("authenticated-submission", view["provenance_class"])
        self.assertFalse(view["source_authenticated"])
        self.assertEqual({"declared": "agent", "authenticated": False},
                         view["source_assertion"])
        # Caller-supplied identity/provenance fields are unsupported, not trusted.
        for field in ("author", "principal_id", "agent_id", "role", "binding_id",
                      "provenance", "submitted_by", "source_authenticated"):
            with self.subTest(field=field):
                self.fails("invalid_payload", self.command, "knowledge_propose",
                           dict(self.data(), **{field: "claimed"}))

    def test_revision_refreshes_submission_and_cannot_relabel_inline_claim(self):
        item = self.propose(kind="convention", source="review")
        revised = self.command("knowledge_revise", dict(
            self.data(kind="convention", source="human"),
            knowledge_id=item["knowledge_id"], expected_revision=item["revision"]), self.controller)
        view = self.full(revised)
        self.assertEqual(self.controller["principal_id"], view["submitted_by"]["principal_id"])
        self.assertEqual(self.controller["agent_id"], view["submitted_by"]["agent_id"])
        self.assertEqual({"declared": "human", "authenticated": False},
                         view["source_assertion"])
        self.assertEqual(item["revision_sha256"], view["provenance"]["supersedes_sha256"])
        old = knowledge.query(self.con, self.project, self.worker, "knowledge_get",
                              {"knowledge_id": item["knowledge_id"], "content_revision": 1})
        self.assertNotIn("supersedes_sha256", old["provenance"])
        self.assertEqual(self.worker["principal_id"],
                         old["provenance"]["submitted_by"]["principal_id"])
        # Relabeling an inline claim as a Fact is rejected during revision too.
        self.fails("evidence_required", self.command, "knowledge_revise", dict(
            self.data(source="human", source_refs=[self.inline()]),
            knowledge_id=item["knowledge_id"], expected_revision=revised["revision"]))
        self.assertEqual(revised["revision"], self.read(item)["revision"])
        self.assertEqual(0, self.con.execute("SELECT COUNT(*) FROM v2_knowledge_review").fetchone()[0])

    def legacy_fact(self):
        """Insert a pre-fix inline-only active Fact with no submission metadata."""
        body = "Legacy claim recorded before principal provenance was stored."
        inline = {"kind": "inline", "content": body, "label": "legacy note", "sha256": sha(body)}
        manifest = {"title": "Legacy claim", "scope": "project", "expires_at": None,
                    "verify_after_seconds": knowledge.DEFAULT_VERIFY_SECONDS,
                    "reviewer_principal_ids": [], "kind": "fact", "body": body,
                    "source": "human", "source_refs": [inline], "confidence": "high",
                    "rationale": None, "alternatives": []}
        provenance = {"type": "local"}
        entity_id, now = "kn-legacy00000000000000000000001", "2026-11-10T00:00:00+00:00"
        self.con.execute("INSERT INTO v2_knowledge VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (entity_id, self.project, "knowledge", self.worker["principal_id"],
                          self.worker["agent_id"], "project", 1, 1, "active", None, None,
                          None, now, now))
        self.con.execute("INSERT INTO v2_knowledge_revision VALUES (?,?,?,?,?,?,?,?,?)",
                         (entity_id, 1, self.worker["principal_id"], self.worker["agent_id"], now,
                          knowledge._json(manifest),
                          knowledge._digest({"manifest": manifest, "provenance": provenance}),
                          knowledge._sha(body.encode("utf-8")), knowledge._json(provenance)))
        return {"knowledge_id": entity_id}

    def test_legacy_unknown_provenance_is_preserved_and_stays_unverifiable(self):
        item = self.legacy_fact()
        view = self.full(item)
        self.assertEqual("active", view["state"])
        self.assertTrue(view["usable"])              # historical behavior preserved
        self.assertEqual("legacy-unknown", view["provenance_class"])
        self.assertIsNone(view["submitted_by"])
        self.assertIsNone(view["source_assertion"])
        self.assertFalse(view["source_authenticated"])
        self.assertEqual({"type": "local"}, view["provenance"])
        # The old inline-only Fact cannot be re-attested or silently relabeled.
        self.fails("evidence_required", self.review, item)
        self.fails("evidence_required", self.command, "knowledge_revise", dict(
            self.data(source="human", source_refs=[self.inline()]),
            knowledge_id=item["knowledge_id"], expected_revision=1))
        self.assertEqual("active", self.read(item)["state"])
        self.assertEqual(0, self.con.execute("SELECT COUNT(*) FROM v2_knowledge_review").fetchone()[0])

    def test_sync_import_records_authenticated_local_importer_not_origin_author(self):
        source = self.promote(self.review(self.propose(kind="convention", scope="team")))
        imported = self.command("knowledge_sync_import",
                                {"proposal": self.export(source)}, self.worker2)
        view = self.full(imported, self.worker2)
        self.assertEqual(self.worker2["principal_id"], view["submitted_by"]["principal_id"])
        self.assertEqual("authenticated-submission", view["provenance_class"])
        self.assertEqual({"declared": "external", "authenticated": False},
                         view["source_assertion"])
        self.assertEqual("untrusted", view["source_trust"])
        self.assertTrue(view["provenance"]["untrusted"])
        self.assertEqual("sync", view["provenance"]["type"])
        self.assertEqual(self.worker["principal_id"], view["provenance"]["origin"]["exported_by"])


class DomainBoundaryTests(KnowledgeFixture):
    def test_schema_and_commands_require_caller_transaction(self):
        self.fails("transaction_required", knowledge.create_knowledge_schema, self.con)
        self.fails("transaction_required", knowledge.apply_command, self.con, self.project,
                   self.worker, "knowledge_propose", self.data())

    def test_schema_is_transactional_idempotent_and_no_downgrade(self):
        empty = sqlite3.connect(":memory:", isolation_level=None)
        try:
            empty.execute("BEGIN")
            knowledge.create_knowledge_schema(empty)
            knowledge.create_knowledge_schema(empty)
            self.assertTrue(empty.in_transaction)
            empty.rollback()
            self.assertFalse(empty.execute("SELECT 1 FROM sqlite_master WHERE name='v2_knowledge'").fetchone())
        finally:
            empty.close()
        self.con.execute("UPDATE v2_knowledge_meta SET version=999")
        self.con.execute("BEGIN IMMEDIATE")
        self.fails("schema_conflict", knowledge.create_knowledge_schema, self.con)
        self.con.rollback()

    def test_caller_can_rollback_proposal_and_audit_together(self):
        before = self.con.execute("SELECT COUNT(*) FROM v2_knowledge_audit").fetchone()[0]
        self.con.execute("BEGIN IMMEDIATE")
        item = knowledge.apply_command(self.con, self.project, self.worker, "knowledge_propose", self.data())
        self.assertTrue(self.con.in_transaction)
        other = sqlite3.connect(self.db)
        try:
            self.assertEqual(0, other.execute("SELECT COUNT(*) FROM v2_knowledge").fetchone()[0])
        finally:
            other.close()
        self.con.rollback()
        self.assertEqual(before, self.con.execute("SELECT COUNT(*) FROM v2_knowledge_audit").fetchone()[0])
        self.fails("not_found", self.read, item)

    def test_invalid_principal_revoked_agent_and_role_mismatch_rejected(self):
        for principal in ({}, {"role": "god", "principal_id": "a"}, dict(self.worker, principal_id=""),
                          dict(self.worker, agent_id="unknown"), dict(self.worker, agent_id=None),
                          dict(self.worker, principal_id="bad\nidentity"),
                          dict(self.worker, principal_id="\ud800"), dict(self.worker, role="controller")):
            self.fails("invalid_principal", self.command, "knowledge_propose", self.data(), principal or {"role": "bad"})
        self.con.execute("UPDATE v2_agent SET lifecycle='revoked' WHERE agent_id=?", (self.worker["agent_id"],))
        self.fails("invalid_principal", self.command, "knowledge_propose", self.data())

    def test_observer_scoped_agent_credential_can_read_but_not_write(self):
        item = self.propose()
        observer = dict(self.worker, role="observer")
        self.assertEqual("proposed", self.read(item, observer)["state"])
        self.fails("forbidden_role", self.command, "knowledge_propose", self.data(), observer)

    def test_expiry_timezone_overflow_is_a_domain_rejection(self):
        for expiry in ("0001-01-01T00:00:00+23:00", "9999-12-31T23:59:59-23:00"):
            self.fails("invalid_payload", self.propose, expires_at=expiry)

    def test_wrong_project_principal_query_and_revision(self):
        item = self.propose()
        self.fails("wrong_project", self.command, "knowledge_propose", self.data(), dict(self.worker, project_id=self.other))
        self.fails("invalid_principal", self.read, item, self.worker, self.other)
        foreign = self.agent("foreign-controller", "controller", self.other)
        self.fails("not_found", self.read, item, foreign, self.other)
        self.fails("not_found", self.command, "knowledge_retire", {"knowledge_id": item["knowledge_id"],
                   "expected_revision": 1, "reason": "wrong project"}, foreign, self.other)

    def test_observer_and_advisor_cannot_write(self):
        advisor = {"role": "advisor", "principal_id": "advisor", "agent_id": None}
        for principal in (self.observer, advisor):
            self.fails("forbidden_role", self.command, "knowledge_propose", self.data(), principal)

    def test_payload_cannot_spoof_author_or_bypass_review(self):
        for key in ("author", "state", "review_id", "last_verified_at", "principal_id", "trust"):
            self.fails("invalid_payload", self.command, "knowledge_propose", dict(self.data(), **{key: "forged"}))

    def test_expected_revision_checks_type_and_concurrent_stale_writer(self):
        item = self.propose()
        for invalid in (True, "1", None, 0, 1.0):
            self.fails("invalid_payload", self.command, "knowledge_retire", {"knowledge_id": item["knowledge_id"],
                       "expected_revision": invalid, "reason": "typed revision required"})
        data = dict(self.data(), knowledge_id=item["knowledge_id"], expected_revision=1)
        self.command("knowledge_revise", data)
        second = sqlite3.connect(self.db, isolation_level=None)
        try:
            second.execute("BEGIN IMMEDIATE")
            with self.assertRaises(knowledge.KnowledgeError) as error:
                knowledge.apply_command(second, self.project, self.worker, "knowledge_revise", data)
            self.assertEqual("revision_conflict", error.exception.code)
            second.rollback()
        finally:
            second.close()

    def test_immutable_revision_review_audit_and_history_pagination(self):
        item = self.promote(self.review(self.propose()))
        for table in ("v2_knowledge_revision", "v2_knowledge_review", "v2_knowledge_audit"):
            with self.assertRaises(sqlite3.IntegrityError):
                self.con.execute("DELETE FROM " + table)
        page = knowledge.query(self.con, self.project, self.worker, "knowledge_history", {"knowledge_id": item["knowledge_id"], "limit": 1})
        self.assertEqual(1, len(page["events"]))
        second = knowledge.query(self.con, self.project, self.worker, "knowledge_history",
                                 {"knowledge_id": item["knowledge_id"], "after_seq": page["next_after_seq"]})
        self.assertEqual(2, len(second["events"]))
        self.assertEqual("accept", second["events"][0]["review"]["verdict"])

    def test_row_factory_is_not_required_or_changed(self):
        self.assertIsNone(self.con.row_factory)
        item = self.propose()
        self.con.row_factory = sqlite3.Row
        self.assertEqual("proposed", self.read(item)["state"])
        self.assertIs(self.con.row_factory, sqlite3.Row)

    def test_revoked_reviewer_cannot_authorize_promotion(self):
        item = self.review(self.propose())
        self.con.execute("UPDATE v2_agent SET lifecycle='revoked' WHERE agent_id=?", (self.reviewer["agent_id"],))
        self.fails("invalid_principal", self.promote, item)

    def test_unknown_commands_queries_and_malformed_json_values(self):
        self.fails("unknown_command", self.command, "knowledge_auto_accept", {})
        self.fails("unknown_query", knowledge.query, self.con, self.project, self.worker, "knowledge_touch", {})
        for data in (self.data(source_refs=[{"kind": [], "sha256": "a" * 64}]),
                     self.data(confidence=float("nan")), self.data(body="\ud800")):
            self.fails("invalid_payload", self.command, "knowledge_propose", data)


if __name__ == "__main__":
    unittest.main()
