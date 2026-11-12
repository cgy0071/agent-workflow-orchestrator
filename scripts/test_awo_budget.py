"""Regression tests for the additive V2 budget ledger."""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import unittest
import uuid

import awo_service as service
import budget
import workflow_control as control


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = (Path(__file__).resolve().parents[1] / "runtime").resolve()
        self.sandbox.mkdir(exist_ok=True)
        self.root = self.sandbox / ("test-awo-budget-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.state = self.root / "state"
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
        self.project = service.service_status(self.state)["project_id"]

    def tearDown(self):
        target = self.root.resolve()
        if target.parent != self.sandbox or not target.name.startswith("test-awo-budget-"):
            raise RuntimeError("refuse unsafe cleanup")
        shutil.rmtree(target)

    def spec(self, role):
        identity = {"kind": "any-harness", "terminal": "terminal-" + role,
                    "launch_cwd": str(self.root)}
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

    def envelope(self, tool, payload, command_id=None, epoch=0):
        return {"command_id": command_id or ("cmd-" + uuid.uuid4().hex),
                "tool": tool, "expected_epoch": epoch, "payload": payload}

    def set_limits(self, scope_type, scope_id, limits, policy=None, revision=1):
        return budget.budget_limit(self.state, *self.actor,
                                   self.envelope("budget.limit", {
                                       "scope_type": scope_type, "scope_id": scope_id,
                                       "limits": limits, "policy": policy or {},
                                       "expected_revision": revision}))

    def reserve(self, assignment, generation, dimensions, run_id="run-1",
                command_id=None, epoch=0, task="T-BUD"):
        self.insert_task(task, [assignment])
        return budget.budget_reserve(self.state, *self.actor,
                                     self.envelope("budget.reserve", {
                                         "assignment_id": assignment, "generation": generation,
                                         "run_id": run_id,
                                         "dimensions": dimensions, "basis": "estimate"},
                                         command_id=command_id, epoch=epoch))

    def sample(self, run, assignment, generation, seq, dimension, value, is_delta=True,
               command_id=None):
        return budget.budget_sample(self.state, *self.actor,
                                    self.envelope("budget.sample", {
                                        "run_id": run, "assignment_id": assignment,
                                        "generation": generation, "sample_seq": seq,
                                        "dimension": dimension, "value": value,
                                        "is_delta": is_delta, "source": "test"},
                                        command_id=command_id))

    def close(self, run, assignment, generation, state, usage, authoritative=None,
              reconcile=False, command_id=None):
        usage = {key: (dict(item, source=item.get("source", "test")) if isinstance(item, dict) else item)
                 for key, item in usage.items()}
        payload = {"run_id": run, "assignment_id": assignment, "generation": generation,
                   "state": state, "usage": usage, "reconcile": reconcile}
        if reconcile:
            payload["expected_revision"] = 1
        if authoritative is not None:
            payload["authoritative"] = authoritative
        return budget.budget_close(self.state, *self.actor,
                                   self.envelope("budget.close", payload, command_id=command_id))

    def insert_task(self, task_id, assignments):
        with self.db() as con:
            now = service._now()
            con.execute("INSERT OR IGNORE INTO v2_task(task_id,project_id,task_state,"
                        "created_at,updated_at) VALUES (?,?, 'active',?,?)",
                        (task_id, self.project, now, now))
            for assignment in assignments:
                next_slot = con.execute("SELECT count(*) FROM v2_assignment WHERE task_id=?",
                                        (task_id,)).fetchone()[0]
                con.execute("INSERT OR IGNORE INTO v2_assignment(assignment_id,task_id,generation,"
                            "slot_id,holds_slot,state,review_status,revision,created_at,updated_at) "
                            "VALUES (?,?,1,?,0,'running','none',1,?,?)",
                            (assignment, task_id, next_slot, now, now))

    def test_dimension_validation_and_unlimited(self):
        with self.assertRaises(service.ServiceError):
            self.set_limits("run", "run-bad", {"tokens.input": -1})
        with self.assertRaises(service.ServiceError):
            self.set_limits("run", "run-bad", {"tokens.input": True})
        with self.assertRaises(service.ServiceError):
            self.set_limits("run", "run-bad", {"tokens.input": float("nan")})
        status = budget.budget_status(self.state, "run", "run-bad")
        self.assertFalse(status["exists"])
        # Unlimited scope exists after a reservation with no limit.
        result = self.reserve("A0", 1, {"tokens.input": 1000000}, run_id="run-bad")
        self.assertEqual(result["status"], "RESERVED")
        self.assertEqual(budget.budget_status(self.state, "run", "run-bad")["limits"], {})

    def test_three_scope_reservation_is_all_or_nothing(self):
        self.set_limits("run", "run-1", {"tokens.input": 100})
        self.set_limits("task", "T-FAIL", {"tokens.input": 50})
        self.set_limits("task", "T-OK", {"tokens.input": 50})
        self.set_limits("workflow", self.project, {"tokens.input": 1000})
        with self.assertRaises(service.ServiceError):
            self.reserve("A-FAIL", 1, {"tokens.input": 80}, task="T-FAIL")
        with self.db() as con:
            self.assertEqual(con.execute(
                "SELECT count(*) FROM v2_budget_reservation WHERE assignment_id='A-FAIL'").fetchone()[0], 0)
            con.execute("DELETE FROM v2_assignment WHERE assignment_id='A-FAIL'")
        result = self.reserve("A-OK", 1, {"tokens.input": 30}, task="T-OK")
        self.assertEqual(result["status"], "RESERVED")
        self.assertEqual(len(result["reservation_ids"]), 3)

    def test_concurrent_reservation_has_one_winner(self):
        self.set_limits("run", "run-race", {"tokens.input": 100})
        self.insert_task("T-BUD", ["race-a", "race-b"])
        script = Path(service.__file__).resolve()
        envs = []
        for name in ("race-a", "race-b"):
            env = self.root / (name + ".json")
            env.write_text(json.dumps(self.envelope("budget.reserve", {
                "assignment_id": name, "generation": 1, "run_id": "run-race",
                "dimensions": {"tokens.input": 60}, "basis": "estimate"})), encoding="utf-8")
            envs.append(env)
        procs = []
        for env in envs:
            procs.append(subprocess.Popen(
                [sys.executable, "-B", str(script), "--state", str(self.state), "budget-reserve",
                 "--envelope", str(env), "--actor", self.actor[0],
                 "--version", str(self.actor[1]), "--context", "main"],
                cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        results = [proc.communicate() + (proc.returncode,) for proc in procs]
        self.assertEqual(sorted(item[2] for item in results), [0, 2])

    def test_duplicate_and_conflicting_reservation(self):
        self.set_limits("run", "run-dup", {"tokens.input": 100})
        first = self.reserve("A-DUP", 1, {"tokens.input": 10}, run_id="run-dup",
                             command_id="cmd-reserve-dup-0001")
        replay = self.reserve("A-DUP", 1, {"tokens.input": 10}, run_id="run-dup",
                              command_id="cmd-reserve-dup-0001")
        self.assertTrue(replay["replayed"])
        same = self.reserve("A-DUP", 1, {"tokens.input": 10}, run_id="run-dup")
        self.assertEqual(same["reservation_ids"], [])
        with self.assertRaises(service.ServiceError):
            self.reserve("A-DUP", 1, {"tokens.input": 11}, run_id="run-dup")

    def test_samples_and_authoritative_snapshot_do_not_double_count(self):
        self.reserve("A-S", 1, {"tokens.input": 0}, run_id="run-s")
        self.sample("run-s", "A-S", 1, 0, "tokens.input", 10)
        self.sample("run-s", "A-S", 1, 1, "tokens.input", 15)
        partial = self.close("run-s", "A-S", 1, "partial", {}, authoritative=False)
        self.assertEqual(partial["state"], "partial")
        status = budget.budget_status(self.state, "run", "run-s")
        self.assertEqual(status["known_actual"], {})
        self.assertEqual(status["stream_exposure"]["tokens.input"], 25)
        self.close("run-s", "A-S", 1, "actual",
                   {"tokens.input": {"state": "actual", "value": 20}}, authoritative=True,
                   reconcile=True)
        status = budget.budget_status(self.state, "run", "run-s")
        self.assertEqual(status["known_actual"]["tokens.input"], 20)
        duplicate = self.close("run-s", "A-S", 1, "actual",
                               {"tokens.input": {"state": "actual", "value": 20}})
        self.assertEqual(duplicate["status"], "CLOSE_DUPLICATE")
        with self.assertRaises(service.ServiceError):
            self.close("run-s", "A-S", 1, "actual",
                       {"tokens.input": {"state": "actual", "value": 99}})
        with self.assertRaises(service.ServiceError):
            self.sample("run-s", "A-S", 1, 0, "tokens.input", 11)

    def test_unknown_cost_with_known_tokens_is_dimension_specific(self):
        self.reserve("A-U", 1, {"tokens.input": 0}, run_id="run-u")
        self.close("run-u", "A-U", 1, "partial", {
            "tokens.input": {"state": "actual", "value": 10},
            "cost.usd": {"state": "unknown", "reason": "harness reported no price"}},
            authoritative=True)
        status = budget.budget_status(self.state, "run", "run-u")
        self.assertEqual(status["known_actual"]["tokens.input"], 10)
        self.assertEqual(status["unknown"]["cost.usd"], 1)
        self.set_limits("task", "T-BUD", {"tokens.input": 100})
        self.reserve("A-U3", 1, {"tokens.input": 20}, run_id="run-u3")
        self.set_limits("task", "T-BUD", {"cost.usd": 5, "tokens.input": 100}, revision=2)
        with self.assertRaises(service.ServiceError):
            self.reserve("A-U2", 1, {"cost.usd": 1, "tokens.input": 1}, run_id="run-u2")
        self.set_limits("task", "T-BUD", {"cost.usd": 5, "tokens.input": 100},
                        policy={"on_unknown": "warn"}, revision=3)
        self.reserve("A-U4", 1, {"cost.usd": 1, "tokens.input": 1}, run_id="run-u4")

    def test_revision_role_and_action_states(self):
        self.set_limits("run", "run-a", {"tokens.input": 10})
        with self.assertRaises(service.ServiceError):
            budget.budget_limit(self.state, *self.actor, self.envelope("budget.limit", {
                "scope_type": "run", "scope_id": "run-a", "limits": {"tokens.input": 20},
                "expected_revision": 1}))
        with self.assertRaises(ValueError):
            budget.budget_limit(self.state, self.worker["instance_id"], self.worker["version"],
                                "main", self.envelope("budget.limit", {
                                    "scope_type": "run", "scope_id": "run-a",
                                    "limits": {"tokens.input": 20}, "expected_revision": 2}))
        action = budget.budget_action(self.state, *self.actor, self.envelope("budget.action", {
            "scope_type": "run", "scope_id": "run-a", "action": "pause_requested",
            "reason": "test", "expected_revision": 2}))
        self.assertEqual(action["state"], "pause_requested")
        self.assertIn("does not prove", action["note"])
        status = budget.budget_status(self.state, "run", "run-a")
        self.assertEqual(status["state"], "pause_requested")
        approved = budget.budget_approve(self.state, *self.actor, self.envelope("budget.approve", {
            "scope_type": "run", "scope_id": "run-a", "expected_revision": 3, "reason": "approved"}))
        self.assertEqual(approved["state"], "ok")

    def test_task_attempts_aggregate_across_assignments(self):
        self.insert_task("T-AGG", ["A-1", "A-2"])
        self.reserve("A-1", 1, {"tokens.input": 10}, run_id="run-a1", task="T-AGG")
        self.reserve("A-2", 1, {"tokens.input": 15}, run_id="run-a2", task="T-AGG")
        self.close("run-a1", "A-1", 1, "actual",
                   {"tokens.input": {"state": "actual", "value": 10}}, authoritative=True)
        self.close("run-a2", "A-2", 1, "actual",
                   {"tokens.input": {"state": "actual", "value": 15}}, authoritative=True)
        status = budget.budget_status(self.state, "task", "T-AGG")
        self.assertEqual(status["known_actual"]["tokens.input"], 25)
        self.assertEqual(status["attempts"], 2)

    def test_settlement_and_overrun(self):
        self.set_limits("run", "run-set", {"tokens.input": 100})
        self.reserve("A-SET", 1, {"tokens.input": 30}, run_id="run-set")
        self.close("run-set", "A-SET", 1, "actual",
                   {"tokens.input": {"state": "actual", "value": 20}}, authoritative=True)
        with self.db() as con:
            row = con.execute("SELECT state,settled_amount FROM v2_budget_reservation "
                              "WHERE assignment_id='A-SET'").fetchone()
        self.assertEqual(row["state"], "settled")
        self.assertEqual(row["settled_amount"], 20)
        self.close("run-set", "A-SET", 1, "actual",
                   {"tokens.input": {"state": "actual", "value": 120}}, authoritative=True,
                   reconcile=True)
        self.assertEqual(budget.budget_status(self.state, "run", "run-set")["state"], "exceeded")

    def test_subprocess_interruption_rolls_back(self):
        self.set_limits("run", "run-crash", {"tokens.input": 100})
        with self.db() as con:
            budget_id = con.execute("SELECT budget_id FROM v2_budget WHERE scope_id='run-crash'").fetchone()[0]
        child = self.root / "crash_child.py"
        child.write_text(
            "import sqlite3, os, sys, json\n"
            "con=sqlite3.connect(sys.argv[1]); con.execute('BEGIN IMMEDIATE')\n"
            "con.execute('INSERT INTO v2_budget_reservation(reservation_id,budget_id,assignment_id,generation,"
            "dimension,amount,state,basis,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)',\n"
            "('res-crash',sys.argv[2],'A-CRASH',1,'tokens.input',10.0,'held','estimate','x','x'))\n"
            "os._exit(9)\n", encoding="utf-8")
        proc = subprocess.run([sys.executable, "-B", str(child), str(self.state / "workflow.sqlite3"), budget_id],
                              cwd=self.root, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 9)
        with self.db() as con:
            self.assertEqual(con.execute(
                "SELECT count(*) FROM v2_budget_reservation WHERE assignment_id='A-CRASH'").fetchone()[0], 0)

    def test_cli_budget_flow(self):
        self.insert_task("T-BUD", ["A-CLI"])
        script = Path(service.__file__).resolve()
        base = [sys.executable, "-B", str(script), "--state", str(self.state)]
        limit_env = self.root / "limit.json"
        limit_env.write_text(json.dumps(self.envelope("budget.limit", {
            "scope_type": "run", "scope_id": "run-cli", "limits": {"tokens.input": 100},
            "expected_revision": 1})), encoding="utf-8")
        proc = subprocess.run(base + ["budget-limit", "--envelope", str(limit_env),
                                     "--actor", self.actor[0], "--version", str(self.actor[1]),
                                     "--context", "main"], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        reserve_env = self.root / "reserve.json"
        reserve_env.write_text(json.dumps(self.envelope("budget.reserve", {
            "assignment_id": "A-CLI", "generation": 1, "run_id": "run-cli",
            "dimensions": {"tokens.input": 30}, "basis": "estimate"})), encoding="utf-8")
        proc = subprocess.run(base + ["budget-reserve", "--envelope", str(reserve_env),
                                     "--actor", self.actor[0], "--version", str(self.actor[1]),
                                     "--context", "main"], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        close_env = self.root / "close.json"
        close_env.write_text(json.dumps(self.envelope("budget.close", {
            "run_id": "run-cli", "assignment_id": "A-CLI", "generation": 1, "state": "actual",
            "usage": {"tokens.input": {"state": "actual", "value": 20, "source": "test"}},
            "authoritative": True, "reconcile": False})), encoding="utf-8")
        proc = subprocess.run(base + ["budget-close", "--envelope", str(close_env),
                                     "--actor", self.actor[0], "--version", str(self.actor[1]),
                                     "--context", "main"], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = subprocess.run(base + ["budget-status", "--scope-type", "run", "--scope-id", "run-cli"],
                              cwd=self.root, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["known_actual"]["tokens.input"], 20)


    def test_freeze_blocks_new_reservation(self):
        self.set_limits("run", "run-frozen", {"tokens.input": 100})
        service.set_freeze(self.state, *self.actor, True, "budget test")
        epoch = service.service_status(self.state)["controller_epoch"]
        with self.assertRaises(service.ServiceError):
            self.reserve("A-FROZEN", 1, {"tokens.input": 1}, run_id="run-frozen", epoch=epoch)

    def test_sample_must_match_run_binding(self):
        self.reserve("A-BIND", 1, {"tokens.input": 0}, run_id="run-bind")
        with self.assertRaises(service.ServiceError):
            self.sample("run-bind", "A-OTHER", 1, 0, "tokens.input", 1)

    def test_exact_cap_retry_with_new_command_succeeds(self):
        self.set_limits("run", "run-cap", {"tokens.input": 10})
        first = self.reserve("A-CAP", 1, {"tokens.input": 10}, run_id="run-cap",
                             command_id="cmd-cap-first-0001")
        retry = self.reserve("A-CAP", 1, {"tokens.input": 10}, run_id="run-cap",
                             command_id="cmd-cap-retry-0002")
        self.assertEqual(retry["reservation_ids"], [])
        self.assertEqual(budget.budget_status(self.state, "run", "run-cap")
                         ["nominal_remaining"]["tokens.input"], 0)

    def test_authoritative_close_stops_counting_stream(self):
        self.set_limits("run", "run-close", {"tokens.input": 100})
        self.reserve("A-CLOSE", 1, {"tokens.input": 10}, run_id="run-close")
        self.sample("run-close", "A-CLOSE", 1, 0, "tokens.input", 15)
        status = budget.budget_status(self.state, "run", "run-close")
        self.assertEqual(status["nominal_remaining"]["tokens.input"], 85)
        self.close("run-close", "A-CLOSE", 1, "actual",
                   {"tokens.input": {"state": "actual", "value": 18}}, authoritative=True)
        status = budget.budget_status(self.state, "run", "run-close")
        self.assertEqual(status["known_actual"]["tokens.input"], 18)
        self.assertEqual(status["nominal_remaining"]["tokens.input"], 82)

    def test_estimated_close_never_settles_actual(self):
        self.set_limits("run", "run-est", {"tokens.input": 100})
        self.set_limits("task", "T-BUD", {"tokens.input": 100})
        self.reserve("A-EST", 1, {"tokens.input": 30}, run_id="run-est")
        self.close("run-est", "A-EST", 1, "partial",
                   {"tokens.input": {"state": "estimated", "value": 20, "source": "estimate"}},
                   authoritative=False)
        with self.db() as con:
            row = con.execute("SELECT state,settled_amount FROM v2_budget_reservation "
                              "WHERE assignment_id='A-EST'").fetchone()
        self.assertEqual(row["state"], "unknown")
        self.assertIsNone(row["settled_amount"])
        status = budget.budget_status(self.state, "run", "run-est")
        self.assertEqual(status["estimated"]["tokens.input"], 20)
        self.assertEqual(status["known_actual"], {})
        self.assertEqual(status["unknown_reserved"]["tokens.input"], 30)
        with self.assertRaises(service.ServiceError):
            self.reserve("A-EST-2", 1, {"tokens.input": 1}, run_id="run-est-2")

    def test_historical_assignment_without_usage_blocks_admission(self):
        self.insert_task("T-HIST", ["A-HIST"])
        with self.db() as con:
            con.execute("UPDATE v2_assignment SET legacy_task_id='legacy-old' WHERE assignment_id='A-HIST'")
        self.set_limits("task", "T-HIST", {"tokens.input": 100})
        with self.assertRaises(service.ServiceError):
            self.reserve("A-HIST-NEW", 1, {"tokens.input": 1}, run_id="run-hist-new", task="T-HIST")


    def test_mixed_authoritative_close_suppresses_stream_per_dimension(self):
        self.set_limits("task", "T-BUD", {"tokens.input": 100, "cost.usd": 10})
        self.reserve("A-MIX", 1, {"tokens.input": 10, "cost.usd": 2}, run_id="run-mix")
        self.sample("run-mix", "A-MIX", 1, 0, "tokens.input", 6)
        self.close("run-mix", "A-MIX", 1, "partial", {
            "tokens.input": {"state": "actual", "value": 5, "source": "final"},
            "cost.usd": {"state": "unknown", "reason": "not-reported", "source": "final"}},
            authoritative=True)
        status = budget.budget_status(self.state, "task", "T-BUD")
        self.assertEqual(status["nominal_remaining"]["tokens.input"], 95)
        self.assertEqual(status["known_actual"]["tokens.input"], 5)
        self.assertEqual(status["unknown"].get("cost.usd"), 1)
        self.assertEqual(status["unknown_reserved"].get("cost.usd"), 2)

    def test_estimated_cap_does_not_free_new_work(self):
        self.insert_task("T-EST2", ["A-EST-A", "A-EST-B"])
        self.set_limits("task", "T-EST2", {"cost.usd": 10})
        self.reserve("A-EST-A", 1, {"cost.usd": 10}, run_id="run-est-a", task="T-EST2")
        self.close("run-est-a", "A-EST-A", 1, "partial",
                   {"cost.usd": {"state": "estimated", "value": 1, "source": "estimator"}},
                   authoritative=False)
        with self.assertRaises(service.ServiceError):
            self.reserve("A-EST-B", 1, {"cost.usd": 9}, run_id="run-est-b", task="T-EST2")

    def test_attempt_cap_uses_durable_assignments(self):
        self.insert_task("T-ATT", [])
        self.set_limits("task", "T-ATT", {"attempts": 1})
        self.reserve("A-ATT-1", 1, {"attempts": 0}, run_id="run-att-1", task="T-ATT")
        self.close("run-att-1", "A-ATT-1", 1, "actual",
                   {"attempts": {"state": "actual", "value": 0, "source": "report"}},
                   authoritative=True)
        self.insert_task("T-ATT", ["A-ATT-2"])
        with self.assertRaises(service.ServiceError):
            self.reserve("A-ATT-2", 1, {"attempts": 0}, run_id="run-att-2", task="T-ATT")

    def test_stream_only_partial_is_visible(self):
        self.set_limits("run", "run-part", {"tokens.input": 100})
        self.reserve("A-PART", 1, {"tokens.input": 10}, run_id="run-part")
        self.sample("run-part", "A-PART", 1, 0, "tokens.input", 25)
        self.close("run-part", "A-PART", 1, "partial", {}, authoritative=False)
        status = budget.budget_status(self.state, "run", "run-part")
        self.assertEqual(status["partial_actual"]["tokens.input"], 25)
        self.assertEqual(status["known_actual"], {})
        self.assertEqual(status["unknown"]["tokens.input"], 1)


    def test_repair_cap_uses_durable_reviews(self):
        self.insert_task("T-REP", ["A-REP"])
        with self.db() as con:
            con.execute("INSERT INTO v2_review(review_id,task_id,event_id,verdict,note_path,state,source) "
                        "VALUES ('rev-rep','T-REP','evt','repair','/tmp/x','sealed','test')")
        self.set_limits("task", "T-REP", {"repairs": 1})
        with self.assertRaises(service.ServiceError):
            self.reserve("A-REP-NEW", 1, {"repairs": 0}, run_id="run-rep-new", task="T-REP")

    def test_read_status_has_no_schema_side_effects(self):
        other = self.root / "empty-state"
        control.initialize(other, self.config)
        before = sorted(p.name for p in other.iterdir())
        script = Path(service.__file__).resolve()
        proc = subprocess.run([sys.executable, "-B", str(script), "--state", str(other),
                               "budget-status", "--scope-type", "run", "--scope-id", "x"],
                              cwd=self.root, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(sorted(p.name for p in other.iterdir()), before)
        con = sqlite3.connect(other / "workflow.sqlite3")
        try:
            self.assertFalse(con.execute(
                "SELECT 1 FROM sqlite_master WHERE name='v2_budget'").fetchone())
        finally:
            con.close()


    def r2_scopes(self, run_id, task="T-BUD"):
        return (("run", run_id), ("task", task), ("workflow", self.project))

    def r2_limits(self, run_id, limit=10, task="T-BUD", tokens=None):
        limits = {"cost.usd": limit}
        if tokens is not None:
            limits["tokens.input"] = tokens
        for scope_type, scope_id in self.r2_scopes(run_id, task):
            self.set_limits(scope_type, scope_id, limits, policy={"on_unknown": "warn"})

    def r2_snapshot(self):
        with self.db() as con:
            return tuple(con.iterdump())

    def r2_assert_cap_denied(self, assignment, run_id, task="T-BUD", amount=1,
                             dimensions=None):
        # Create the identity before the snapshot: only the budget command must
        # roll back, including any run link or earlier scope it tried to insert.
        self.insert_task(task, [assignment])
        before = self.r2_snapshot()
        env = self.envelope("budget.reserve", {
            "assignment_id": assignment, "generation": 1, "run_id": run_id,
            "dimensions": dimensions if dimensions is not None else {"cost.usd": amount},
            "basis": "estimate"})
        with self.assertRaisesRegex(service.ServiceError,
                                    "budget cap exceeded|budget state blocks admission: exceeded"):
            budget.budget_reserve(self.state, *self.actor, env)
        self.assertEqual(self.r2_snapshot(), before)

    def r2_cli_status(self, scope_type, scope_id):
        proc = subprocess.run(
            [sys.executable, "-B", str(Path(service.__file__).resolve()),
             "--state", str(self.state), "budget-status",
             "--scope-type", scope_type, "--scope-id", scope_id],
            cwd=self.root, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def r2_check_unresolved_at_cap(self, usage):
        self.r2_limits("run-r2-cap")
        self.reserve("A-R2-CAP", 1, {"cost.usd": 10}, run_id="run-r2-cap")
        self.close("run-r2-cap", "A-R2-CAP", 1, "partial",
                   {"cost.usd": usage}, authoritative=True)
        for scope_type, scope_id in self.r2_scopes("run-r2-cap"):
            with self.subTest(scope=scope_type):
                status = budget.budget_status(self.state, scope_type, scope_id)
                self.assertEqual(status["nominal_remaining"]["cost.usd"], 0)
                self.assertEqual(status["unknown_reserved"]["cost.usd"], 10)
                self.assertNotIn("cost.usd", status["known_actual"])
                self.assertTrue(status["warning"]["cost.usd"])
        self.r2_assert_cap_denied("A-R2-TASK-DENIED", "run-r2-task-denied")
        self.r2_assert_cap_denied("A-R2-WF-DENIED", "run-r2-wf-denied", task="T-R2-OTHER")

    def test_r2_authoritative_unknown_retains_cap_in_three_scopes(self):
        self.r2_check_unresolved_at_cap({"state": "unknown", "reason": "no price"})

    def test_r2_authoritative_small_estimate_retains_cap_in_three_scopes(self):
        self.r2_check_unresolved_at_cap({"state": "estimated", "value": 1})

    def r2_check_estimate_maximum(self, estimate, authoritative):
        self.r2_limits("run-r2-est", limit=20)
        self.reserve("A-R2-EST", 1, {"cost.usd": 10}, run_id="run-r2-est")
        self.sample("run-r2-est", "A-R2-EST", 1, 0, "cost.usd", 8)
        self.close("run-r2-est", "A-R2-EST", 1, "partial",
                   {"cost.usd": {"state": "estimated", "value": estimate}},
                   authoritative=authoritative)
        remaining = 20 - max(10, 8, estimate)
        for scope_type, scope_id in self.r2_scopes("run-r2-est"):
            with self.subTest(scope=scope_type):
                status = budget.budget_status(self.state, scope_type, scope_id)
                self.assertEqual(status["nominal_remaining"]["cost.usd"], remaining)
                self.assertEqual(status["estimated"]["cost.usd"], estimate)
                self.assertEqual(status["unknown_reserved"]["cost.usd"], 10)
                self.assertNotIn("cost.usd", status["known_actual"])
        self.r2_assert_cap_denied("A-R2-OVER", "run-r2-over", amount=remaining + 1)
        admitted = self.reserve("A-R2-EXACT", 1, {"cost.usd": remaining}, run_id="run-r2-exact")
        self.assertEqual(admitted["status"], "RESERVED")
        self.assertEqual(budget.budget_status(self.state, "task", "T-BUD")
                         ["nominal_remaining"]["cost.usd"], 0)

    def test_r2_small_estimate_uses_maximum_without_double_counting(self):
        self.r2_check_estimate_maximum(1, authoritative=False)

    def test_r2_large_estimate_uses_maximum_without_double_counting(self):
        self.r2_check_estimate_maximum(12, authoritative=False)

    def test_r2_authoritative_large_estimate_still_retains_uncertainty(self):
        self.r2_check_estimate_maximum(12, authoritative=True)

    def test_r2_nonauthoritative_actual_cannot_settle_reservation(self):
        self.r2_limits("run-r2-observed")
        self.reserve("A-R2-OBSERVED", 1, {"cost.usd": 10}, run_id="run-r2-observed")
        self.close("run-r2-observed", "A-R2-OBSERVED", 1, "partial",
                   {"cost.usd": {"state": "actual", "value": 1}}, authoritative=False)
        with self.db() as con:
            rows = con.execute("SELECT state,settled_amount FROM v2_budget_reservation "
                               "WHERE assignment_id='A-R2-OBSERVED'").fetchall()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row["state"] == "unknown" and row["settled_amount"] is None
                            for row in rows))
        for scope_type, scope_id in self.r2_scopes("run-r2-observed"):
            status = budget.budget_status(self.state, scope_type, scope_id)
            # This compatibility field is an observation, not proof of settlement.
            self.assertEqual(status["known_actual"]["cost.usd"], 1)
            self.assertEqual(status["nominal_remaining"]["cost.usd"], 0)
        self.r2_assert_cap_denied("A-R2-OBS-OVER", "run-r2-obs-over")

    def test_r2_large_nonauthoritative_actual_is_one_partial_exposure(self):
        self.r2_limits("run-r2-large-observed", limit=20)
        self.reserve("A-R2-LARGE-OBS", 1, {"cost.usd": 10}, run_id="run-r2-large-observed")
        self.sample("run-r2-large-observed", "A-R2-LARGE-OBS", 1, 0, "cost.usd", 8)
        self.close("run-r2-large-observed", "A-R2-LARGE-OBS", 1, "partial",
                   {"cost.usd": {"state": "actual", "value": 12}}, authoritative=False)
        for scope_type, scope_id in self.r2_scopes("run-r2-large-observed"):
            status = budget.budget_status(self.state, scope_type, scope_id)
            self.assertEqual(status["nominal_remaining"]["cost.usd"], 8)
            self.assertEqual(status["unknown_reserved"]["cost.usd"], 10)
        self.reserve("A-R2-LARGE-EXACT", 1, {"cost.usd": 8}, run_id="run-r2-large-exact")
        self.r2_assert_cap_denied("A-R2-LARGE-OVER", "run-r2-large-over")

    def test_r2_mixed_close_settles_actual_dimension_only(self):
        self.r2_limits("run-r2-mixed", tokens=100)
        self.reserve("A-R2-MIXED", 1, {"tokens.input": 10, "cost.usd": 10},
                     run_id="run-r2-mixed")
        self.sample("run-r2-mixed", "A-R2-MIXED", 1, 0, "tokens.input", 6)
        self.sample("run-r2-mixed", "A-R2-MIXED", 1, 0, "cost.usd", 4)
        self.close("run-r2-mixed", "A-R2-MIXED", 1, "partial", {
            "tokens.input": {"state": "actual", "value": 5},
            "cost.usd": {"state": "unknown", "reason": "no price"}}, authoritative=True)
        for scope_type, scope_id in self.r2_scopes("run-r2-mixed"):
            status = budget.budget_status(self.state, scope_type, scope_id)
            self.assertEqual(status["nominal_remaining"], {"tokens.input": 95, "cost.usd": 0})
            self.assertEqual(status["known_actual"], {"tokens.input": 5})
            self.assertEqual(status["unknown_reserved"].get("cost.usd"), 10)
        with self.db() as con:
            rows = con.execute("SELECT dimension,state,settled_amount FROM v2_budget_reservation "
                               "WHERE assignment_id='A-R2-MIXED'").fetchall()
        self.assertEqual(len(rows), 6)
        for row in rows:
            self.assertEqual((row["state"], row["settled_amount"]),
                             ("settled", 5) if row["dimension"] == "tokens.input" else ("unknown", None))
        self.r2_assert_cap_denied("A-R2-MIXED-OVER", "run-r2-mixed-over",
                                 dimensions={"tokens.input": 1, "cost.usd": 1})

    def test_r2_two_runs_sum_their_individual_maxima(self):
        self.r2_limits("run-r2-sum-a", limit=20)
        self.set_limits("run", "run-r2-sum-b", {"cost.usd": 20}, policy={"on_unknown": "warn"})
        for suffix, reserve, stream, estimate, exposure in (("a", 7, 3, 5, 7), ("b", 4, 9, 2, 9)):
            run, assignment = "run-r2-sum-" + suffix, "A-R2-SUM-" + suffix
            self.reserve(assignment, 1, {"cost.usd": reserve}, run_id=run)
            self.sample(run, assignment, 1, 0, "cost.usd", stream)
            self.close(run, assignment, 1, "partial",
                       {"cost.usd": {"state": "estimated", "value": estimate}}, authoritative=True)
            self.assertEqual(budget.budget_status(self.state, "run", run)
                             ["nominal_remaining"]["cost.usd"], 20 - exposure)
        for scope_type, scope_id in (("task", "T-BUD"), ("workflow", self.project)):
            status = budget.budget_status(self.state, scope_type, scope_id)
            self.assertEqual(status["nominal_remaining"]["cost.usd"], 4)
            self.assertTrue(status["warning"]["cost.usd"])
        self.r2_assert_cap_denied("A-R2-SUM-OVER", "run-r2-sum-over", amount=5)
        self.r2_assert_cap_denied("A-R2-SUM-WF-OVER", "run-r2-sum-wf-over",
                                 task="T-R2-SUM-OTHER", amount=5)
        self.reserve("A-R2-SUM-EXACT", 1, {"cost.usd": 4}, run_id="run-r2-sum-exact")

    def test_r2_stream_only_close_uses_cumulative_then_delta_once(self):
        self.r2_limits("run-r2-stream", limit=20)
        self.reserve("A-R2-STREAM", 1, {"cost.usd": 10}, run_id="run-r2-stream")
        self.sample("run-r2-stream", "A-R2-STREAM", 1, 0, "cost.usd", 3)
        self.sample("run-r2-stream", "A-R2-STREAM", 1, 1, "cost.usd", 8, is_delta=False)
        self.sample("run-r2-stream", "A-R2-STREAM", 1, 2, "cost.usd", 4)
        self.close("run-r2-stream", "A-R2-STREAM", 1, "partial", {}, authoritative=False)
        for scope_type, scope_id in self.r2_scopes("run-r2-stream"):
            status = budget.budget_status(self.state, scope_type, scope_id)
            self.assertEqual(status["nominal_remaining"]["cost.usd"], 8)
            self.assertEqual(status["partial_actual"]["cost.usd"], 12)
            self.assertEqual(status["known_actual"], {})
        self.reserve("A-R2-STREAM-EXACT", 1, {"cost.usd": 8}, run_id="run-r2-stream-exact")


    def test_r2_nonauthoritative_actual_default_blocks_then_warn_obeys_cap(self):
        self.set_limits("task", "T-BUD", {"cost.usd": 10})
        self.reserve("A-R2-DEFAULT", 1, {"cost.usd": 6}, run_id="run-r2-default")
        self.close("run-r2-default", "A-R2-DEFAULT", 1, "partial",
                   {"cost.usd": {"state": "actual", "value": 1}}, authoritative=False)
        status = budget.budget_status(self.state, "task", "T-BUD")
        self.assertEqual(status["nominal_remaining"]["cost.usd"], 4)
        self.assertEqual(status["unknown_reserved"]["cost.usd"], 6)
        with self.assertRaisesRegex(service.ServiceError, "unknown usage blocks admission"):
            self.reserve("A-R2-BLOCK", 1, {"cost.usd": 1}, run_id="run-r2-block")
        self.set_limits("task", "T-BUD", {"cost.usd": 10},
                        policy={"on_unknown": "warn"}, revision=status["version"])
        self.r2_assert_cap_denied("A-R2-WARN-OVER", "run-r2-warn-over", amount=5)
        self.reserve("A-R2-WARN-EXACT", 1, {"cost.usd": 4}, run_id="run-r2-warn-exact")

    def test_r2_actual_reconciliation_releases_once_across_replay_and_restart(self):
        run, assignment = "run-r2-reconcile", "A-R2-RECONCILE"
        self.r2_limits(run)
        self.reserve(assignment, 1, {"cost.usd": 10}, run_id=run)
        self.sample(run, assignment, 1, 0, "cost.usd", 8)
        unknown = {"cost.usd": {"state": "unknown", "reason": "no price"}}
        self.close(run, assignment, 1, "partial", unknown, authoritative=True,
                   command_id="cmd-r2-unknown-close")
        before = self.r2_snapshot()
        replay = self.close(run, assignment, 1, "partial", unknown, authoritative=True,
                            command_id="cmd-r2-unknown-close")
        self.assertTrue(replay["replayed"])
        self.assertEqual(self.r2_snapshot(), before)
        for scope_type, scope_id in self.r2_scopes(run):
            self.assertEqual(self.r2_cli_status(scope_type, scope_id)
                             ["nominal_remaining"]["cost.usd"], 0)
        payload = {"run_id": run, "assignment_id": assignment, "generation": 1,
                   "state": "actual", "authoritative": True, "reconcile": True,
                   "expected_revision": 1,
                   "usage": {"cost.usd": {"state": "actual", "value": 6, "source": "test"}}}
        final_env = self.envelope("budget.close", payload, command_id="cmd-r2-final-close")
        final = budget.budget_close(self.state, *self.actor, final_env)
        self.assertEqual(final["revision"], 2)
        before = self.r2_snapshot()
        replay = budget.budget_close(self.state, *self.actor, final_env)
        self.assertTrue(replay["replayed"])
        self.assertEqual(self.r2_snapshot(), before)
        with self.assertRaisesRegex(service.ServiceError, "stale close revision"):
            budget.budget_close(self.state, *self.actor, self.envelope("budget.close", payload))
        self.assertEqual(self.r2_snapshot(), before)
        duplicate = self.close(run, assignment, 1, "actual",
                               {"cost.usd": {"state": "actual", "value": 6}}, authoritative=True)
        self.assertEqual(duplicate["status"], "CLOSE_DUPLICATE")
        for scope_type, scope_id in self.r2_scopes(run):
            status = self.r2_cli_status(scope_type, scope_id)
            self.assertEqual(status["nominal_remaining"]["cost.usd"], 4)
            self.assertEqual(status["known_actual"]["cost.usd"], 6)
            self.assertEqual(status["unknown_reserved"].get("cost.usd", 0), 0)
        with self.db() as con:
            rows = con.execute("SELECT state,settled_amount FROM v2_budget_reservation "
                               "WHERE assignment_id=?", (assignment,)).fetchall()
            revision = con.execute("SELECT revision FROM v2_usage_close WHERE run_id=?", (run,)).fetchone()[0]
        self.assertEqual(revision, 2)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(tuple(row) == ("settled", 6) for row in rows))
        self.reserve("A-R2-FINAL-EXACT", 1, {"cost.usd": 4}, run_id="run-r2-final-exact")
        self.r2_assert_cap_denied("A-R2-FINAL-OVER", "run-r2-final-over")
        self.r2_assert_cap_denied("A-R2-FINAL-WF-OVER", "run-r2-final-wf-over", task="T-R2-FINAL-OTHER")

    def test_r2_close_requires_strict_boolean_flags(self):
        invalid = ("true", "false", 1, 0, None, [], {})
        for field in ("authoritative", "reconcile"):
            for index, value in enumerate(invalid):
                with self.subTest(field=field, value=value):
                    run = "run-r2-flag-" + field + str(index)
                    assignment = "A-R2-FLAG-" + field + str(index)
                    self.reserve(assignment, 1, {"cost.usd": 10}, run_id=run)
                    payload = {"run_id": run, "assignment_id": assignment, "generation": 1,
                               "state": "partial", "authoritative": True, "reconcile": False,
                               "usage": {"cost.usd": {"state": "actual", "value": 6, "source": "test"}}}
                    payload[field] = value
                    before = self.r2_snapshot()
                    with self.assertRaisesRegex(service.ServiceError, "boolean"):
                        budget.budget_close(self.state, *self.actor, self.envelope("budget.close", payload))
                    self.assertEqual(self.r2_snapshot(), before)

    def test_r2_contradictory_whole_close_states_are_rejected_atomically(self):
        cases = (("actual", {"state": "unknown", "reason": "not reported"}),
                 ("actual", {"state": "estimated", "value": 1}),
                 ("unknown", {"state": "actual", "value": 1}),
                 ("unknown", {"state": "estimated", "value": 1}))
        for index, (state, item) in enumerate(cases):
            with self.subTest(state=state, measurement=item["state"]):
                run, assignment = "run-r2-contradict-" + str(index), "A-R2-CONTRADICT-" + str(index)
                self.reserve(assignment, 1, {"cost.usd": 10}, run_id=run)
                before = self.r2_snapshot()
                with self.assertRaises(service.ServiceError):
                    self.close(run, assignment, 1, state, {"cost.usd": item}, authoritative=True)
                self.assertEqual(self.r2_snapshot(), before)

    def test_r2_r1_authority_list_cannot_settle_unknown_or_estimated(self):
        self.set_limits("task", "T-BUD", {"cost.usd": 40}, policy={"on_unknown": "warn"})
        self.set_limits("workflow", self.project, {"cost.usd": 40}, policy={"on_unknown": "warn"})
        cases = (({"state": "unknown", "reason": "no price"}, 10),
                 ({"state": "estimated", "value": 1}, 10),
                 ({"state": "estimated", "value": 12}, 12))
        for index, (item, exposure) in enumerate(cases):
            run, assignment = "run-r2-r1-" + str(index), "A-R2-R1-" + str(index)
            self.set_limits("run", run, {"cost.usd": 20}, policy={"on_unknown": "warn"})
            self.reserve(assignment, 1, {"cost.usd": 10}, run_id=run)
            self.close(run, assignment, 1, "partial", {"cost.usd": item}, authoritative=False)
            # Seed exactly the erroneous R1 authority metadata in this temporary DB.
            with self.db() as con:
                con.execute("UPDATE v2_usage_close SET authority_json=? WHERE run_id=?",
                            (json.dumps(["cost.usd"]), run))
            before = self.r2_snapshot()
            self.assertEqual(budget.budget_status(self.state, "run", run)
                             ["nominal_remaining"]["cost.usd"], 20 - exposure)
            self.assertEqual(self.r2_snapshot(), before)
        for scope_type, scope_id in (("task", "T-BUD"), ("workflow", self.project)):
            status = self.r2_cli_status(scope_type, scope_id)
            self.assertEqual(status["nominal_remaining"]["cost.usd"], 8)
        self.r2_assert_cap_denied("A-R2-R1-OVER", "run-r2-r1-over", amount=9)
        self.r2_assert_cap_denied("A-R2-R1-WF-OVER", "run-r2-r1-wf-over",
                                 task="T-R2-R1-OTHER", amount=9)

    def test_r2_old_schema_read_is_conservative_and_does_not_migrate(self):
        self.r2_limits("run-r2-old", limit=20)
        self.reserve("A-R2-OLD", 1, {"cost.usd": 10}, run_id="run-r2-old")
        self.close("run-r2-old", "A-R2-OLD", 1, "partial",
                   {"cost.usd": {"state": "actual", "value": 1}}, authoritative=False)
        with self.db() as con:
            # Old versions settled nonauthoritative observations. That label is
            # not proof of authority; also exercise the pre-run_id reservation schema.
            con.execute("UPDATE v2_budget_reservation SET state='settled',settled_amount=1 "
                        "WHERE assignment_id='A-R2-OLD'")
            con.execute("ALTER TABLE v2_usage_close DROP COLUMN authority_json")
            con.execute("ALTER TABLE v2_budget_reservation DROP COLUMN run_id")
        before = self.r2_snapshot()
        for scope_type, scope_id in self.r2_scopes("run-r2-old"):
            status = self.r2_cli_status(scope_type, scope_id)
            self.assertEqual(status["known_actual"]["cost.usd"], 1)
            self.assertEqual(status["nominal_remaining"]["cost.usd"], 10)
        self.assertEqual(self.r2_snapshot(), before)
        with self.db() as con:
            self.assertNotIn("authority_json", [row[1] for row in con.execute("PRAGMA table_info(v2_usage_close)")])
            budget.create_budget_schema(con)
            self.assertIn("authority_json", [row[1] for row in con.execute("PRAGMA table_info(v2_usage_close)")])
        # An explicit additive schema upgrade alone must not release exposure.
        for scope_type, scope_id in self.r2_scopes("run-r2-old"):
            self.assertEqual(budget.budget_status(self.state, scope_type, scope_id)
                             ["nominal_remaining"]["cost.usd"], 10)
        self.close("run-r2-old", "A-R2-OLD", 1, "actual",
                   {"cost.usd": {"state": "actual", "value": 6}},
                   authoritative=True, reconcile=True)
        for scope_type, scope_id in self.r2_scopes("run-r2-old"):
            self.assertEqual(budget.budget_status(self.state, scope_type, scope_id)
                             ["nominal_remaining"]["cost.usd"], 14)

    def test_r2_reconcile_cannot_drop_or_lower_unsettled_measurements(self):
        run, assignment = "run-r2-monotonic", "A-R2-MONOTONIC"
        self.r2_limits(run, limit=20, tokens=100)
        self.reserve(assignment, 1, {"cost.usd": 10, "tokens.input": 10}, run_id=run)
        self.close(run, assignment, 1, "partial", {
            "cost.usd": {"state": "estimated", "value": 12},
            "tokens.input": {"state": "actual", "value": 5}}, authoritative=True)
        cases = ({"tokens.input": {"state": "actual", "value": 5}},
                 {"tokens.input": {"state": "actual", "value": 5},
                  "cost.usd": {"state": "estimated", "value": 1}},
                 {"cost.usd": {"state": "estimated", "value": 12}})
        for usage in cases:
            with self.subTest(usage=usage):
                before = self.r2_snapshot()
                with self.assertRaises(service.ServiceError):
                    self.close(run, assignment, 1, "partial", usage, authoritative=True, reconcile=True)
                self.assertEqual(self.r2_snapshot(), before)
        self.close(run, assignment, 1, "actual", {
            "cost.usd": {"state": "actual", "value": 6},
            "tokens.input": {"state": "actual", "value": 5}}, authoritative=True, reconcile=True)
        status = budget.budget_status(self.state, "task", "T-BUD")
        self.assertEqual(status["nominal_remaining"], {"cost.usd": 14, "tokens.input": 95})


    def test_r2_process_race_after_reconciliation_has_one_cap_winner(self):
        self.r2_limits("run-r2-race-origin")
        self.reserve("A-R2-RACE-ORIGIN", 1, {"cost.usd": 10}, run_id="run-r2-race-origin")
        self.close("run-r2-race-origin", "A-R2-RACE-ORIGIN", 1, "partial",
                   {"cost.usd": {"state": "unknown", "reason": "not priced"}}, authoritative=True)
        self.close("run-r2-race-origin", "A-R2-RACE-ORIGIN", 1, "actual",
                   {"cost.usd": {"state": "actual", "value": 6}},
                   authoritative=True, reconcile=True)
        gate = self.root / "race-go"
        child_code = """from pathlib import Path
import sys, time
sys.path.insert(0, sys.argv[1])
import awo_service as service
Path(sys.argv[2]).write_text('ready', encoding='ascii')
deadline = time.monotonic() + 20
while not Path(sys.argv[3]).exists():
    if time.monotonic() > deadline:
        raise SystemExit(8)
    time.sleep(0.01)
raise SystemExit(service.main(sys.argv[4:]))
"""
        procs, ready_paths, cli_commands = [], [], []
        try:
            for index in range(2):
                assignment, task = "A-R2-RACE-" + str(index), "T-R2-RACE-" + str(index)
                self.insert_task(task, [assignment])
                env = self.root / ("race-" + str(index) + ".json")
                env.write_text(json.dumps(self.envelope("budget.reserve", {
                    "assignment_id": assignment, "generation": 1, "run_id": "run-r2-race-" + str(index),
                    "dimensions": {"cost.usd": 3}, "basis": "estimate"},
                    command_id="cmd-r2-race-" + str(index))), encoding="utf-8")
                ready = self.root / ("race-ready-" + str(index))
                ready_paths.append(ready)
                cli = ["--state", str(self.state), "budget-reserve", "--envelope", str(env),
                       "--actor", self.actor[0], "--version", str(self.actor[1]), "--context", "main"]
                cli_commands.append(cli)
                procs.append(subprocess.Popen(
                    [sys.executable, "-B", "-c", child_code, str(Path(service.__file__).resolve().parent),
                     str(ready), str(gate)] + cli, cwd=self.root,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            deadline = time.monotonic() + 20
            while not all(path.exists() for path in ready_paths):
                self.assertTrue(all(proc.poll() is None for proc in procs), "race child exited before barrier")
                if time.monotonic() > deadline:
                    self.fail("race children did not reach the barrier")
                time.sleep(0.01)
            gate.write_text("go", encoding="ascii")
            results = [proc.communicate(timeout=30) + (proc.returncode,) for proc in procs]
        finally:
            for proc in procs:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate(timeout=5)
        self.assertEqual(sorted(result[2] for result in results), [0, 2], results)
        winner = next(index for index, result in enumerate(results) if result[2] == 0)
        loser = 1 - winner
        self.assertIn("budget cap exceeded for workflow:cost.usd", results[loser][1])
        self.assertEqual(json.loads(results[winner][0])["status"], "RESERVED")
        with self.db() as con:
            self.assertEqual(con.execute("SELECT count(*) FROM v2_budget_reservation WHERE assignment_id=?",
                                         ("A-R2-RACE-" + str(loser),)).fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT count(*) FROM v2_run_link WHERE run_id=?",
                                         ("run-r2-race-" + str(loser),)).fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT count(*) FROM v2_budget WHERE scope_type='run' AND scope_id=?",
                                         ("run-r2-race-" + str(loser),)).fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT count(*) FROM v2_command_log WHERE command_id=?",
                                         ("cmd-r2-race-" + str(loser),)).fetchone()[0], 0)
        self.assertEqual(self.r2_cli_status("workflow", self.project)
                         ["nominal_remaining"]["cost.usd"], 1)
        # A restarted process replays the winning command without charging again.
        replay = subprocess.run(
            [sys.executable, "-B", str(Path(service.__file__).resolve())] + cli_commands[winner],
            cwd=self.root, capture_output=True, text=True, timeout=30)
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertTrue(json.loads(replay.stdout)["replayed"])
        self.assertEqual(self.r2_cli_status("workflow", self.project)
                         ["nominal_remaining"]["cost.usd"], 1)
        self.reserve("A-R2-RACE-" + str(loser), 1, {"cost.usd": 1},
                     run_id="run-r2-race-" + str(loser), task="T-R2-RACE-" + str(loser))
        self.r2_assert_cap_denied("A-R2-RACE-OVER", "run-r2-race-over", task="T-R2-RACE-OVER")

    def test_r2_interrupted_reconciliation_rolls_back_close_and_reservations(self):
        run, assignment = "run-r2-crash-close", "A-R2-CRASH-CLOSE"
        self.r2_limits(run, limit=20)
        self.reserve(assignment, 1, {"cost.usd": 10}, run_id=run)
        self.sample(run, assignment, 1, 0, "cost.usd", 8)
        self.close(run, assignment, 1, "partial",
                   {"cost.usd": {"state": "estimated", "value": 12}}, authoritative=True)
        before = self.r2_snapshot()
        child_code = """import json, os, sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute('BEGIN IMMEDIATE')
usage = json.dumps({'cost.usd': {'state': 'actual', 'value': 6, 'source': 'crash-test'}}, sort_keys=True)
con.execute('UPDATE v2_usage_close SET state=?,usage_json=?,authority_json=?,revision=revision+1 WHERE run_id=?',
            ('actual', usage, json.dumps(['cost.usd']), sys.argv[2]))
con.execute('UPDATE v2_budget_reservation SET state=?,settled_amount=6 WHERE assignment_id=?',
            ('settled', sys.argv[3]))
os._exit(9)
"""
        proc = subprocess.run(
            [sys.executable, "-B", "-c", child_code, str(self.state / "workflow.sqlite3"), run, assignment],
            cwd=self.root, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 9, proc.stderr)
        self.assertEqual(self.r2_snapshot(), before)
        for scope_type, scope_id in self.r2_scopes(run):
            status = self.r2_cli_status(scope_type, scope_id)
            self.assertEqual(status["nominal_remaining"]["cost.usd"], 8)
            self.assertEqual(status["unknown_reserved"]["cost.usd"], 10)
        self.close(run, assignment, 1, "actual",
                   {"cost.usd": {"state": "actual", "value": 6}},
                   authoritative=True, reconcile=True)
        for scope_type, scope_id in self.r2_scopes(run):
            self.assertEqual(self.r2_cli_status(scope_type, scope_id)
                             ["nominal_remaining"]["cost.usd"], 14)


    def test_r2_reconcile_requires_an_existing_close_revision(self):
        run, assignment = "run-r2-no-close", "A-R2-NO-CLOSE"
        self.r2_limits(run)
        self.reserve(assignment, 1, {"cost.usd": 10}, run_id=run)
        first_payload = {"run_id": run, "assignment_id": assignment, "generation": 1,
                         "state": "actual", "authoritative": True,
                         "usage": {"cost.usd": {"state": "actual", "value": 1, "source": "test"}}}
        for revision_fields in ({}, {"expected_revision": 999}, {"expected_revision": 1}):
            with self.subTest(revision_fields=revision_fields):
                payload = dict(first_payload, reconcile=True, **revision_fields)
                before = self.r2_snapshot()
                with self.assertRaisesRegex(service.ServiceError, "existing close"):
                    budget.budget_close(self.state, *self.actor, self.envelope("budget.close", payload))
                self.assertEqual(self.r2_snapshot(), before)
                for scope_type, scope_id in self.r2_scopes(run):
                    self.assertEqual(budget.budget_status(self.state, scope_type, scope_id)
                                     ["nominal_remaining"]["cost.usd"], 0)
        # A legitimate first close omits reconcile and must still be accepted.
        result = budget.budget_close(self.state, *self.actor, self.envelope("budget.close", first_payload))
        self.assertEqual((result["status"], result["revision"]), ("CLOSED", 1))
        for scope_type, scope_id in self.r2_scopes(run):
            self.assertEqual(budget.budget_status(self.state, scope_type, scope_id)
                             ["nominal_remaining"]["cost.usd"], 9)

    def test_r2_closed_run_rejects_new_reservations_but_original_command_replays(self):
        run, assignment = "run-r2-already-closed", "A-R2-ALREADY-CLOSED"
        self.insert_task("T-BUD", [assignment])
        original_payload = {"run_id": run, "assignment_id": assignment, "generation": 1,
                            "dimensions": {"tokens.input": 1}, "basis": "estimate"}
        original_env = self.envelope("budget.reserve", original_payload, command_id="cmd-r2-preclose-reserve")
        first = budget.budget_reserve(self.state, *self.actor, original_env)
        self.close(run, assignment, 1, "actual", {
            "tokens.input": {"state": "actual", "value": 1},
            "cost.usd": {"state": "actual", "value": 1}}, authoritative=True)
        self.set_limits("workflow", self.project, {"cost.usd": 10}, policy={"on_unknown": "warn"})
        for dimensions in ({"cost.usd": 9}, {"tokens.input": 1, "cost.usd": 9}, {"tokens.input": 1}):
            with self.subTest(dimensions=dimensions):
                before = self.r2_snapshot()
                payload = dict(original_payload, dimensions=dimensions)
                with self.assertRaisesRegex(service.ServiceError, "closed"):
                    budget.budget_reserve(self.state, *self.actor, self.envelope("budget.reserve", payload))
                self.assertEqual(self.r2_snapshot(), before)
        before = self.r2_snapshot()
        replay = budget.budget_reserve(self.state, *self.actor, original_env)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["reservation_ids"], first["reservation_ids"])
        self.assertEqual(self.r2_snapshot(), before)
        self.assertEqual(budget.budget_status(self.state, "workflow", self.project)
                         ["nominal_remaining"]["cost.usd"], 9)
        admitted = self.reserve("A-R2-OTHER-OPEN", 1, {"cost.usd": 9},
                                run_id="run-r2-other-open", task="T-R2-OTHER-OPEN")
        self.assertEqual(admitted["status"], "RESERVED")
        self.assertEqual(budget.budget_status(self.state, "workflow", self.project)
                         ["nominal_remaining"]["cost.usd"], 0)
        with self.db() as con:
            self.assertEqual(con.execute(
                "SELECT count(*) FROM v2_budget_reservation WHERE assignment_id=? AND dimension='cost.usd'",
                (assignment,)).fetchone()[0], 0)

    def test_r2_unsettled_closes_also_reject_late_dimensions(self):
        cases = (("partial", {"state": "estimated", "value": 1}),
                 ("unknown", {"state": "unknown", "reason": "not reported"}))
        for index, (state, item) in enumerate(cases):
            with self.subTest(close_state=state):
                run, assignment = "run-r2-late-" + str(index), "A-R2-LATE-" + str(index)
                self.reserve(assignment, 1, {"tokens.input": 1}, run_id=run)
                self.close(run, assignment, 1, state, {"tokens.input": item}, authoritative=True)
                before = self.r2_snapshot()
                with self.assertRaisesRegex(service.ServiceError, "closed"):
                    budget.budget_reserve(self.state, *self.actor, self.envelope("budget.reserve", {
                        "run_id": run, "assignment_id": assignment, "generation": 1,
                        "dimensions": {"cost.usd": 9}, "basis": "estimate"}))
                self.assertEqual(self.r2_snapshot(), before)

    def test_r2_sample_sequence_requires_nonnegative_integer_without_erasing_deltas(self):
        run, assignment = "run-r2-sequence", "A-R2-SEQUENCE"
        self.r2_limits(run)
        self.reserve(assignment, 1, {"cost.usd": 1}, run_id=run)
        self.sample(run, assignment, 1, 0, "cost.usd", 9)
        invalid = (True, False, -1, 0.0, 0.5, 1.0, "1", "999999", "not-an-integer", None, [], {})
        for sequence in invalid:
            with self.subTest(sequence=sequence):
                before = self.r2_snapshot()
                with self.assertRaisesRegex(service.ServiceError, "sample_seq.*integer"):
                    self.sample(run, assignment, 1, sequence, "cost.usd", 0, is_delta=False)
                self.assertEqual(self.r2_snapshot(), before)
        # A malformed text sequence would sort after these integer sequences in
        # SQLite and let its cumulative zero erase both the old and new deltas.
        self.sample(run, assignment, 1, 1, "cost.usd", 2)
        for scope_type, scope_id in self.r2_scopes(run):
            status = budget.budget_status(self.state, scope_type, scope_id)
            self.assertEqual(status["stream_exposure"]["cost.usd"], 11)
            self.assertEqual(status["nominal_remaining"]["cost.usd"], 0)
        with self.db() as con:
            rows = con.execute("SELECT sample_seq,typeof(sample_seq),value,is_delta FROM v2_usage_sample "
                               "WHERE run_id=? ORDER BY sample_seq", (run,)).fetchall()
        self.assertEqual([tuple(row) for row in rows], [(0, "integer", 9, 1), (1, "integer", 2, 1)])
        self.r2_assert_cap_denied("A-R2-SEQUENCE-OVER", "run-r2-sequence-over", amount=9)
        self.r2_assert_cap_denied("A-R2-SEQUENCE-WF-OVER", "run-r2-sequence-wf-over",
                                 task="T-R2-SEQUENCE-OTHER", amount=9)

    def test_r2_sample_generation_requires_positive_integer(self):
        run, assignment = "run-r2-sample-generation", "A-R2-SAMPLE-GENERATION"
        self.reserve(assignment, 1, {"cost.usd": 10}, run_id=run)
        invalid = (True, False, 0, -1, 1.0, 1.5, "1", "not-an-integer", None, [], {})
        for generation in invalid:
            with self.subTest(generation=generation):
                before = self.r2_snapshot()
                with self.assertRaisesRegex(service.ServiceError, "generation.*integer"):
                    self.sample(run, assignment, generation, 0, "cost.usd", 0, is_delta=False)
                self.assertEqual(self.r2_snapshot(), before)
        result = self.sample(run, assignment, 1, 0, "cost.usd", 3)
        self.assertEqual(result["status"], "SAMPLE_RECORDED")
        self.assertEqual(budget.budget_status(self.state, "run", run)["stream_exposure"]["cost.usd"], 3)

    def test_r2_close_generation_requires_positive_integer(self):
        run, assignment = "run-r2-close-generation", "A-R2-CLOSE-GENERATION"
        self.reserve(assignment, 1, {"cost.usd": 10}, run_id=run)
        usage = {"cost.usd": {"state": "actual", "value": 1}}
        invalid = (True, False, 0, -1, 1.0, 1.5, "1", "not-an-integer", None, [], {})
        for generation in invalid:
            with self.subTest(generation=generation):
                before = self.r2_snapshot()
                with self.assertRaisesRegex(service.ServiceError, "generation.*integer"):
                    self.close(run, assignment, generation, "actual", usage, authoritative=True)
                self.assertEqual(self.r2_snapshot(), before)
        result = self.close(run, assignment, 1, "actual", usage, authoritative=True)
        self.assertEqual((result["status"], result["revision"]), ("CLOSED", 1))


if __name__ == "__main__":
    unittest.main()
