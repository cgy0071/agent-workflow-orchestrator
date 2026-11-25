import contextlib
import io
import json
import unittest

import material_glossary as g
import material_packets as m


class GlossaryTests(unittest.TestCase):
    def test_covered_span_only_uses_project_terms(self):
        alias, latency = "\u541e\u5410\u7387", "\u5ef6\u8fdf"
        rule = {alias: {"terms": ["inference throughput"], "avoid": ["bandwidth"]}}
        opts = (alias + latency + " BANDWIDTH", rule, ["guess"],
                [(alias, "speed"), (latency, "BANDWIDTH estimate"), (latency, "delay")])
        query, meta = g.expand(*opts, False, True)
        self.assertEqual(meta, {"glossary": ["inference throughput"],
                                "secretary": ["delay", "latency"], "strict": False})
        self.assertNotIn("bandwidth", query.lower())
        _, strict = g.expand(*opts, True, True)
        self.assertEqual(strict["secretary"], [])
        self.assertEqual(strict["glossary"], meta["glossary"])

    def test_project_glossary_english_hit_and_revocation(self):
        from test_project_retrieve import ProjectRetrieveTests
        import service_access as access
        fixture = ProjectRetrieveTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        env = fixture.env
        expected = fixture.publish("Inference throughput reached 312 frames per second.")
        fixture.publish("Bandwidth under load is 10 GB/s.")
        who = access.principal(env.state, str(env.root), env.f.worker)
        directory = env.state / "glossaries"
        directory.mkdir()
        alias = "\u5c0f\u98de\u8f66"
        config = {alias: {"terms": ["inference throughput"], "avoid": ["bandwidth"]}}
        (directory / (who["project_id"] + ".json")).write_text(json.dumps(config), encoding="utf-8")
        token = env.root / "glossary.token"
        token.write_text(env.f.worker)
        args = ["--state", str(env.state), "--project-root", str(env.root), "--doc-token", str(token),
                "--query", alias, "--provider", "lexical", "--expand", "bandwidth", "--strict-glossary"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(m.main(args), 0)
        packet = json.loads(out.getvalue())
        self.assertEqual(packet["materials"][0]["citation"]["doc_id"], expected)
        self.assertEqual(packet["expansion"], {"glossary": ["inference throughput"],
                                               "secretary": [], "strict": True})
        access.revoke_token(env.state, *env.f.f.actor, env.f.f.envelope(
            "access.revoke", {"token_id": who["token_id"], "expected_revision": 1}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(m.main(args), 2)
        self.assertEqual(out.getvalue(), "")
