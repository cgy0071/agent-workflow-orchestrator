"""Exercise B05 inventory across the actual MCP stdio boundary, offline."""
import hashlib
from importlib import metadata
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import unittest

import test_kb_host_bridge as fixture


@unittest.skipUnless(metadata.version("awo-kb") in {"0.4.0", "0.4.1"}, "inventory contract requires B05 or B06")
class InventoryStdioTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixture.BridgeTests()
        self.addCleanup(self.fx.doCleanups)
        self.fx.setUp()
        self.fx.enable_writes()
        self.root = self.fx.f.root
        self.project = self.fx.project
        self.grants = self.fx.cfg["projects"][self.project]["tasks"]["T-A"]["source_grants"]
        self.grant = self.grants[self.fx.actor]
        self.seq = 0

    def client(self, token):
        self.seq += 1
        credential = self.root / ("stdio-token-%s" % self.seq)
        credential.write_text(token, encoding="ascii")
        proc = subprocess.Popen([sys.executable, "-B", str(Path(fixture.gateway.__file__)),
                                 "--extensions-enabled", "--project-root", str(self.root),
                                 "--state", str(self.fx.f.state), "--token-file", str(credential)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        messages = queue.Queue()
        def pump():
            for line in proc.stdout:
                messages.put(line)
            messages.put(None)
        thread = threading.Thread(target=pump, daemon=True)
        thread.start()
        def close():
            proc.stdin.close()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            thread.join(5)
            proc.stdout.close()
        self.addCleanup(close)
        def rpc(method, params, notify=False):
            self.seq += 1
            request = dict(jsonrpc="2.0", method=method, params=params)
            if not notify:
                request["id"] = self.seq
            proc.stdin.write((json.dumps(request) + "\n").encode())
            proc.stdin.flush()
            if not notify:
                response = json.loads(messages.get(timeout=30))
                self.assertEqual(response["id"], self.seq)
                return response
        rpc("initialize", dict(protocolVersion="2024-11-05", capabilities={},
                               clientInfo=dict(name="offline", version="1")))
        rpc("notifications/initialized", {}, True)
        def call(name="kb.inventory", *, denied=False, **kw):
            args = dict(task_id="T-A", **kw)
            if name == "kb.inventory":
                args.setdefault("variants", [])
            response = rpc("tools/call", dict(name=name, arguments=args))
            if denied:
                self.assertTrue("error" in response or response["result"]["isError"], response)
                return
            self.assertNotIn("error", response)
            result = response["result"]
            self.assertFalse(result["isError"], result)
            return result["structuredContent"]
        return call

    def doc(self, key, raw, kind="note"):
        (self.root / key).write_bytes(raw)
        return dict(id=key, project=self.project, kind=kind, path=key,
                    sha256=hashlib.sha256(raw).hexdigest())

    def test_current_grants_on_same_stdio_session_and_pin(self):
        call = self.client(self.fx.token)
        legacy = call()
        self.assertNotIn("maintenance", legacy)
        note = self.doc("typed.md", b"---\ntype: term\n---\nprivate note")
        untyped = self.doc("plain.md", b"Body only")
        notice = dict(source_id="S1", source_sha256=self.grant["kb.evidence.text"]["S1"]["sha256"],
                      issuer="synthetic", status="withdrawn")
        good = self.doc("good.json", json.dumps(notice).encode(), "notice")
        wrong = self.doc("wrong.json", json.dumps(dict(notice, source_sha256="0"*64)).encode(), "notice")
        scoped = dict(notes=self.grant["kb.inventory"], maintenance=[note, untyped, good, wrong])
        self.grant["kb.inventory"] = scoped
        self.fx.save()
        result = call()
        maintenance = result.pop("maintenance")
        self.assertEqual(result, legacy)
        self.assertEqual({row["key"] for row in maintenance["items"]}, {"good.json", "typed.md"})
        self.assertNotIn("private note", str(maintenance))
        for field in ("path", "kb_pin", "inventory_pin"):
            call(denied=True, **{field: maintenance["inventory_pin"]})
        (self.root / note["path"]).write_bytes(b"changed")
        call(denied=True)
        scoped["maintenance"] = []
        self.fx.save()
        fresh = call()["maintenance"]
        self.assertEqual(fresh["kb_pin"], maintenance["kb_pin"])
        self.assertEqual(fresh["items"], [])
        self.fx.policy["actors"][self.fx.actor]["read"] = []
        self.fx.save()
        call(denied=True)
        call("kb.pull", denied=True, record_id="S1")

    def test_review_requires_current_material_grant(self):
        call = self.client(self.fx.token)
        approve = self.client(self.fx.f.controller)
        doc = self.doc("typed.md", b"---\ntype: term\n---\n")
        self.grant["kb.inventory"] = dict(notes=[], maintenance=[doc])
        self.grants[self.fx.human] = dict(self.grant)
        self.fx.save()
        row = call()["maintenance"]["items"][0]
        record = dict(id="T1", type="TERM", project=self.project, profiles=["core"],
                      canonical="sample", definition="example",
                      inventory_origin={k: row[k] for k in ("id", "kb_pin", "inventory_pin")})
        epoch = self.fx.f.f.epoch
        candidate = call("kb.candidate.propose", command_id="propose-inventory", expected_epoch=epoch, record=record)["result"]
        original = fixture.store.history(self.root / "vault")
        self.grants[self.fx.human]["kb.inventory"] = dict(notes=[], maintenance=[])
        self.fx.save()
        fields = dict(candidate_id=candidate, approved_sha256=fixture.editor.digest(record), expected_epoch=epoch)
        approve("kb.candidate.approve", denied=True, command_id="revoked-review", **fields)
        self.assertEqual(fixture.store.history(self.root / "vault"), original)
        self.grants[self.fx.human]["kb.inventory"] = self.grant["kb.inventory"]
        self.fx.save()
        approve("kb.candidate.approve", command_id="authorized-review", **fields)
        self.assertEqual(len(fixture.store.history(self.root / "vault")), len(original) + 1)
