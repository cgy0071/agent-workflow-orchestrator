import hashlib
from importlib import metadata
from importlib.util import find_spec
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

if find_spec("awo_kb") is None:
    try:
        metadata.distribution("awo-kb")
    except metadata.PackageNotFoundError:
        raise unittest.SkipTest("Optional awo-kb package is not installed") from None

from awo_kb import store
from awo_kb.service import Reader
from kb_injection_budget import Tokenizer
from kb_packet_injection import PacketBudgetHost


class PacketBudgetHostTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.policy = {"actors": {"a": {"read": ["P", "S"]}},
                       "projects": {"P": {"sources": ["S"], "profiles": ["paper"]}}}
        self.reader = Reader(root, lambda: self.policy)
        pin = store.GENESIS
        base = {"project": "S", "profiles": ["paper"]}
        for row in (dict(base, id="term", type="TERM", profiles=["core"],
                         canonical="合成词", definition="Synthetic definition."),
                    dict(base, id="src", type="SRC", author="A", version="1", locator="p.2",
                         quote="fixture quote is for pull only"),
                    dict(base, id="jud", type="JUD", subject="x", as_of="2026-10-01",
                         review_by="2026-10-31", stance="Synthetic stance.", holder="H",
                         source_ref="src")):
            pin = store.append(root, row, pin)
        self.task = {"kb_revision": pin, "project": "P", "profiles": ["paper"]}

    def test_render_delivers_the_same_bytes_that_were_measured(self):
        seen = []

        def count(data):
            seen.append(data)
            return len(data.decode())

        host = PacketBudgetHost(self.reader, Tokenizer("fixture", "1", count))
        prepared = host.prepare("a", self.task, today="2026-10-31")
        self.assertEqual(seen, [prepared.core_bytes, prepared.full_bytes])
        self.assertEqual(prepared.core_bytes, '{"judgments":[],"terms":[{"canonical":"合成词",'
                         '"definition":"Synthetic definition.","id":"term"}]}'.encode())
        self.assertFalse(json.loads(prepared.full_bytes)["judgments"][0]["stale"])
        self.assertNotIn(b"fixture quote", prepared.full_bytes)
        self.assertNotIn(b"kb_revision", prepared.full_bytes)
        self.assertEqual(prepared.receipt["profile_tokens"]["sha256"],
                         hashlib.sha256(prepared.full_bytes).hexdigest())
        tomorrow = host.prepare("a", self.task, today="2026-11-01")
        self.assertTrue(json.loads(tomorrow.full_bytes)["judgments"][0]["stale"])
        self.assertNotEqual(prepared.receipt["receipt_sha256"], tomorrow.receipt["receipt_sha256"])

    def test_b04_long_positions_use_only_the_signed_summary_or_detail_marker(self):
        from awo_kb.handoff import fingerprint
        root = self.reader.root
        pin = self.task["kb_revision"]
        row = dict(project="S", profiles=["paper"], id="long", type="JUD", subject="x",
                   as_of="2026-10-01", review_by="2026-10-31", stance="long position " * 32,
                   holder="H", source_ref="src", summary="Signed summary.", scope="This study.")
        row["amendment"] = {"proposer": "H", "mode": "authored", "approved_sha256": fingerprint(row)}
        pin = store.append(root, row, pin)
        legacy = dict(row, id="legacy")
        for key in ("amendment", "scope", "summary"):
            legacy.pop(key)
        pin = store.append(root, legacy, pin)
        blocks = []
        receipt = PacketBudgetHost(self.reader, Tokenizer("bytes", "1", len)).inject(
            "a", dict(self.task, kb_revision=pin), blocks.append)
        block, = blocks
        rows = {j["id"]: j for j in json.loads(block)["judgments"]}
        self.assertEqual((rows["long"]["summary"], rows["long"]["scope"]), ("Signed summary.", "This study."))
        self.assertIs(rows["legacy"]["detail_required"], True)
        self.assertNotIn(b"long position", block)
        self.assertEqual(receipt["profile_tokens"]["sha256"], hashlib.sha256(block).hexdigest())

    def test_changes_to_read_scope_at_same_pin(self):
        host = PacketBudgetHost(self.reader)
        prior = host.prepare("a", self.task)
        self.policy["actors"]["a"]["read"].remove("S")
        after = host.prepare("a", self.task)
        self.assertEqual(json.loads(after.full_bytes), {"judgments": [], "terms": []})
        self.assertNotEqual(prior.receipt["scope_id"], after.receipt["scope_id"])
        self.policy["actors"]["a"]["read"].remove("P")
        with self.assertRaises(PermissionError):
            host.prepare("a", self.task)

    def test_revocation_during_measurement_prevents_delivery(self):
        def count(data):
            self.policy["actors"]["a"]["read"] = ["P"]
            return len(data)

        delivered = []
        with self.assertRaises(PermissionError):
            PacketBudgetHost(self.reader, Tokenizer("test", "1", count)).inject(
                "a", self.task, delivered.append)
        self.assertEqual(delivered, [])

    def test_context_callback_receives_exact_measured_bytes_once(self):
        delivered = []
        receipt = PacketBudgetHost(self.reader, Tokenizer("test", "1", len)).inject(
            "a", self.task, delivered.append)
        block, = delivered
        self.assertEqual(hashlib.sha256(block).hexdigest(), receipt["profile_tokens"]["sha256"])
        self.assertEqual(len(block), receipt["profile_tokens"]["measured"])
        def error(block):
            raise RuntimeError("context rejected")
        with self.assertRaisesRegex(RuntimeError, "context rejected"):
            PacketBudgetHost(self.reader).inject("a", self.task, error)

    def test_disabled_profile_or_caller_supplied_configuration(self):
        host = PacketBudgetHost(self.reader)
        with self.assertRaises(PermissionError):
            host.prepare("a", dict(self.task, profiles=["grant"]))
        for key in ("core_tokens", "tokenizer", "scope_id", "full_bytes"):
            with self.assertRaises(ValueError):
                host.prepare("a", dict(self.task, **{key: "forged"}))
        with self.assertRaises(TypeError):
            host.prepare("a", self.task, tokenizer=None)

    def test_fail_closed_if_a_future_reader_includes_raw_evidence(self):
        packet = {"index": {"terms": [], "judgments": [{"quote": "fixture"}]}}
        with patch.object(Reader, "packet", return_value=packet):
            with self.assertRaisesRegex(ValueError, "unexpected"):
                PacketBudgetHost(self.reader).prepare("a", self.task)
