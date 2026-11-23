"""Route an authorized task between two disposable projects using a real document grant."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import service_access
import test_project_entry as entry_fixtures
import test_project_retrieve as retrieval_fixtures


class ProjectRoutingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.requester = entry_fixtures.Fixture(self.base)
        self.thread = self.base / "conversation"
        self.spec = self.base / "spec.json"
        self.spec.write_text(json.dumps(self.requester.spec(self.thread, "refs/heads/demo")), encoding="utf-8")
        docs = retrieval_fixtures.ProjectRetrieveTests()
        docs.setUp()
        self.addCleanup(docs.doCleanups)
        self.source = docs.env
        self.body = "Calibration note: sample AL-17 measured 201 HV."
        self.doc = docs.publish(self.body)
        self.private_doc = docs.publish("Private control result: 199 HV.")
        self.token = self.source.f.issue("reviewer", "planner", knowledge_id=self.doc)
        credential = self.base / "source.token"
        credential.write_text(self.token, encoding="utf-8")
        self.grant = {
            "source": "materials", "recipient": "student", "doc_id": self.doc,
            "revision_sha256": self.source.call("doc_get", {"doc_id": self.doc})["revision_sha256"],
            "token_file": str(credential),
        }
        self.catalog = {"version": 1, "projects": {
            "student": {"manager": "student-lead", "root": str(self.requester.repo)},
            "materials": {"manager": "materials-lead", "root": str(self.source.root),
                          "state": str(self.source.state)},
        }, "exports": {"calibration": self.grant}}
        self.catalog_path = self.base / "directory.json"
        self.env = dict(os.environ, **self.requester.write_mode())

    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", str(Path(__file__).with_name("maid.py")),
                               "--thread", str(self.thread), *args], capture_output=True, text=True,
                              env=self.env, timeout=240)

    def submit(self):
        self.catalog_path.write_text(json.dumps(self.catalog), encoding="utf-8")
        return self.cli("--directory", str(self.catalog_path), "--project", "student",
                        "--material", "calibration", "submit", "--spec", str(self.spec))

    def test_requester_receives_reviewed_result_using_only_exported_material(self):
        # Make the simulated worker require the published data in its actual prompt.
        script = self.requester.fake.read_text(encoding="utf-8")
        script = script.replace("    for rel, content", "    assert " + repr(self.body) +
                                " in prompt and '199 HV' not in prompt\n    for rel, content")
        self.requester.fake.write_text(script, encoding="utf-8")
        prepared = self.submit()
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        route = json.loads(prepared.stdout)["route"]
        self.assertEqual((route["project"], route["manager"]), ("student", "student-lead"))
        self.assertEqual(route["material"]["doc_id"], self.doc)
        self.requester.install_fixture_executables(self.thread)
        started = self.cli("start", "--confirm-execute")
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        status = json.loads(self.cli("status").stdout)
        self.assertEqual(status["route"], route)
        self.assertTrue(status["accepted_evidence_id"])
        self.assertEqual(status["review"], "accept")
        self.assertEqual(entry_fixtures.git(self.thread / "repo", "show", "demo:app.py"),
                         entry_fixtures.APP_FIXED.strip())
        self.assertFalse((self.source.root / "app.py").exists())
        self.assertEqual(self.source.call("doc_get", {"doc_id": self.doc})["manifest"]["body"], self.body)
        self.assertNotIn(self.token, (self.thread / "plan.json").read_text(encoding="utf-8"))

    def test_wrong_recipient_revision_document_and_revoked_credential_fail_before_prepare(self):
        for key, value in (("recipient", "materials"), ("revision_sha256", "0" * 64),
                           ("doc_id", self.private_doc)):
            previous = self.grant[key]
            self.grant[key] = value
            result = self.submit()
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertFalse(self.thread.exists())
            self.grant[key] = previous
        actor = service_access.principal(self.source.state, str(self.source.root), self.token)
        service_access.revoke_token(self.source.state, *self.source.f.f.actor,
                                    self.source.f.f.envelope("access.revoke", {
                                        "token_id": actor["token_id"], "expected_revision": 1}))
        self.assertEqual(self.submit().returncode, 2)
        self.assertFalse(self.thread.exists())
