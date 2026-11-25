import contextlib
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import unittest

import material_packets as m
from material_http_embeddings import EmbeddingError, http_scorer


class EmbeddingTests(unittest.TestCase):
    def test_wire_inputs_follow_scoped_document_access(self):
        from test_project_retrieve import ProjectRetrieveTests
        import service_access as access
        fixture = ProjectRetrieveTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        env = fixture.env
        phrase = "inference throughput on test chips"
        ref = fixture.publish(phrase)
        fixture.publish("Private neighbouring research")
        token = env.f.issue("reviewer", "planner", knowledge_id=ref)
        tokenfile = env.root / "scope.token"
        tokenfile.write_text(token)
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(payload["input"])
                data = [{"index": i, "embedding": [1, 0] if x in (query, phrase) else [0, 1]}
                        for i, x in enumerate(payload["input"])]
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"data": data[::-1]}).encode())

        query = "\u63a8\u7406\u541e\u5410\u91cf"
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        with server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = ["--state", str(env.state), "--project-root", str(env.root), "--doc-token",
                        str(tokenfile), "--query", query]
                online = ["--provider", "http", "--embedding-url",
                          "http://127.0.0.1:%d/v1/embeddings" % server.server_port,
                          "--embedding-model", "mock"]
                def run(options):
                    out, err = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        rc = m.main(base + options)
                    return rc, out.getvalue(), err.getvalue()

                rc, out, err = run(online)
                self.assertEqual(rc, 0, err)
                self.assertEqual(json.loads(out)["materials"][0]["text"], phrase)
                self.assertEqual(requests, [[query, phrase]])
                self.assertFalse(json.loads(run(["--provider", "lexical"])[1])["materials"])
                self.assertEqual(json.loads(run(["--provider", "lexical", "--expand", "throughput"])[1])
                                 ["materials"][0]["text"], phrase)
                who = access.principal(env.state, str(env.root), token)
                access.revoke_token(env.state, *env.f.f.actor, env.f.f.envelope(
                    "access.revoke", {"token_id": who["token_id"], "expected_revision": 1}))
                self.assertEqual(run(online)[:2], (2, ""))
                self.assertEqual(requests, [[query, phrase]])
            finally:
                server.shutdown()
                thread.join()
        with self.assertRaisesRegex(EmbeddingError, "no fallback"):
            http_scorer(online[3], "mock")(["public"], query, "http")

    def test_revoked_during_collection_never_dispatched(self):
        from unittest.mock import Mock
        send = Mock()
        reads = iter([[("first", {})], [("second", {})], PermissionError()])
        def read(*args):
            value = next(reads)
            if isinstance(value, Exception):
                raise value
            return value
        with self.assertRaises(PermissionError):
            m.packet(read, [("doc", "a"), ("doc", "b")], "query", "http", scorer=send)
        send.assert_not_called()

    def test_remote_requires_explicit_https(self):
        for url, opt in [("https://example.org/v1/embeddings", False),
                         ("http://example.org/v1/embeddings", True)]:
            with self.assertRaises(EmbeddingError):
                http_scorer(url, "model", opt)
