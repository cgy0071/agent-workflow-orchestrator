import copy
import json
import unittest
from unittest.mock import Mock, patch

from mcp_gateway import ServiceBackend
from task_evidence import KnowledgeReadFacade, checksum
import test_project_retrieve as fixtures


class FacadeTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.ProjectRetrieveTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.doc = fixture.publish("Named result\nNamed result")
        self.env = fixture.env
        self.api = ServiceBackend(self.env.state, self.env.root, extensions_enabled=True)
        self.creds = {"doc": self.env.f.worker}
        self.learning = KnowledgeReadFacade(self.api, self.creds)
        self.task = KnowledgeReadFacade(self.api, self.creds, "T-A")
        self.hits = self.learning.search("Named", provider="lexical")["materials"]
        self.preview = checksum(self.learning.selection(self.hits))

    def freeze(self):
        return self.task.pin(self.hits, "a"*64, self.preview)

    def test_learning_to_task_exact_read_and_private_fields(self):
        manifest = self.freeze()
        self.assertEqual(manifest["schema"], "awo.task-evidence.v1")
        for secret in ("Named result", self.creds["doc"], str(self.env.root)):
            self.assertNotIn(secret, json.dumps(manifest))
        reader = KnowledgeReadFacade(self.api, self.creds, "T-A")
        self.assertEqual([x["text"] for x in reader.resolve(manifest, manifest["packet_sha256"])],
                         [h["text"] for h in self.hits])
        with self.assertRaises(PermissionError):
            self.learning.pin(self.hits, "a"*64, self.preview)

    def test_changed_position_revision_bytes_and_envelope(self):
        for field, new in (("char_start", 1), ("revision_sha256", "0"*64)):
            hits = copy.deepcopy(self.hits)
            hits[0]["citation"][field] = new
            with self.assertRaisesRegex(ValueError, "unavailable"):
                self.task.pin(hits, "a"*64, checksum(self.task.selection(hits)))
        manifest = self.freeze()
        old = manifest["packet_sha256"]
        manifest["items"][0]["locator"]["char_start"] += 1
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            self.task.resolve(manifest, old)
        self.hits[0]["text"] += "!"
        with self.assertRaises(ValueError):
            self.freeze()

    def test_scoped_discovery_filters_before_ranking_and_revocation(self):
        import service_access
        token = self.env.f.issue("reviewer", "planner", knowledge_id=self.doc)
        facade = KnowledgeReadFacade(self.api, {"doc": token}, "T-A")
        other = fixtures.ProjectRetrieveTests()
        other.env = self.env
        other.publish("SECRET not allowed")
        def scorer(texts, query, provider):
            self.assertTrue(texts)
            self.assertFalse(any("SECRET" in x for x in texts))
            return [1] * len(texts)
        facade.search("anything", scorer=scorer)
        manifest = self.freeze()
        ident = facade.identity()
        self.assertEqual(manifest["project_id"], ident)
        who = service_access.principal(self.env.state, self.env.root, token)
        service_access.revoke_token(self.env.state, *self.env.f.f.actor, self.env.f.f.envelope(
            "access.revoke", {"token_id": who["token_id"], "expected_revision": 1}))
        with self.assertRaises(service_access.service.ServiceError):
            facade.resolve(manifest, manifest["packet_sha256"])

    def test_task_only_credentials_never_enter_learning_ranker(self):
        scorer = Mock()
        for kind in ("doc", "note"):
            with patch("task_evidence.principal", return_value={"task_id":"T-A"}):
                with self.assertRaises(PermissionError):
                    KnowledgeReadFacade(self.api, {kind:"token"}).search("Named", scorer=scorer)
        scorer.assert_not_called()

    def test_identical_source_ids_keep_authority(self):
        cite = self.hits[0]["citation"]
        plugin = dict(authority="awo-kb", project_id=cite["project_id"], task_id="T-A",
                      record_id=self.doc, version="v1", locator="p.1", kb_revision="d"*64, file_sha256="c"*64, verification="kb.evidence.pdf")
        hits = [self.hits[0], dict(self.hits[0], citation=plugin)]
        def read(kind, key):
            self.assertEqual(key, self.doc)
            return [(hits[0]["text"], cite if kind == "doc" else plugin)]
        with patch.object(self.task, "read", side_effect=read):
            packet = self.task.pin(hits, "f"*64, checksum(self.task.selection(hits)))
            self.assertEqual([r["authority"] for r in packet["items"]], ["awo", "awo-kb"])
            self.assertEqual(len(self.task.resolve(packet, packet["packet_sha256"])), 2)

class PluginTests(unittest.TestCase):
    def test_real_note_pdf_pins_and_grant_revocation(self):
        from test_kb_host_bridge import BridgeTests
        b = BridgeTests()
        b.setUp()
        self.addCleanup(b.doCleanups)
        reader = KnowledgeReadFacade(b.api, {"note": b.token, "pdf": b.token}, "T-A")
        hits = reader.search("Named evidence excerpt", provider="lexical")["materials"]
        self.assertEqual({h["citation"]["record_id"] for h in hits}, {"S1", "S2"})
        manifest = reader.pin(hits, "e"*64, checksum(reader.selection(hits)))
        self.assertEqual(len(reader.resolve(manifest, manifest["packet_sha256"])), 2)
        b.policy["actors"][b.actor]["read"] = []
        b.save()
        from mcp_gateway import GatewayError
        with self.assertRaises((PermissionError, GatewayError)):
            reader.resolve(manifest, manifest["packet_sha256"])
