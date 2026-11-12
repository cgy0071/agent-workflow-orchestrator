"""Disposable Git/SQLite fixtures. Provider tests are local simulations, not live."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import awo_service as service
import merge_queue as mq
import workspace_manager as wm


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="awo-merge-fixture-")
        self.root = Path(self.temporary.name).resolve()
        self.addCleanup(self.cleanup_fixture)
        self.repo = self.root / "repo"
        self.wsroot = self.root / "workspaces"
        self.evidence = self.root / "evidence"
        self.operations = self.root / "operations"
        self.archive = self.root / "archive"
        for path in (self.repo, self.wsroot, self.evidence, self.operations, self.archive):
            path.mkdir()
        self.git(self.repo, "init", "-b", "main")
        self.git(self.repo, "config", "user.name", "Merge Fixture")
        self.git(self.repo, "config", "user.email", "fixture@example.invalid")
        self.git(self.repo, "config", "core.autocrlf", "false")
        (self.repo / "base.txt").write_bytes(b"base\n")
        self.commit(self.repo)
        self.base = self.git(self.repo, "rev-parse", "HEAD")
        self.git(self.repo, "branch", "target", self.base)
        self.db = self.root / "authority.sqlite3"
        self.con = sqlite3.connect(self.db, timeout=1)
        self.addCleanup(self.con.close)
        self.con.execute("PRAGMA foreign_keys=ON")
        self.con.execute("BEGIN IMMEDIATE")
        service.create_schema(self.con)
        wm.create_workspace_schema(self.con)
        mq.create_merge_schema(self.con)
        at = mq._now()
        self.con.execute("INSERT INTO v2_project VALUES ('project',?,?)", (str(self.root), at))
        self.con.execute("INSERT INTO v2_service_state VALUES (1,'project',1,0,1,?)", (at,))
        for role in ("controller", "reviewer", "worker", "observer"):
            self.con.execute("INSERT INTO v2_agent(agent_id,project_id,handle,role,harness,harness_version,"
                             "host,os,lifecycle,source,created_at) VALUES (?,'project',?,?,'fixture','1',"
                             "'local','fixture','active','test',?)", (role, role, role, at))
        self.con.commit()
        self.verifier = self.root / "verify.py"
        self.verifier.write_bytes(b"from pathlib import Path\nassert Path('base.txt').is_file()\nprint('verified fixture')\n")
        self.policy = self.make_policy()

    def cleanup_fixture(self):
        self.con.close()
        def writable(function, path, _error):
            os.chmod(path, 0o700)
            function(path)
        target = self.root.resolve()
        self.assertTrue(target.name.startswith("awo-merge-fixture-"))
        shutil.rmtree(target, onerror=writable)
        self.temporary.cleanup()

    def git(self, cwd, *args):
        result = wm.run_argv(["git", "-c", "core.hooksPath=" + os.devnull, "-C", str(cwd), *args], cwd=cwd)
        self.assertEqual(result["returncode"], 0, result["stderr"].decode("utf-8", "replace"))
        return result["stdout"].decode("utf-8").strip()

    def commit(self, cwd):
        self.git(cwd, "add", "--all")
        self.git(cwd, "commit", "-m", "disposable fixture")

    def principal(self, role="controller"):
        return {"agent_id": role, "role": role, "principal_id": "service:" + role, "project_id": "project"}

    def command(self, tool, payload, role="controller"):
        self.con.execute("BEGIN IMMEDIATE")
        try:
            result = mq.apply_command(self.con, "project", self.principal(role), tool, payload)
            self.con.commit()
            return result
        except BaseException:
            self.con.rollback()
            raise

    def seal(self, name, value):
        path = self.evidence / name
        path.write_bytes(mq._json(value).encode("ascii"))
        return str(path), hashlib.sha256(path.read_bytes()).hexdigest()

    def make_policy(self, provider=None, key=None, max_attempts=3):
        executable = str(Path(sys.executable).resolve())
        paths = [executable, str(self.verifier)]
        payload = {"repository_id": "fixture/repo", "repo_root": str(self.repo),
                   "operation_root": str(self.operations), "archive_root": str(self.archive),
                   "verify_argv": [executable, "-B", str(self.verifier)], "timeout": 20,
                   "max_attempts": max_attempts}
        if provider:
            paths.extend((str(provider[0]), str(key)))
            payload.update(provider_argv=[executable, "-B"] + provider, receipt_key_path=str(key))
        payload["pins"] = {path: hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in paths}
        return self.command("merge.policy", payload)["policy_id"]

    def accepted(self, task="T", *, contents=None, kind="git_worktree"):
        assignment = "A-" + task
        at = mq._now()
        self.con.execute("BEGIN IMMEDIATE")
        self.con.execute("INSERT INTO v2_task(task_id,project_id,task_state,created_at,updated_at) "
                         "VALUES (?,'project','ready',?,?)", (task, at, at))
        self.con.execute("INSERT INTO v2_assignment(assignment_id,task_id,agent_id,generation,state,created_at,updated_at)"
                         " VALUES (?,?,'worker',1,'reserved',?,?)", (assignment, task, at, at))
        self.con.commit()
        options = dict(task_id=task, assignment_id=assignment, host="local", kind=kind,
                       root=str(self.wsroot), path=str(self.wsroot / assignment), allowed_paths=["**"])
        if kind == "git_worktree":
            options.update(repository_id="fixture/repo", repo_root=str(self.repo), base_commit=self.base)
        spec = wm.prepare_workspace(**options)
        self.con.execute("BEGIN IMMEDIATE")
        ws = wm.reserve_workspace(self.con, spec)
        ticket = wm.begin_provision(self.con, ws["workspace_id"])
        self.con.commit()
        report = wm.provision_workspace(self.db, ws["workspace_id"], ticket["operation_token"])
        self.assertTrue(report["ok"], report)
        self.con.execute("BEGIN IMMEDIATE")
        ws = wm.finalize_workspace(self.con, ws["workspace_id"], report)
        self.con.commit()
        path = Path(ws["path"])
        contents = contents or ({task + ".txt": b"feature\n"} if kind == "git_worktree" else {"outputs/result.txt": b"research\n"})
        for name, data in contents.items():
            (path / name).parent.mkdir(parents=True, exist_ok=True)
            (path / name).write_bytes(data)
        identity = dict(task_id=task, assignment_id=assignment, generation=1)
        if kind == "git_worktree":
            self.commit(path)
            manifest = dict(identity, kind="git", repository_id="fixture/repo",
                            source_ref="refs/heads/" + ws["branch"],
                            source_commit=self.git(path, "rev-parse", "HEAD"), base_commit=self.base)
        else:
            manifest = dict(identity, kind="artifact", files=[
                {"path": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                for name, data in contents.items()])
        evidence_id = "ev-" + task
        evidence_path, evidence_sha = self.seal(task + "-evidence.json", manifest)
        review_path, review_sha = self.seal(task + "-review.json", dict(identity, evidence_id=evidence_id,
            evidence_sha256=evidence_sha, reviewer_agent_id="reviewer", verdict="accept"))
        quiet_path, quiet_sha = self.seal(task + "-quiescence.json", {"fixture": "no active writer"})
        quiet = dict(workspace_id=ws["workspace_id"], assignment_id=assignment, verified=True,
                     evidence_path=quiet_path, evidence_sha256=quiet_sha)
        self.con.execute("BEGIN IMMEDIATE")
        self.con.execute("UPDATE v2_workspace SET status='frozen',quiescence_json=? WHERE workspace_id=?",
                         (mq._json(quiet), ws["workspace_id"]))
        self.con.execute("UPDATE v2_assignment SET state='completed',review_status='accepted',holds_slot=0 WHERE assignment_id=?",
                         (assignment,))
        self.con.execute("INSERT INTO v2_evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (evidence_id, task, assignment, "manifest", evidence_path, evidence_sha, evidence_sha,
                          Path(evidence_path).stat().st_size, 1, "sealed", "test", at))
        self.con.execute("INSERT INTO v2_review VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         ("review-" + task, task, assignment, "original-event", "accept", review_path, review_sha,
                          "sealed", '{"agent_id":"reviewer"}', at, "test"))
        self.con.execute("UPDATE v2_task SET task_state='accepted',accepted_evidence_id=? WHERE task_id=?", (evidence_id, task))
        self.con.commit()
        return dict(identity, workspace_id=ws["workspace_id"], manifest=manifest, path=path,
                    evidence_path=evidence_path, review_path=review_path)

    def propose(self, item, parents=(), policy=None, pr_id=None):
        payload = {key: item[key] for key in ("task_id", "assignment_id", "generation", "workspace_id")}
        payload.update(policy_id=policy or self.policy, target_ref="refs/heads/target", expected_revision=1,
                       parents=[{k: p[k] for k in ("task_id", "assignment_id", "generation")} for p in parents])
        if pr_id:
            payload["pr_id"] = pr_id
        return self.command("merge.propose", payload)["merge_request_id"]

    def row(self, identity):
        return mq._one(self.con, "SELECT * FROM v2_merge_request WHERE merge_request_id=?", (identity,))

    def action(self, identity, tool, **kwargs):
        return self.command(tool, dict(merge_request_id=identity, expected_revision=self.row(identity)["revision"], **kwargs))

    def start(self, identity, kind="build"):
        return self.action(identity, "merge.begin", kind=kind)

    def execute(self, ticket):
        return mq.execute_intent(self.db, ticket["intent_id"], ticket["operation_token"])

    def finish(self, report):
        return self.command("merge.finish", {"intent_id": report["intent_id"], "expected_revision": report["intent_revision"]})

    def build(self, item, **kwargs):
        identity = self.propose(item, **kwargs)
        self.action(identity, "merge.enqueue")
        ticket = self.start(identity)
        report = self.execute(ticket)
        self.assertEqual(report["outcome"], "verified", report)
        self.finish(report)
        return identity

    def review(self, identity, role="reviewer"):
        candidate = json.loads(self.row(identity)["candidate_json"])
        note = {key: candidate[key] for key in ("task_id", "assignment_id", "generation")}
        note.update(candidate_sha256=mq._digest(candidate), verdict="accept", reviewer_agent_id=role)
        path, digest = self.seal(identity + "-integration.json", note)
        result = self.command("merge.review", dict(merge_request_id=identity, expected_revision=self.row(identity)["revision"],
                                                   note_path=path, note_sha256=digest), role)
        return path, result

    def state(self, task="T"):
        return self.con.execute("SELECT task_state FROM v2_task WHERE task_id=?", (task,)).fetchone()[0]

    def authorize_recovery(self, ticket):
        intent = mq._one(self.con, "SELECT * FROM v2_merge_intent WHERE intent_id=?", (ticket["intent_id"],))
        path, digest = self.seal(ticket["intent_id"] + "-quiet.json",
                                dict(intent_id=intent["intent_id"], executor_pid=intent["executor_pid"],
                                     quiescent=True, reason="test child exited; all fixture writers reconciled"))
        self.command("merge.recover", dict(intent_id=intent["intent_id"], expected_revision=intent["revision"],
                                           evidence_path=path, evidence_sha256=digest))

    def test_schema_and_domain_commands_are_caller_owned(self):
        other = sqlite3.connect(":memory:")
        with self.assertRaisesRegex(mq.MergeError, "transaction_required"):
            mq.create_merge_schema(other)
        other.execute("BEGIN")
        mq.create_merge_schema(other)
        other.rollback()
        self.assertIsNone(other.execute("SELECT name FROM sqlite_master WHERE name='v2_merge_request'").fetchone())
        other.close()
        item = self.accepted()
        identity = self.propose(item)
        before = self.con.total_changes
        with mock.patch.object(wm, "run_argv", side_effect=AssertionError("read executed external process")):
            self.assertEqual(len(mq.read_queue(self.con, "project", self.principal())), 1)
            self.assertFalse(mq.materialization_gate(self.con, "project", "T", self.base)["ready"])
        self.assertEqual(self.con.total_changes, before)
        self.con.execute("BEGIN IMMEDIATE")
        mq.apply_command(self.con, "project", self.principal(), "merge.enqueue",
                         {"merge_request_id": identity, "expected_revision": 1})
        self.con.rollback()
        self.assertEqual(self.row(identity)["state"], "proposed")

    def test_role_matrix_project_generation_revision_and_ownership(self):
        item = self.accepted()
        for role in ("worker", "reviewer", "observer"):
            with self.subTest(role=role), self.assertRaisesRegex(mq.MergeError, "permission_denied"):
                self.command("merge.policy", {}, role)
        for field, value in (("generation", 2), ("workspace_id", "foreign")):
            with self.subTest(field=field), self.assertRaises(mq.MergeError):
                self.propose(dict(item, **{field: value}))
        identity = self.propose(item)
        with self.assertRaisesRegex(mq.MergeError, "revision_conflict"):
            self.command("merge.enqueue", {"merge_request_id": identity, "expected_revision": True})
        with self.assertRaisesRegex(mq.MergeError, "permission_denied"):
            mq.read_queue(self.con, "foreign", self.principal())
        with self.assertRaisesRegex(mq.MergeError, "permission_denied"):
            mq.read_queue(self.con, "project", dict(self.principal("worker"), role="controller"))

    def test_accepted_is_not_landed_and_real_cas_after_review(self):
        item = self.accepted()
        identity = self.build(item)
        self.assertEqual(self.state(), "accepted")
        self.assertEqual(self.git(self.repo, "rev-parse", "target"), self.base)
        with self.assertRaisesRegex(mq.MergeError, "review_required"):
            self.start(identity, "land")
        self.review(identity)
        ticket = self.start(identity, "land")
        report = self.execute(ticket)
        self.assertEqual(report["outcome"], "landed", report)
        self.assertEqual(self.state(), "accepted")  # Executor cannot mutate Task.
        self.assertEqual(self.finish(report)["task_state"], "landed")
        self.assertEqual(self.git(self.repo, "show", "target:T.txt"), "feature")
        self.assertEqual(self.execute(ticket)["outcome"], "landed")

    def test_real_rebase_and_verification_before_cas(self):
        item = self.accepted()
        (self.repo / "target-only.txt").write_bytes(b"target content\n")
        self.commit(self.repo)
        tip = self.git(self.repo, "rev-parse", "HEAD")
        self.git(self.repo, "update-ref", "refs/heads/target", tip, self.base)
        identity = self.build(item)
        candidate = json.loads(self.row(identity)["candidate_json"])
        self.assertNotEqual(candidate["candidate_commit"], item["manifest"]["source_commit"])
        self.assertEqual(candidate["expected_old_tip"], tip)
        self.assertEqual(self.git(self.repo, "show", candidate["candidate_commit"] + ":target-only.txt"), "target content")
        self.assertEqual(self.git(self.repo, "rev-parse", "target"), tip)
        self.assertIn("verified fixture", Path(candidate["verify_log_path"]).read_text())
        self.review(identity)
        self.assertEqual(self.finish(self.execute(self.start(identity, "land")))["task_state"], "landed")

    def test_checked_out_target_is_blocked_even_when_clean(self):
        identity = self.propose(self.accepted())
        self.git(self.repo, "checkout", "target")
        self.action(identity, "merge.enqueue")
        report = self.execute(self.start(identity))
        self.assertEqual(report["outcome"], "blocked_environment", report)
        self.finish(report)
        self.assertEqual(self.row(identity)["attempts"], 0)
        self.assertEqual(self.state(), "accepted")
        self.assertEqual(self.git(self.repo, "rev-parse", "target"), self.base)

    def test_dirty_source_and_dirty_main_preserved(self):
        item = self.accepted()
        identity = self.propose(item)
        for root in (item["path"], self.repo):
            with self.subTest(root=root):
                dirty = root / "not-reviewed.txt"
                dirty.write_bytes(b"preserve me\n")
                self.action(identity, "merge.enqueue")
                report = self.execute(self.start(identity))
                self.assertEqual(report["outcome"], "blocked_environment", report)
                self.finish(report)
                self.assertEqual(dirty.read_bytes(), b"preserve me\n")
                dirty.unlink()
        self.assertEqual(self.row(identity)["attempts"], 0)

    def test_original_evidence_and_review_mutation_rejected(self):
        item = self.accepted()
        identity = self.propose(item)
        for field in ("evidence_path", "review_path"):
            path = Path(item[field])
            saved = path.read_bytes()
            path.write_bytes(saved + b" ")
            with self.assertRaisesRegex(mq.MergeError, "EVIDENCE_CHANGED"):
                self.action(identity, "merge.enqueue")
            path.write_bytes(saved)

    def test_integration_review_and_candidate_mutation_rejected(self):
        identity = self.build(self.accepted())
        review_path, _ = self.review(identity)
        ticket = self.start(identity, "land")
        Path(review_path).write_bytes(b"{}")
        report = self.execute(ticket)
        self.assertEqual(report["outcome"], "failed", report)
        self.assertEqual(self.git(self.repo, "rev-parse", "target"), self.base)
        self.finish(report)
        self.assertEqual(self.state(), "repair_required")

    def test_candidate_dirty_after_review_cannot_land(self):
        identity = self.build(self.accepted())
        self.review(identity)
        candidate = json.loads(self.row(identity)["candidate_json"])
        (Path(candidate["worktree"]) / "base.txt").write_bytes(b"mutated\n")
        report = self.execute(self.start(identity, "land"))
        self.assertEqual(report["outcome"], "failed", report)
        self.assertEqual(self.git(self.repo, "rev-parse", "target"), self.base)

    def test_no_worker_or_competing_assignment_self_review(self):
        identity = self.build(self.accepted())
        self.con.execute("INSERT INTO v2_assignment(assignment_id,task_id,agent_id,generation,slot_id,state,"
                         "created_at,updated_at) VALUES ('competing','T','controller',1,1,'failed','now','now')")
        self.con.commit()
        with self.assertRaisesRegex(mq.MergeError, "permission_denied"):
            self.review(identity, "controller")

    def test_real_cas_race_requeues_and_preserves_other_writer(self):
        identity = self.build(self.accepted())
        self.review(identity)
        (self.repo / "other.txt").write_bytes(b"concurrent target writer\n")
        self.commit(self.repo)
        other = self.git(self.repo, "rev-parse", "HEAD")
        original = mq._git
        def race(policy, cwd, *args, **kwargs):
            if args[0] == "update-ref":
                self.git(self.repo, "update-ref", "refs/heads/target", other, self.base)
            return original(policy, cwd, *args, **kwargs)
        with mock.patch.object(mq, "_git", side_effect=race):
            report = self.execute(self.start(identity, "land"))
        self.assertEqual(report["outcome"], "rebase_required", report)
        self.finish(report)
        self.assertEqual(self.row(identity)["state"], "queued")
        self.assertEqual(self.row(identity)["attempts"], 1)
        self.assertEqual(self.git(self.repo, "rev-parse", "target"), other)
        self.assertEqual(self.state(), "accepted")

    def test_multiple_accepted_parents_materialized_and_rehashed(self):
        one, two = self.accepted("P1"), self.accepted("P2")
        child = self.accepted()
        identity = self.build(child, parents=[one, two])
        candidate = json.loads(self.row(identity)["candidate_json"])
        for task in ("P1", "P2", "T"):
            self.assertEqual(self.git(self.repo, "show", candidate["candidate_commit"] + ":" + task + ".txt"), "feature")
        gate = mq.materialization_gate(self.con, "project", "T", candidate["stack_base"])
        self.assertTrue(gate["ready"], gate)
        self.assertEqual(gate["materialized_from"], ["P1", "P2"])
        Path(one["evidence_path"]).write_bytes(b"{}")
        self.assertFalse(mq.materialization_gate(self.con, "project", "T", candidate["stack_base"])["ready"])

    def test_real_parent_conflict_requires_repair_keeps_sources(self):
        one = self.accepted("P1", contents={"base.txt": b"parent one\n"})
        two = self.accepted("P2", contents={"base.txt": b"parent two\n"})
        identity = self.propose(self.accepted(), parents=[one, two])
        self.action(identity, "merge.enqueue")
        report = self.execute(self.start(identity))
        self.assertEqual(report["outcome"], "failed", report)
        self.assertIn("conflict", report["error"])
        self.finish(report)
        self.assertEqual(self.state(), "repair_required")
        self.assertEqual((one["path"] / "base.txt").read_bytes(), b"parent one\n")
        self.assertEqual((two["path"] / "base.txt").read_bytes(), b"parent two\n")
        self.assertEqual(self.git(self.repo, "rev-parse", "target"), self.base)

    def test_rebase_conflict_aborts_temporary_rebase(self):
        item = self.accepted(contents={"base.txt": b"source version\n"})
        (self.repo / "base.txt").write_bytes(b"target version\n")
        self.commit(self.repo)
        tip = self.git(self.repo, "rev-parse", "HEAD")
        self.git(self.repo, "update-ref", "refs/heads/target", tip, self.base)
        identity = self.propose(item)
        self.action(identity, "merge.enqueue")
        ticket = self.start(identity)
        report = self.execute(ticket)
        self.assertEqual(report["outcome"], "failed", report)
        tree = self.operations / ticket["intent_id"] / "candidate"
        self.assertEqual(self.git(tree, "rev-parse", "HEAD"), item["manifest"]["source_commit"])
        rebase_path = Path(self.git(tree, "rev-parse", "--git-path", "rebase-merge"))
        self.assertFalse(rebase_path.exists())

    def test_verifier_failure_never_moves_target(self):
        self.verifier.write_bytes(b"print('real verification failure')\nraise SystemExit(7)\n")
        policy = self.make_policy()
        identity = self.propose(self.accepted(), policy=policy)
        self.action(identity, "merge.enqueue")
        report = self.execute(self.start(identity))
        self.assertEqual(report["outcome"], "failed", report)
        self.assertEqual(report["error"], "verification_failed")
        self.assertIn("real verification failure", Path(report["verify_log_path"]).read_text())
        self.finish(report)
        self.assertEqual(self.state(), "repair_required")
        self.assertEqual(self.git(self.repo, "rev-parse", "target"), self.base)

    def test_verifier_pins_and_worker_shell_payload_rejected(self):
        identity = self.propose(self.accepted())
        self.action(identity, "merge.enqueue")
        with self.assertRaisesRegex(mq.MergeError, "invalid_request"):
            self.action(identity, "merge.begin", verify_command="echo unsafe")
        self.verifier.write_bytes(b"raise SystemExit(0)\n")
        report = self.execute(self.start(identity))
        self.assertEqual(report["outcome"], "failed", report)
        self.assertEqual(self.git(self.repo, "rev-parse", "target"), self.base)

    def test_committed_one_shot_ticket_restart_and_wrong_token(self):
        identity = self.propose(self.accepted())
        self.action(identity, "merge.enqueue")
        self.con.execute("BEGIN IMMEDIATE")
        ticket = mq.apply_command(self.con, "project", self.principal(), "merge.begin",
                                  dict(merge_request_id=identity, expected_revision=self.row(identity)["revision"]))
        with self.assertRaises(sqlite3.OperationalError):
            self.execute(ticket)
        self.con.rollback()
        ticket = self.start(identity)
        with self.assertRaisesRegex(mq.MergeError, "permission_denied"):
            mq.execute_intent(self.db, ticket["intent_id"], "wrong")
        report = self.execute(ticket)
        self.con.close()
        self.con = sqlite3.connect(self.db)
        with mock.patch.object(mq, "_build", side_effect=AssertionError("replayed effect")):
            self.assertEqual(self.execute(ticket), report)
        self.assertEqual(self.finish(report)["state"], "verifying")

    def test_process_death_after_cas_recovers_without_reexecution(self):
        identity = self.build(self.accepted())
        self.review(identity)
        ticket = self.start(identity, "land")
        # A real child exits after real update-ref, before durable report storage.
        script = (
            "import os,sys;sys.path.insert(0,sys.argv[1]);import merge_queue as m;"
            "m._store_report=lambda *a:os._exit(73);"
            "m.execute_intent(sys.argv[2],sys.argv[3],sys.argv[4])")
        child = subprocess.run([sys.executable, "-B", "-c", script, str(Path(mq.__file__).parent),
                                str(self.db), ticket["intent_id"], ticket["operation_token"]],
                               capture_output=True, timeout=45)
        self.assertEqual(child.returncode, 73, child.stderr)
        self.assertEqual(self.state(), "accepted")
        self.assertEqual(self.row(identity)["holds_lock"], 1)
        with self.assertRaisesRegex(mq.MergeError, "uncertain"):
            self.execute(ticket)
        with self.assertRaisesRegex(mq.MergeError, "uncertain"):
            mq.recover_intent(self.db, ticket["intent_id"], ticket["operation_token"])
        self.authorize_recovery(ticket)
        report = mq.recover_intent(self.db, ticket["intent_id"], ticket["operation_token"])
        self.assertEqual(report["method"], "recovered_local_cas")
        self.assertEqual(self.finish(report)["task_state"], "landed")

    def test_unknown_recovery_tip_never_forces_or_unlocks(self):
        identity = self.build(self.accepted())
        self.review(identity)
        ticket = self.start(identity, "land")
        with mock.patch.object(mq, "_land", side_effect=OSError("executor crash before CAS")):
            with self.assertRaises(OSError):
                self.execute(ticket)
        (self.repo / "other.txt").write_bytes(b"third tip\n")
        self.commit(self.repo)
        tip = self.git(self.repo, "rev-parse", "HEAD")
        self.git(self.repo, "update-ref", "refs/heads/target", tip, self.base)
        self.authorize_recovery(ticket)
        with self.assertRaisesRegex(mq.MergeError, "uncertain"):
            mq.recover_intent(self.db, ticket["intent_id"], ticket["operation_token"])
        self.assertEqual(self.row(identity)["holds_lock"], 1)
        self.assertEqual(self.git(self.repo, "rev-parse", "target"), tip)

    def test_fifo_and_target_lock(self):
        first = self.propose(self.accepted("T1"))
        second = self.propose(self.accepted("T2"))
        self.action(first, "merge.enqueue")
        self.action(second, "merge.enqueue")
        self.assertEqual(self.start(second)["state"], "waiting_blockers")
        ticket = self.start(first)
        self.assertEqual(self.start(second)["state"], "waiting_blockers")
        self.finish(self.execute(ticket))
        self.assertEqual(self.start(second)["kind"], "build")

    def test_epoch_change_fences_committed_executor(self):
        identity = self.propose(self.accepted())
        self.action(identity, "merge.enqueue")
        ticket = self.start(identity)
        self.con.execute("UPDATE v2_service_state SET controller_epoch=controller_epoch+1")
        self.con.commit()
        report = self.execute(ticket)
        self.assertEqual(report["outcome"], "blocked_environment", report)
        self.assertIn("stale_epoch", report["error"])
        self.finish(report)
        self.assertFalse((self.operations / ticket["intent_id"]).exists())

    def test_artifact_finalization_distinct_and_content_addressed(self):
        item = self.accepted(kind="directory")
        ticket = self.command("artifact.begin", {**{k: item[k] for k in ("task_id", "assignment_id", "generation")},
                                                "policy_id": self.policy, "expected_revision": 1})
        report = self.execute(ticket)
        self.assertEqual(report["outcome"], "finalized", report)
        self.assertEqual(self.state(), "accepted")
        self.assertEqual(self.finish(report)["task_state"], "finalized")
        self.assertEqual(self.con.execute("SELECT count(*) FROM v2_merge_request").fetchone()[0], 0)
        manifest = json.loads(Path(report["manifest_path"]).read_bytes())
        self.assertEqual(Path(manifest["files"][0]["archive_path"]).read_bytes(), b"research\n")
        self.assertEqual(self.git(self.repo, "rev-parse", "target"), self.base)

    def simulated_provider(self, mode):
        """Only a local stdlib test simulator; never contacts a remote provider."""
        key = self.root / "receipt-key"
        key.write_bytes(b"local-simulated-key-32-bytes-long!!")
        script = self.root / ("simulated-" + mode + ".py")
        script.write_text(
            "import argparse,json,hmac,hashlib,pathlib\n"
            "p=argparse.ArgumentParser();p.add_argument('--request');p.add_argument('--receipt');a=p.parse_args()\n"
            "body=json.loads(pathlib.Path(a.request).read_bytes())\n"
            "body.update(state='pending' if " + repr(mode) + "=='pending' else 'merged',ci='passed',"
            "merge_commit=body['candidate_commit'])\n"
            "if " + repr(mode) + "=='wrong-binding':body['pr_id']='another-pr'\n"
            "if " + repr(mode) + "=='wrong-intent':body['intent_id']='previous-operation'\n"
            "if " + repr(mode) + "=='wrong-tree':body['merge_commit']=body['expected_old_tip']\n"
            "if " + repr(mode) + "=='ci-failed':body['ci']='failed'\n"
            "raw=json.dumps(body,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode('ascii')\n"
            "sig=hmac.new(pathlib.Path(" + repr(str(key)) + ").read_bytes(),raw,hashlib.sha256).hexdigest()\n"
            "if " + repr(mode) + "=='bad-signature':sig='0'*64\n"
            "pathlib.Path(a.receipt).write_text(json.dumps(dict(body=body,hmac_sha256=sig)),encoding='utf-8')\n",
            encoding="utf-8")
        return self.make_policy([str(script)], key)

    def test_simulated_provider_pending_releases_target_hold(self):
        policy = self.simulated_provider("pending")
        identity = self.build(self.accepted(), policy=policy, pr_id="SIMULATED-1")
        self.review(identity)
        report = self.execute(self.start(identity, "poll"))
        self.assertEqual(report["outcome"], "waiting_remote", report)
        self.finish(report)
        self.assertEqual(self.row(identity)["holds_lock"], 0)
        second = self.propose(self.accepted("T2"))
        self.action(second, "merge.enqueue")
        self.assertEqual(self.start(second)["kind"], "build")

    def test_simulated_provider_valid_signed_status_local_objects_only(self):
        policy = self.simulated_provider("merged")
        identity = self.build(self.accepted(), policy=policy, pr_id="SIMULATED-1")
        self.review(identity)
        report = self.execute(self.start(identity, "poll"))
        self.assertEqual(report["outcome"], "landed", report)
        self.assertEqual(report["method"], "authenticated_pr_receipt")
        self.assertEqual(self.finish(report)["task_state"], "landed")
        self.assertEqual(self.git(self.repo, "rev-parse", "target"), self.base)  # No local or remote write.

    def test_simulated_provider_bad_signature_cannot_land(self):
        policy = self.simulated_provider("bad-signature")
        identity = self.build(self.accepted(), policy=policy, pr_id="SIMULATED-1")
        self.review(identity)
        report = self.execute(self.start(identity, "poll"))
        self.assertEqual(report["outcome"], "failed", report)
        self.assertIn("authentication", report["error"])
        self.assertEqual(self.git(self.repo, "rev-parse", "target"), self.base)

    def test_simulated_provider_wrong_operation_binding_cannot_land(self):
        policy = self.simulated_provider("wrong-binding")
        identity = self.build(self.accepted(), policy=policy, pr_id="SIMULATED-1")
        self.review(identity)
        report = self.execute(self.start(identity, "poll"))
        self.assertEqual(report["outcome"], "failed", report)
        self.assertIn("exact operation", report["error"])

    def test_standalone_stack_before_downstream_assignment(self):
        parents = [self.accepted("P1"), self.accepted("P2")]
        self.con.execute("INSERT INTO v2_task(task_id,project_id,task_state,created_at,updated_at) "
                         "VALUES ('downstream','project','ready','now','now')")
        self.con.commit()
        ticket = self.command("merge.materialize", {
            "task_id": "downstream", "expected_revision": 1, "policy_id": self.policy,
            "target_ref": "refs/heads/target", "parents": [
                {k: parent[k] for k in ("task_id", "assignment_id", "generation")} for parent in parents]})
        report = self.execute(ticket)
        self.assertEqual(report["outcome"], "materialized", report)
        self.assertFalse(mq.materialization_gate(self.con, "project", "downstream", report["candidate"]["stack_base"])["ready"])
        self.finish(report)
        gate = mq.materialization_gate(self.con, "project", "downstream", report["candidate"]["stack_base"])
        self.assertTrue(gate["ready"], gate)
        self.assertEqual(self.state("downstream"), "ready")
        self.assertEqual(self.con.execute("SELECT count(*) FROM v2_assignment WHERE task_id='downstream'").fetchone()[0], 0)
        self.assertEqual(self.git(self.repo, "rev-parse", "target"), self.base)
        self.assertEqual(mq.read_intents(self.con, "project", self.principal(), "downstream")[0]["state"], "finished")

    def test_original_source_commit_mutation_after_review_rejected(self):
        item = self.accepted()
        identity = self.build(item)
        self.review(identity)
        (item["path"] / "unreviewed.txt").write_bytes(b"later change\n")
        self.commit(item["path"])
        report = self.execute(self.start(identity, "land"))
        self.assertEqual(report["outcome"], "failed", report)
        self.assertIn("source commit", report["error"])
        self.assertEqual(self.git(self.repo, "rev-parse", "target"), self.base)

    def test_custom_merge_driver_never_executed(self):
        item = self.accepted()
        identity = self.propose(item)
        self.git(self.repo, "config", "merge.unsafe.driver", "must-never-run")
        self.action(identity, "merge.enqueue")
        report = self.execute(self.start(identity))
        self.assertEqual(report["outcome"], "blocked_environment", report)
        self.assertIn("custom filters/merge drivers", report["error"])

    def test_finish_rollback_retains_receipt_and_task_authority(self):
        identity = self.build(self.accepted())
        self.review(identity)
        ticket = self.start(identity, "land")
        report = self.execute(ticket)
        self.con.execute("BEGIN IMMEDIATE")
        mq.apply_command(self.con, "project", self.principal(), "merge.finish",
                         {"intent_id": report["intent_id"], "expected_revision": report["intent_revision"]})
        self.con.rollback()
        self.assertEqual(self.state(), "accepted")
        self.assertEqual(self.row(identity)["holds_lock"], 1)
        self.assertEqual(self.finish(report)["task_state"], "landed")

    def test_crash_before_cas_after_epoch_handover_can_reconcile_no_effect(self):
        identity = self.build(self.accepted())
        self.review(identity)
        ticket = self.start(identity, "land")
        with mock.patch.object(mq, "_land", side_effect=OSError("crash before CAS")):
            with self.assertRaises(OSError):
                self.execute(ticket)
        self.con.execute("UPDATE v2_service_state SET controller_epoch=controller_epoch+1")
        self.con.commit()
        self.authorize_recovery(ticket)
        report = mq.recover_intent(self.db, ticket["intent_id"], ticket["operation_token"])
        self.assertEqual(report["outcome"], "rebase_required")
        self.finish(report)
        self.assertEqual(self.row(identity)["state"], "queued")

    def test_artifact_mutation_after_review_refuses_archive(self):
        item = self.accepted(kind="directory")
        ticket = self.command("artifact.begin", {**{k: item[k] for k in ("task_id", "assignment_id", "generation")},
                                                "policy_id": self.policy, "expected_revision": 1})
        (item["path"] / "outputs/result.txt").write_bytes(b"unreviewed artifact\n")
        report = self.execute(ticket)
        self.assertEqual(report["outcome"], "failed", report)
        self.assertEqual(self.state(), "accepted")
        self.assertEqual(self.con.execute("SELECT count(*) FROM v2_artifact_finalization").fetchone()[0], 0)

    def test_bounded_cas_failure_requires_repair(self):
        policy = self.make_policy(max_attempts=1)
        identity = self.build(self.accepted(), policy=policy)
        self.review(identity)
        (self.repo / "advance.txt").write_bytes(b"target advanced\n")
        self.commit(self.repo)
        self.git(self.repo, "update-ref", "refs/heads/target", self.git(self.repo, "rev-parse", "HEAD"), self.base)
        report = self.execute(self.start(identity, "land"))
        self.assertEqual(report["outcome"], "rebase_required", report)
        self.finish(report)
        self.assertEqual(self.row(identity)["state"], "failed")
        self.assertEqual(self.state(), "repair_required")

    def test_cancel_unstarted_request_never_changes_task(self):
        identity = self.propose(self.accepted())
        self.assertEqual(self.action(identity, "merge.cancel")["state"], "cancelled")
        self.assertEqual(self.state(), "accepted")
        with self.assertRaisesRegex(mq.MergeError, "state_conflict"):
            self.start(identity)

    def test_latest_original_review_must_still_accept(self):
        item = self.accepted()
        self.con.execute("INSERT INTO v2_review SELECT 'newer-review',task_id,assignment_id,event_id,'reject',"
                         "note_path,note_sha256,state,actor_json,'9999',source FROM v2_review WHERE task_id='T'")
        self.con.commit()
        with self.assertRaisesRegex(mq.MergeError, "EVIDENCE_CHANGED"):
            self.propose(item)

    def test_simulated_provider_receipt_replay_wrong_intent_rejected(self):
        policy = self.simulated_provider("wrong-intent")
        identity = self.build(self.accepted(), policy=policy, pr_id="SIMULATED-1")
        self.review(identity)
        report = self.execute(self.start(identity, "poll"))
        self.assertEqual(report["outcome"], "failed", report)
        self.assertIn("exact operation", report["error"])

    def test_simulated_provider_wrong_tree_and_failed_ci_gates(self):
        for name, mode, outcome in (("T1", "wrong-tree", "failed"), ("T2", "ci-failed", "waiting_remote")):
            with self.subTest(mode=mode):
                policy = self.simulated_provider(mode)
                identity = self.build(self.accepted(name), policy=policy, pr_id="SIMULATED-" + name)
                self.review(identity)
                report = self.execute(self.start(identity, "poll"))
                self.assertEqual(report["outcome"], outcome, report)
                self.finish(report)
        self.assertEqual(self.git(self.repo, "rev-parse", "target"), self.base)

    def test_complete_archive_crash_recovery_and_blob_mutation_guard(self):
        item = self.accepted(kind="directory")
        ticket = self.command("artifact.begin", {**{k: item[k] for k in ("task_id", "assignment_id", "generation")},
                                                "policy_id": self.policy, "expected_revision": 1})
        with mock.patch.object(mq, "_store_report", side_effect=OSError("crash after archive")):
            with self.assertRaises(OSError):
                self.execute(ticket)
        self.authorize_recovery(ticket)
        report = mq.recover_intent(self.db, ticket["intent_id"], ticket["operation_token"])
        self.assertEqual(report["outcome"], "finalized", report)
        manifest = json.loads(Path(report["manifest_path"]).read_bytes())
        blob = Path(manifest["files"][0]["archive_path"])
        saved = blob.read_bytes()
        blob.write_bytes(b"archive corrupted")
        with self.assertRaisesRegex(mq.MergeError, "EVIDENCE_CHANGED"):
            self.finish(report)
        self.assertEqual(self.state(), "accepted")
        blob.write_bytes(saved)
        self.assertEqual(self.finish(report)["task_state"], "finalized")

    def test_checked_out_target_after_build_and_review_is_refused(self):
        identity = self.build(self.accepted())
        self.review(identity)
        other = self.root / "target-checkout"
        self.git(self.repo, "worktree", "add", str(other), "target")
        report = self.execute(self.start(identity, "land"))
        self.assertEqual(report["outcome"], "blocked_environment", report)
        self.finish(report)
        self.assertEqual(self.row(identity)["attempts"], 0)
        self.assertEqual(self.git(other, "rev-parse", "HEAD"), self.base)

    def test_assignment_ownership_mutation_invalidates_proposal(self):
        identity = self.propose(self.accepted())
        self.con.execute("UPDATE v2_assignment SET agent_id='observer' WHERE task_id='T'")
        self.con.commit()
        with self.assertRaisesRegex(mq.MergeError, "EVIDENCE_CHANGED"):
            self.action(identity, "merge.enqueue")

    def test_local_fsmonitor_command_is_never_run_by_workspace_inspector(self):
        identity = self.propose(self.accepted())
        self.git(self.repo, "config", "core.fsmonitor", "must-never-run")
        self.action(identity, "merge.enqueue")
        report = self.execute(self.start(identity))
        self.assertEqual(report["outcome"], "blocked_environment", report)
        self.assertEqual(self.git(self.repo, "rev-parse", "target"), self.base)


class NativeMergeBase:
    """Actual scheduler/Markdown review/freeze APIs; no mocked acceptance gate.

    Older sidecar baselines lack workspace_runtime. Run this class with current
    parent dependencies on sys.path to exercise the native integration. The
    helpers live here so the frozen-source subclass does not rerun every
    directory-mode test against an executor whose launch directory is new.
    """

    fixture_class = None

    def setUp(self):
        try:
            import workspace_runtime
            import test_workspace_runtime
        except ModuleNotFoundError as exc:
            if exc.name not in ("workspace_runtime", "test_workspace_runtime"):
                raise
            self.skipTest("native parent workspace runtime not present in this baseline")
        self.runtime = workspace_runtime
        fixture_class = self.fixture_class or test_workspace_runtime.WorkspaceRuntimeTests
        self.fixture = fixture_class()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture.f
        self.root = self.f.root
        self.project = self.f.project
        self.db = service._db_path(self.f.state)
        with self.f.db() as con:
            mq.create_merge_schema(con)

    def principal(self, role="controller"):
        return {"agent_id": self.f.agents["planner"], "role": role,
                "principal_id": "fixture-credential:" + role, "project_id": self.project,
                "binding_id": self.f.actor[0], "binding_version": self.f.actor[1]}

    def command(self, tool, payload, role="controller"):
        with self.f.db() as con:
            return mq.apply_command(con, self.project, self.principal(role), tool, payload)

    def row(self, identity):
        return self.f.rows("SELECT * FROM v2_merge_request WHERE merge_request_id=?", (identity,))[0]

    def action(self, identity, tool, **extra):
        return self.command(tool, dict(merge_request_id=identity, expected_revision=self.row(identity)["revision"], **extra))

    def accepted(self, kind="git_worktree", freeze=True, role="executor"):
        if kind == "git_worktree":
            aid, request, ws, _ = self.fixture.managed(kind, role=role)
            repo = self.root / "repo"
        else:
            aid, request = self.fixture.reserve("artifact", role=role)
            response = self.runtime.create(self.f.state, *self.f.actor, self.f.envelope("workspace.create",
                {"assignment_id": aid, "expected_revision": 1, "kind": "directory",
                 "root": str(self.fixture.workspaces), "path": str(self.fixture.workspaces / "artifact"),
                 "allowed_paths": ["outputs/**"]}))
            self.assertEqual(response["status"], "WORKSPACE_READY")
            ws = response["workspace"]
            repo = self.root / "policy-repo"
            repo.mkdir()
            self.fixture.git(repo, "init")
        self.assertEqual(self.f.dispatch(aid)["status"], "SUBMITTED")
        path = Path(ws["path"])
        if kind == "git_worktree":
            (path / "allowed.txt").write_bytes(b"independently reviewed native source")
            self.fixture.git(path, "add", "allowed.txt")
            self.fixture.git(path, "commit", "-m", "native disposable source")
            self.fixture.git(repo, "branch", "target", ws["base_commit"])
        else:
            (path / "outputs").mkdir()
            (path / "outputs" / "result.bin").write_bytes(b"native artifact output")
        completion = self.f.reconcile(aid)
        self.assertEqual(completion["status"], "REVIEW_READY")
        self.f.review(completion, request, same_connection=True)
        self.f.close(aid, completion)
        if freeze:
            current = self.f.rows("SELECT * FROM v2_workspace WHERE workspace_id=?", (ws["workspace_id"],))[0]
            response = self.runtime.freeze(self.f.state, *self.f.actor, self.f.envelope("workspace.freeze",
                {"workspace_id": ws["workspace_id"], "expected_revision": current["revision"],
                 "evidence_root": str(self.root / "freeze-evidence")}))
            self.assertEqual(response["status"], "WORKSPACE_FROZEN")
        operations, archive = self.root / "merge-operations", self.root / "artifact-archive"
        operations.mkdir()
        archive.mkdir()
        verifier = self.root / "approved-verify.py"
        verifier.write_bytes(b"from pathlib import Path\nassert Path('allowed.txt').read_bytes() == b'independently reviewed native source'\n")
        executable = str(Path(sys.executable).resolve())
        policy = self.command("merge.policy", {"repository_id": "fixture-repo", "repo_root": str(repo),
            "operation_root": str(operations), "archive_root": str(archive),
            "verify_argv": [executable, "-B", str(verifier)],
            "pins": {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in (executable, str(verifier))}})
        assignment = self.f.get_assignment(aid)
        self.item = dict(task_id=assignment["task_id"], assignment_id=aid, generation=assignment["generation"],
                         workspace_id=ws["workspace_id"], policy_id=policy["policy_id"], path=path, repo=repo,
                         review_path=Path(request["cwd"]) / "REVIEW.md", base_commit=ws["base_commit"])
        return self.item

    def propose(self):
        payload = {k: self.item[k] for k in ("task_id", "assignment_id", "generation", "workspace_id", "policy_id")}
        payload.update(target_ref="refs/heads/target",
                       expected_revision=self.f.rows("SELECT revision FROM v2_task WHERE task_id=?", (self.item["task_id"],))[0]["revision"])
        return self.command("merge.propose", payload)["merge_request_id"]

    def finish(self, ticket):
        report = mq.execute_intent(self.db, ticket["intent_id"], ticket["operation_token"])
        self.assertIn(report["outcome"], ("verified", "landed", "finalized", "materialized"), report)
        return self.command("merge.finish", {"intent_id": report["intent_id"], "expected_revision": report["intent_revision"]})

    def build(self):
        identity = self.propose()
        self.action(identity, "merge.enqueue")
        self.finish(self.action(identity, "merge.begin", kind="build"))
        return identity

    def review(self, identity):
        row = self.row(identity)
        candidate = json.loads(row["candidate_json"])
        note = {k: candidate[k] for k in ("task_id", "assignment_id", "generation")}
        note.update(candidate_sha256=mq._digest(candidate), reviewer_agent_id=self.f.agents["planner"], verdict="accept")
        path = self.root / "integration-review.json"
        path.write_bytes(mq._json(note).encode("ascii"))
        self.command("merge.review", dict(merge_request_id=identity, expected_revision=row["revision"],
                     note_path=str(path), note_sha256=hashlib.sha256(path.read_bytes()).hexdigest()), "reviewer")

    def test_native_markdown_review_real_git_lifecycle_and_narrowed_binding(self):
        item = self.accepted()
        original_bytes = item["review_path"].read_bytes()
        identity = self.build()
        accepted = json.loads(self.row(identity)["spec_json"])["accepted"]
        self.assertEqual(accepted["manifest"]["review_sha256"], hashlib.sha256(original_bytes).hexdigest())
        self.assertNotIn("agent_id", json.loads(accepted["review"]["actor_json"]))
        self.assertTrue(accepted["evidence"]["path"].endswith(".md"))
        self.assertEqual(accepted["native"]["reviewer_agent_id"], self.f.agents["planner"])
        self.review(identity)
        review = json.loads(self.row(identity)["review_json"])
        self.assertEqual(review["binding_id"], self.f.actor[0])
        self.assertEqual(review["binding_version"], self.f.actor[1])
        self.assertEqual(review["reviewer_agent_id"], self.principal("reviewer")["agent_id"])
        ticket = self.action(identity, "merge.begin", kind="land")
        # Real process restart: load parent dependencies plus this exact sidecar.
        script = ("import sys;sys.path.insert(0,sys.argv[1]);import awo_service,workspace_manager;"
                  "sys.path.insert(0,sys.argv[2]);import merge_queue as q,json;"
                  "print(json.dumps(q.execute_intent(sys.argv[3],sys.argv[4],sys.argv[5])))")
        proc = subprocess.run([sys.executable, "-B", "-c", script, str(Path(service.__file__).parent),
                               str(Path(mq.__file__).parent), str(self.db), ticket["intent_id"], ticket["operation_token"]],
                              capture_output=True, text=True, timeout=45)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["outcome"], "landed", report)
        self.assertEqual(self.f.rows("SELECT task_state FROM v2_task")[0]["task_state"], "accepted")
        self.assertEqual(self.fixture.git(item["repo"], "rev-parse", "target"), report["merge_commit"])
        self.command("merge.finish", {"intent_id": report["intent_id"], "expected_revision": report["intent_revision"]})
        self.assertEqual(self.f.rows("SELECT task_state FROM v2_task")[0]["task_state"], "landed")
        self.assertEqual(item["review_path"].read_bytes(), original_bytes)

    def test_native_artifact_inventory_finalizes_without_merge_request(self):
        item = self.accepted("directory")
        p = {k: item[k] for k in ("task_id", "assignment_id", "generation", "policy_id")}
        p["expected_revision"] = self.f.rows("SELECT revision FROM v2_task")[0]["revision"]
        self.finish(self.command("artifact.begin", p))
        self.assertEqual(self.f.rows("SELECT task_state FROM v2_task")[0]["task_state"], "finalized")
        self.assertEqual(self.f.rows("SELECT * FROM v2_merge_request"), [])

    def test_native_stacked_base_gate_is_pure_even_outside_transaction(self):
        item = self.accepted()
        self.f.create("DOWNSTREAM")
        ticket = self.command("merge.materialize", {"task_id": "DOWNSTREAM", "expected_revision": 1,
            "policy_id": item["policy_id"], "target_ref": "refs/heads/target",
            "parents": [{k: item[k] for k in ("task_id", "assignment_id", "generation")}]})
        self.finish(ticket)
        base = self.f.rows("SELECT base_commit FROM v2_merge_materialization WHERE task_id='DOWNSTREAM'")[0]["base_commit"]
        con = service._connect(self.db, readonly=True)
        self.addCleanup(con.close)
        self.assertFalse(con.in_transaction)
        with mock.patch.object(wm, "run_argv", side_effect=AssertionError("pure gate launched subprocess")):
            gate = mq.materialization_gate(con, self.project, "DOWNSTREAM", base)
        self.assertTrue(gate["ready"], gate)
        self.assertEqual(gate["materialized_from"], [item["task_id"]])

    def test_native_acceptance_requires_observed_freeze(self):
        self.accepted(freeze=False)
        with self.assertRaisesRegex(mq.MergeError, "owned frozen workspace"):
            self.propose()

    def test_native_tuple_connection_commands_are_caller_owned_without_subprocess(self):
        item = self.accepted()
        con = sqlite3.connect(self.db)
        self.addCleanup(con.close)
        con.execute("BEGIN IMMEDIATE")
        payload = {k: item[k] for k in ("task_id", "assignment_id", "generation", "workspace_id", "policy_id")}
        payload.update(target_ref="refs/heads/target",
                       expected_revision=con.execute("SELECT revision FROM v2_task WHERE task_id=?", (item["task_id"],)).fetchone()[0])
        with mock.patch.object(wm, "run_argv", side_effect=AssertionError("transaction launched subprocess")):
            result = mq.apply_command(con, self.project, self.principal(), "merge.propose", payload)
        self.assertIsNone(con.row_factory)
        self.assertTrue(con.in_transaction)
        con.rollback()
        self.assertIsNone(con.execute("SELECT 1 FROM v2_merge_request WHERE merge_request_id=?",
                                     (result["merge_request_id"],)).fetchone())
        self.assertEqual(self.fixture.git(item["repo"], "rev-parse", "target"), item["base_commit"])

    def test_native_original_review_delivery_inventory_and_quiescence_mutation(self):
        item = self.accepted()
        identity = self.propose()
        saved = json.loads(self.row(identity)["spec_json"])["accepted"]
        quiet = json.loads(saved["workspace"]["quiescence_json"])
        paths = [item["review_path"], Path(saved["evidence"]["path"]), item["path"] / "allowed.txt",
                 Path(quiet["evidence_path"])]
        for path in paths:
            with self.subTest(path=path.name):
                old = path.read_bytes()
                path.write_bytes(old + b" altered")
                try:
                    with self.assertRaisesRegex(mq.MergeError, "EVIDENCE_CHANGED"):
                        self.action(identity, "merge.enqueue")
                finally:
                    path.write_bytes(old)
        with self.f.db() as con:
            con.execute("UPDATE v2_workspace_review SET inventory_sha256=? WHERE assignment_id=?", ("0" * 64, item["assignment_id"]))
        with self.assertRaisesRegex(mq.MergeError, "EVIDENCE_CHANGED"):
            self.action(identity, "merge.enqueue")

    def test_native_role_narrowing_rejects_missing_wrong_and_worker_binding(self):
        with self.f.db() as con:
            good = self.principal("reviewer")
            self.assertEqual(mq._principal(con, self.project, good, ("reviewer",))["agent_id"], good["agent_id"])
            bads = [dict(good, binding_id=None), dict(good, binding_version=True),
                    dict(good, agent_id=self.f.agents["second"]),
                    dict(good, agent_id=self.f.agents["executor"], binding_id=self.f.bindings["executor"]["instance_id"],
                         binding_version=self.f.bindings["executor"]["version"])]
            for bad in bads:
                with self.assertRaisesRegex(mq.MergeError, "permission_denied"):
                    mq._principal(con, self.project, bad, ("reviewer",))

    def test_native_saved_integration_binding_rechecked_when_revoked(self):
        self.accepted()
        identity = self.build()
        self.review(identity)
        row = self.row(identity)
        with self.f.db() as con:
            con.execute("UPDATE bindings SET status='retired' WHERE id=?", (self.f.actor[0],))
            with self.assertRaisesRegex(mq.MergeError, "permission_denied"):
                mq._review_binding(con, self.project, json.loads(row["spec_json"]),
                                   json.loads(row["candidate_json"]), json.loads(row["review_json"]))

    def test_native_reviewer_independent_of_all_competing_worker_identities(self):
        self.accepted()
        queued, _ = self.f.enqueue(task="T-B", role="second")
        reserved = self.f.claim(task_id="T-B", expected_revision=queued["revision"])
        self.assertEqual(reserved["status"], "RESERVED")
        with self.f.db() as con:
            contract = json.loads(con.execute("SELECT contract_json FROM v2_scheduler_intent WHERE assignment_id=?",
                                             (reserved["assignment_id"],)).fetchone()[0])
            contract["worker_binding"] = self.f.bindings["planner"]
            con.execute("UPDATE v2_assignment SET task_id=?,slot_id=1 WHERE assignment_id=?",
                        (self.item["task_id"], reserved["assignment_id"]))
            con.execute("UPDATE v2_scheduler_intent SET contract_json=? WHERE assignment_id=?",
                        (json.dumps(contract), reserved["assignment_id"]))
        with self.assertRaisesRegex(mq.MergeError, "competing worker binding"):
            self.propose()


class NativeMergeTests(NativeMergeBase, unittest.TestCase):
    """Directory-mode fixture; inherits the shared native integration tests."""


class NativeMaterializedSourceTests(NativeMergeBase, unittest.TestCase):
    """Real materialization proof plus the frozen source workspace.

    B2/B3: a code dependency may only unlock a Task whose worker is bound to a
    new worktree created from the exact recorded materialization base commit.
    """

    def setUp(self):
        import test_workspace_runtime
        self.fixture_class = test_workspace_runtime.FrozenWorkspaceRuntimeTests
        super().setUp()

    def test_real_gate_and_frozen_worktree_bind_the_actual_send(self):
        item = self.accepted(role="second")
        self.f.create("DOWNSTREAM")
        ticket = self.command("merge.materialize", {"task_id": "DOWNSTREAM", "expected_revision": 1,
            "policy_id": item["policy_id"], "target_ref": "refs/heads/target",
            "parents": [{k: item[k] for k in ("task_id", "assignment_id", "generation")}]})
        self.finish(ticket)
        base = self.f.rows("SELECT base_commit FROM v2_merge_materialization WHERE task_id='DOWNSTREAM'")[0]["base_commit"]
        frozen = str((self.root / "workspaces" / "frozen-source").resolve())
        request = self.f.request("ATTEMPT-DOWNSTREAM", "executor")
        queued, _ = self.f.enqueue("DOWNSTREAM", "executor", create=False, dependencies=["T-A"],
            workspace_mode="git_worktree", task_kind="code", workspace_base_commit=base, request=request)
        reserved = self.f.claim(task_id="DOWNSTREAM", expected_revision=queued["revision"])
        self.assertEqual(reserved["status"], "RESERVED", reserved)
        aid = reserved["assignment_id"]
        with self.assertRaisesRegex(ValueError, "reserved managed workspace"):
            self.f.dispatch(aid)
        sends = self.root / "worker-executor" / "sends.jsonl"
        self.assertFalse(sends.exists())
        common = {"assignment_id": aid, "expected_revision": 1, "kind": "git_worktree",
                  "root": str(self.fixture.workspaces), "allowed_paths": ["allowed.txt"],
                  "repository_id": "fixture-repo", "repo_root": str(item["repo"])}
        with self.assertRaisesRegex(ValueError, "frozen worker launch directory"):
            self.runtime.create(self.f.state, *self.f.actor, self.f.envelope("workspace.create",
                dict(common, path=str(self.fixture.workspaces / "other-source"), base_commit=base)))
        with self.assertRaisesRegex(ValueError, "frozen materialized base commit"):
            self.runtime.create(self.f.state, *self.f.actor, self.f.envelope("workspace.create",
                dict(common, path=frozen, base_commit="b" * 40)))
        created = self.runtime.create(self.f.state, *self.f.actor, self.f.envelope("workspace.create",
            dict(common, path=frozen, base_commit=base)))
        self.assertEqual(created["status"], "WORKSPACE_READY", created)
        self.assertEqual(created["workspace"]["base_commit"], base)
        self.assertEqual(self.f.dispatch(aid)["status"], "SUBMITTED")
        contract = json.loads(self.f.rows(
            "SELECT contract_json FROM v2_scheduler_intent WHERE assignment_id=?", (aid,))[0]["contract_json"])
        self.assertEqual(contract["workspace_path"], frozen)
        self.assertEqual(contract["request"]["worker"]["expected_identity"]["launch_cwd"], frozen)
        self.assertNotEqual(contract["request"]["cwd"], frozen)
        sent = [json.loads(line) for line in sends.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(sent[-1]["cwd"], contract["request"]["cwd"])
        completion = self.f.reconcile(aid)
        self.assertEqual(completion["status"], "REVIEW_READY", completion)
        self.f.review(completion, request, same_connection=True)
        self.f.close(aid, completion)
        self.assertEqual(self.f.rows("SELECT task_state FROM v2_task WHERE task_id='DOWNSTREAM'")[0]["task_state"],
                         "accepted")
        self.assertEqual(self.f.rows("SELECT * FROM v2_merge_request"), [])


if __name__ == "__main__":
    unittest.main()
