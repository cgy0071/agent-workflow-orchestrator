import copy
import json
import unittest

import evidence_draft as draft
import test_project_retrieve as fixtures
import awo_service as service
from service_access import principal


class DraftTests(unittest.TestCase):
    def setUp(self):
        f = fixtures.ProjectRetrieveTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        key = f.publish("Latency is 13 ms.\nLatency is workload dependent.")
        env = f.env
        service.execute_command(env.state, *env.f.f.actor, env.f.f.envelope("task.create",
                                {"task_id":"TARGET", "title":"Draft target", "body":json.dumps({"evidence_document_ids":[key]})}))
        token = env.f.issue("worker", "executor", task_id="TARGET")
        path = env.root / "target.token"
        path.write_text(token)
        doc_file = env.root / "doc.token"
        doc_file.write_text(env.f.worker)
        self.config = dict(root=str(env.root), state=str(env.state), doc_token_file=str(doc_file), task_token_file=str(path),
                           project_id=principal(env.state, env.root, token)["project_id"], task="TARGET")
        self.worker = env.f.worker

    def test_select_recheck_and_missing(self):
        view = draft.handle(self.config, {"query":"latency"})
        refs = [view["items"][1]["ref"]]
        out = draft.handle(self.config, {"goal":"summarize", "selected":refs})
        self.assertFalse(out["draft"]["dispatched"])
        self.assertEqual(out["draft"]["items"][0]["excerpt_sha256"], refs[0]["excerpt_sha256"])
        self.assertNotIn(view["items"][1]["text"], json.dumps(out["draft"]))
        refs[0]["pinned_revision"]["sha256"] = "0"*64
        with self.assertRaises(ValueError):
            draft.handle(self.config, {"goal":"summarize", "selected":refs})
        self.assertEqual(draft.handle(self.config, {"query":"xyzzy"})["status"], "no_hits")

    def test_learning_or_other_task_token_refused(self):
        from pathlib import Path
        with self.assertRaises(PermissionError):
            draft.handle(dict(self.config, task="other"), {"query":"latency"})
        Path(self.config["task_token_file"]).write_text(self.worker)
        with self.assertRaises(PermissionError):
            draft.handle(self.config, {"query":"latency"})


class PersonalTests(unittest.TestCase):
    def test_pdf_note_handoff_and_wrong_location(self):
        from test_kb_host_bridge import BridgeTests
        from pathlib import Path
        b = BridgeTests()
        b.setUp()
        self.addCleanup(b.doCleanups)
        token = b.f.root / "target.token"
        token.write_text(b.token)
        config = dict(state=str(b.f.state), root=str(b.f.root), task="T-A",
                      project_id=principal(b.f.state,b.f.root,b.token)["project_id"],
                      task_token_file=str(token), kb=True, personal_sources={"paper":{"kind":"pdf","record_id":"S2"},
                                                               "notes":{"kind":"note","record_id":"S1"}})
        refs = []
        for item in draft.handle(config, {"query":"excerpt evidence"})["items"]:
            ref = item["ref"]
            pdf = ref["source_id"] == "S2"
            refs.append(dict(authority="personal", source_id="paper" if pdf else "notes",
                        pinned_revision={"sha256":ref["pinned_revision"]["file_sha256"]},
                        locator={"physical_page":1,"chunk":0} if pdf else
                                {"byte_start":0,"byte_end":len(item["text"].encode())},
                        excerpt_sha256=ref["excerpt_sha256"]))
        self.assertEqual(len(refs), 2)
        out = draft.handle(config, {"goal":"summarize", "selected":refs})
        self.assertEqual(len(out["draft"]["personal_origins"]), 2)
        bad = copy.deepcopy(next(r for r in refs if r["source_id"] == "notes"))
        bad["locator"]["byte_start"] = 1
        with self.assertRaises(ValueError):
            draft.handle(config, {"goal":"summarize","selected":[bad]})
