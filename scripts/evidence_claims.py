"""Validate claim pointers and independent judgments; never infer semantic support."""
import json
from pathlib import Path

from task_evidence import checksum


def footer(data):
    return "\n```json\n" + json.dumps(data, ensure_ascii=True, sort_keys=True) + "\n```\n"


def unique(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate JSON key")
    return result


def claims(text, packet):
    lines = text.rstrip().splitlines()
    if not lines or lines[-1] != "```":
        raise ValueError("missing final JSON block")
    pos = max(i for i, line in enumerate(lines[:-1]) if line == "```json")
    body = json.loads("\n".join(lines[pos+1:-1]), object_pairs_hook=unique)
    if (set(body) != {"task_id", "packet_sha256", "claims"} or
            body["task_id"] != packet["task_id"] or body["packet_sha256"] != packet["packet_sha256"]):
        raise ValueError("result packet mismatch")
    refs = {r["evidence_id"]: r for r in packet["items"]}
    if not isinstance(body["claims"], list) or not 1 <= len(body["claims"]) <= 64:
        raise ValueError("missing or excessive claims")
    for n, claim in enumerate(body["claims"], 1):
        if (set(claim) != {"id", "statement", "citations"} or claim["id"] != f"C{n}"
                or not isinstance(claim["statement"], str) or not claim["statement"].strip()
                or not isinstance(claim["citations"], list) or not claim["citations"]):
            raise ValueError("claim missing support")
        seen = set()
        for ref in claim["citations"]:
            key = ref["evidence_id"]
            if key in seen or key not in refs or checksum(ref) != checksum(refs[key]):
                raise ValueError("duplicate, unknown or changed citation")
            seen.add(key)
    return body


def audit(body, review):
    checks = review["checks"]
    expected = [(c["id"], r["evidence_id"]) for c in body["claims"] for r in c["citations"]]
    if (review.get("task_id") != body["task_id"] or review.get("packet_sha256") != body["packet_sha256"]
            or review.get("claims_sha256") != checksum(body) or not isinstance(checks, list)
            or [(x["claim_id"], x["evidence_id"]) for x in checks] != expected):
        raise ValueError("missing, duplicate or mismatched independent judgment")
    for x in checks:
        if (set(x) != {"claim_id", "evidence_id", "supported", "reason"}
                or x["supported"] is not True or not isinstance(x["reason"], str)
                or not x["reason"].strip()):
            raise ValueError("citation unsupported or missing reason")


def report(plan, packet):
    from project_entry import read_json
    from native_worker import task_key
    d = plan["derived"]
    text = (Path(d["paths"]["delivery"]) / "RESULT.md").read_text(encoding="utf-8")
    if text != read_json(Path(d["paths"]["worker"]) / "jobs" / task_key({"task_id": d["attempt_id"], "nonce": d["nonce"]}) / "receipt" / "status.json")["native_turn"]["text"] + "\n":
        raise ValueError("result differs from worker receipt")
    return claims(text, packet)


ERRORS = (KeyError, ValueError, TypeError, IndexError, AttributeError)
