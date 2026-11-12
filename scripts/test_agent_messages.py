"""Offline, real-SQLite regression coverage for the P1-1 messaging domain."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import agent_messages as messages
import awo_service as service


NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
PROJECT = "project-one"


def principal(agent_id, role="worker", project_id=PROJECT):
    return {"agent_id": agent_id, "role": role, "principal_id": "agent:" + agent_id,
            "project_id": project_id}


class MessageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="awo-message-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "workflow.sqlite3"
        self.con = sqlite3.connect(self.path, timeout=15)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA foreign_keys=ON")
        self.addCleanup(self.con.close)
        self.clock = patch.object(messages, "_now", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.controller = principal("controller", "controller")
        self.worker = principal("worker")
        self.peer = principal("peer")
        self.reviewer = principal("reviewer", "reviewer")
        self.observer = principal("observer", "observer")
        self.other = principal("other", project_id="project-two")
        with self.tx():
            service.create_schema(self.con)
            messages.create_message_schema(self.con)
            for project in (PROJECT, "project-two"):
                self.con.execute("INSERT INTO v2_project VALUES (?,?,?)", (project, str(self.root), NOW.isoformat()))
            for person in (self.controller, self.worker, self.peer, self.reviewer, self.observer, self.other):
                self.con.execute("INSERT INTO v2_agent(agent_id,project_id,handle,role,harness,harness_version,"
                                 "host,os,lifecycle,source,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                 (person["agent_id"], person["project_id"], person["agent_id"], person["role"],
                                  "test-harness", "1.2", "test-host", "test-os", "active", "test", NOW.isoformat()))
            for task_id, agent_id, project in (("task-worker", "worker", PROJECT),
                                                ("task-peer", "peer", PROJECT),
                                                ("task-other", "other", "project-two")):
                self.con.execute("INSERT INTO v2_task(task_id,project_id,task_state,created_at,updated_at) "
                                 "VALUES (?,?,'active',?,?)", (task_id, project, NOW.isoformat(), NOW.isoformat()))
                self.con.execute("INSERT INTO v2_assignment(assignment_id,task_id,agent_id,generation,"
                                 "holds_slot,state,created_at,updated_at) VALUES (?,?,?,1,1,'running',?,?)",
                                 ("assignment-" + agent_id, task_id, agent_id, NOW.isoformat(), NOW.isoformat()))

    @contextmanager
    def tx(self, con=None):
        con = con or self.con
        con.execute("BEGIN IMMEDIATE")
        try:
            yield con
        except BaseException:
            con.rollback()
            raise
        else:
            con.commit()

    def send(self, sender=None, kind="notice", to=None, **extra):
        payload = {"kind": kind, "to": ["peer"] if to is None else to, "body": "A durable message."}
        payload.update(extra)
        with self.tx():
            return messages.apply_command(self.con, PROJECT, sender or self.worker, "message.send", payload)

    def ack(self, person, delivery, state="acknowledged", expected_revision=1):
        with self.tx():
            return messages.apply_command(self.con, PROJECT, person, "message.ack", {
                "delivery_id": delivery, "state": state, "expected_revision": expected_revision})

    def read(self, person=None, **filters):
        return messages.read_messages(self.con, PROJECT, person or self.peer, filters)["messages"]

    def count(self, table):
        return self.con.execute("SELECT count(*) FROM " + table).fetchone()[0]

    def snapshot(self):
        return {table: self.con.execute("SELECT * FROM " + table).fetchall() for table in (
            "v2_message", "v2_message_delivery", "v2_event", "v2_task", "v2_assignment", "v2_command_log")}

    def assert_rejected(self, operation, code=None):
        before = self.snapshot()
        with self.assertRaises(service.ServiceError) as caught:
            operation()
        if code:
            self.assertIn(code, str(caught.exception))
        self.assertEqual(self.snapshot(), before)

    def question(self, to=None, **extra):
        return self.send(kind="question", to=to, **extra)

    def answer(self, question, person=None, **extra):
        return self.send(sender=person or self.peer, kind="answer", to=["worker"],
                         reply_to=question["message_id"], **extra)

    def capability(self, level="supported", **extra):
        evidence = self.root / "capability.txt"
        evidence.write_text("measured steering capability", encoding="utf-8")
        values = {"agent_id": "worker", "capability_key": "steer", "level": level,
                  "harness_version": "1.2", "os": "test-os", "host": "test-host",
                  "checked_at": (NOW - timedelta(minutes=1)).isoformat(),
                  "evidence_sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
                  "evidence_path": str(evidence), "source": "regression_test", "declared_by": "controller"}
        values.update(extra)
        with self.tx():
            self.con.execute("INSERT OR REPLACE INTO v2_agent_capability(" + ",".join(values)
                             + ") VALUES (" + ",".join("?" for _ in values) + ")", tuple(values.values()))
        return evidence

    def steer(self, sender=None, **extra):
        payload = {"assignment_id": "assignment-worker", "generation": 1}
        payload.update(extra)
        return self.send(sender=sender or self.controller, kind="steer", to=["worker"], **payload)

    def test_schema_is_repeatable_and_foreign_keys_are_valid(self):
        with self.tx():
            messages.create_message_schema(self.con)
            messages.create_message_schema(self.con)
        self.assertEqual(self.con.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_domain_also_supports_default_tuple_rows(self):
        self.con.row_factory = None
        response = self.send()
        delivery = response["deliveries"][0]["delivery_id"]
        self.assertEqual(self.read()[0]["delivery_id"], delivery)
        self.assertEqual(self.ack(self.peer, delivery)["revision"], 2)

    def test_schema_and_commands_require_caller_transaction(self):
        self.assert_rejected(lambda: messages.create_message_schema(self.con), "transaction_required")
        self.assert_rejected(lambda: messages.apply_command(
            self.con, PROJECT, self.worker, "message.send", {}), "transaction_required")

    def test_schema_creation_does_not_commit_callers_transaction(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.execute("BEGIN")
        messages.create_message_schema(con)
        self.assertTrue(con.in_transaction)
        con.rollback()
        self.assertFalse(con.execute("SELECT name FROM sqlite_master WHERE name='v2_message'").fetchone())

    def test_all_seven_kinds_and_no_business_state_changes(self):
        before = self.snapshot()
        for kind in ("question", "handoff", "notice", "consult", "state_request"):
            response = self.send(kind=kind, body="accepted; completed; cancel this task")
            if kind == "question":
                self.answer(response)
            else:
                self.ack(self.peer, response["deliveries"][0]["delivery_id"])
        self.steer()
        self.assertEqual({row[0] for row in self.con.execute("SELECT kind FROM v2_message")}, set(messages.KINDS))
        after = self.snapshot()
        self.assertEqual(before["v2_task"], after["v2_task"])
        self.assertEqual(before["v2_assignment"], after["v2_assignment"])
        self.assertEqual(before["v2_command_log"], after["v2_command_log"])
        self.assertTrue(all(row[2] in ("message", "message_delivery") for row in after["v2_event"]))

    def test_send_role_matrix(self):
        for person in (self.controller, self.worker, self.reviewer):
            with self.subTest(role=person["role"]):
                self.assertEqual(self.send(sender=person)["status"], "CREATED")
        self.assert_rejected(lambda: self.send(sender=self.observer), "permission_denied")

    def test_read_and_ack_role_matrix(self):
        for person in (self.controller, self.worker, self.reviewer, self.observer):
            with self.subTest(role=person["role"]):
                response = self.send(sender=self.peer, to=[person["agent_id"]])
                delivery = response["deliveries"][0]["delivery_id"]
                before = self.snapshot()
                self.assertEqual(self.read(person)[0]["delivery_id"], delivery)
                self.assertEqual(self.snapshot(), before)
                if person == self.observer:
                    self.assert_rejected(lambda: self.ack(person, delivery), "permission_denied")
                else:
                    self.assertEqual(self.ack(person, delivery)["revision"], 2)

    def test_service_observer_downgrade_can_only_read_its_own_inbox(self):
        sent = self.send(to=["worker"])
        observer_scope = dict(self.worker, role="observer")
        self.assertEqual(self.read(observer_scope)[0]["message_id"], sent["message_id"])
        self.assert_rejected(lambda: self.send(sender=observer_scope), "permission_denied")
        self.assert_rejected(lambda: self.ack(observer_scope, sent["deliveries"][0]["delivery_id"]),
                             "permission_denied")
        self.send(to=["peer"])
        self.assertEqual(len(self.read(observer_scope)), 1)

    def test_principal_must_match_active_registry_project_role(self):
        cases = [None, {}, dict(self.worker, role="controller"),
                 dict(self.worker, project_id="project-two"), dict(self.worker, agent_id="missing"),
                 dict(self.worker, principal_id=""), dict(self.worker, role="owner")]
        for person in cases:
            with self.subTest(person=person):
                self.assert_rejected(lambda: messages.read_messages(self.con, PROJECT, person, {}))
                def write():
                    with self.tx():
                        messages.apply_command(self.con, PROJECT, person, "message.send",
                                               {"to": ["peer"], "kind": "notice", "body": "test"})
                self.assert_rejected(write)
        for lifecycle in ("pending", "revoked"):
            with self.tx():
                self.con.execute("UPDATE v2_agent SET lifecycle=? WHERE agent_id='worker'", (lifecycle,))
            self.assert_rejected(lambda: self.send(), "permission_denied")
            self.assert_rejected(lambda: self.read(self.worker), "permission_denied")

    def test_recipient_must_be_registered_active_same_project(self):
        for recipient in ("other", "missing", "task/task-worker", "run/run-1", "human/owner", "milestone/one", {}):
            with self.subTest(recipient=recipient):
                self.assert_rejected(lambda: self.send(to=["peer", recipient]))
        for lifecycle in ("pending", "revoked"):
            with self.tx():
                self.con.execute("UPDATE v2_agent SET lifecycle=? WHERE agent_id='peer'", (lifecycle,))
            self.assert_rejected(lambda: self.send(), "permission_denied")

    def test_strict_inbox_acl_even_for_controller_or_sender(self):
        response = self.send()
        for person in (self.worker, self.controller, self.reviewer, self.observer):
            with self.subTest(role=person["role"]):
                self.assertEqual(self.read(person, message_id=response["message_id"]), [])
                self.assertEqual(self.read(person, thread_id=response["thread_id"]), [])
                self.assert_rejected(lambda: self.ack(person, response["deliveries"][0]["delivery_id"]))
        self.assertEqual(len(self.read(self.peer)), 1)
        self.assert_rejected(lambda: self.read(self.controller, recipient_agent_id="peer"))
        self.assert_rejected(lambda: self.read(self.observer, mark_read=True))

    def test_send_rejects_identity_and_authority_fields(self):
        for field in ("agent_id", "role", "principal", "principal_id", "project_id", "from_principal", "sender_agent_id",
                      "command_id", "idempotency_key", "expected_epoch", "gate", "wake", "steer_status"):
            with self.subTest(field=field):
                self.assert_rejected(lambda: self.send(**{field: "spoof"}), "invalid_request")

    def test_input_validation_and_bounded_recipient_deduplication(self):
        for extra in ({"kind": "message"}, {"kind": []}, {"body": ""}, {"body": []},
                      {"body": "a" * 65537}, {"body": "\ud800"}, {"body": "a\x00b"},
                      {"priority": []}, {"to": []}, {"to": "peer"}, {"to": ["peer"] * 51}):
            with self.subTest(extra=repr(extra)[:100]):
                self.assert_rejected(lambda: self.send(**extra))
        response = self.send(to=["peer", "peer", "reviewer"])
        self.assertEqual(len(response["deliveries"]), 2)
        self.assertEqual(self.count("v2_message"), 1)
        self.assertEqual(self.count("v2_message_delivery"), 2)

    def test_no_self_question_including_duplicate_recipients(self):
        self.assert_rejected(lambda: self.question(to=["peer", "worker", "worker"]), "permission_denied")

    def test_worker_binding_ownership_project_and_generation(self):
        invalid = ({"task_id": "task-peer"}, {"task_id": "task-other"}, {"task_id": "missing"},
                   {"assignment_id": "assignment-peer", "generation": 1},
                   {"assignment_id": "assignment-other", "generation": 1},
                   {"assignment_id": "missing", "generation": 1},
                   {"assignment_id": "assignment-worker"},
                   {"assignment_id": "assignment-worker", "generation": 2},
                   {"assignment_id": "assignment-worker", "generation": True}, {"generation": 1},
                   {"assignment_id": "assignment-worker", "generation": 1, "task_id": "task-peer"})
        for binding in invalid:
            with self.subTest(binding=binding):
                self.assert_rejected(lambda: self.send(**binding))
        self.send(task_id="task-worker")
        response = self.send(assignment_id="assignment-worker", generation=1)
        self.assertEqual(self.read(message_id=response["message_id"])[0]["task_id"], "task-worker")
        self.send(sender=self.controller, assignment_id="assignment-peer", generation=1)
        self.send(sender=self.reviewer, task_id="task-peer")

    def test_bound_assignment_owner_must_belong_to_project(self):
        for owner in (None, "other"):
            with self.tx():
                self.con.execute("UPDATE v2_assignment SET agent_id=? WHERE assignment_id='assignment-worker'", (owner,))
            self.assert_rejected(lambda: self.send(sender=self.controller,
                                                   assignment_id="assignment-worker", generation=1), "permission_denied")

    def test_unicode_payload_and_malformed_identifier_validation(self):
        response = self.send(body="\u4f60\u597d")
        self.assertEqual(self.read(message_id=response["message_id"])[0]["body"], "\u4f60\u597d")
        self.assert_rejected(lambda: self.send(body="\u4f60" * 21846), "invalid_request")
        self.assert_rejected(lambda: self.send(to=["\ud800"]), "invalid_request")
        self.assert_rejected(lambda: self.send(thread_id="\ud800"), "invalid_request")
        self.assert_rejected(lambda: self.read(offset=2**63), "invalid_request")

    def test_superseded_generation_is_rejected(self):
        question = self.question(assignment_id="assignment-worker", generation=1)
        with self.tx():
            self.con.execute("UPDATE v2_assignment SET holds_slot=0 WHERE assignment_id='assignment-worker'")
            self.con.execute("INSERT INTO v2_assignment(assignment_id,task_id,agent_id,generation,holds_slot,"
                             "state,created_at,updated_at) VALUES ('next','task-worker','worker',2,1,'running',?,?)",
                             (NOW.isoformat(), NOW.isoformat()))
        self.assert_rejected(lambda: self.send(assignment_id="assignment-worker", generation=1), "state_conflict")
        self.assert_rejected(lambda: self.answer(question), "state_conflict")

    def test_recipient_can_answer_bound_question_without_assignment_ownership(self):
        question = self.question(assignment_id="assignment-worker", generation=1)
        answer = self.answer(question)
        stored = self.read(self.worker, message_id=answer["message_id"])[0]
        self.assertEqual((stored["task_id"], stored["assignment_id"], stored["generation"]),
                         ("task-worker", "assignment-worker", 1))
        self.assertEqual(stored["thread_id"], question["thread_id"])

    def test_answer_requires_recipient_and_routes_only_to_question_sender(self):
        question = self.question()
        self.assert_rejected(lambda: self.answer(question, person=self.controller), "permission_denied")
        self.assert_rejected(lambda: self.answer(question, person=self.worker), "permission_denied")
        self.assert_rejected(lambda: self.send(sender=self.peer, kind="answer", to=["reviewer"],
                                               reply_to=question["message_id"]), "permission_denied")
        self.assert_rejected(lambda: self.send(sender=self.peer, kind="answer", to=["worker", "reviewer"],
                                               reply_to=question["message_id"]), "permission_denied")
        self.assert_rejected(lambda: self.send(sender=self.peer, kind="answer", to=["worker"]))
        notice = self.send()
        self.assert_rejected(lambda: self.answer(notice), "state_conflict")

    def test_only_one_answer_with_partial_unique_index(self):
        question = self.question(to=["peer", "reviewer"])
        answer = self.answer(question)
        self.assert_rejected(lambda: self.answer(question, person=self.reviewer), "state_conflict")
        self.assertEqual(self.read(self.peer)[0]["state"], "answered")
        # Enforce the invariant below the Python precheck too.
        with self.assertRaises(sqlite3.IntegrityError):
            with self.tx():
                self.con.execute("INSERT INTO v2_message(message_id,project_id,sender_agent_id,from_principal,"
                                 "kind,body,thread_id,reply_to,priority,created_at) "
                                 "VALUES ('duplicate',?,'reviewer','agent:reviewer','answer','no',?,?,'P3',?)",
                                 (PROJECT, answer["thread_id"], question["message_id"], NOW.isoformat()))

    def test_reply_binding_and_thread_cannot_change(self):
        question = self.question(task_id="task-worker")
        for fields in ({"task_id": "task-peer"}, {"assignment_id": "assignment-peer"},
                       {"generation": 1}, {"thread_id": "hidden-thread"}):
            with self.subTest(fields=fields):
                self.assert_rejected(lambda: self.answer(question, **fields), "state_conflict")

    def test_threads_do_not_grant_bystander_access(self):
        response = self.send(thread_id="private-thread")
        self.assert_rejected(lambda: self.send(sender=self.reviewer, thread_id="private-thread"), "permission_denied")
        self.assert_rejected(lambda: self.send(sender=self.reviewer, reply_to=response["message_id"]), "permission_denied")
        self.send(sender=self.peer, to=["worker"], reply_to=response["message_id"])
        self.assertEqual(len(self.read(self.worker, thread_id="private-thread")), 1)
        self.assertEqual(len(self.read(self.peer, thread_id="private-thread")), 1)

    def test_ack_requires_expected_revision_and_strict_type(self):
        response = self.send()
        delivery = response["deliveries"][0]["delivery_id"]
        for value in (None, True, "1", 0, 1.0, -1, 2):
            with self.subTest(expected=value):
                self.assert_rejected(lambda: self.ack(self.peer, delivery, expected_revision=value))
        self.assertEqual(self.ack(self.peer, delivery, "read")["revision"], 2)
        self.assert_rejected(lambda: self.ack(self.peer, delivery), "revision_conflict")
        self.assertEqual(self.ack(self.peer, delivery, expected_revision=2)["revision"], 3)

    def test_ack_is_monotonic_and_same_state_is_noop(self):
        response = self.send()
        delivery = response["deliveries"][0]["delivery_id"]
        self.ack(self.peer, delivery)
        before = self.snapshot()
        self.assertEqual(self.ack(self.peer, delivery, expected_revision=2)["status"], "UNCHANGED")
        self.assertEqual(before, self.snapshot())
        self.assert_rejected(lambda: self.ack(self.peer, delivery, "read", 2), "state_conflict")
        for state in ("expired", "answered", "held", [], None):
            self.assert_rejected(lambda: self.ack(self.peer, delivery, state, 2), "invalid_request")
        question = self.question()
        self.answer(question)
        self.assert_rejected(lambda: self.ack(self.peer, question["deliveries"][0]["delivery_id"],
                                               expected_revision=2), "state_conflict")

    def test_ack_rejects_extra_fields_and_cross_project_ids(self):
        response = self.send()
        delivery = response["deliveries"][0]["delivery_id"]
        with self.tx():
            foreign = messages.apply_command(self.con, "project-two", self.other, "message.send",
                                             {"to": ["other"], "kind": "notice", "body": "private"})
        self.assert_rejected(lambda: self.ack(self.peer, foreign["deliveries"][0]["delivery_id"]))
        def bad_ack():
            with self.tx():
                messages.apply_command(self.con, PROJECT, self.peer, "message.ack", {
                    "delivery_id": delivery, "state": "read", "expected_revision": 1, "agent_id": "peer"})
        self.assert_rejected(bad_ack, "invalid_request")

    def test_expiry_is_pure_even_on_readonly_connection(self):
        response = self.question(expires_at=(NOW + timedelta(seconds=1)).isoformat())
        before = self.snapshot()
        ro = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
        self.addCleanup(ro.close)
        with patch.object(messages, "_now", return_value=NOW + timedelta(seconds=1)):
            row = messages.read_messages(ro, PROJECT, self.peer, {"state": "expired"})["messages"][0]
            self.assertEqual(row["state"], "expired")
            self.assertEqual(row["stored_state"], "held")
            self.assertEqual(row["revision"], 1)
            self.assertTrue(row["message_expired"])
            self.assert_rejected(lambda: self.answer(response), "state_conflict")
            self.assert_rejected(lambda: self.ack(self.peer, row["delivery_id"]), "state_conflict")
        self.assertEqual(before, self.snapshot())
        self.assertEqual(ro.total_changes, 0)

    def test_expiry_maintenance_is_transactional_scoped_and_repeatable(self):
        response = self.send(expires_at=(NOW + timedelta(seconds=1)).isoformat())
        with self.tx():
            messages.apply_command(self.con, "project-two", self.other, "message.send", {
                "to": ["other"], "kind": "notice", "body": "private", "expires_at": (NOW + timedelta(seconds=1)).isoformat()})
        before = self.snapshot()
        with patch.object(messages, "_now", return_value=NOW + timedelta(seconds=1)):
            self.con.execute("BEGIN IMMEDIATE")
            result = messages.expire_deliveries(self.con, PROJECT, self.controller)
            self.assertEqual(result["expired_count"], 1)
            self.con.rollback()
            self.assertEqual(before, self.snapshot())
            with self.tx():
                messages.expire_deliveries(self.con, PROJECT, self.controller)
            count = self.count("v2_event")
            with self.tx():
                self.assertEqual(messages.expire_deliveries(self.con, PROJECT, self.controller)["expired_count"], 0)
            self.assertEqual(self.count("v2_event"), count)
        row = self.read(delivery_id=response["deliveries"][0]["delivery_id"])[0]
        self.assertEqual((row["stored_state"], row["revision"]), ("expired", 2))
        self.assertEqual(self.con.execute("SELECT state FROM v2_message_delivery WHERE project_id='project-two'").fetchone()[0], "held")

    def test_expiry_maintenance_requires_controller(self):
        for person in (self.worker, self.reviewer, self.observer):
            def expire():
                with self.tx():
                    messages.expire_deliveries(self.con, PROJECT, person)
            self.assert_rejected(expire, "permission_denied")

    def test_read_question_still_cannot_be_answered_after_ttl(self):
        response = self.question(expires_at=(NOW + timedelta(seconds=1)).isoformat())
        delivery = response["deliveries"][0]["delivery_id"]
        self.ack(self.peer, delivery, "read")
        with patch.object(messages, "_now", return_value=NOW + timedelta(seconds=2)):
            self.assert_rejected(lambda: self.answer(response), "state_conflict")
            with self.tx():
                self.assertEqual(messages.expire_deliveries(self.con, PROJECT, self.controller)["expired_count"], 0)
            self.assertEqual(self.read()[0]["state"], "read")
            self.ack(self.peer, delivery, expected_revision=2)

    def test_effective_state_filter_precedes_pagination(self):
        for index in range(3):
            self.send(body=str(index), expires_at=(NOW + timedelta(seconds=1)).isoformat())
        alive = self.send(body="unexpired")
        with patch.object(messages, "_now", return_value=NOW + timedelta(seconds=1)):
            unread = self.read(state="held", limit=1)
            self.assertEqual([row["message_id"] for row in unread], [alive["message_id"]])
            self.assertEqual(len(self.read(state="expired", limit=2)), 2)
            self.assertEqual(len(self.read(state="expired", limit=2, offset=2)), 1)

    def test_expiry_timestamp_validation_and_timezone_normalization(self):
        for expiry in (NOW.isoformat(), (NOW - timedelta(seconds=1)).isoformat(),
                       "2027-01-01T00:00:00", "bad-date", 12, [], "99999-01-01T00:00:00Z"):
            with self.subTest(expiry=expiry):
                self.assert_rejected(lambda: self.send(expires_at=expiry))
        self.send(expires_at="2026-11-10T21:00:00+08:00")
        self.assertEqual(self.read()[0]["expires_at"], "2026-11-10T13:00:00+00:00")

    def test_steer_capability_unknown_unsupported_and_verified_are_honest(self):
        unknown = self.steer()
        self.assertEqual((unknown["steer_status"], unknown["capability_level"]), ("queued", "unknown"))
        self.capability("unknown")
        self.assertEqual(self.steer()["capability_level"], "unknown")
        self.capability("unsupported")
        unsupported = self.steer()
        self.assertEqual((unsupported["steer_status"], unsupported["capability_level"]), ("unsupported", "unsupported"))
        self.capability()
        supported = self.steer(sender=self.reviewer)
        self.assertEqual((supported["steer_status"], supported["capability_level"]), ("queued", "supported"))
        self.assertTrue(all(row["stored_state"] == "held" for row in self.read(self.worker)))
        self.assertNotIn("injected", supported)

    def test_capability_drift_and_unverifiable_evidence_remain_unknown(self):
        for values in ({"harness_version": "old"}, {"os": "old"}, {"host": "old"},
                       {"source": "self_declared"}, {"source": "imported_binding"}, {"checked_at": None},
                       {"checked_at": "broken"}, {"checked_at": (NOW + timedelta(days=1)).isoformat()},
                       {"evidence_path": None}, {"evidence_path": str(self.root / "absent")},
                       {"evidence_sha256": "0" * 64}):
            with self.subTest(values=values):
                self.capability(**values)
                result = self.steer()
                self.assertEqual((result["steer_status"], result["capability_level"]), ("queued", "unknown"))
        evidence = self.capability()
        evidence.write_text("changed", encoding="utf-8")
        self.assertEqual(self.steer()["capability_reason"], "capability_evidence_changed")

    def test_steer_requires_authority_current_running_target(self):
        self.assert_rejected(lambda: self.steer(sender=self.worker), "permission_denied")
        self.assert_rejected(lambda: self.send(sender=self.controller, kind="steer", to=["worker"]))
        self.assert_rejected(lambda: self.send(sender=self.controller, kind="steer", to=["peer"],
                                               assignment_id="assignment-worker", generation=1))
        self.assert_rejected(lambda: self.steer(generation=2))
        for state, holds_slot in (("completed", 1), ("blocked", 1), ("running", 0)):
            with self.tx():
                self.con.execute("UPDATE v2_assignment SET state=?,holds_slot=? WHERE assignment_id='assignment-worker'",
                                 (state, holds_slot))
            self.assert_rejected(lambda: self.steer(), "state_conflict")

    def test_read_filters_and_pagination_do_not_widen_acl(self):
        for index in range(6):
            self.send(kind="notice" if index % 2 else "consult", body=str(index), thread_id="thread")
        all_rows = self.read()
        paged = self.read(limit=2, offset=0) + self.read(limit=2, offset=2) + self.read(limit=2, offset=4)
        self.assertEqual(all_rows, paged)
        self.assertEqual(len(self.read(kind="notice", thread_id="thread")), 3)
        self.assertEqual(self.read(self.controller, kind="notice", limit=500), [])
        for filters in ({"limit": 501}, {"limit": True}, {"offset": -1}, {"offset": True},
                        {"state": "bad"}, {"kind": "bad"}, {"mark_read": True}):
            self.assert_rejected(lambda: self.read(**filters))

    def test_message_and_event_roll_back_together(self):
        before = self.snapshot()
        self.con.execute("BEGIN IMMEDIATE")
        messages.apply_command(self.con, PROJECT, self.worker, "message.send", {
            "to": ["peer"], "kind": "notice", "body": "rollback"})
        self.assertTrue(self.con.in_transaction)
        self.con.rollback()
        self.assertEqual(before, self.snapshot())
        with patch.object(service, "_event", side_effect=RuntimeError("event write failed")):
            with self.assertRaises(RuntimeError):
                self.send()
        self.assertEqual(before, self.snapshot())

    def test_domain_does_not_invent_its_own_replay_authority(self):
        first = self.send(body="same content")
        second = self.send(body="same content")
        self.assertNotEqual(first["message_id"], second["message_id"])
        self.assertEqual(self.count("v2_command_log"), 0)
        self.assertNotIn("replayed", first)

    def replay_boundary(self, person, command_id, tool, payload, project_id=PROJECT):
        """Test adapter exercising the accepted parent's real replay helpers."""
        env = {"command_id": command_id, "tool": tool, "payload": payload}
        with self.tx():
            # The parent authenticates before replay; do not expose another
            # principal's response by performing global command_id lookup.
            digest = service._request_sha(env)
            old = service._replay(self.con, project_id, person["principal_id"], env, digest)
            if old is not None:
                return old
            result = messages.apply_command(self.con, project_id, person, tool, payload)
            result["replayed"] = False
            service._record(self.con, project_id, person["principal_id"], env, digest, result)
            return result

    def test_parent_replay_preserves_message_and_delivery_ids(self):
        payload = {"to": ["peer"], "kind": "notice", "body": "durable"}
        first = self.replay_boundary(self.worker, "cmd-send-test", "message.send", payload)
        before = self.snapshot()
        replay = self.replay_boundary(self.worker, "cmd-send-test", "message.send", payload)
        self.assertEqual(replay, dict(first, replayed=True))
        self.assertEqual(before, self.snapshot())
        ack = {"delivery_id": first["deliveries"][0]["delivery_id"], "state": "read", "expected_revision": 1}
        original = self.replay_boundary(self.peer, "cmd-ack-test", "message.ack", ack)
        before = self.snapshot()
        self.assertEqual(self.replay_boundary(self.peer, "cmd-ack-test", "message.ack", ack), dict(original, replayed=True))
        self.assertEqual(before, self.snapshot())
        self.assert_rejected(lambda: self.ack(self.peer, ack["delivery_id"], "read", 1), "revision_conflict")
        self.assert_rejected(lambda: self.replay_boundary(self.worker, "cmd-send-test", "message.send",
                                                          dict(payload, body="different")))

    def test_parent_replay_is_scoped_by_principal_and_project(self):
        payload = {"to": ["peer"], "kind": "notice", "body": "same"}
        first = self.replay_boundary(self.worker, "cmd-scoped", "message.send", payload)
        second = self.replay_boundary(self.controller, "cmd-scoped", "message.send", payload)
        other = self.replay_boundary(self.other, "cmd-scoped", "message.send",
                                     dict(payload, to=["other"]), "project-two")
        self.assertEqual(len({first["message_id"], second["message_id"], other["message_id"]}), 3)
        self.assertEqual(len(self.read()), 2)

    def test_committed_answer_replays_after_expiry_without_new_answer(self):
        question = self.question(expires_at=(NOW + timedelta(seconds=1)).isoformat())
        payload = {"to": ["worker"], "kind": "answer", "body": "original answer",
                   "reply_to": question["message_id"]}
        first = self.replay_boundary(self.peer, "cmd-answer", "message.send", payload)
        before = self.snapshot()
        with patch.object(messages, "_now", return_value=NOW + timedelta(seconds=2)):
            replay = self.replay_boundary(self.peer, "cmd-answer", "message.send", payload)
            self.assertEqual(replay, dict(first, replayed=True))
            self.assert_rejected(lambda: self.replay_boundary(self.peer, "cmd-new-answer", "message.send", payload))
        self.assertEqual(before, self.snapshot())

    def test_restart_process_preserves_unread_delivery_ack_and_replay(self):
        payload = {"to": ["peer"], "kind": "notice", "body": "survives restart"}
        sent = self.replay_boundary(self.worker, "restart-send", "message.send", payload)
        script = """import json, sqlite3, sys
sys.path.insert(0, sys.argv[1])
import agent_messages as m
import awo_service as s
con = sqlite3.connect(sys.argv[2]); con.execute('PRAGMA foreign_keys=ON')
p = json.loads(sys.argv[3]); payload = json.loads(sys.argv[4])
rows = m.read_messages(con, p['project_id'], p, {})['messages']
con.execute('BEGIN IMMEDIATE')
result = m.apply_command(con, p['project_id'], p, 'message.ack', {
    'delivery_id': rows[0]['delivery_id'], 'state': 'acknowledged', 'expected_revision': rows[0]['revision']})
con.commit()
con.row_factory = sqlite3.Row
env = {'command_id': 'restart-send', 'tool': 'message.send', 'payload': payload}
replay = s._replay(con, p['project_id'], 'agent:worker', env, s._request_sha(env))
print(json.dumps({'before': rows, 'ack': result, 'replay': replay}))
con.close()
"""
        result = subprocess.run([sys.executable, "-B", "-c", script, str(Path(messages.__file__).parent),
                                 str(self.path), json.dumps(self.peer), json.dumps(payload)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = json.loads(result.stdout)
        self.assertEqual(observed["before"][0]["state"], "held")
        self.assertEqual(observed["ack"]["delivery_id"], sent["deliveries"][0]["delivery_id"])
        self.assertEqual(observed["replay"], dict(sent, replayed=True))
        self.assertEqual((self.read()[0]["state"], self.read()[0]["revision"]), ("acknowledged", 2))

    def test_concurrent_answers_have_exactly_one_winner(self):
        question = self.question(to=["peer", "reviewer"])
        barrier = threading.Barrier(2)
        outcomes = []
        lock = threading.Lock()

        def answer(person):
            con = sqlite3.connect(self.path, timeout=15)
            con.execute("PRAGMA foreign_keys=ON")
            try:
                barrier.wait(timeout=10)
                with self.tx(con):
                    messages.apply_command(con, PROJECT, person, "message.send", {
                        "kind": "answer", "to": ["worker"], "body": "one answer", "reply_to": question["message_id"]})
                outcome = "created"
            except service.ServiceError as exc:
                outcome = str(exc).split(":")[0]
            except BaseException as exc:
                outcome = repr(exc)
            finally:
                con.close()
            with lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=answer, args=(person,)) for person in (self.peer, self.reviewer)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=25)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sorted(outcomes), ["created", "state_conflict"])
        self.assertEqual(self.con.execute("SELECT count(*) FROM v2_message WHERE kind='answer'").fetchone()[0], 1)

    def test_restart_preserves_ttl_without_read_side_effects(self):
        self.question(expires_at=(NOW + timedelta(seconds=1)).isoformat())
        before = self.snapshot()
        script = """import json, sqlite3, sys
from datetime import datetime
sys.path.insert(0, sys.argv[1])
import agent_messages as m
import awo_service as s
m._now = lambda: datetime.fromisoformat(sys.argv[4])
con = sqlite3.connect(sys.argv[2])
p = json.loads(sys.argv[3])
rows = m.read_messages(con, p['project_id'], p, {})['messages']
con.execute('BEGIN IMMEDIATE')
try:
    m.apply_command(con, p['project_id'], p, 'message.ack', {
        'delivery_id': rows[0]['delivery_id'], 'state': 'read', 'expected_revision': 1})
except s.ServiceError as exc:
    error = str(exc)
else:
    raise RuntimeError('expired ACK was accepted')
finally:
    con.rollback()
print(json.dumps({'rows': rows, 'error': error, 'changes': con.total_changes}))
con.close()
"""
        result = subprocess.run([sys.executable, "-B", "-c", script, str(Path(messages.__file__).parent),
                                 str(self.path), json.dumps(self.peer), (NOW + timedelta(seconds=2)).isoformat()],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = json.loads(result.stdout)
        self.assertEqual(observed["rows"][0]["state"], "expired")
        self.assertIn("state_conflict", observed["error"])
        self.assertEqual(observed["changes"], 0)
        self.assertEqual(before, self.snapshot())

    def test_concurrent_ack_revision_has_exactly_one_winner(self):
        sent = self.send()
        delivery = sent["deliveries"][0]["delivery_id"]
        barrier = threading.Barrier(2)
        outcomes = []
        lock = threading.Lock()

        def acknowledge():
            con = sqlite3.connect(self.path, timeout=15)
            try:
                barrier.wait(timeout=10)
                with self.tx(con):
                    result = messages.apply_command(con, PROJECT, self.peer, "message.ack", {
                        "delivery_id": delivery, "state": "read", "expected_revision": 1})
                outcome = result["status"]
            except service.ServiceError as exc:
                outcome = str(exc).split(":")[0]
            except BaseException as exc:
                outcome = repr(exc)
            finally:
                con.close()
            with lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=acknowledge) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=25)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sorted(outcomes), ["UPDATED", "revision_conflict"])
        self.assertEqual(self.read()[0]["revision"], 2)


if __name__ == "__main__":
    unittest.main()
