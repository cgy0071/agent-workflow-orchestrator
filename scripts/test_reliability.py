"""Independent-process crash/race checks, evidence ownership and read-only summaries."""
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

import monitor_task as monitor
import test_scheduler as fixtures
import workflow_control as control
import workflow_evidence as evidence
import workflow_scheduler as scheduler
import workflow_status as status


CHILD = r'''
import json, os, sys, time
from pathlib import Path
import workflow_scheduler as scheduler
state, actor, version, run, root, mode = sys.argv[1:]
root = Path(root)
class Transport:
    def __init__(self): self.queries = 0
    def status(self, spec):
        self.queries += 1
        if self.queries == 1 and mode in ('barrier', 'before'):
            (root / ('ready-' + str(os.getpid()))).write_text('ready')
            deadline = time.monotonic() + 15
            while not (root / 'go').exists():
                if time.monotonic() > deadline: raise TimeoutError('fixture barrier')
                time.sleep(.01)
        return {'identity': spec['expected_identity'], 'status': 'idle', 'completion_seq': 10}
    def send(self, spec, message):
        fd = os.open(str(root / 'sends.log'), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (message + '\n').encode()); os.fsync(fd)
        finally: os.close(fd)
        if mode == 'after':
            (root / 'sent').write_text(str(os.getpid()))
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline: time.sleep(.05)
            raise TimeoutError('fixture parent did not terminate child')
        return 'submitted'
print(json.dumps(scheduler.tick(state, run, actor, int(version), 'main', transport=Transport())))
'''


