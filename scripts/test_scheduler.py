"""Offline dispatch crash, concurrency, isolation, and reviewed DAG regression tests."""
import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
import uuid
from unittest.mock import patch

import monitor_task as monitor
import workflow_control as control
import workflow_scheduler as scheduler


class FakeTransport:
    def __init__(self):
        self.messages = []
        self.error = None
        self.sequences = {}

    def status(self, spec):
        return {"identity": spec["expected_identity"], "status": "idle", "completion_seq": self.sequences.get(spec["role"], 10)}

    def send(self, spec, message):
        self.messages.append(message)
        if self.error:
            raise self.error
        return "submitted"


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = (Path(__file__).resolve().parents[1] / "runtime").resolve()
        self.sandbox.mkdir(exist_ok=True)
        self.root = self.sandbox / ("test-scheduler-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.state = self.root / "state"
        cfg = {"roles": {"planner": {}, "executor": {}, "second": {}},
               "workflow": {"reviewer_role": "planner", "transitions": {"accept": "next", "reject": "stop", "repair": "repair"}},
               "scheduler": {"max_concurrency": 2, "resources": {"gpu": 1}}}
        control.initialize(self.state, cfg)
        self.specs = {role: self.spec(role) for role in cfg["roles"]}
        self.controller = control.bind(self.state, "planner", self.specs["planner"], self.specs["planner"]["expected_identity"], "controller")
        for role in ("executor", "second"):
            control.bind(self.state, role, self.specs[role], self.specs[role]["expected_identity"])
        self.fake = FakeTransport()

    def tearDown(self):
        target = self.root.resolve()
        if target.parent != self.sandbox or not target.name.startswith("test-scheduler-"):
            raise RuntimeError("unsafe cleanup")
        shutil.rmtree(target)

    def actor(self):
        return self.controller["instance_id"], self.controller["version"], "main"

    def spec(self, role):
        workspace = self.root / (role + "-workspace")
        workspace.mkdir(exist_ok=True)
        identity = {"kind": "offline", "terminal": "terminal-" + role, "launch_cwd": str(workspace)}
        status = {"identity": identity, "status": "idle", "completion_seq": 10}
        return {"role": role, "adapter": "command", "harness": "offline", "expected_identity": identity,
                "status_argv": [sys.executable, "-c", "print(" + repr(json.dumps(status)) + ")"],
                "output_argv": [sys.executable, "-c", "print('offline')"],
                "identity_argv": [sys.executable, "-c", "print(" + repr(json.dumps(status)) + ")"],
                "prompt_argv": [sys.executable, "-c", "print('received')", "{message}"]}

    def request(self, name="TASK-A", role="executor", cwd=None):
        cwd = cwd or self.root / name
        cwd.mkdir(parents=True, exist_ok=True)
        return {"task_id": name, "nonce": "nonce-" + name, "generation": 1, "cwd": str(cwd),
                "workflow_state": str(self.state), "worker": dict(self.specs[role], baseline_completion_seq=10),
                "reviewer": self.specs["planner"], "dispatch_confirmed": False,
                "authorization": {"source": "offline regression", "dispatch_worker": True, "notify_reviewer": False},
                "completion": {"binding_artifact": "HANDOFF.md", "artifacts": ["RESULT.md"], "marker_source": "artifact"}}

    def message(self, req):
        return "Execute " + req["task_id"] + " " + req["nonce"]

    def send(self, req, **kwargs):
        return scheduler.dispatch(self.state, req, self.message(req), *self.actor(), transport=self.fake, **kwargs)

    def confirm(self, req, outcome="running"):
        note = Path(req["cwd"]) / ("CONFIRM-" + outcome + ".md")
        note.write_text(req["task_id"] + " " + req["nonce"] + "\nOutcome: " + outcome + "\nObserved execution independently.\n", encoding="utf-8")
        return scheduler.reconcile(self.state, req["task_id"], outcome, note, *self.actor())["request"]

    def finish(self, req, verdict="accept"):
        req = self.confirm(req)
        for name in ("HANDOFF.md", "RESULT.md"):
            text = self.message(req)
            if name == "HANDOFF.md" and req["completion"].get("marker"):
                text += "\n" + req["completion"]["marker"]
            (Path(req["cwd"]) / name).write_text(text, encoding="utf-8")
        class Done:
            def status(inner, spec):
                return {"identity": spec["expected_identity"], "status": "done", "completion_seq": 11}
        status, _, _, receipt = monitor.inspect_once(req, Done())
        self.assertEqual(status, "review_ready")
        event = control.publish_event(self.state, req, {"status": status}, receipt)
        control.acknowledge(self.state, event, *self.actor())
        review = Path(req["cwd"]) / "REVIEW.md"
        review.write_text(self.message(req) + "\nVerdict: " + verdict + "\n", encoding="utf-8")
        control.record_review(self.state, event, *self.actor(), review, verdict)
        return req, event

    def test_real_command_send_and_confirmation(self):
        req = self.request()
        result = scheduler.dispatch(self.state, req, self.message(req), *self.actor())
        self.assertEqual(result["status"], "SUBMITTED")
        with self.assertRaises(ValueError):
            control.publish_event(self.state, req, {"status": "review_ready"})
        confirmed = self.confirm(req)
        control.check_registered(self.state, confirmed)
        self.assertTrue(confirmed["dispatch_confirmed"])
        self.assertEqual(self.confirm(req), confirmed)

    def test_uncertain_send_never_repeats_and_holds_claim(self):
        req = self.request()
        self.fake.error = TimeoutError("send may have arrived")
        self.assertEqual(self.send(req)["status"], "DISPATCH_UNCERTAIN")
        self.assertEqual(self.send(req)["status"], "ALREADY_ATTEMPTED")
        self.assertEqual(len(self.fake.messages), 1)
        with self.assertRaises(ValueError):
            control.bind(self.state, "executor", self.specs["executor"], self.specs["executor"]["expected_identity"], reason="still uncertain")
        other = self.request("TASK-B", "second", Path(req["cwd"]) / "nested")
        with self.assertRaisesRegex(ValueError, "workspace"):
            self.send(other)
        self.confirm(req, "not_sent")
        self.fake.error = None
        self.assertEqual(self.send(other)["status"], "SUBMITTED")

    def test_crash_after_send_before_commit_recovers_without_resending(self):
        req = self.request()
        self.fake.error = KeyboardInterrupt("process crash after external send")
        with self.assertRaises(KeyboardInterrupt):
            self.send(req)
        view = scheduler.snapshot(self.state, req["task_id"])
        self.assertEqual(view["tasks"][0]["dispatch"]["status"], "reserved")
        self.assertEqual(self.send(req)["status"], "ALREADY_ATTEMPTED")
        self.assertEqual(len(self.fake.messages), 1)
        self.confirm(req)

    def test_competing_controllers_and_duplicate_requests_send_once(self):
        req = self.request()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.send(req), range(2)))
        self.assertEqual(sorted(item["status"] for item in results), ["ALREADY_ATTEMPTED", "SUBMITTED"])
        self.assertEqual(len(self.fake.messages), 1)

    def test_concurrent_workspace_claim_is_atomic(self):
        first = self.request()
        second = self.request("TASK-B", "second", Path(first["cwd"]))
        def attempt(req):
            try:
                return self.send(req)["status"]
            except ValueError:
                return "CONFLICT"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, (first, second)))
        self.assertEqual(sorted(results), ["CONFLICT", "SUBMITTED"])
        self.assertEqual(len(self.fake.messages), 1)

    def test_separate_artifact_directories_do_not_hide_shared_execution_workspace(self):
        first, second = self.request(), self.request("TASK-B", "second")
        self.send(first)
        spec = copy.deepcopy(self.specs["second"])
        spec["expected_identity"]["launch_cwd"] = first["worker"]["expected_identity"]["launch_cwd"]
        control.bind(self.state, "second", spec, spec["expected_identity"], reason="shared source directory")
        second["worker"] = dict(spec, baseline_completion_seq=10)
        with self.assertRaisesRegex(ValueError, "workspace"):
            self.send(second)

    def test_resources_legacy_registration_and_termination(self):
        first, second = self.request(), self.request("TASK-B", "second")
        self.send(first, resources={"gpu": 1})
        with self.assertRaisesRegex(ValueError, "capacity"):
            self.send(second, resources={"gpu": 1})
        legacy = self.request("LEGACY", "second", Path(first["cwd"]))
        legacy["dispatch_confirmed"] = True
        with self.assertRaisesRegex(ValueError, "workspace"):
            control.register_task(self.state, legacy, *self.actor())
        with self.assertRaises(ValueError):
            scheduler.release(self.state, first["task_id"], *self.actor())
        self.confirm(first, "terminated")
        self.assertEqual(self.send(second, resources={"gpu": 1})["status"], "SUBMITTED")

    def test_stale_controller_after_reservation_cannot_send(self):
        req = self.request()
        original = control.register_task
        def handoff(*args, **kwargs):
            result = original(*args, **kwargs)
            spec = copy.deepcopy(self.specs["planner"])
            spec["expected_identity"]["terminal"] = "new-controller"
            control.bind(self.state, "planner", spec, spec["expected_identity"], "controller", reason="handoff")
            return result
        with patch.object(control, "register_task", side_effect=handoff):
            self.assertEqual(self.send(req)["status"], "DISPATCH_UNCERTAIN")
        self.assertEqual(self.fake.messages, [])

    def test_failed_event_cannot_be_accepted(self):
        req = self.request(); self.send(req); req = self.confirm(req)
        event = control.publish_event(self.state, req, {"status": "failed"})
        control.acknowledge(self.state, event, *self.actor())
        review = Path(req["cwd"]) / "REVIEW.md"
        review.write_text(self.message(req) + "\nVerdict: accept\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "review_ready"):
            control.record_review(self.state, event, *self.actor(), review, "accept")

    def test_late_completion_cannot_reopen_or_accept_a_terminated_task(self):
        req = self.request(); self.send(req); req = self.confirm(req)
        self.confirm(req, "terminated")
        event = control.publish_event(self.state, req, {"status": "review_ready"})
        control.acknowledge(self.state, event, *self.actor())
        review = Path(req["cwd"]) / "REVIEW.md"
        review.write_text(self.message(req) + "\nVerdict: accept\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "closed"):
            control.record_review(self.state, event, *self.actor(), review, "accept")
        view = scheduler.snapshot(self.state, req["task_id"])["tasks"][0]
        self.assertEqual(view["execution"], "resolved")
        self.assertFalse(view["claim_held"])

    def test_legacy_termination_cannot_be_reopened_by_reconciliation(self):
        req = self.request(); self.send(req)
        note = Path(req["cwd"]) / "TERMINATION.md"
        note.write_text(self.message(req) + "\nObserved stopped.\n", encoding="utf-8")
        control.resolve_task(self.state, req["task_id"], *self.actor(), note)
        with self.assertRaisesRegex(ValueError, "closed"):
            self.confirm(req)

    def graph(self, prefix):
        nodes = []
        for name, role, deps in (("a", "executor", []), ("b", "second", []), ("join", "executor", ["a", "b"])):
            req = self.request(prefix + name, role)
            nodes.append({"id": name, "needs": deps, "request": req, "message": self.message(req)})
        return {"nodes": nodes}

    def test_parallel_fanin_review_gate_three_complete_runs(self):
        for index in range(3):
            with self.subTest(run=index):
                run = "run-" + str(index)
                graph = self.graph(run)
                scheduler.load_dag(self.state, run, graph, *self.actor())
                initial = scheduler.tick(self.state, run, *self.actor(), transport=self.fake)
                self.assertEqual([row["status"] for row in initial["results"]], ["SUBMITTED", "SUBMITTED"])
                self.assertEqual(scheduler.tick(self.state, run, *self.actor(), transport=self.fake)["results"], [])
                self.finish(graph["nodes"][0]["request"])
                self.assertEqual(scheduler.tick(self.state, run, *self.actor(), transport=self.fake)["results"], [])
                self.finish(graph["nodes"][1]["request"])
                joined = scheduler.tick(self.state, run, *self.actor(), transport=self.fake)
                self.assertEqual([row["node"] for row in joined["results"]], ["join"])
                self.finish(graph["nodes"][2]["request"])
                scheduler.tick(self.state, run, *self.actor(), transport=self.fake)
                self.assertEqual(set(scheduler.snapshot(self.state)["dags"][run]["nodes"].values()), {"accepted"})
        self.assertEqual(len(self.fake.messages), 9)

    def test_dependency_cannot_be_bypassed_by_direct_or_legacy_dispatch(self):
        graph = self.graph("g")
        scheduler.load_dag(self.state, "g", graph, *self.actor())
        req = graph["nodes"][2]["request"]
        for kwargs in ({}, {"run": "g", "node": "join"}):
            with self.assertRaises(ValueError):
                self.send(req, **kwargs)
        req = dict(req, dispatch_confirmed=True)
        with self.assertRaises(ValueError):
            control.register_task(self.state, req, *self.actor())
        root_req = graph["nodes"][0]["request"]
        with self.assertRaisesRegex(ValueError, "message"):
            scheduler.dispatch(self.state, root_req, self.message(root_req) + " altered scope", *self.actor(),
                               run="g", node="a", transport=self.fake)
        self.assertEqual(self.fake.messages, [])

    def test_downstream_freezes_live_baseline_after_upstream_completes(self):
        graph = self.graph("baseline")
        scheduler.load_dag(self.state, "g", graph, *self.actor())
        scheduler.tick(self.state, "g", *self.actor(), transport=self.fake)
        self.finish(graph["nodes"][0]["request"])
        self.finish(graph["nodes"][1]["request"])
        self.fake.sequences["executor"] = 41
        result = scheduler.tick(self.state, "g", *self.actor(), transport=self.fake)
        self.assertEqual(result["results"][0]["status"], "SUBMITTED")
        req = self.confirm(graph["nodes"][2]["request"])
        self.assertEqual(req["worker"]["baseline_completion_seq"], 41)
        self.assertEqual(req["dag_binding"]["template_sha256"], control.digest(graph["nodes"][2]["request"]))

    def test_done_status_mapping_applies_to_both_admission_checks(self):
        req = self.request()
        req["status_map"] = {"done": "idle"}
        self.fake.status = lambda spec: {"identity": spec["expected_identity"], "status": "done", "completion_seq": 10}
        self.assertEqual(self.send(req)["status"], "SUBMITTED")
        self.assertEqual(len(self.fake.messages), 1)

    def test_herdr_done_label_mapping_with_state_change_sequence(self):
        spec = {"role": "executor", "adapter": "herdr", "harness": "pi", "executable": "offline-herdr",
                "session": "offline-session", "pane": "p1",
                "expected_identity": dict(self.specs["executor"]["expected_identity"], kind="pi", pane="p1")}
        control.bind(self.state, "executor", spec, spec["expected_identity"], reason="verified offline Herdr fixture")
        req = self.request()
        req["worker"] = dict(spec, baseline_completion_seq=10)
        req["status_map"] = {"done": "idle"}
        identity = spec["expected_identity"]
        agent = {"agent": "pi", "terminal_id": identity["terminal"], "cwd": identity["launch_cwd"], "pane_id": "p1",
                 "agent_status": "done", "state_change_seq": 10}
        sends = []
        def run(argv, cwd=None):
            if argv[4] == "get": return json.dumps({"result": {"agent": agent}})
            self.assertEqual(argv[4], "prompt")
            sends.append(argv[-1])
            return "submitted"
        with patch.object(monitor.Transport, "run", side_effect=run):
            result = scheduler.dispatch(self.state, req, self.message(req), *self.actor())
        self.assertEqual(result["status"], "SUBMITTED")
        self.assertEqual(sends, [self.message(req)])

    def herdr_worker(self, kind):
        spec = {"role": "executor", "adapter": "herdr", "harness": kind, "executable": "offline-herdr",
                "session": "offline-session", "pane": "p1",
                "expected_identity": dict(self.specs["executor"]["expected_identity"], kind=kind, pane="p1")}
        control.bind(self.state, "executor", spec, spec["expected_identity"], reason="verified offline Herdr fixture")
        identity = spec["expected_identity"]
        agent = {"agent": kind, "terminal_id": identity["terminal"], "cwd": identity["launch_cwd"], "pane_id": "p1",
                 "agent_status": "idle", "state_change_seq": 10}
        sends = []
        def run(argv, cwd=None):
            if argv[4] == "get": return json.dumps({"result": {"agent": agent}})
            sends.append(argv[-1])
            raise TimeoutError("prompt may have arrived")
        req = self.request()
        req["worker"] = dict(spec, baseline_completion_seq=10)
        return req, sends, run

    def test_herdr_native_uncertain_prompt_holds_claim_without_retry(self):
        req, sends, run = self.herdr_worker("codex")
        with patch.object(monitor.Transport, "run", side_effect=run):
            self.assertEqual(scheduler.dispatch(self.state, req, self.message(req), *self.actor())["status"],
                             "DISPATCH_UNCERTAIN")
            self.assertEqual(scheduler.dispatch(self.state, req, self.message(req), *self.actor())["status"],
                             "ALREADY_ATTEMPTED")
        self.assertEqual(sends, [self.message(req)])
        other = self.request("TASK-B", "second", Path(req["cwd"]) / "nested")
        with self.assertRaisesRegex(ValueError, "workspace"):
            self.send(other)

    def test_herdr_custom_label_is_rejected_before_any_claim(self):
        req, sends, run = self.herdr_worker("custom:dsh-tui")
        with patch.object(monitor.Transport, "run", side_effect=run):
            with self.assertRaisesRegex(ValueError, "not a native Herdr prompt agent"):
                scheduler.dispatch(self.state, req, self.message(req), *self.actor())
        self.assertEqual(sends, [])
        with control.database(self.state) as con:
            self.assertIsNone(con.execute("SELECT 1 FROM dispatches WHERE task=?", (req["task_id"],)).fetchone())

    def test_invalid_status_map_fails_before_contacting_the_adapter(self):
        for mapping in (None, [], {"done": None}, {1: "idle"}):
            with self.subTest(mapping=mapping):
                req = self.request()
                req["status_map"] = mapping
                with patch.object(self.fake, "status", side_effect=AssertionError("no adapter query")):
                    with self.assertRaisesRegex(ValueError, "status_map"):
                        self.send(req)

    def test_done_status_requires_an_explicit_mapping(self):
        req = self.request()
        self.fake.status = lambda spec: {"identity": spec["expected_identity"], "status": "done", "completion_seq": 10}
        with self.assertRaises(ValueError): self.send(req)
        self.assertEqual(self.fake.messages, [])
        self.assertEqual(scheduler.snapshot(self.state)["tasks"], [])

    def test_busy_or_failed_status_cannot_be_mapped_to_idle(self):
        for raw in ("working", "blocked", "exited", "failed"):
            with self.subTest(status=raw):
                req = self.request("BUSY-" + raw)
                req["status_map"] = {raw: "idle"}
                self.fake.status = lambda spec: {"identity": spec["expected_identity"], "status": raw, "completion_seq": 10}
                with self.assertRaises(ValueError): self.send(req)
        self.assertEqual(self.fake.messages, [])

    def test_mapping_does_not_skip_the_final_busy_check(self):
        req = self.request()
        req["status_map"] = {"done": "idle", "working": "idle"}
        identity = req["worker"]["expected_identity"]
        statuses = [{"identity": identity, "status": name, "completion_seq": 10} for name in ("done", "working")]
        with patch.object(self.fake, "status", side_effect=statuses):
            self.assertEqual(self.send(req)["status"], "DISPATCH_UNCERTAIN")
        self.assertEqual(self.fake.messages, [])
        self.assertTrue(scheduler.snapshot(self.state)["tasks"][0]["claim_held"])

    def test_done_mapping_preserves_final_identity_and_sequence_checks(self):
        for changed in ("identity", "sequence"):
            with self.subTest(changed=changed):
                req = self.request("CHANGED-" + changed)
                req["status_map"] = {"done": "idle"}
                observed = {"identity": req["worker"]["expected_identity"], "status": "done", "completion_seq": 10}
                final = copy.deepcopy(observed)
                if changed == "identity": final["identity"]["terminal"] = "different-terminal"
                else: final["completion_seq"] = 11
                with patch.object(self.fake, "status", side_effect=[observed, final]):
                    self.assertEqual(self.send(req)["status"], "DISPATCH_UNCERTAIN")
                self.confirm(req, "not_sent")
        self.assertEqual(self.fake.messages, [])

    def test_done_mapping_does_not_release_an_unreviewed_workspace(self):
        first, second = self.request(), self.request("TASK-B")
        first["status_map"] = second["status_map"] = {"done": "idle"}
        self.fake.status = lambda spec: {"identity": spec["expected_identity"], "status": "done", "completion_seq": 10}
        self.send(first)
        with self.assertRaisesRegex(ValueError, "active or uncertain"):
            self.send(second)
        first = self.confirm(first)
        control.publish_event(self.state, first, {"status": "review_ready"})
        with self.assertRaisesRegex(ValueError, "workspace"):
            self.send(second)
        self.assertEqual(len(self.fake.messages), 1)

    def test_public_dag_view_accepts_a_state_path_and_is_read_only(self):
        graph = self.graph("view")
        scheduler.load_dag(self.state, "view", graph, *self.actor())
        path = self.state / "workflow.sqlite3"
        before = path.read_bytes()
        with patch.object(control, "database", side_effect=AssertionError("read view must not take a write lock")), \
                patch.object(scheduler, "Transport", side_effect=AssertionError("read view must not query clients")):
            saved_graph, states = scheduler.dag_view(self.state, "view")
            self.assertEqual(saved_graph, graph)
            self.assertEqual(states, {"a": "ready", "b": "ready", "join": "waiting"})
            self.assertEqual(scheduler.snapshot(self.state)["dags"]["view"]["nodes"], states)
            with self.assertRaisesRegex(ValueError, "unknown DAG"):
                scheduler.dag_view(self.state, "missing")
        self.assertEqual(path.read_bytes(), before)

    def test_example_markers_are_required_in_artifacts_before_review_ready(self):
        example = Path(scheduler.__file__).resolve().parents[1] / "examples" / "dag.json"
        for node in json.loads(example.read_text(encoding="utf-8"))["nodes"]:
            with self.subTest(node=node["id"]):
                req = self.request("EXAMPLE-" + node["id"])
                req.update(task_id=node["request"]["task_id"], nonce=node["request"]["nonce"],
                           completion=copy.deepcopy(node["request"]["completion"]), monitor={"settle_grace_seconds": 0})
                text = req["task_id"] + " " + req["nonce"]
                for name in ("HANDOFF.md", "RESULT.md", "EVIDENCE.md"):
                    (Path(req["cwd"]) / name).write_text(text, encoding="utf-8")
                class Done:
                    def status(inner, spec):
                        return {"identity": spec["expected_identity"], "status": "done", "completion_seq": 11}
                self.assertEqual(monitor.inspect_once(req, Done())[0], "incomplete")
                (Path(req["cwd"]) / "HANDOFF.md").write_text(text + "\n" + req["completion"]["marker"], encoding="utf-8")
                self.assertEqual(monitor.inspect_once(req, Done())[0], "review_ready")

    def test_real_subprocess_dag_cli_three_runs(self):
        script = Path(scheduler.__file__).with_name("demo_scheduler.py")
        shapes = []
        for index in range(3):
            directory = self.root / ("demo-" + str(index))
            proc = subprocess.run([sys.executable, "-B", str(script), "--directory", str(directory)],
                                  cwd=self.root, capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads((directory / "demo-result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "OFFLINE_DEMO_PASS")
            self.assertEqual(result["snapshot"]["claims"], [])
            shapes.append([[item["node"] for item in report["results"]] for report in result["passes"]])
        self.assertEqual(shapes, [[["A", "B"], ["JOIN"], []]] * 3)

    def test_real_subprocess_dag_reuses_a_sticky_done_worker(self):
        script = Path(scheduler.__file__).with_name("demo_scheduler.py")
        directory = self.root / "sticky-done-demo"
        proc = subprocess.run([sys.executable, "-B", str(script), "--directory", str(directory), "--completion-status", "done"],
                              cwd=self.root, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads((directory / "demo-result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "OFFLINE_DEMO_PASS")
        tasks = {item["task_id"]: item for item in result["snapshot"]["tasks"]}
        self.assertEqual(tasks["A"]["worker"]["instance_id"], tasks["JOIN"]["worker"]["instance_id"])
        status = json.loads((directory / "executor-workspace" / "worker_status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["status"], "done")
        self.assertEqual(status["completion_seq"], 2)
        confirmed = json.loads((directory / "delivery" / "JOIN" / "request.json").read_text(encoding="utf-8"))
        self.assertEqual(confirmed["worker"]["baseline_completion_seq"], 1)
        self.assertEqual(result["snapshot"]["dags"]["demo"]["nodes"], {"A": "accepted", "B": "accepted", "JOIN": "accepted"})

    def test_changed_evidence_rejection_and_duplicate_events_block_fanin(self):
        graph = self.graph("tamper")
        scheduler.load_dag(self.state, "g", graph, *self.actor())
        scheduler.tick(self.state, "g", *self.actor(), transport=self.fake)
        req, event = self.finish(graph["nodes"][0]["request"])
        with control.database(self.state) as con:
            receipt = json.loads(con.execute("SELECT data FROM events WHERE id=?", (event,)).fetchone()[0])["receipt"]
        self.assertEqual(control.publish_event(self.state, req, {"status": "review_ready"}, receipt), event)
        (Path(req["cwd"]) / "RESULT.md").write_text("changed", encoding="utf-8")
        self.finish(graph["nodes"][1]["request"], "reject")
        self.assertEqual(scheduler.tick(self.state, "g", *self.actor(), transport=self.fake)["results"], [])
        view = scheduler.snapshot(self.state)["dags"]["g"]["nodes"]
        self.assertEqual(view["a"], "evidence_changed")
        self.assertEqual(view["join"], "blocked_dependency")

    def test_dag_cycle_unknown_dependencies_and_run_overwrite(self):
        graph = self.graph("cycle")
        graph["nodes"][0]["needs"] = ["join"]
        with self.assertRaisesRegex(ValueError, "cycle"):
            scheduler.validate_dag(graph)
        graph["nodes"][0]["needs"] = ["missing"]
        with self.assertRaises(ValueError): scheduler.validate_dag(graph)
        graph["nodes"][0]["needs"] = []
        scheduler.load_dag(self.state, "g", graph, *self.actor())
        with self.assertRaises(ValueError): scheduler.load_dag(self.state, "g", graph, *self.actor())
        with self.assertRaises(ValueError): scheduler.load_dag(self.state, "another", graph, *self.actor())

    def test_readonly_status_cli_and_legacy_state_migration(self):
        req = self.request(); self.send(req)
        path = self.state / "workflow.sqlite3"
        before = path.read_bytes()
        proc = subprocess.run([sys.executable, "-B", str(Path(control.__file__).resolve()), "--state", str(self.state),
                               "inspect", "--task", req["task_id"]], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(json.loads(proc.stdout)["tasks"][0]["claim_held"])
        self.assertEqual(path.read_bytes(), before)
        with control.database(self.state) as con:
            for table in ("task_journal", "dag_runs", "dispatches"):
                con.execute("DROP TABLE " + table)
            con.execute("DELETE FROM meta WHERE key='scheduler_schema'")
        self.assertIsNone(scheduler.snapshot(self.state)["scheduler_schema"])
        scheduler.migrate(self.state, *self.actor())
        self.assertEqual(scheduler.snapshot(self.state)["scheduler_schema"], 1)

    def test_capability_claim_requires_versioned_hash_evidence(self):
        profile = {key: "offline-test" for key in ("harness", "harness_version", "transport", "transport_version", "os", "skill_commit")}
        profile["operations"] = {key: {"status": "unverified"} for key in scheduler.OPERATIONS}
        scheduler.validate_capabilities(profile, self.root)
        profile["operations"]["send"] = {"status": "verified"}
        with self.assertRaises(ValueError): scheduler.validate_capabilities(profile, self.root)
        evidence = self.root / "evidence.txt"; evidence.write_text("offline evidence", encoding="utf-8")
        claim = {"status": "verified", "commands": [[sys.executable, "--version"]],
                 "evidence": [{"path": evidence.name, "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest()}]}
        profile["operations"]["send"] = claim
        scheduler.validate_capabilities(profile, self.root)
        evidence.write_text("changed")
        with self.assertRaises(ValueError): scheduler.validate_capabilities(profile, self.root)

    def test_monitor_crash_restores_delivery_grace_instead_of_resetting_it(self):
        req = self.request(); self.send(req); req = self.confirm(req)
        reqfile = Path(req["cwd"]) / "request.json"
        reqfile.write_text(json.dumps(req), encoding="utf-8")
        class MissingDelivery:
            def __init__(inner, request): pass
            def status(inner, spec):
                return {"identity": spec["expected_identity"], "status": "done", "completion_seq": 11}
        argv = ["monitor", "--request", str(reqfile)]
        with patch.object(sys, "argv", argv), patch.object(monitor, "Transport", MissingDelivery), \
                patch.object(monitor.time, "time", return_value=1000), \
                patch.object(monitor.time, "sleep", side_effect=KeyboardInterrupt("crash")):
            with self.assertRaises(KeyboardInterrupt): monitor.main()
        checkpoint = json.loads((reqfile.parent / "monitor_checkpoint.json").read_text())
        self.assertEqual(checkpoint["idle_since"]["delivery_since"], 1000)
        with patch.object(sys, "argv", argv), patch.object(monitor, "Transport", MissingDelivery), \
                patch.object(monitor.time, "time", return_value=1121):
            monitor.main()
        view = scheduler.snapshot(self.state, req["task_id"])["tasks"][0]
        self.assertEqual(view["last_observation"]["status"], "incomplete")
        self.assertTrue(view["claim_held"])

    def test_monitor_crash_after_summary_replays_event_without_query_or_send(self):
        req = self.request(); self.send(req); req = self.confirm(req)
        for name in ("HANDOFF.md", "RESULT.md"):
            (Path(req["cwd"]) / name).write_text(self.message(req), encoding="utf-8")
        reqfile = Path(req["cwd"]) / "request.json"
        reqfile.write_text(json.dumps(req), encoding="utf-8")
        class Done:
            def __init__(inner, request): pass
            def status(inner, spec):
                return {"identity": spec["expected_identity"], "status": "done", "completion_seq": 11}
        argv = ["monitor", "--request", str(reqfile)]
        with patch.object(sys, "argv", argv), patch.object(monitor, "Transport", Done), \
                patch.object(control, "publish_event", side_effect=KeyboardInterrupt("crash before ledger write")):
            with self.assertRaises(KeyboardInterrupt): monitor.main()
        self.assertEqual(scheduler.snapshot(self.state)["tasks"][0]["events"], [])
        with patch.object(sys, "argv", argv), patch.object(monitor, "Transport", side_effect=AssertionError("must not query")):
            monitor.main()
            monitor.main()
        events = scheduler.snapshot(self.state)["tasks"][0]["events"]
        self.assertEqual(len(events), 1)
        self.assertIsNotNone(events[0]["data"]["receipt"])

    def test_read_only_errors_back_off_and_preserve_budget_after_crash(self):
        req = self.request(); self.send(req); req = self.confirm(req)
        reqfile = Path(req["cwd"]) / "request.json"
        reqfile.write_text(json.dumps(req), encoding="utf-8")
        class Lost:
            def __init__(inner, request): pass
            def status(inner, spec): raise TimeoutError("lost connection")
        argv = ["monitor", "--request", str(reqfile)]
        with patch.object(sys, "argv", argv), patch.object(monitor, "Transport", Lost), \
                patch.object(monitor.time, "sleep", side_effect=KeyboardInterrupt("crash")):
            with self.assertRaises(KeyboardInterrupt): monitor.main()
        with patch.object(sys, "argv", argv), patch.object(monitor, "Transport", Lost), \
                patch.object(monitor.time, "sleep") as sleep:
            monitor.main()
        self.assertEqual(sleep.call_args_list[0].args[0], 10)
        view = scheduler.snapshot(self.state)["tasks"][0]
        self.assertEqual(view["last_observation"]["consecutive_errors"], 3)
        self.assertTrue(view["claim_held"])


if __name__ == "__main__":
    unittest.main()
