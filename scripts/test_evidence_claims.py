import copy
import unittest
import evidence_claims as chain


class ChainTests(unittest.TestCase):
    def test_invalid_source_and_judgment_matrix(self):
        item = {"evidence_id":"E1", "locator":{"physical_page":3}, "excerpt_sha256":"a"*64}
        packet = {"task_id":"T", "packet_sha256":"b"*64, "items":[item]}
        body = {**{k:packet[k] for k in ("task_id","packet_sha256")},
                "claims":[{"id":"C1", "statement":"Measured latency.", "citations":[item]}]}
        for change in ({"locator":{"physical_page":2}}, {"excerpt_sha256":"c"*64},
                       {"evidence_id":"E2"}):
            bad = copy.deepcopy(body)
            bad["claims"][0]["citations"][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                chain.claims(chain.footer(bad), packet)
        for bad in (dict(body, claims=[]), dict(body, packet_sha256="c"*64),
                    dict(body, claims=[dict(body["claims"][0], citations=[])])):
            with self.assertRaises(ValueError):
                chain.claims(chain.footer(bad), packet)
        check = dict(claim_id="C1", evidence_id="E1", supported=True, reason="matches source")
        review = dict(task_id="T",packet_sha256="b"*64,claims_sha256=chain.checksum(body),checks=[check])
        chain.audit(chain.claims(chain.footer(body),packet),review)
        for bad in (dict(review, checks=[]), dict(review, checks=[dict(check,supported=False)]),
                    dict(review, checks=[check,check]), dict(review,claims_sha256="c"*64)):
            with self.assertRaises(ValueError):
                chain.audit(body,bad)
