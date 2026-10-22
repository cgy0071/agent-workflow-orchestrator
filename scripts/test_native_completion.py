"""Receipt regressions using OS children and protocol fixtures, not real Harness acceptance."""
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from monitor_task import Transport, inspect_once, read_json, save_json
from native_events import inspect_turn
import native_worker
import run_job
import test_scheduler as fixtures
import validate_native


CODEX = [{"type": "thread.started", "thread_id": "fixture-thread"},
         {"type": "item.completed", "item": {"type": "agent_message", "text": "323"}},
         {"type": "turn.completed"}]
PI_ERROR = [{"type": "message_end", "message": {"role": "assistant", "provider": "fixture-provider",
             "model": "fixture-model", "content": [], "stopReason": "error"}}, {"type": "agent_end"}]


class CompletionTests(unittest.TestCase):
    setUp = fixtures.SchedulerTests.setUp
    tearDown = fixtures.SchedulerTests.tearDown
    spec = fixtures.SchedulerTests.spec

    def job(self, events, harness="codex", tail="", name="job"):
        return {"task_id": "FIXTURE", "nonce": "fixture-nonce", "cwd": str(self.root),
                "identity": self.specs["executor"]["expected_identity"],
                "authorization": {"source": "explicit protocol fixture", "terminate_on_timeout": True},
                "output_dir": str(self.root / name), "completion_contract": {"type": "native_turn", "harness": harness},
                "argv": [sys.executable, "-c", "import sys,time; print(" + repr("\n".join(json.dumps(e) for e in events)) + ",flush=True); " + (tail or "pass")]}

    def test_zero_exit_pi_error_fails_receipt_without_advancing_sequence(self):
        job = self.job(PI_ERROR, "pi")
        receipt = run_job.run(job)
        self.assertEqual((receipt["status"], receipt["completion_seq"], receipt["returncode"]), ("failed", 0, 0))
        self.assertEqual(receipt["process_status"], "clean_exit")
        self.assertEqual(receipt["native_turn"]["provider"], "fixture-provider")
        self.assertEqual(receipt["native_turn"]["model"], "fixture-model")
        worker = {"adapter": "receipt", "expected_identity": job["identity"], "baseline_completion_seq": 0,
                  "status_path": str(self.root / "job/status.json"), "output_path": str(self.root / "job/output.log")}
        req = {"worker": worker, "task_id": job["task_id"], "nonce": job["nonce"]}
        self.assertEqual(inspect_once(req, Transport(req))[0], "failed")

    def test_zero_exit_without_complete_turn_fails(self):
        for index, events in enumerate(([], CODEX[:-1], [{"type": "turn.completed"}])):
            with self.subTest(events=events):
                receipt = run_job.run(self.job(events, name="missing-" + str(index)))
                self.assertEqual((receipt["status"], receipt["completion_seq"], receipt["returncode"]), ("failed", 0, 0))

    def test_complete_turn_retains_actual_nonzero_exit(self):
        receipt = run_job.run(self.job(CODEX, tail="sys.exit(7)"))
        self.assertEqual((receipt["status"], receipt["completion_seq"], receipt["returncode"]), ("done", 1, 7))
        self.assertEqual(receipt["process_status"], "nonzero_exit")
        self.assertEqual(receipt["native_turn"]["text"], "323")

    def test_codex_retry_errors_recover_only_after_a_complete_turn(self):
        retries = [{"type": "error", "message": "Reconnecting... 1/5 (502 Bad Gateway)"},
                   {"type": "error", "message": "Reconnecting... 2/5 (502 Bad Gateway)"}]
        receipt = run_job.run(self.job(CODEX[:1] + retries + CODEX[1:]))
        self.assertEqual((receipt["status"], receipt["completion_seq"], receipt["returncode"]), ("done", 1, 0))
        self.assertEqual(receipt["native_turn"]["text"], "323")
        self.assertEqual(receipt["native_turn"]["diagnostics"], [event["message"] for event in retries])

    def test_codex_unrecovered_or_terminal_errors_still_fail(self):
        error = {"type": "error", "message": "connection failed"}
        cases = (CODEX[:1] + [error], CODEX + [error],
                 CODEX[:1] + [{"type": "turn.failed"}] + CODEX[1:],
                 CODEX[:1] + [error, {"type": "turn.completed"}])
        for index, events in enumerate(cases):
            with self.subTest(events=events):
                receipt = run_job.run(self.job(events, name="error-" + str(index)))
                self.assertEqual((receipt["status"], receipt["completion_seq"]), ("failed", 0))

    def test_confirmed_timeout_preserves_completed_turn_and_exit_evidence(self):
        directory = self.root / "captured"
        directory.mkdir()
        data = b"non-UTF-8 diagnostic: \xff\n" + "\n".join(json.dumps(e) for e in CODEX).encode()
        (directory / "output.log").write_bytes(data)
        state = {"status": "failed", "completion_seq": 0, "returncode": 1, "timed_out": True,
                 "process_status": "terminated_on_timeout"}
        result = run_job.finalize({"completion_contract": {"type": "native_turn", "harness": "codex"}}, state, directory)
        self.assertEqual((result["status"], result["completion_seq"], result["returncode"]), ("done", 1, 1))
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["native_turn"]["text"], "323")
        self.assertEqual(result["native_turn"]["output_sha256"], hashlib.sha256(data).hexdigest())
        self.assertIn("CLI did not exit", result["warning"])

    def test_owned_hanging_child_retains_answer_without_hiding_uncertain_cleanup(self):
        worker = self.root / "native-worker"
        native_worker.initialize(worker, "codex", sys.executable)
        job = self.job(CODEX, tail="time.sleep(20)")
        job["timeout_seconds"] = .4
        jobdir = worker / "jobs" / "owned-fixture"
        jobdir.mkdir(parents=True)
        job["output_dir"] = str(jobdir / "receipt")
        path = jobdir / "job.json"
        save_json(path, job)
        result = native_worker.run(worker, path)
        receipt = read_json(jobdir / "receipt/status.json")
        self.assertTrue(receipt["timed_out"])
        self.assertEqual(receipt["native_turn"]["status"], "completed")
        self.assertEqual((self.root / "RESULT.md").read_text().strip(), "323")
        if receipt["status"] == "blocked":
            self.assertEqual(receipt["completion_seq"], 0)
            self.assertEqual(result["status"], "blocked")
        else:
            self.assertEqual((receipt["status"], result["status"]), ("done", "done"))

    def test_truncated_completion_and_later_failure_cannot_pass(self):
        raw = "\n".join(json.dumps(e) for e in CODEX[:-1]) + '\n{"type":"turn.completed"'
        self.assertEqual(inspect_turn("codex", raw)["status"], "incomplete")
        raw = "\n".join(json.dumps(e) for e in CODEX + [{"type": "turn.failed"}])
        self.assertEqual(inspect_turn("codex", raw)["status"], "failed")

    def test_pi_selection_is_per_invocation_and_defaults_are_optional(self):
        spec = native_worker.initialize(self.root / "pi", "pi", sys.executable, provider="test-provider", model="test-model")
        config = read_json(self.root / "pi/config.json")
        argv = native_worker.argv_for(config, {"prompt": "test"}, self.root)
        self.assertEqual(argv[argv.index("--provider") + 1], "test-provider")
        self.assertEqual(argv[argv.index("--model") + 1], "test-model")
        config.update(provider=None, model=None)
        argv = native_worker.argv_for(config, {"prompt": "test"}, self.root)
        self.assertNotIn("--provider", argv)
        self.assertNotIn("--model", argv)
        for harness, provider, model in (("codex", "test", "test"), ("pi", "test", None), ("pi", None, "--bad")):
            with self.assertRaises(ValueError): native_worker.selection(harness, provider, model)

    def test_protocol_fixtures_work_in_herdr_but_real_acceptance_is_rejected(self):
        with patch.dict(os.environ, {"HERDR_ENV": "1"}):
            with self.assertRaisesRegex(ValueError, "outside Herdr"):
                validate_native.prepare(self.root / "real", "pi", sys.executable, "one-shot", 10)
            root = self.root / "protocol"
            validate_native.prepare(root, "pi", sys.executable, "one-shot", 10, evidence_class="protocol-fixture",
                                    provider="test-provider", model="test-model")
        self.assertFalse((self.root / "real").exists())
        meta = read_json(root / "provenance.json")
        self.assertEqual(meta["evidence_class"], "protocol-fixture")
        self.assertEqual(meta["model_selection"]["requested"], {"provider": "test-provider", "model": "test-model"})
        for role in ("worker", "reviewer"):
            self.assertEqual(read_json(root / role / "config.json")["provider"], "test-provider")


if __name__ == "__main__":
    unittest.main()
