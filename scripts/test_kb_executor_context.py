import hashlib
from importlib import metadata
from importlib.util import find_spec
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

if find_spec("awo_kb") is None:
    try:
        metadata.distribution("awo-kb")
    except metadata.PackageNotFoundError:
        raise unittest.SkipTest("Optional awo-kb package is not installed") from None

import kb_executor_context as context
from kb_injection_budget import Tokenizer
import native_worker
import test_kb_host_bridge as bridge_tests


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        fixture = bridge_tests.BridgeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.root = fixture.f.root / "worker"
        self.root.mkdir()
        self.capture = self.root / "delivered.bin"
        mock = self.root / "capture.py"
        mock.write_text("from pathlib import Path\nimport sys\n"
                        "Path(sys.argv[1]).write_bytes(sys.argv[-1].encode('utf8'))\n"
                        "print('{\"type\":\"thread.started\",\"thread_id\":\"local\"}')\n"
                        "print('{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"ok\"}}')\n"
                        "print('{\"type\":\"turn.completed\"}')\n", encoding="utf8")
        token_file = fixture.f.root / "task-token"
        token_file.write_text(fixture.token,encoding="ascii")
        self.config = {"mode":"one-shot","harness":"codex",
                       "argv_prefix":[sys.executable, "-B", str(mock), str(self.capture)],
                       "kb_context":{"state":str(fixture.f.state),"project_root":str(fixture.f.root),
                                     "token_file":str(token_file)}}
        self.job = {"task_id":"T-A","nonce":"once","cwd":str(self.root),"prompt":"Synthetic task",
                    "identity":"local","output_dir":str(self.root/"receipt"),
                    "authorization":{"source":"fixture"},"completion_contract":{"type":"native_turn","harness":"codex"}}
        self.spec = {"input_sha256":context.input_digest(self.job),"require_measured":True}
        project, = fixture.cfg["projects"]
        fixture.cfg["projects"][project]["tasks"]["T-A"]["execution"] = self.spec
        fixture.save()
        (self.root/"config.json").write_text(json.dumps(self.config),encoding="utf8")
        self.path = self.root/"job.json"
        self.path.write_text(json.dumps(self.job),encoding="utf8")

    def test_executor_observes_measured_bytes_and_packet_wire_format(self):
        packet = self.fixture.call("kb.packet")
        self.assertEqual(set(packet),{"task","index","packet_sha256"})
        counted = []
        def count(blob):
            counted.append(blob)
            return len(blob)
        with patch.object(context,"_tokenizer",return_value=Tokenizer("fixture-byte","1",count)):
            result = native_worker.run(self.root,self.path)
        self.assertEqual(result["status"],"done",result)
        measured = json.loads((self.root/"kb-context-receipt.json").read_bytes())
        blob = self.capture.read_bytes().split(b"\n\n",1)[1]
        self.assertEqual(json.loads(blob),packet["index"])
        self.assertEqual(blob,counted[-1])
        self.assertEqual(measured["profile_tokens"]["sha256"],hashlib.sha256(blob).hexdigest())
        self.assertEqual(measured["profile_tokens"]["measured"],len(blob))

    def test_revocation_prevents_process_from_starting(self):
        def revoke(blob):
            self.fixture.policy["actors"][self.fixture.actor]["read"] = []
            self.fixture.save()
            return len(blob)
        with patch.object(context,"_tokenizer",return_value=Tokenizer("fixture","1",revoke)):
            self.assertEqual(native_worker.run(self.root,self.path)["status"],"failed")
        self.assertFalse(self.capture.exists())
        self.assertFalse((self.root/"kb-context-receipt.json").exists())

    @unittest.skipIf(metadata.version("awo-kb") == "0.2.0", "B03 has no note references")
    def test_one_shot_receives_only_current_note_links(self):
        from awo_kb import store
        import awo_service as service
        import kb_packet_injection as injection
        fixture = self.fixture
        project, = fixture.cfg["projects"]
        config = fixture.cfg["projects"][project]
        config["policy"]["projects"][project]["profiles"] = ["paper"]
        vault = Path(config["vault"])
        granted, hidden = "[[04_Concepts/granted]]", "[[04_Concepts/private]]"
        pin = store.append(vault, {"id":"mapped", "type":"TERM", "project":project, "profiles":["core"],
                           "canonical":"term", "definition":"definition", "refs":[granted, hidden]}, store.head(vault))
        body = json.dumps({"kb_task":{"project":project, "profiles":["paper"], "kb_revision":pin}})
        host = fixture.f
        service.execute_command(host.state, *host.f.actor, host.f.envelope("task.create",
                                {"task_id":"T-M", "title":"notes", "body":body}))
        token = host.issue("worker", "executor", task_id="T-M")
        Path(self.config["kb_context"]["token_file"]).write_text(token, encoding="ascii")
        self.job["task_id"] = "T-M"
        self.path.write_text(json.dumps(self.job), encoding="utf8")
        binding = {"execution":dict(self.spec, input_sha256=context.input_digest(self.job)),
                   "body_sha256":hashlib.sha256(body.encode()).hexdigest(),
                   "note_grants":{fixture.actor:[granted]}}
        config["tasks"]["T-M"] = binding
        fixture.save()
        with patch.object(context, "_tokenizer", return_value=Tokenizer("fixture", "1", len)):
            outcome = native_worker.run(self.root, self.path)
            self.assertEqual(outcome["status"], "done", outcome)
            payload = self.capture.read_bytes()
            self.assertIn(granted.encode(), payload)
            self.assertNotIn(hidden.encode(), payload)
            self.capture.unlink()
            self.job["output_dir"] = str(self.root / "next-receipt")
            self.path.write_text(json.dumps(self.job), encoding="utf8")
            real_measure = injection.measure_injection_budget
            def withdraw(core, full, **kw):
                receipt = real_measure(core, full, **kw)
                self.assertIn(granted.encode(), full)
                binding["note_grants"].clear()
                fixture.save()
                return receipt
            with patch.object(injection, "measure_injection_budget", side_effect=withdraw) as measured:
                self.assertEqual(native_worker.run(self.root, self.path)["status"], "failed")
            measured.assert_called_once()
            self.assertFalse(self.capture.exists())
            outcome = native_worker.run(self.root, self.path)
            self.assertEqual(outcome["status"], "done", outcome)
            self.assertNotIn(granted.encode(), self.capture.read_bytes())
            self.assertNotIn(hidden.encode(), self.capture.read_bytes())

    @unittest.skipUnless(importlib.util.find_spec("tokenizers"), "optional local tokenizer backend")
    def test_actual_file_tokenizer_counts_executor_input(self):
        from tokenizers import Tokenizer as HFTokenizer, models, pre_tokenizers
        tokenizer = HFTokenizer(models.WordLevel({"[UNK]":0,"terms":1,"judgments":2},unk_token="[UNK]"))
        tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
        path = self.root/"local-tokenizer.json"
        tokenizer.save(str(path))
        self.spec["tokenizer"] = {"file":str(path),"name":"synthetic-wordlevel",
                                  "sha256":hashlib.sha256(path.read_bytes()).hexdigest()}
        self.fixture.save()
        outcome = native_worker.run(self.root,self.path)
        self.assertEqual(outcome["status"],"done",outcome)
        block = self.capture.read_bytes().split(b"\n\n",1)[1]
        receipt = json.loads((self.root/"kb-context-receipt.json").read_bytes())
        self.assertEqual(receipt["profile_tokens"]["measured"],
                         len(tokenizer.encode(block.decode(),add_special_tokens=False).ids))
        self.assertEqual(receipt["profile_tokens"]["sha256"],hashlib.sha256(block).hexdigest())

    def test_unmeasured_or_over_budget_prevents_launch(self):
        for tokenizer in (None, Tokenizer("fixture","1",lambda _:4001)):
            with patch.object(context,"_tokenizer",return_value=tokenizer):
                self.assertEqual(native_worker.run(self.root,self.path)["status"],"failed")
            self.assertFalse(self.capture.exists())