class ReliabilityTests(unittest.TestCase):
    setUp = fixtures.SchedulerTests.setUp
    tearDown = fixtures.SchedulerTests.tearDown
    actor = fixtures.SchedulerTests.actor
    spec = fixtures.SchedulerTests.spec
    request = fixtures.SchedulerTests.request
    message = fixtures.SchedulerTests.message
    send = fixtures.SchedulerTests.send
    confirm = fixtures.SchedulerTests.confirm
    finish = fixtures.SchedulerTests.finish

    def start(self, run, mode):
        return subprocess.Popen([sys.executable, "-B", "-c", CHILD, str(self.state), self.actor()[0],
                                 str(self.actor()[1]), run, str(self.root), mode],
                                cwd=Path(scheduler.__file__).parent, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, creationflags=0x08000000 if os.name == "nt" else 0)

    def wait_for(self, predicate):
        deadline = time.monotonic() + 12
        while not predicate():
            if time.monotonic() >= deadline: self.fail("fixture condition timed out")
            time.sleep(.01)

    def collect(self, proc):
        try:
            out, err = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.communicate()
            self.fail("fixture child did not finish")
        self.assertEqual(proc.returncode, 0, err)
        return json.loads(out)

    def one_node(self, run, name="TASK-A", role="executor", resources=None):
        req = self.request(name, role)
        graph = {"nodes": [{"id": "node", "needs": [], "request": req, "message": self.message(req), "resources": resources or {}}]}
        scheduler.load_dag(self.state, run, graph, *self.actor())
        return req

    def test_two_processes_tick_the_same_ready_node_once(self):
        self.one_node("race")
        children = [self.start("race", "barrier") for _ in range(2)]
        try:
            self.wait_for(lambda: len(list(self.root.glob("ready-*"))) == 2)
            (self.root / "go").write_text("go")
            results = [self.collect(proc) for proc in children]
        finally:
            for proc in children:
                if proc.poll() is None: proc.kill(); proc.communicate()
        self.assertEqual(sorted(item["results"][0]["status"] for item in results), ["ALREADY_ATTEMPTED", "SUBMITTED"])
        self.assertEqual(len((self.root / "sends.log").read_text().splitlines()), 1)

    def test_two_processes_cannot_exceed_resource_capacity(self):
        self.one_node("first", "FIRST", resources={"gpu": 1})
        self.one_node("second", "SECOND", "second", {"gpu": 1})
        children = [self.start(run, "barrier") for run in ("first", "second")]
        try:
            self.wait_for(lambda: len(list(self.root.glob("ready-*"))) == 2)
            (self.root / "go").write_text("go")
            results = [self.collect(proc) for proc in children]
        finally:
            for proc in children:
                if proc.poll() is None: proc.kill(); proc.communicate()
        self.assertEqual(sorted(item["results"][0]["status"] for item in results), ["DEFERRED", "SUBMITTED"])
        self.assertEqual(len((self.root / "sends.log").read_text().splitlines()), 1)

    def test_kill_after_external_send_before_commit_never_resends(self):
        req = self.one_node("crash")
        proc = self.start("crash", "after")
        try:
            self.wait_for(lambda: (self.root / "sent").exists())
        finally:
            proc.kill(); proc.communicate(timeout=5)
        view = scheduler.snapshot(self.state, req["task_id"])["tasks"][0]
        self.assertEqual(view["dispatch"]["status"], "reserved")
        self.assertTrue(view["claim_held"])
        self.assertEqual(self.collect(self.start("crash", "recover"))["results"], [])
        self.assertEqual(len((self.root / "sends.log").read_text().splitlines()), 1)

    def test_kill_before_reservation_can_start_once_after_restart(self):
        self.one_node("before")
        proc = self.start("before", "before")
        try:
            self.wait_for(lambda: bool(list(self.root.glob("ready-*"))))
        finally:
            proc.kill(); proc.communicate(timeout=5)
        self.assertEqual(scheduler.snapshot(self.state)["tasks"], [])
        self.assertEqual(self.collect(self.start("before", "recover"))["results"][0]["status"], "SUBMITTED")
        self.assertEqual(len((self.root / "sends.log").read_text().splitlines()), 1)

    def test_allocate_paths_is_exclusive_and_review_path_is_frozen(self):
        first = evidence.allocate(self.request(), self.root / "delivery")
        second = evidence.allocate(self.request("SECOND"), self.root / "delivery")
        self.assertFalse(evidence.delivery_paths(first) & evidence.delivery_paths(second))
        self.assertTrue(first["completion"]["marker"])
        with self.assertRaises(FileExistsError): evidence.allocate(self.request(), self.root / "delivery")
        self.send(first)
        req, event = self.finish(first)
        self.assertEqual(Path(req["review_path"]).parent, Path(req["cwd"]))

    def test_released_claim_does_not_release_historical_evidence_names(self):
        first = self.request(); self.send(first); self.finish(first)
        original = (Path(first["cwd"]) / "RESULT.md").read_bytes()
        scheduler.release(self.state, first["task_id"], *self.actor())
        second = self.request("SECOND", cwd=Path(first["cwd"]))
        with self.assertRaisesRegex(ValueError, "evidence path"):
            self.send(second)
        second["completion"] = {"binding_artifact": "SECOND/HANDOFF.md", "artifacts": ["SECOND/RESULT.md"]}
        self.assertEqual(self.send(second)["status"], "SUBMITTED")
        self.assertEqual((Path(first["cwd"]) / "RESULT.md").read_bytes(), original)

    def test_shared_cwd_requires_distinct_graph_evidence_paths(self):
        first = self.request(); second = self.request("SECOND", cwd=Path(first["cwd"]))
        graph = {"nodes": [{"id": key, "needs": deps, "request": req, "message": self.message(req)}
                           for key, deps, req in (("a", [], first), ("b", ["a"], second))]}
        with self.assertRaisesRegex(ValueError, "DAG evidence"):
            scheduler.load_dag(self.state, "shared", graph, *self.actor())
        second["completion"] = {"binding_artifact": "SECOND/HANDOFF.md", "artifacts": ["SECOND/RESULT.md"]}
        self.assertEqual(scheduler.load_dag(self.state, "shared", graph, *self.actor())["status"], "DAG_LOADED")

    def test_summary_covers_ready_unconfirmed_working_and_review(self):
        req = self.one_node("summary")
        before = (self.state / "workflow.sqlite3").read_bytes()
        ready = status.summarize(self.state)["tasks"][0]
        self.assertEqual((ready["phase"], ready["next_role"], ready["next_action"]), ("ready", "planner", "tick"))
        self.assertEqual((self.state / "workflow.sqlite3").read_bytes(), before)
        scheduler.tick(self.state, "summary", *self.actor(), transport=self.fake)
        self.assertEqual(status.summarize(self.state)["tasks"][0]["phase"], "execution_unconfirmed")
        confirmed = self.confirm(req)
        at = datetime.now(timezone.utc) - timedelta(hours=1)
        scheduler.record_observation(self.state, confirmed, {"status": "working", "observed_at": at.isoformat()})
        row = status.summarize(self.state)["tasks"][0]
        self.assertEqual((row["phase"], row["next_role"]), ("executing", "executor"))
        self.assertIn("observation_stale", row["warnings"])
        self.assertEqual(row["worker_heartbeat"], "unavailable")
        self.finish(req)
        self.assertEqual(status.summarize(self.state)["tasks"][0]["phase"], "accepted")
        self.assertIn("accepted", status.format_text(status.summarize(self.state)))

    def test_summary_cli_can_inspect_an_unregistered_graph_task(self):
        req = self.one_node("summary")
        script = str(Path(control.__file__).resolve())
        before = (self.state / "workflow.sqlite3").read_bytes()
        proc = subprocess.run([sys.executable, "-B", script, "--state", str(self.state), "inspect", "--task", req["task_id"],
                               "--format", "text"], cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("planner/tick", proc.stdout)
        self.assertEqual((self.state / "workflow.sqlite3").read_bytes(), before)

    def test_summary_supports_receipt_adapter_without_any_herdr_fields(self):
        spec = {"role": "second", "adapter": "receipt", "harness": "native-receipt",
                "expected_identity": self.specs["second"]["expected_identity"],
                "status_path": str(self.root / "receipt-status.json"), "output_path": str(self.root / "output.log")}
        control.bind(self.state, "second", spec, spec["expected_identity"], reason="receipt fixture")
        req = self.request("RECEIPT", "second")
        req["worker"] = dict(spec, baseline_completion_seq=0)
        req["dispatch_confirmed"] = True
        control.register_task(self.state, req, *self.actor())
        scheduler.record_observation(self.state, req, {"status": "working", "observed_at": datetime.now(timezone.utc).isoformat()})
        row = status.summarize(self.state, "RECEIPT")["tasks"][0]
        self.assertEqual((row["adapter"], row["phase"], row["next_role"]), ("receipt", "executing", "second"))
        self.assertIn("native-receipt/receipt/", status.format_text(status.summarize(self.state)))

    def test_atomic_json_commit_survives_a_separate_reader_process(self):
        path = self.root / "atomic.json"
        monitor.save_json(path, {"value": 0})
        source = """import sys,time
from pathlib import Path
from monitor_task import read_json
path = Path(sys.argv[1]); ready = Path(sys.argv[2])
for index in range(150):
    value = read_json(path)
    assert isinstance(value['value'], int)
    if index == 0: ready.write_text('ready')
    time.sleep(.002)
"""
        ready = self.root / "reader-ready"
        proc = subprocess.Popen([sys.executable, "-B", "-c", source, str(path), str(ready)],
                                cwd=Path(monitor.__file__).parent,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.wait_for(ready.exists)
            for value in range(1, 21): monitor.save_json(path, {"value": value})
            _, err = proc.communicate(timeout=8)
            self.assertEqual(proc.returncode, 0, err)
        finally:
            if proc.poll() is None: proc.kill(); proc.communicate()
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"value": 20})

    def test_monitor_process_crash_restores_checkpoint_and_deduplicates_final_event(self):
        spec = copy.deepcopy(self.specs["executor"])
        observed = {"identity": spec["expected_identity"], "status": "done", "completion_seq": 11}
        spec["status_argv"] = [sys.executable, "-c", "print(" + repr(json.dumps(observed)) + ")"]
        control.bind(self.state, "executor", spec, spec["expected_identity"], reason="monitor process fixture")
        self.specs["executor"] = spec
        req = self.request()
        req["monitor"] = {"interval_seconds": 1, "settle_grace_seconds": .3}
        self.send(req); req = self.confirm(req)
        path = Path(req["cwd"]) / "request.json"
        path.write_text(json.dumps(req), encoding="utf-8")
        argv = [sys.executable, "-B", str(Path(monitor.__file__).resolve()), "--request", str(path)]
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        checkpoint = path.parent / "monitor_checkpoint.json"
        try:
            self.wait_for(checkpoint.exists)
            before = json.loads(checkpoint.read_text(encoding="utf-8"))["idle_since"]
        finally:
            proc.kill(); proc.communicate(timeout=5)
        lock = path.parent / "monitor.lock"
        self.assertEqual(int(lock.read_text()), proc.pid)
        lock.unlink()  # This exact test-owned process has been killed and reaped.
        deadline = time.monotonic() + .35
        while time.monotonic() < deadline: time.sleep(.01)
        for _ in range(2):
            resumed = subprocess.run(argv, capture_output=True, text=True, timeout=5)
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(json.loads(checkpoint.read_text(encoding="utf-8"))["idle_since"]["delivery_since"], before["delivery_since"])
        task = scheduler.snapshot(self.state, req["task_id"])["tasks"][0]
        self.assertEqual(len(task["events"]), 1)
        self.assertEqual(task["events"][0]["data"]["report"]["status"], "incomplete")
        self.assertTrue(task["claim_held"])


if __name__ == "__main__":
    unittest.main()
