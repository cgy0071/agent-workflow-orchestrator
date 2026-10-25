"""OMP JSONL and OS-child regressions; these fixtures are not real OMP acceptance."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from monitor_task import read_json, save_json
from native_events import inspect_turn
import native_worker
import run_job


def stream(stop="stop", text="323"):
    events = [
        {"type": "session", "id": "fixture-session"},
        {"type": "agent_start"},
        {"type": "message_end", "message": {"role": "assistant", "provider": "fixture-provider",
                                          "model": "fixture-model", "stopReason": stop,
                                          "content": [{"type": "text", "text": text}]}},
        {"type": "agent_end", "isTerminal": True, "yielded": True,
         "messages": [{"role": "assistant", "provider": "fixture-provider",
                                            "model": "fixture-model", "stopReason": stop,
                                            "content": [{"type": "text", "text": text}]}]},
    ]
    return ("\n".join(json.dumps(event) for event in events) + "\n").encode()


class OmpBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def job(self, raw, name="job", exit_code=0, delay=0):
        script = "import sys,time; sys.stdout.buffer.write(bytes.fromhex(" + repr(raw.hex()) + ")); sys.stdout.flush(); "
        script += "sys.stderr.write('fixture diagnostic\\n'); time.sleep(" + str(delay) + "); sys.exit(" + str(exit_code) + ")"
        return {"task_id": "OMP-FIXTURE", "nonce": "nonce", "cwd": str(self.root),
                "identity": {"kind": "omp", "terminal": "fixture", "launch_cwd": str(self.root)},
                "authorization": {"source": "fixture", "terminate_on_timeout": True},
                "output_dir": str(self.root / name), "completion_contract": {"type": "native_turn", "harness": "omp"},
                "argv": [sys.executable, "-c", script], "timeout_seconds": .4 if delay else 5}

    def test_complete_omp_event_and_clean_exit_are_both_required(self):
        good = run_job.run(self.job(stream()))
        self.assertEqual((good["status"], good["completion_seq"], good["returncode"]), ("done", 1, 0))
        self.assertEqual(good["native_turn"]["model"], "fixture-model")
        self.assertIn("fixture diagnostic", (self.root / "job/stderr.log").read_text())
        self.assertNotIn("fixture diagnostic", (self.root / "job/output.log").read_text())
        self.assertIn("started_at", good)
        self.assertIn("finished_at", good)
        bad = run_job.run(self.job(stream(), "nonzero", exit_code=7))
        self.assertEqual((bad["status"], bad["completion_seq"], bad["returncode"]), ("failed", 0, 7))
        self.assertEqual(bad["native_turn"]["status"], "completed")

    def test_omp_error_truncation_and_missing_end_fail_closed(self):
        for name, raw in (("error", stream("error")),
                          ("aborted", stream("aborted")),
                          ("empty", stream(text="")),
                          ("truncated", stream()[:-7]),
                          ("no-end", stream().split(b'{"type": "agent_end"')[0]),
                          ("malformed", stream() + b'{bad\n')):
            with self.subTest(name=name):
                result = run_job.run(self.job(raw, name))
                self.assertEqual((result["status"], result["completion_seq"]), ("failed", 0))

    def test_nonterminal_agent_end_cannot_complete(self):
        raw = stream().replace(b'"isTerminal": true', b'"isTerminal": false')
        self.assertEqual(inspect_turn("omp", raw)["status"], "incomplete")

    def test_timeout_keeps_native_text_but_cannot_complete(self):
        result = run_job.run(self.job(stream(), "timed", delay=5))
        self.assertEqual((result["status"], result["completion_seq"]), ("failed", 0))
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["native_turn"]["text"], "323")

    def test_omp_cli_args_are_one_shot_and_session_is_rejected(self):
        config = {"harness": "omp", "mode": "one-shot", "argv_prefix": ["omp"],
                  "provider": "fixture-provider", "model": "fixture-model"}
        argv = native_worker.argv_for(config, {"cwd": str(self.root), "prompt": "task"}, self.root)
        for flag in ("--print", "json", "--cwd", "--no-session", "--auto-approve", "--model",
                     "--tools=read,bash,edit,write,grep,glob"):
            self.assertIn(flag, argv)
        self.assertNotIn("--no-tools", argv)
        self.assertEqual(argv[-1], "task")
        config["mode"] = "session"
        with self.assertRaisesRegex(ValueError, "not verified"):
            native_worker.argv_for(config, {"cwd": str(self.root), "prompt": "task"}, self.root)

    def test_omp_missing_or_changed_version_fails_before_dispatch(self):
        worker = self.root / "worker"
        with self.assertRaises((OSError, ValueError)):
            native_worker.initialize(worker, "omp", self.root / "missing-omp")
        self.assertFalse(worker.exists())
        with patch.object(native_worker, "omp_version", return_value="omp/fixture-1"):
            native_worker.initialize(worker, "omp", sys.executable)
        payload = {"task_id": "OMP-FIXTURE", "nonce": "nonce", "cwd": str(self.root),
                   "prompt": "task", "authorization": {"source": "fixture"}}
        with patch.object(native_worker, "omp_version", return_value="omp/fixture-2"):
            with self.assertRaisesRegex(ValueError, "version changed"):
                native_worker.send(worker, payload)
        self.assertFalse((worker / "jobs").exists())
        self.assertFalse((worker / "active.json").exists())

    def test_omp_outcome_cannot_write_handoff_on_nonzero_exit(self):
        worker = self.root / "worker"
        worker.mkdir()
        save_json(worker / "config.json", {"harness": "omp", "mode": "one-shot"})
        jobdir = worker / "jobs" / "fixture"
        jobdir.mkdir(parents=True)
        job = self.job(stream(), "fixture-receipt", exit_code=7)
        job["output_dir"] = str(jobdir / "receipt")
        save_json(jobdir / "job.json", job)
        outcome = native_worker.run(worker, jobdir / "job.json")
        self.assertEqual(outcome["status"], "failed")
        self.assertFalse((self.root / "HANDOFF.md").exists())
        self.assertEqual(read_json(jobdir / "receipt/status.json")["completion_seq"], 0)


if __name__ == "__main__":
    unittest.main()
