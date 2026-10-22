"""Protocol fixtures only. These tests never establish real Codex/Pi capability."""
import json
from pathlib import Path
import subprocess
import sys
import unittest

import native_worker
import run_job
import test_scheduler as fixtures
import validate_native
import workflow_scheduler as scheduler


FIXTURE = r'''
import json, re, sys
from pathlib import Path
root = Path(sys.argv[1]); prompt = sys.argv[-1]
if prompt.startswith('Independently review'):
    task = re.search(r'task_id=([^,]+), nonce=([^,]+),', prompt)
    text = json.dumps({'task_id': task[1], 'nonce': task[2], 'verdict': 'accept', 'reason': 'protocol fixture only'})
elif '"memory"' in prompt:
    text = json.dumps({'answer': 324, 'memory': (root / 'memory.txt').read_text()})
else:
    memory = re.search(r'Remember the secret ([a-z0-9]+)\.', prompt)
    if memory: (root / 'memory.txt').write_text(memory[1])
    text = '324' if '+ 1' in prompt else '323'
for event in ({'type': 'thread.started', 'thread_id': 'protocol-fixture-thread'},
              {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': text}},
              {'type': 'turn.completed'}):
    print(json.dumps(event), flush=True)
'''


class NativeProtocolTests(unittest.TestCase):
    setUp = fixtures.SchedulerTests.setUp
    tearDown = fixtures.SchedulerTests.tearDown
    spec = fixtures.SchedulerTests.spec

    def prepare_fixture(self, mode="one-shot"):
        root = self.root / ("native-protocol-" + mode)
        validate_native.prepare(root, "codex", sys.executable, mode, 10, evidence_class="protocol-fixture")
        script = self.root / "fixture.py"
        script.write_text(FIXTURE, encoding="utf-8")
        for role in ("worker", "reviewer"):
            path = root / role / "config.json"
            config = json.loads(path.read_text(encoding="utf-8"))
            config["argv_prefix"] = [sys.executable, str(script), str(self.root)]
            path.write_text(json.dumps(config), encoding="utf-8")
        return root

    def test_native_parser_requires_native_completion_and_assistant_text(self):
        for raw in ('TASK_DONE T n', '{"type":"turn.completed"}', '{"type":"turn.failed"}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                native_worker.extract("codex", raw)
        raw = '\n'.join(json.dumps(event) for event in (
            {"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": "answer"}], "stopReason": "error"}},
            {"type": "agent_end"}))
        with self.assertRaises(ValueError): native_worker.extract("pi", raw)

    def test_protocol_one_shot_loop_never_upgrades_native_profiles(self):
        root = self.prepare_fixture()
        result = validate_native.execute(root, budget=25)
        self.assertEqual(result["status"], "PROTOCOL_FIXTURE_PASS")
        self.assertEqual(result["receipt_adapter"], "passed")
        for adapter in ("command", "receipt"):
            path = root / (adapter + "-capability-profile.json")
            profile = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual({item["status"] for item in profile["operations"].values()}, {"unverified"})
            self.assertEqual(set(profile["native_capabilities"].values()), {"unverified"})

    def test_protocol_session_recall_stays_separate_from_one_shot_evidence(self):
        root = self.prepare_fixture("session")
        result = validate_native.execute(root, budget=25)
        self.assertEqual(result["status"], "PROTOCOL_FIXTURE_PASS")
        self.assertEqual(result["session_reuse"], "passed")
        profile = json.loads((root / "command-capability-profile.json").read_text(encoding="utf-8"))
        self.assertEqual(profile["native_capabilities"]["conversation_reuse"], "unverified")

    def test_controller_crash_recovery_does_not_send_a_second_job(self):
        root = self.prepare_fixture()
        (root / "crash-after-send").write_text("explicit fixture crash")
        source = "import sys; import validate_native; validate_native.execute(sys.argv[1], budget=20)"
        proc = subprocess.run([sys.executable, "-B", "-c", source, str(root)], cwd=Path(scheduler.__file__).parent,
                              capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, 90, proc.stderr)
        result = validate_native.execute(root, budget=25, recover_dispatch=True)
        self.assertEqual(result["status"], "PROTOCOL_FIXTURE_PASS")
        attempts = [json.loads(line) for line in (root / "adapter-events.jsonl").read_text().splitlines()]
        worker_sends = [item for item in attempts if item["kind"] == "send_attempt" and item["value"]["identity"]["kind"] == "codex"
                        and json.loads(item["value"]["message"])["task_id"].startswith("NATIVE-")]
        self.assertEqual(len(worker_sends), 2)  # A once and B once, including restart.

    def test_only_explicitly_authorized_bounded_child_is_terminated(self):
        job = {"task_id": "BOUNDED", "nonce": "bounded-nonce", "cwd": str(self.root), "output_dir": str(self.root / "bounded"),
               "identity": {"kind": "protocol-fixture", "terminal": "owned-child", "launch_cwd": str(self.root)},
               "argv": [sys.executable, "-c", "import time; time.sleep(20)"], "timeout_seconds": .15,
               "authorization": {"source": "explicit test of owned child", "terminate_on_timeout": True}}
        result = run_job.run(job)
        self.assertIn(result["status"], ("failed", "blocked"))
        if result["status"] == "blocked": self.assertEqual(result["completion_seq"], 0)
        self.assertTrue(result["timed_out"])
        job["output_dir"] = str(self.root / "unauthorized")
        job["authorization"].pop("terminate_on_timeout")
        with self.assertRaisesRegex(ValueError, "terminate_on_timeout"): run_job.run(job)
        self.assertFalse(Path(job["output_dir"]).exists())


if __name__ == "__main__":
    unittest.main()
