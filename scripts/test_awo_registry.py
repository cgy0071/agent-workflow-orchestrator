"""Regression tests for the minimal V2 agent registry and capabilities."""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import unittest
import uuid
from unittest import mock
from unittest.mock import patch

import awo_service as service
import workflow_control as control


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = (Path(__file__).resolve().parents[1] / "runtime").resolve()
        self.sandbox.mkdir(exist_ok=True)
        self.root = self.sandbox / ("test-awo-registry-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.state = self.root / "state"
        self.secrets = self.root / "secrets"
        self.secrets.mkdir()
        self.config = {"roles": {"planner": {}, "executor": {}}, "workflow": {
            "dispatch_role": "planner", "execution_role": "executor",
            "reviewer_role": "planner",
            "transitions": {"accept": "next", "repair": "repair", "reject": "stop"}}}
        control.initialize(self.state, self.config)
        self.planner_spec = self.spec("planner")
        self.worker_spec = self.spec("executor")
        self.planner = control.bind(self.state, "planner", self.planner_spec,
                                    self.planner_spec["expected_identity"], "controller")
        self.worker = control.bind(self.state, "executor", self.worker_spec,
                                   self.worker_spec["expected_identity"])
        self.actor = (self.planner["instance_id"], self.planner["version"], "main")
        service.apply_migration(self.state, *self.actor)
        self.epoch = service.service_status(self.state)["controller_epoch"]

    def tearDown(self):
        target = self.root.resolve()
        if target.parent != self.sandbox or not target.name.startswith("test-awo-registry-"):
            raise RuntimeError("refuse unsafe cleanup")
        shutil.rmtree(target)

    def spec(self, role):
        identity = {"kind": "any-harness", "terminal": "terminal-" + role,
                    "launch_cwd": str(self.root), "instance_id": "frozen-" + role}
        status = {"identity": identity, "status": "idle", "completion_seq": 10}
        return {"role": role, "harness": "any-harness", "version": "unknown",
                "adapter": "command", "expected_identity": identity,
                "identity_argv": [sys.executable, "-c", "print(" + repr(json.dumps(status)) + ")"],
                "status_argv": [sys.executable, "-c", "print(" + repr(json.dumps(status)) + ")"],
                "prompt_argv": [sys.executable, "-c", "print('offline')", "{message}"],
                "output_argv": [sys.executable, "-c", "print('offline')"]}

    @contextmanager
    def db(self):
        con = sqlite3.connect(self.state / "workflow.sqlite3")
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def envelope(self, tool, payload, command_id=None, epoch=None):
        return {"command_id": command_id or ("cmd-" + uuid.uuid4().hex),
                "tool": tool, "expected_epoch": self.epoch if epoch is None else epoch,
                "payload": payload}

    def enroll(self, role="worker", allowed=("status", "collect"), minutes=10,
               command_id=None, secret_name=None):
        target = self.secrets / (secret_name or ("grant-" + uuid.uuid4().hex + ".txt"))
        result = service._enroll_agent(
            self.state, *self.actor,
            self.envelope("agent.enroll", {"role": role, "allowed_capabilities": list(allowed),
                                           "expires_minutes": minutes}, command_id=command_id),
            str(target))
        return result, target

    def register(self, grant_file, handle, capabilities=("status",), command_id=None,
                 token_name=None, epoch=None, harness_version="1.2.3"):
        target = self.secrets / (token_name or ("token-" + uuid.uuid4().hex + ".txt"))
        env = self.envelope("agent.register", {
            "handle": handle, "harness": "any-harness", "harness_version": harness_version,
            "host": "local", "os": sys.platform, "capabilities": list(capabilities)},
            command_id=command_id, epoch=epoch)
        return service.register_agent(self.state, str(grant_file), env, str(target)), target

    def rows(self, sql, params=()):
        with self.db() as con:
            return [dict(row) for row in con.execute(sql, params)]

    def agent(self, agent_id):
        return self.rows("SELECT * FROM v2_agent WHERE agent_id=?", (agent_id,))[0]

    def capability_env(self, agent_id, key, level="supported", **overrides):
        payload = {"agent_id": agent_id, "capability_key": key, "level": level,
                   "harness_version": "unknown", "os": sys.platform, "host": "local",
                   "checked_at": "2026-11-10T00:00:00+00:00",
                   "evidence_sha256": None, "evidence_path": None,
                   "source": "live_probe", "expected_revision": self.agent(agent_id)["revision"]}
        payload.update(overrides)
        return self.envelope("agent.capability", payload)

    def heartbeat_env(self, agent_id, revision, command_id=None):
        return self.envelope("agent.heartbeat", {"expected_revision": revision},
                             command_id=command_id)

    def test_schema_upgrade_is_repeatable_and_refuses_future(self):
        service.apply_migration(self.state, *self.actor)
        with self.db() as con:
            self.assertEqual(con.execute(
                "SELECT value FROM v2_meta WHERE key='schema_version'").fetchone()[0],
                str(service.V2_SCHEMA_VERSION))
            self.assertEqual(con.execute("SELECT count(*) FROM v2_agent").fetchone()[0], 2)
            con.execute("UPDATE v2_meta SET value='999' WHERE key='schema_version'")
        with self.assertRaises(service.ServiceError):
            service.apply_migration(self.state, *self.actor)

    def test_enrollment_grant_limits_allowed_keys(self):
        grant, grant_file = self.enroll(allowed=("status", "collect"))
        with self.assertRaises(service.ServiceError):
            self.register(grant_file, "agent-bad", capabilities=("status", "cancel"))
        result, token_file = self.register(grant_file, "agent-good", capabilities=("status", "collect"))
        self.assertEqual(result["status"], "REGISTERED")
        self.assertTrue(token_file.is_file())
        self.assertTrue(grant_file.is_file())

    def test_single_use_grant_race_has_one_winner(self):
        grant, grant_file = self.enroll(allowed=("status",))
        script = Path(service.__file__).resolve()
        outputs = []
        for name in ("race-a", "race-b"):
            env_file = self.root / (name + ".json")
            env_file.write_text(json.dumps(self.envelope("agent.register", {
                "handle": name, "harness": "any-harness", "harness_version": "unknown",
                "host": "local", "os": sys.platform, "capabilities": ["status"]})),
                encoding="utf-8")
            token_file = self.secrets / (name + "-token.txt")
            outputs.append((env_file, token_file))
        procs = []
        for env_file, token_file in outputs:
            procs.append(subprocess.Popen(
                [sys.executable, "-B", str(script), "--state", str(self.state), "agent-register",
                 "--grant-file", str(grant_file), "--envelope", str(env_file),
                 "--token-out", str(token_file)],
                cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        results = [proc.communicate() + (proc.returncode,) for proc in procs]
        self.assertEqual(sorted(proc[2] for proc in results), [0, 2])
        with self.db() as con:
            self.assertEqual(con.execute(
                "SELECT count(*) FROM v2_agent WHERE source='enrollment_grant'").fetchone()[0], 1)

    def test_expired_and_revoked_grants(self):
        grant, grant_file = self.enroll()
        with self.db() as con:
            con.execute("UPDATE v2_enrollment_grant SET expires_at='2000-01-01T00:00:00+00:00' "
                        "WHERE grant_id=?", (grant["grant_id"],))
        with self.assertRaises(service.ServiceError):
            self.register(grant_file, "expired-agent", token_name="expired-token.txt")
        self.assertFalse((self.secrets / "expired-token.txt").exists())

        grant2, grant_file2 = self.enroll()
        service.revoke_grant(self.state, *self.actor, self.envelope("grant.revoke", {
            "grant_id": grant2["grant_id"], "expected_revision": 1}))
        with self.assertRaises(service.ServiceError):
            self.register(grant_file2, "revoked-grant-agent", token_name="revoked-grant-token.txt")

    def test_self_registration_is_pending_and_cannot_escalate(self):
        with self.assertRaises(service.ServiceError):
            service.self_register_agent(self.state, self.envelope("agent.self_register", {
                "handle": "escalate", "harness": "any-harness", "harness_version": "unknown",
                "host": "local", "os": sys.platform, "role": "reviewer"}))
        pending = service.self_register_agent(self.state, self.envelope("agent.self_register", {
            "handle": "pending-one", "harness": "any-harness", "harness_version": "unknown",
            "host": "local", "os": sys.platform, "capabilities": ["status"]}))
        self.assertEqual(pending["lifecycle"], "pending")
        with self.assertRaises(service.ServiceError):
            service.approve_agent(self.state, *self.actor, self.envelope("agent.approve", {
                "agent_id": pending["agent_id"], "expected_revision": True}),
                str(self.secrets / "pending-token.txt"))
        approved = service.approve_agent(self.state, *self.actor, self.envelope("agent.approve", {
            "agent_id": pending["agent_id"], "expected_revision": 1}),
            str(self.secrets / "pending-token.txt"))
        self.assertEqual(approved["lifecycle"], "active")
        self.assertTrue((self.secrets / "pending-token.txt").is_file())

    def test_forged_identity_fields_are_rejected(self):
        grant, grant_file = self.enroll()
        env = self.envelope("agent.register", {
            "handle": "forged", "harness": "any-harness", "harness_version": "unknown",
            "host": "local", "os": sys.platform, "capabilities": ["status"],
            "project_id": "other-project"})
        with self.assertRaises(service.ServiceError):
            service.register_agent(self.state, str(grant_file), env,
                                   str(self.secrets / "forged-token.txt"))
        self.assertFalse((self.secrets / "forged-token.txt").exists())

    def test_supported_matching_rechecks_context_and_evidence(self):
        grant, grant_file = self.enroll()
        registered, token_file = self.register(grant_file, "cap-agent", capabilities=("status",))
        agent_id = registered["agent_id"]
        matched = service.agent_match(self.state, *self.actor, agent_id, ["status"])
        self.assertFalse(matched["matched"])
        self.assertEqual(matched["unknown"], ["status"])
        evidence = self.root / "capability-evidence.txt"
        evidence.write_text("real local probe evidence\n", encoding="utf-8")
        sha = service._file_sha(evidence)
        agent = self.agent(agent_id)
        result = service.set_agent_capability(
            self.state, *self.actor,
            self.capability_env(agent_id, "status", harness_version=agent["harness_version"],
                                os=agent["os"], host=agent["host"],
                                evidence_sha256=sha, evidence_path=str(evidence),
                                source="live_probe"))
        self.assertEqual(result["level"], "supported")
        self.assertTrue(service.agent_match(self.state, *self.actor, agent_id, ["status"])["matched"])
        self.assertFalse(service.agent_match(self.state, *self.actor, agent_id, ["collect"])["matched"])
        evidence.write_text("tampered\n", encoding="utf-8")
        tampered = service.agent_match(self.state, *self.actor, agent_id, ["status"])
        self.assertFalse(tampered["matched"])
        self.assertIn("status", tampered["missing"])

    def test_supported_requires_real_context_and_evidence(self):
        grant, grant_file = self.enroll()
        registered, _ = self.register(grant_file, "bad-cap", capabilities=("status",))
        agent_id = registered["agent_id"]
        agent = self.agent(agent_id)
        evidence = self.root / "bad-evidence.txt"
        evidence.write_text("evidence\n", encoding="utf-8")
        sha = service._file_sha(evidence)
        with self.assertRaises(service.ServiceError):
            service.set_agent_capability(self.state, *self.actor,
                                         self.capability_env(agent_id, "status", harness_version="unknown",
                                                             os=agent["os"], host=agent["host"],
                                                             evidence_sha256=sha,
                                                             evidence_path=str(evidence)))
        with self.assertRaises(service.ServiceError):
            service.set_agent_capability(self.state, *self.actor,
                                         self.capability_env(agent_id, "status",
                                                             harness_version=agent["harness_version"],
                                                             os=agent["os"], host=agent["host"],
                                                             evidence_sha256="0" * 64,
                                                             evidence_path=str(evidence)))
        with self.assertRaises(service.ServiceError):
            service.set_agent_capability(self.state, *self.actor,
                                         self.capability_env(agent_id, "status",
                                                             harness_version=agent["harness_version"],
                                                             os="other-os", host=agent["host"],
                                                             evidence_sha256=sha,
                                                             evidence_path=str(evidence)))

    def test_heartbeat_replay_is_deduplicated_and_does_not_touch_assignments(self):
        grant, grant_file = self.enroll()
        registered, token_file = self.register(grant_file, "beat-agent")
        agent_id = registered["agent_id"]
        before_assign = self.rows("SELECT * FROM v2_assignment ORDER BY assignment_id")
        env = self.heartbeat_env(agent_id, 1, command_id="cmd-heartbeat-0001")
        first = service.heartbeat_agent(self.state, str(token_file), env)
        self.assertFalse(first["replayed"])
        last_seen = self.agent(agent_id)["last_seen_at"]
        second = service.heartbeat_agent(self.state, str(token_file), env)
        self.assertTrue(second["replayed"])
        self.assertEqual(self.agent(agent_id)["last_seen_at"], last_seen)
        third = service.heartbeat_agent(self.state, str(token_file),
                                        self.heartbeat_env(agent_id, 2, command_id="cmd-heartbeat-0002"))
        self.assertEqual(third["revision"], 3)
        self.assertEqual(self.rows("SELECT * FROM v2_assignment ORDER BY assignment_id"), before_assign)

    def test_revocation_blocks_token_and_retains_records(self):
        grant, grant_file = self.enroll()
        registered, token_file = self.register(grant_file, "revoke-agent")
        agent_id = registered["agent_id"]
        service.revoke_agent(self.state, *self.actor, self.envelope("agent.revoke", {
            "agent_id": agent_id, "expected_revision": 1}))
        self.assertEqual(self.agent(agent_id)["lifecycle"], "revoked")
        self.assertEqual(len(self.rows("SELECT * FROM v2_agent_capability WHERE agent_id=?", (agent_id,))), 1)
        with self.assertRaises(service.ServiceError):
            service.heartbeat_agent(self.state, str(token_file),
                                    self.heartbeat_env(agent_id, 2, command_id="cmd-revoked-heartbeat"))

    def test_historical_assignment_uses_frozen_worker_binding(self):
        frozen_id = "old-executor-instance"
        request = {"task_id": "T-HIST", "nonce": "nonce-hist", "cwd": str(self.root),
                   "dispatch_confirmed": True, "worker": self.worker_spec,
                   "reviewer": self.planner_spec, "workflow_state": str(self.state),
                   "authorization": {"source": "test", "notify_reviewer": False},
                   "completion": {"binding_artifact": "HANDOFF.md"}}
        data = {"request": request, "request_sha256": control.digest(request),
                "worker_binding": {"instance_id": frozen_id, "role": "executor",
                                   "mode": "worker", "version": 9,
                                   "spec": self.worker_spec}}
        with self.db() as con:
            con.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                        ("T-HIST", "nonce-hist", control.digest(request), "executor",
                         "running", json.dumps(data)))
            con.execute("INSERT INTO dispatches VALUES (?,?,?)",
                        ("T-HIST", "running", json.dumps({"generation": 1, "resources": {},
                                                          "workspaces": [str(self.root)]})))
        service.apply_migration(self.state, *self.actor)
        ass = self.rows("SELECT * FROM v2_assignment WHERE task_id='T-HIST'")[0]
        expected = service._agent_id_for_binding(frozen_id)
        self.assertEqual(ass["agent_id"], expected)
        self.assertNotEqual(ass["agent_id"], service._agent_id_for_binding(self.worker["instance_id"]))
        frozen = self.agent(expected)
        self.assertEqual(frozen["lifecycle"], "revoked")
        self.assertEqual(frozen["harness_version"], "unknown")

    def test_role_rebind_imports_new_identity_without_fk_or_handle_collision(self):
        old_agent = service._agent_id_for_binding(self.worker["instance_id"])
        request = {"task_id": "T-OLD", "nonce": "nonce-old", "cwd": str(self.root),
                   "dispatch_confirmed": True, "worker": self.worker_spec,
                   "reviewer": self.planner_spec, "workflow_state": str(self.state),
                   "authorization": {"source": "test", "notify_reviewer": False},
                   "completion": {"binding_artifact": "HANDOFF.md"}}
        with self.db() as con:
            con.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                        ("T-OLD", "nonce-old", control.digest(request), "executor",
                         "finished", json.dumps({"request": request,
                                                "request_sha256": control.digest(request),
                                                "worker_binding": {"instance_id": self.worker["instance_id"],
                                                                   "role": "executor", "mode": "worker",
                                                                   "spec": self.worker_spec}})))
            con.execute("INSERT INTO dispatches VALUES (?,?,?)",
                        ("T-OLD", "running", json.dumps({"generation": 1, "resources": {},
                                                          "workspaces": [str(self.root)]})))
        service.apply_migration(self.state, *self.actor)
        new_binding = control.bind(self.state, "executor", self.worker_spec,
                                   self.worker_spec["expected_identity"], reason="switch executor")
        new_agent = service._agent_id_for_binding(new_binding["instance_id"])
        self.assertNotEqual(old_agent, new_agent)
        service.apply_migration(self.state, *self.actor)
        old = self.agent(old_agent)
        new = self.agent(new_agent)
        self.assertEqual(old["lifecycle"], "revoked")
        self.assertEqual(new["lifecycle"], "active")
        self.assertNotEqual(old["handle"], new["handle"])
        ass = self.rows("SELECT * FROM v2_assignment WHERE task_id='T-OLD'")[0]
        self.assertEqual(ass["agent_id"], old_agent)
        # An explicitly revoked imported agent is not revived by a later apply.
        with self.db() as con:
            con.execute("UPDATE v2_agent SET lifecycle='revoked' WHERE agent_id=?", (new_agent,))
        service.apply_migration(self.state, *self.actor)
        self.assertEqual(self.agent(new_agent)["lifecycle"], "revoked")

    def test_freeze_blocks_admission_but_not_liveness_or_revocation(self):
        grant, grant_file = self.enroll()
        registered, token_file = self.register(grant_file, "frozen-agent")
        agent_id = registered["agent_id"]
        pending = service.self_register_agent(self.state, self.envelope("agent.self_register", {
            "handle": "frozen-pending", "harness": "any-harness", "harness_version": "unknown",
            "host": "local", "os": sys.platform}))
        service.set_freeze(self.state, *self.actor, True, "test freeze")
        self.epoch = service.service_status(self.state)["controller_epoch"]
        with self.assertRaises(service.ServiceError):
            self.enroll(secret_name="frozen-grant.txt")
        with self.assertRaises(service.ServiceError):
            service.self_register_agent(self.state, self.envelope("agent.self_register", {
                "handle": "frozen-self", "harness": "any-harness", "harness_version": "unknown",
                "host": "local", "os": sys.platform}))
        with self.assertRaises(service.ServiceError):
            self.register(grant_file, "frozen-register", token_name="frozen-token.txt")
        with self.assertRaises(service.ServiceError):
            service.approve_agent(self.state, *self.actor, self.envelope("agent.approve", {
                "agent_id": pending["agent_id"], "expected_revision": 1}),
                str(self.secrets / "frozen-approve-token.txt"))
        with self.assertRaises(service.ServiceError):
            service.set_agent_capability(self.state, *self.actor,
                                         self.capability_env(agent_id, "status", level="unknown",
                                                             evidence_sha256=None,
                                                             evidence_path=None,
                                                             source="operator_note"))
        beat = service.heartbeat_agent(self.state, str(token_file),
                                       self.heartbeat_env(agent_id, 1, command_id="cmd-frozen-beat"))
        self.assertEqual(beat["status"], "HEARTBEAT")
        revoked = service.revoke_agent(self.state, *self.actor, self.envelope("agent.revoke", {
            "agent_id": agent_id, "expected_revision": 2}))
        self.assertEqual(revoked["status"], "REVOKED")

    def test_secrets_are_not_in_db_events_or_replay(self):
        grant, grant_file = self.enroll()
        grant_secret = grant_file.read_text(encoding="utf-8").strip()
        registered, token_file = self.register(grant_file, "secret-agent")
        token = token_file.read_text(encoding="utf-8").strip()
        raw = (self.state / "workflow.sqlite3").read_bytes().decode("utf-8", "ignore")
        self.assertNotIn(grant_secret, raw)
        self.assertNotIn(token, raw)
        for row in self.rows("SELECT response_json FROM v2_command_log"):
            self.assertNotIn(grant_secret, row["response_json"])
            self.assertNotIn(token, row["response_json"])
        for row in self.rows("SELECT payload_json FROM v2_event"):
            self.assertNotIn(grant_secret, row["payload_json"])
            self.assertNotIn(token, row["payload_json"])

    def test_cli_registry_flow(self):
        script = Path(service.__file__).resolve()
        base = [sys.executable, "-B", str(script), "--state", str(self.state)]
        enroll_env = self.root / "enroll.json"
        enroll_env.write_text(json.dumps(self.envelope("agent.enroll", {
            "role": "worker", "allowed_capabilities": ["status"], "expires_minutes": 10})),
            encoding="utf-8")
        secret = self.secrets / "cli-grant.txt"
        proc = subprocess.run(base + ["agent-enroll", "--envelope", str(enroll_env),
                                     "--secret-out", str(secret), "--actor", self.actor[0],
                                     "--version", str(self.actor[1]), "--context", "main"],
                              cwd=self.root, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        grant_id = json.loads(proc.stdout)["grant_id"]
        reg_env = self.root / "register.json"
        reg_env.write_text(json.dumps(self.envelope("agent.register", {
            "handle": "cli-agent", "harness": "any-harness", "harness_version": "unknown",
            "host": "local", "os": sys.platform, "capabilities": ["status"]})), encoding="utf-8")
        token = self.secrets / "cli-token.txt"
        proc = subprocess.run(base + ["agent-register", "--envelope", str(reg_env),
                                     "--grant-file", str(secret), "--token-out", str(token)],
                              cwd=self.root, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        agent_id = json.loads(proc.stdout)["agent_id"]
        proc = subprocess.run(base + ["agent-status", "--token-file", str(token)],
                              cwd=self.root, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["agent"]["agent_id"], agent_id)
        beat_env = self.root / "beat.json"
        beat_env.write_text(json.dumps(self.heartbeat_env(agent_id, 1)), encoding="utf-8")
        proc = subprocess.run(base + ["agent-heartbeat", "--envelope", str(beat_env),
                                     "--token-file", str(token)],
                              cwd=self.root, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        revoke_env = self.root / "revoke.json"
        revoke_env.write_text(json.dumps(self.envelope("agent.revoke", {
            "agent_id": agent_id, "expected_revision": 2})), encoding="utf-8")
        proc = subprocess.run(base + ["agent-revoke", "--envelope", str(revoke_env),
                                     "--actor", self.actor[0], "--version", str(self.actor[1]),
                                     "--context", "main"], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        grep_env = self.root / "grant-revoke.json"
        grep_env.write_text(json.dumps(self.envelope("grant.revoke", {
            "grant_id": grant_id, "expected_revision": 2})), encoding="utf-8")
        proc = subprocess.run(base + ["grant-revoke", "--envelope", str(grep_env),
                                     "--actor", self.actor[0], "--version", str(self.actor[1]),
                                     "--context", "main"], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
