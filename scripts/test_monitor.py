"""Offline behavioral tests; never contact a real agent or notification endpoint."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
import uuid
from unittest.mock import patch

import monitor_task as module
import check_encoding


class FakeTransport:
    def __init__(self, request, status="done", seq=11):
        self.request, self.status_value, self.seq = request, status, seq
        self.reads, self.sends = 0, 0
        self.worker_identity = copy.deepcopy(request["worker"]["expected_identity"])
        self.target_identity = copy.deepcopy(request["reviewer"]["expected_identity"])
        self.raise_on_send = False

    def status(self, spec):
        return {"identity": self.worker_identity, "status": self.status_value, "completion_seq": self.seq,
                "task_id": self.request["task_id"], "nonce": self.request["nonce"]}

    def output(self, spec):
        self.reads += 1
        return self.request["completion"]["marker"]

    def reviewer_identity(self, spec):
        return self.target_identity

    def send(self, spec, message):
        self.sends += 1
        if self.raise_on_send:
            raise TimeoutError("uncertain delivery")
        assert "not a new Owner instruction" in message
        return "queued"


class Tests(unittest.TestCase):
    def setUp(self):
        self.sandbox = (Path(__file__).resolve().parents[1] / "runtime").resolve()
        self.sandbox.mkdir(exist_ok=True)
        self.root = self.sandbox / ("test-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.request = {
            "task_id": "TASK-TEST", "nonce": "unique-nonce", "dispatch_confirmed": True,
            "cwd": str(self.root), "authorization": {"source": "offline test only", "notify_reviewer": True},
            "worker": {"adapter": "command", "harness": "pi", "baseline_completion_seq": 10,
                       "expected_identity": {"kind": "pi", "terminal": "worker-1", "launch_cwd": str(self.root)}},
            "reviewer": {"adapter": "command", "harness": "claude",
                         "expected_identity": {"kind": "claude", "terminal": "reviewer-2", "launch_cwd": str(self.root)}},
            "completion": {"artifacts": ["RESULT.md", "EVIDENCE.md", "HANDOFF.md"],
                           "binding_artifact": "HANDOFF.md", "marker": "DONE TASK-TEST unique-nonce"},
            "monitor": {"settle_grace_seconds": 0}}
        self.request["worker"].update(status_argv=[sys.executable, "-c", "print('{}')"],
                                      output_argv=[sys.executable, "-c", "print('DONE')"])
        self.request["reviewer"].update(identity_argv=[sys.executable, "-c", "print('{}')"],
                                        prompt_argv=[sys.executable, "-c", "print('queued')", "{message}"],
                                        context_policy="dedicated_window", context_declaration="exclusive offline test window")
        for name in self.request["completion"]["artifacts"]:
            (self.root / name).write_text("TASK-TEST unique-nonce\n", encoding="utf-8")

    def tearDown(self):
        target = self.root.resolve()
        if target.parent != self.sandbox or not target.name.startswith("test-"):
            raise RuntimeError("refuse cleanup outside this test's runtime directory")
        shutil.rmtree(target)

    def test_valid_delivery_for_every_harness(self):
        for harness in ["codex", "pi", "claude", "dsh-tui", "grok"]:
            with self.subTest(harness=harness):
                req = copy.deepcopy(self.request)
                req["worker"]["harness"] = harness
                req["worker"]["expected_identity"]["kind"] = harness
                client = FakeTransport(req)
                status, _, _, receipt = module.inspect_once(req, client, now=0)
                self.assertEqual(status, "review_ready")
                self.assertEqual(len(receipt["artifacts"]), 3)

    def test_working_never_reads_conversation(self):
        client = FakeTransport(self.request, "working")
        self.assertEqual(module.inspect_once(self.request, client)[0], "working")
        self.assertEqual(client.reads, 0)

    def test_old_completion_not_accepted(self):
        self.assertEqual(module.inspect_once(self.request, FakeTransport(self.request, seq=10))[0], "incomplete")

    def test_identity_change_rejected(self):
        client = FakeTransport(self.request)
        client.worker_identity["terminal"] = "replacement"
        self.assertEqual(module.inspect_once(self.request, client)[0], "identity_changed")

    def test_missing_and_empty_artifact_rejected(self):
        f = self.root / "RESULT.md"
        f.unlink()
        self.assertEqual(module.inspect_once(self.request, FakeTransport(self.request))[0], "incomplete")
        f.touch()
        self.assertEqual(module.inspect_once(self.request, FakeTransport(self.request))[0], "incomplete")

    def test_wrong_nonce_and_missing_marker_rejected(self):
        f = self.root / "HANDOFF.md"
        f.write_text("TASK-TEST wrong-nonce")
        self.assertEqual(module.inspect_once(self.request, FakeTransport(self.request))[0], "incomplete")
        f.write_text("TASK-TEST unique-nonce")
        client = FakeTransport(self.request)
        client.output = lambda spec: ""
        self.assertEqual(module.inspect_once(self.request, client)[0], "incomplete")

    def test_blocked_exited_failed_are_not_success(self):
        for state in ["blocked", "exited", "failed"]:
            self.assertEqual(module.inspect_once(self.request, FakeTransport(self.request, state))[0], state)

    def test_artifact_escape_rejected(self):
        req = copy.deepcopy(self.request)
        req["completion"]["artifacts"] = ["../elsewhere.txt"]
        self.assertEqual(module.inspect_once(req, FakeTransport(req))[0], "incomplete")

    def test_receipt_task_binding(self):
        req = copy.deepcopy(self.request)
        req["worker"]["adapter"] = "receipt"
        client = FakeTransport(req)
        original = client.status
        client.status = lambda spec: dict(original(spec), nonce="different")
        self.assertEqual(module.inspect_once(req, client)[0], "identity_changed")

    def test_notice_sent_once(self):
        client = FakeTransport(self.request)
        report = {"status": "review_ready"}
        self.assertEqual(module.notify_once(self.request, report, self.root, client)["status"], "submitted")
        self.assertEqual(module.notify_once(self.request, report, self.root, client)["status"], "already_attempted")
        self.assertEqual(client.sends, 1)

    def test_stall_notice_does_not_consume_the_delivery_notice(self):
        client = FakeTransport(self.request)
        stall = {"status": "incomplete", "stall_episode": 1}
        self.assertEqual(module.notify_once(self.request, stall, self.root, client)["status"], "submitted")
        self.assertEqual(module.notify_once(self.request, stall, self.root, client)["status"], "already_attempted")
        self.assertEqual(module.notify_once(self.request, dict(stall, stall_episode=2), self.root, client)["status"], "submitted")
        self.assertEqual(module.notify_once(self.request, {"status": "review_ready"}, self.root, client)["status"], "submitted")
        self.assertEqual(client.sends, 3)
        self.assertIn("continue --task " + self.request["task_id"], module.event_guidance("incomplete", self.request["task_id"]))
        self.assertIn("Do not record a review", module.event_guidance("incomplete"))
        self.assertIn("Never answer approval dialogs", module.event_guidance("blocked"))

    def test_uncertain_notice_not_retried(self):
        client = FakeTransport(self.request)
        client.raise_on_send = True
        self.assertEqual(module.notify_once(self.request, {"status": "review_ready"}, self.root, client)["status"], "delivery_uncertain")
        self.assertEqual(module.notify_once(self.request, {"status": "review_ready"}, self.root, client)["status"], "already_attempted")
        self.assertEqual(client.sends, 1)

    def test_unverified_target_not_prompted(self):
        client = FakeTransport(self.request)
        client.target_identity["terminal"] = "wrong"
        self.assertEqual(module.notify_once(self.request, {"status": "review_ready"}, self.root, client)["status"], "reviewer_identity_unverified")
        self.assertEqual(client.sends, 0)

    def test_unauthorized_notice_not_prompted(self):
        self.request["authorization"]["notify_reviewer"] = False
        client = FakeTransport(self.request)
        self.assertEqual(module.notify_once(self.request, {}, self.root, client)["status"], "not_authorized")
        self.assertEqual(client.sends, 0)

    def test_configurable_status_and_interval(self):
        self.request["status_map"] = {"finished": "done"}
        self.request["monitor"]["interval_seconds"] = 1
        module.validate_request(self.request)
        self.assertEqual(module.inspect_once(self.request, FakeTransport(self.request, "finished"))[0], "review_ready")

    def test_strict_configuration(self):
        for field, value in [("overall_timeout_seconds", "bad"), ("overall_timeout_seconds", 0),
                             ("overall_timeout_seconds", float("nan")), ("settle_grace_seconds", -1),
                             ("interval_seconds", True)]:
            req = copy.deepcopy(self.request); req["monitor"][field] = value
            with self.assertRaises(ValueError): module.validate_request(req)
        for value in [None, "17 ", 17.0, 17.9, True]:
            with self.assertRaises(ValueError): module.sequence(value)
        self.request["authorization"]["notify_reviewer"] = "false"
        with self.assertRaises(ValueError): module.validate_request(self.request)

    def test_new_completion_gets_its_own_delivery_grace(self):
        self.request["monitor"]["settle_grace_seconds"] = 120
        first = module.inspect_once(self.request, FakeTransport(self.request, seq=10), now=0)
        (self.root / "RESULT.md").unlink()
        second = module.inspect_once(self.request, FakeTransport(self.request, seq=11), first[2], now=300)
        self.assertEqual(second[0], "settling")
        (self.root / "RESULT.md").write_text("delivered")
        self.assertEqual(module.inspect_once(self.request, FakeTransport(self.request, seq=11), second[2], now=350)[0], "review_ready")

    def test_kind_only_identity_and_escaping_path_fail_early(self):
        req = copy.deepcopy(self.request); req["worker"]["expected_identity"] = {"kind": "pi"}
        with self.assertRaises(ValueError): module.validate_request(req)
        self.request["completion"]["artifacts"] = ["../outside"]
        with self.assertRaises(ValueError): module.validate_request(self.request)

    def test_unregistered_context_queues_without_prompt(self):
        self.request["reviewer"]["context_policy"] = "inbox_only"
        client = FakeTransport(self.request)
        self.assertEqual(module.notify_once(self.request, {"status": "review_ready"}, self.root, client)["status"], "inbox_only")
        self.assertEqual(client.sends, 0)
        self.assertTrue((self.root / "pending_event.json").exists())

    def test_real_command_adapter_and_notification(self):
        state = FakeTransport(self.request).status(None)
        req = copy.deepcopy(self.request)
        req["worker"].update(status_argv=[sys.executable, "-c", "print(" + repr(json.dumps(state)) + ")"],
                             output_argv=[sys.executable, "-c", "print('DONE TASK-TEST unique-nonce')"])
        target = {"identity": req["reviewer"]["expected_identity"]}
        out = self.root / "notification.txt"
        req["reviewer"].update(
            identity_argv=[sys.executable, "-c", "print(" + repr(json.dumps(target)) + ")"],
            prompt_argv=[sys.executable, "-c", "from pathlib import Path;import sys;Path(sys.argv[1]).write_text(sys.argv[2],encoding='utf-8')", str(out), "{message}"])
        transport = module.Transport(req)
        self.assertEqual(module.inspect_once(req, transport)[0], "review_ready")
        self.assertEqual(module.notify_once(req, {"status": "review_ready"}, self.root, transport)["status"], "submitted")
        self.assertIn("not a new Owner instruction", out.read_text(encoding="utf-8"))

    def test_native_herdr_shape_without_fixed_harness(self):
        req = copy.deepcopy(self.request)
        spec = {"adapter": "herdr", "executable": "herdr", "session": "verified", "pane": "p1"}
        transport = module.Transport(req)
        transport.run = lambda argv, cwd=None: json.dumps({"result": {"agent": {
            "agent": "arbitrary-harness", "terminal_id": "t1", "cwd": "/work",
            "pane_id": "p1", "agent_status": "working", "completion_seq": 12}}})
        actual = transport.status(spec)
        self.assertEqual(actual["identity"]["kind"], "arbitrary-harness")
        self.assertEqual(actual["status"], "working")

    def herdr_fixture(self, **fields):
        spec = {"adapter": "herdr", "executable": "herdr", "session": "offline", "pane": "p1",
                "expected_identity": {"kind": "pi", "terminal": "t1", "launch_cwd": str(self.root)}}
        agent = {"agent": "pi", "terminal_id": "t1", "cwd": str(self.root), "pane_id": "p1",
                 "agent_status": "idle", "state_change_seq": 11}
        agent.update(fields)
        transport = module.Transport(self.request)
        transport.calls = []
        transport.run = lambda argv, cwd=None: transport.calls.append(argv) or json.dumps({"result": {"agent": agent}})
        return spec, agent, transport

    def prompts(self, transport):
        return [argv for argv in transport.calls if argv[4] == "prompt"]

    def test_herdr_state_sequence_and_missing_counter(self):
        spec, agent, transport = self.herdr_fixture()
        state = transport.status(spec)
        self.assertEqual((state["completion_seq"], state["sequence_source"]), (11, "state_change_seq"))
        self.request["worker"] = dict(spec, baseline_completion_seq=10)
        self.request["completion"].pop("marker")
        self.assertEqual(module.inspect_once(self.request, transport)[0], "review_ready")
        agent["completion_seq"] = 12
        self.assertEqual(transport.status(spec)["sequence_source"], "completion_seq")
        agent.pop("completion_seq"); agent.pop("state_change_seq")
        with self.assertRaises(module.AdapterResponseError): transport.status(spec)

    def test_state_sequence_churn_cannot_extend_delivery_grace(self):
        spec, agent, transport = self.herdr_fixture()
        self.request["worker"] = dict(spec, baseline_completion_seq=10)
        self.request["monitor"]["settle_grace_seconds"] = 120
        (self.root / "RESULT.md").unlink()
        first = module.inspect_once(self.request, transport, now=0)
        self.assertEqual(first[0], "settling")
        agent.update(agent_status="working", state_change_seq=12)
        working = module.inspect_once(self.request, transport, first[2], now=60)
        self.assertEqual(working[0], "working")
        agent.update(agent_status="idle", state_change_seq=13)
        last = module.inspect_once(self.request, transport, working[2], now=121)
        self.assertEqual(last[0], "incomplete")

    def test_completion_counter_still_gets_new_delivery_grace(self):
        self.request["monitor"]["settle_grace_seconds"] = 120
        (self.root / "RESULT.md").unlink()
        first = module.inspect_once(self.request, FakeTransport(self.request, seq=11), now=0)
        second = module.inspect_once(self.request, FakeTransport(self.request, seq=12), first[2], now=121)
        self.assertEqual(second[0], "settling")

    def test_native_herdr_agent_prompts_without_agent_session(self):
        spec, agent, transport = self.herdr_fixture(agent="codex")
        spec.update(context_policy="dedicated_window")
        spec["expected_identity"]["kind"] = "codex"
        state = transport.status(spec)
        self.assertTrue(state["prompt_available"])
        self.assertNotIn("prompt_unavailable_reason", state)
        self.assertEqual(transport.reviewer_identity(spec)["kind"], "codex")
        transport.send(spec, "offline")
        self.assertEqual(self.prompts(transport), [["herdr", "--session", "offline", "agent", "prompt", "p1", "offline"]])

    def test_custom_herdr_label_is_not_prompt_capable_even_with_agent_session(self):
        spec, agent, transport = self.herdr_fixture(agent="custom:dsh-tui",
                                                    agent_session={"kind": "path", "value": "offline-session"})
        spec["expected_identity"]["kind"] = "custom:dsh-tui"
        # Status reporting still works and inbox_only never needs a prompt.
        self.assertEqual(transport.reviewer_identity(spec)["kind"], "custom:dsh-tui")
        self.assertFalse(transport.status(spec)["prompt_available"])
        spec["context_policy"] = "dedicated_window"
        with self.assertRaisesRegex(ValueError, "not a native Herdr prompt agent"):
            transport.reviewer_identity(spec)
        with self.assertRaisesRegex(ValueError, "not a native Herdr prompt agent"):
            transport.send(spec, "offline")
        self.assertEqual(self.prompts(transport), [])

    def test_herdr_blocked_pending_or_changed_target_is_not_prompted(self):
        cases = ({"agent_status": "blocked"}, {"launch_pending": True}, {"terminal_id": "t2"}, {"agent": "codex"})
        for fields in cases:
            spec, agent, transport = self.herdr_fixture(**fields)
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                transport.send(spec, "offline")
            self.assertEqual(self.prompts(transport), [])
        spec, agent, transport = self.herdr_fixture(launch_pending=False, agent_status="done")
        transport.send(spec, "offline")
        self.assertEqual(len(self.prompts(transport)), 1)

    def test_herdr_native_label_mirrors_herdr_normalization(self):
        for label in ("pi", " Codex.EXE ", "claude-code", "C:\\tools\\cursor-agent.cmd", "/usr/bin/opencode",
                      "kiro-cli", "muse-bin-0.1.0-R708.1"):
            self.assertTrue(module.herdr_native_agent(label), label)
        for label in ("custom:dsh-tui", "dsh-tui", "any-harness", "muse-bin", "muse-binary", "", None, 3):
            self.assertFalse(module.herdr_native_agent(label), label)

    def test_herdr_preflight_rejects_undeliverable_reviewer_without_sending(self):
        spec, agent, transport = self.herdr_fixture()
        self.request["worker"] = dict(spec, baseline_completion_seq=11)
        self.request["reviewer"] = dict(spec, context_policy="dedicated_window", context_declaration="offline only")
        with patch.object(module, "Transport", return_value=transport), patch.object(transport, "send") as send:
            self.assertEqual(module.preflight(self.request)["status"], "PREFLIGHT_OK")
            agent["agent"] = "custom:dsh-tui"
            for role in ("worker", "reviewer"): self.request[role]["expected_identity"]["kind"] = "custom:dsh-tui"
            with self.assertRaisesRegex(ValueError, "not a native Herdr prompt agent"): module.preflight(self.request)
            self.request["reviewer"]["context_policy"] = "inbox_only"
            self.assertEqual(module.preflight(self.request)["status"], "PREFLIGHT_OK")
            send.assert_not_called()

    def test_artifact_marker_ignores_echo_and_both_requires_output(self):
        self.request["completion"]["marker_source"] = "artifact"
        client = FakeTransport(self.request)
        self.assertEqual(module.inspect_once(self.request, client)[0], "incomplete")
        (self.root / "HANDOFF.md").write_text(self.request["completion"]["marker"], encoding="utf-8")
        self.assertEqual(module.inspect_once(self.request, client)[0], "review_ready")
        self.assertEqual(client.reads, 0)
        self.request["completion"]["marker_source"] = "both"
        client.output = lambda spec: ""
        self.assertEqual(module.inspect_once(self.request, client)[0], "incomplete")
        self.request["completion"]["marker_source"] = "typo"
        with self.assertRaisesRegex(ValueError, "marker_source"): module.validate_request(self.request)

    def test_real_local_job_and_receipt_monitor(self):
        scripts = Path(__file__).parent
        identity = {"kind": "test-cli", "terminal": "unique-job", "launch_cwd": str(self.root)}
        jobdir = self.root / "job"
        job = {"task_id": "TASK-TEST", "nonce": "unique-nonce", "cwd": str(self.root),
               "authorization": "offline child process test", "identity": identity,
               "output_dir": str(jobdir), "argv": [sys.executable, "-c", "print('DONE TASK-TEST unique-nonce')"]}
        jobfile = self.root / "job.json"
        jobfile.write_text(json.dumps(job))
        result = subprocess.run([sys.executable, str(scripts / "run_job.py"), "--job", str(jobfile)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        req = copy.deepcopy(self.request)
        req["worker"] = {"adapter": "receipt", "expected_identity": identity, "baseline_completion_seq": 0,
                         "status_path": str(jobdir / "status.json"), "output_path": str(jobdir / "output.log")}
        self.assertEqual(module.inspect_once(req, module.Transport(req))[0], "review_ready")
        reqfile = self.root / "request.json"
        reqfile.write_text(json.dumps(req))
        proc = subprocess.run([sys.executable, str(scripts / "monitor_task.py"), "--request", str(reqfile)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(module.read_json(self.root / "monitor_summary.json")["status"], "review_ready")
        self.assertFalse((self.root / "notification_attempt.json").exists())


class StallCauseTests(unittest.TestCase):
    setUp, tearDown = Tests.setUp, Tests.tearDown

    def test_stall_cause_reads_handoff_and_output_and_never_raises(self):
        req = copy.deepcopy(self.request)
        client = FakeTransport(req)
        client.output = lambda spec: "thinking...\nAll good"
        handoff = self.root / "HANDOFF.md"
        handoff.unlink()
        self.assertEqual(module.stall_cause(req, client)["cause"], "no_delivery")
        handoff.write_text("TASK-TEST unique-nonce\nInputs verified.\nRemaining: implement the parser.\n", encoding="utf-8")
        cause = module.stall_cause(req, client)
        self.assertEqual(cause["cause"], "handoff_without_marker")
        self.assertIn("Remaining: implement the parser.", cause["evidence"])
        client.output = lambda spec: "step 3\nError: 429 Too Many Requests - rate limit reached\nidle"
        cause = module.stall_cause(req, client)
        self.assertEqual(cause["cause"], "provider_error")
        self.assertIn("rate limit", cause["evidence"])
        def broken(spec):
            raise OSError("pane gone")
        client.output = broken
        self.assertEqual(module.stall_cause(req, client)["cause"], "handoff_without_marker")
        self.assertIn("provider recovers", module.event_guidance("incomplete", "T", "provider_error"))
        self.assertEqual(module.event_guidance("blocked", "T", "provider_error"), module.event_guidance("blocked", "T"))

    def test_stall_cause_is_recorded_once_per_episode(self):
        req = copy.deepcopy(self.request)
        (self.root / "RESULT.md").unlink()
        reqfile = self.root / "request.json"
        reqfile.write_text(json.dumps(req), encoding="utf-8")
        client = FakeTransport(req, status="idle", seq=10)
        client.output = lambda spec: client.__dict__.__setitem__("reads", client.reads + 1) or "quota exceeded"
        polls = []
        def sleep(seconds):
            polls.append(seconds)
            if len(polls) == 3:
                (self.root / "STOP_MONITOR").write_text("stop", encoding="utf-8")
        with patch.object(module, "Transport", return_value=client), patch.object(module.time, "sleep", sleep), \
                patch.object(sys, "argv", ["monitor_task.py", "--request", str(reqfile)]), \
                patch("builtins.print"):
            module.main()
        status = json.loads((self.root / "monitor_summary.json").read_text(encoding="utf-8"))
        self.assertEqual(status["status"], "monitoring_stopped")
        self.assertEqual(client.reads, 1)  # Output is read once for the episode, not every poll.


class EncodingTests(unittest.TestCase):
    def test_utf8_chinese_is_preserved(self):
        text = "\u89d2\u8272\u5206\u5de5\u4e0e\u81ea\u52a8\u5316"
        self.assertEqual(check_encoding.check_bytes(text.encode("utf-8"), expect_cjk=True), [])

    def test_invalid_utf8_is_rejected(self):
        self.assertTrue(check_encoding.check_bytes(b"\xff\xfe broken"))

    def test_ascii_question_replacement_is_rejected(self):
        self.assertTrue(check_encoding.check_bytes(b"Role: " + b"?" * 8, expect_cjk=True))

    def test_replacement_and_mojibake_are_rejected(self):
        for text in ["\ufffd", "\u951f\u65a4\u62f7", "\u00ef\u00bb\u00bf"]:
            self.assertTrue(check_encoding.check_bytes(text.encode("utf-8")))

    def test_plain_english_is_valid_without_language_expectation(self):
        self.assertEqual(check_encoding.check_bytes(b"Valid ASCII and one question?"), [])

    def test_english_entry_rejects_non_ascii(self):
        self.assertEqual(check_encoding.check_bytes(b"English entry", expect_ascii=True), [])
        self.assertTrue(check_encoding.check_bytes("\u89d2\u8272".encode("utf-8"), expect_ascii=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
