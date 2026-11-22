"""Exercise the command against an actual service database and issued tokens."""
import json
from pathlib import Path
import subprocess
import sys
import unittest

import service_access as access
import test_service_extensions as fixtures


HERE = Path(__file__).resolve().parent
SKILLS = HERE.parent / "examples" / "project-domains"


class ProjectRetrieveTests(unittest.TestCase):
    def setUp(self):
        self.env = fixtures.ExtensionTests()
        self.env.setUp()
        self.addCleanup(self.env.doCleanups)

    def publish(self, body):
        doc = self.env.call("doc_import", {"title": "Report", "scope": "project", "content": body})
        reviewer = self.env.f.issue("reviewer", "planner", knowledge_id=doc["doc_id"])
        accepted = self.env.review(doc, reviewer)
        self.env.call("doc_publish", {k: accepted[k] for k in ("doc_id", "review_id", "revision_sha256", "content_sha256")} |
                      {"expected_revision": accepted["revision"]}, self.env.f.controller)
        return doc["doc_id"]

    def cli(self, docs, query, skill, token, backend):
        path = self.env.root / "token"
        path.write_text(token, encoding="utf-8")
        return subprocess.run([sys.executable, "-B", str(HERE / "project_retrieve.py"),
                               "--state", str(self.env.state), "--project-root", str(self.env.root),
                               "--token-file", str(path), "--backend", backend, "--query", query,
                               "--skill", str(SKILLS / (skill + ".json")),
                               *(x for d in docs for x in ("--doc", d))],
                              cwd=self.env.root, capture_output=True, text=True, timeout=60)

    def test_two_backends_domains_and_denial(self):
        docs = [self.publish("Assay: Alloy A\nHardness: 201 HV"), self.publish("Timing: Clock A\nWorst slack: -5 ps")]
        for query, skill, key, value in [("alloy", "materials", "Hardness", "201 HV"),
                                        ("clock", "eda", "Worst slack", "-5 ps")]:
            outputs = []
            for backend in ("sqlite", "lexical"):
                result = self.cli(docs, query, skill, self.env.f.worker, backend)
                self.assertEqual(result.returncode, 0, result.stderr)
                outputs.append(json.loads(result.stdout))
            self.assertEqual(outputs[0], outputs[1])
            self.assertEqual(outputs[0]["fields"][key], value)
        scoped = self.env.f.issue("reviewer", "planner", knowledge_id=docs[0])
        for ids, expected in [([docs[0]], 0), (docs, 2)]:
            result = self.cli(ids, "alloy", "materials", scoped, "sqlite")
            self.assertEqual(result.returncode, expected, result.stderr)
            if expected:
                self.assertEqual(result.stdout, "")
        principal = access.principal(self.env.state, str(self.env.root), scoped)
        access.revoke_token(self.env.state, *self.env.f.f.actor, self.env.f.f.envelope(
            "access.revoke", {"token_id": principal["token_id"], "expected_revision": 1}))
        result = self.cli([docs[0]], "alloy", "materials", scoped, "sqlite")
        self.assertEqual((result.returncode, result.stdout), (2, ""))

