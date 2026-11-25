import hashlib
import unittest

import material_packets as m
from material_candidates import extract


class PacketTests(unittest.TestCase):
    def test_markdown_exact_offsets_and_no_transclusion(self):
        raw = "# Heading\r\n\u4e2d ![[other]]\r\n".encode()
        rows = extract(raw, ".md")["candidates"]
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(raw[row["byte_start"]:row["byte_end"]].decode(), row["quote"])
            self.assertEqual(row["file_sha256"], hashlib.sha256(raw).hexdigest())

    def test_revoked_nonwinning_source_and_revision_change_abort(self):
        for denied in (True, False):
            calls = []
            def reader(k, key):
                if key in calls and key == "other":
                    if denied:
                        raise PermissionError
                    return [("changed", {})]
                calls.append(key)
                return [(key, {})]
            with self.assertRaises((ValueError, PermissionError)):
                m.packet(reader, [("doc", "answer"), ("doc", "other")], "answer", "lexical")

    def test_dense_embedding_matches_related_excerpt_without_query_word(self):
        try:
            import numpy
        except ImportError:
            self.skipTest("optional numpy")
        texts = ["thermometer temperature", "thermometer heat", "heat",
                 "neuron spike", "neuron pulse", "pulse"]
        self.assertEqual(m.score(texts, "temperature", "lexical")[2], 0)
        scores = m.score(texts, "temperature", "lsa")
        self.assertGreater(scores[2], scores[5]+1e-5)

    def test_doc_service_packet(self):
        from test_project_retrieve import ProjectRetrieveTests
        env = ProjectRetrieveTests()
        env.setUp()
        self.addCleanup(env.doCleanups)
        ref = env.publish("Heading\nLatency: 13 ms")
        from mcp_gateway import ServiceBackend
        api = ServiceBackend(env.env.state, env.env.root, extensions_enabled=True)
        reader = lambda k, key: m.load(api, env.env.f.worker, k, key, None)
        item = m.packet(reader, [("doc", ref)], "Latency", "lexical")["materials"][0]
        self.assertEqual(item["citation"]["char_start"], 8)
        self.assertEqual(item["text"], "Latency: 13 ms")

    def test_real_plugin_pdf_and_revocation(self):
        from test_kb_host_bridge import BridgeTests
        env = BridgeTests()
        env.setUp()
        self.addCleanup(env.doCleanups)
        raw = (env.f.root / "sample.pdf").read_bytes()
        self.assertEqual(extract(raw, ".pdf")["candidates"][0]["physical_page"], 1)
        reader = lambda k, key: m.load(env.api, env.token, k, key, "T-A")
        refs = [("note", "S1"), ("pdf", "S2")]
        item = m.packet(reader, refs, "excerpt", "lexical")["materials"][0]
        self.assertEqual((item["text"], item["citation"]["locator"]), ("PDF excerpt", "p.1"))
        env.policy["actors"][env.actor]["read"] = []
        env.save()
        from mcp_gateway import GatewayError
        with self.assertRaises(GatewayError):
            m.packet(reader, refs, "excerpt", "lexical")

    def test_mixed_cli_and_different_recipient_denied(self):
        import json
        from pathlib import Path
        import subprocess
        import sys
        import awo_service as service
        from test_service_extensions import ExtensionTests
        from test_kb_host_bridge import BridgeTests
        bridge = BridgeTests()
        bridge.setUp()
        self.addCleanup(bridge.doCleanups)
        env = ExtensionTests()
        env.f, env.root, env.state = bridge.f, bridge.f.root, bridge.f.state
        service.execute_command(env.state, *env.f.f.actor, env.f.f.envelope("project.bind_root",
                                {"project_root": str(env.root), "expected_revision": 0}))
        doc = env.doc()
        reviewed = env.review(doc, env.f.issue("reviewer", "planner", knowledge_id=doc["doc_id"]))
        env.call("doc_publish", {k: reviewed[k] for k in
                 ("doc_id", "review_id", "revision_sha256", "content_sha256")} |
                 {"expected_revision": reviewed["revision"]}, env.f.controller)
        d, t = env.root / "doc.token", env.root / "task.token"
        d.write_text(env.f.worker)
        t.write_text(bridge.token)
        def run():
            return subprocess.run([sys.executable, "-B", str(Path(m.__file__).with_name("awo_project.py")),
                                  "materials", "--state", str(env.state), "--project-root", str(env.root),
                                  "--doc", doc["doc_id"], "--note", "S1", "--pdf", "S2", "--task", "T-A",
                                  "--doc-token", str(d), "--kb-token", str(t), "--provider", "lexical",
                                  "--query", "verified evidence excerpt"], capture_output=True, timeout=45)
        p = run()
        self.assertEqual(p.returncode, 0, p.stderr)
        items = json.loads(p.stdout)["materials"]
        self.assertEqual({i["citation"]["authority"] for i in items}, {"awo", "awo-kb"})
        self.assertEqual(len(items), 3)
        d.write_text(env.f.other)
        p = run()
        self.assertEqual((p.returncode, p.stdout), (2, b""))
