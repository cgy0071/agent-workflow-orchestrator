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
        def run(*selection, query="verified evidence excerpt", limit=5):
            return subprocess.run([sys.executable, "-B", str(Path(m.__file__).with_name("awo_project.py")),
                                  "materials", "--state", str(env.state), "--project-root", str(env.root),
                                  *selection, "--task", "T-A", "--top-k", str(limit),
                                  "--doc-token", str(d), "--kb-token", str(t), "--provider", "lexical",
                                  "--query", query], capture_output=True, timeout=45)
        p = run("--doc", doc["doc_id"], "--note", "S1", "--pdf", "S2")
        self.assertEqual(p.returncode, 0, p.stderr)
        items = json.loads(p.stdout)["materials"]
        self.assertEqual({i["citation"]["authority"] for i in items}, {"awo", "awo-kb"})
        self.assertEqual(len(items), 3)
        auto = run()
        self.assertEqual(auto.returncode, 0, auto.stderr)
        self.assertEqual(json.loads(auto.stdout)["materials"], items)
        winner = run(query="excerp", limit=1)
        self.assertEqual(json.loads(winner.stdout)["materials"][0]["citation"]["locator"], "p.1")
        project = next(iter(bridge.cfg["projects"].values()))
        del project["tasks"]["T-A"]["source_grants"][bridge.actor]["kb.evidence.pdf"]["S2"]
        bridge.save()
        again = run()
        active = json.loads(again.stdout)["materials"]
        self.assertEqual(len(active), 2)
        self.assertFalse(any(i["citation"].get("record_id") == "S2" for i in active))
        self.assertEqual([i["relevance"] for i in active], sorted(
            (i["relevance"] for i in active), reverse=True))
        d.write_text(env.f.other)
        p = run()
        self.assertEqual((p.returncode, p.stdout), (2, b""))

    def test_chinese_topic_discovery_excludes_unpublished_and_respects_entity_scope(self):
        from test_project_retrieve import ProjectRetrieveTests
        from mcp_gateway import ServiceBackend
        fixture = ProjectRetrieveTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        env = fixture.env
        api = ServiceBackend(env.state, env.root, extensions_enabled=True)
        ref = fixture.publish("\u63a8\u7406\u541e\u5410\u7387 608 FPS")
        other = fixture.publish("unrelated")
        draft = env.doc()["doc_id"]
        creds = {"doc": env.f.worker}
        refs = m.discover(api, creds, None)
        self.assertEqual(set(refs), {("doc", ref), ("doc", other)})
        self.assertNotIn(("doc", draft), refs)
        items = m.packet(lambda k, key: m.load(api, creds[k], k, key, None), refs,
                         "\u60f3\u4e86\u89e3\u541e\u5410\u91cf", "lsa", 1, automatic=True)["materials"]
        self.assertEqual(items[0]["citation"]["doc_id"], ref)
        scoped = env.f.issue("reviewer", "planner", knowledge_id=other)
        self.assertEqual(m.discover(api, {"doc": scoped}, None), [("doc", other)])

    def test_foreign_visible_src_excluded_before_scoring(self):
        import hashlib
        import json
        import awo_service as service
        from awo_kb import store
        from test_kb_host_bridge import BridgeTests
        b = BridgeTests()
        b.setUp()
        self.addCleanup(b.doCleanups)
        project, cfg = next(iter(b.cfg["projects"].items()))
        grants = cfg["tasks"]["T-A"]["source_grants"]
        local = b.call("kb.pull", record_id="S1")
        task = json.loads(b.f.f.rows("SELECT body FROM v2_task WHERE task_id='T-A'")[0]["body"])["kb_task"]
        spec = grants[b.actor]["kb.evidence.text"]["S1"]
        pin = store._append(b.f.root / "vault", dict(id="FOREIGN", project="foreign", profiles=["core"],
            type="SRC", author="External", locator="line 1", version="v1", quote=local["quote"],
            byte_start=0, byte_end=len(local["quote"]), file_sha256=spec["sha256"]), task["kb_revision"])
        task["kb_revision"] = pin
        body = json.dumps({"kb_task": task})
        service.execute_command(b.f.state, *b.f.f.actor, b.f.f.envelope("task.create",
                                {"task_id": "T-B", "title": "fixture", "body": body}))
        token = b.f.issue("worker", "executor", task_id="T-B")
        b.policy["actors"][b.actor]["read"].append("foreign")
        b.policy["projects"][project]["sources"].append("foreign")
        cfg["tasks"]["T-B"] = {"body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                               "source_grants": {b.actor: {"kb.evidence.text": {"FOREIGN": spec, "S1": spec}}}}
        b.save()
        self.assertEqual(b.api.call(token, "kb.pull", {"task_id": "T-B", "record_id": "FOREIGN"})["project"], "foreign")
        self.assertEqual(m.discover(b.api, {"note": token}, "T-B"), [("note", "S1")])
        with self.assertRaises(PermissionError):
            m.load(b.api, token, "note", "FOREIGN", "T-B")
