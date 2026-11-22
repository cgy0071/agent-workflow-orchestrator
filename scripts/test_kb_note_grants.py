"""Exercise note isolation through the MCP gateway's real service subprocess."""
import hashlib
from importlib import metadata
from importlib.util import find_spec
import json
import unittest

if find_spec("awo_kb") is None:
    try:
        metadata.distribution("awo-kb")
    except metadata.PackageNotFoundError:
        raise unittest.SkipTest("Optional awo-kb package is not installed") from None

from awo_kb import editor, store
import awo_service as service
import mcp_gateway as gateway
import service_access as access
import test_service_api as fixtures


@unittest.skipIf(metadata.version("awo-kb") == "0.2.0", "B03 Reader has no note callback")
class NoteGrants(unittest.TestCase):
    def setUp(self):
        f = fixtures.ServiceAPITests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.f = f
        self.api = gateway.ServiceBackend(f.state, f.root, extensions_enabled=True)
        info = access.principal(f.state, str(f.root), f.controller)
        project, self.owner = info["project_id"], info["principal_id"]
        self.worker = access.principal(f.state, str(f.root), f.worker)["principal_id"]
        policy = {"actors": {who: {"read": [project], "write": [project], "kind": "human"}
                             for who in (self.owner, self.worker)},
                  "projects": {project: {"profiles": [], "sources": []}}}
        vault = f.root / "notes-vault"
        self.a, self.b = "[[04_Concepts/A]]", "[[04_Concepts/B]]"
        record = {"id":"T1", "type":"TERM", "project":project, "profiles":["core"],
                  "canonical":"fixture", "definition":"meaning", "refs":[self.a, self.b]}
        pin = editor.commit(vault, record, store.GENESIS, self.owner, policy,
                            {"mode":"authored", "proposer":self.owner, "approved_sha256":editor.digest(record)})
        body = json.dumps({"kb_task":{"project":project, "profiles":[], "kb_revision":pin}})
        self.bindings = {}
        for task in ("T-A", "T-B"):
            service.execute_command(f.state, *f.f.actor, f.f.envelope("task.create",
                                    {"task_id":task, "title":"fixture", "body":body}))
            self.bindings[task] = {"body_sha256":hashlib.sha256(body.encode()).hexdigest()}
        self.token = f.issue("worker", "executor", task_id="T-A")
        # Test-local explicit allowlist for the installed review artifact.
        dist = metadata.distribution("awo-kb")
        digest = json.loads(dist.read_text("awo-plugin.json"))["artifact_digest"]
        self.cfg = {"enabled":True, "artifact_digest":digest, "read_operations":["kb.packet","kb.pull"],
                    "projects":{project:{"policy":policy, "vault":str(vault), "tasks":self.bindings}}}
        self.save()

    def save(self):
        (self.f.state / "kb-host.json").write_text(json.dumps(self.cfg), encoding="utf-8")

    def check(self, token, task, expected):
        for op, args in (("kb.pull", {"record_id":"T1"}), ("kb.packet", {})):
            result = self.api.call(token, op, dict(args, task_id=task))
            row = result if op == "kb.pull" else result["index"]["terms"][0]
            self.assertEqual(row.get("note_refs", []), expected)
            for hidden in {self.a,self.b} - set(expected):
                self.assertNotIn(hidden, json.dumps(result))

    def test_task_principal_and_revocation(self):
        self.check(self.token, "T-A", [])
        self.bindings["T-A"]["note_grants"] = {
            self.worker:[self.a, "[[04_Concepts/*]]"], self.owner:[self.b]}
        self.bindings["T-B"]["note_grants"] = {self.owner:[self.a]}
        self.save()
        self.check(self.token, "T-A", [self.a])
        self.check(self.f.controller, "T-A", [self.b])
        self.check(self.f.controller, "T-B", [self.a])
        with self.assertRaises(gateway.GatewayError):
            self.api.call(self.token, "kb.packet", {"task_id":"T-B"})
        del self.bindings["T-A"]["note_grants"][self.worker]
        self.save()
        self.check(self.token, "T-A", [])

    def test_bad_configuration_and_caller_claims(self):
        for field in ("note_grants", "note_refs", "actor", "vault"):
            with self.subTest(field=field), self.assertRaises(gateway.GatewayError):
                self.api.call(self.token, "kb.pull", {"task_id":"T-A", "record_id":"T1", field:[self.a]})
        self.bindings["T-A"]["note_grants"] = {self.worker:self.a}
        self.save()
        with self.assertRaises(gateway.GatewayError):
            self.api.call(self.token, "kb.packet", {"task_id":"T-A"})
