"""Search published AWO documents with an authenticated project credential."""
import argparse
from collections import Counter
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

from mcp_gateway import ServiceBackend, read_token_file, safe_error


class RetrievalError(ValueError):
    pass


def terms(text):
    return re.findall(r"\w+", text.casefold())


def load_skill(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(8193)
    if len(raw) > 8192:
        raise RetrievalError("invalid_skill")
    skill = json.loads(raw.decode("utf-8"))
    if not isinstance(skill, dict) or set(skill) != {"skill", "fields"}:
        raise RetrievalError("invalid_skill")
    if not isinstance(skill["skill"], str) or not re.fullmatch(r"[a-z][a-z0-9.-]{0,62}@[1-9][0-9]{0,5}", skill["skill"]):
        raise RetrievalError("invalid_skill")
    fields = skill["fields"]
    if (not isinstance(fields, list) or not 1 <= len(fields) <= 8 or
            any(not isinstance(f, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9 -]{0,47}", f) for f in fields)
            or len(set(fields)) != len(fields)):
        raise RetrievalError("invalid_skill")
    return skill, hashlib.sha256(raw).hexdigest()


def read_document(gateway, token, ref):
    view = gateway.call(token, "doc_get", {"payload": {"doc_id": ref}})
    if view.get("doc_id") != ref or view.get("usable") is not True or view.get("state") != "published":
        raise RetrievalError("document_unavailable")
    body = view["manifest"]["body"]
    if hashlib.sha256(body.encode("utf-8")).hexdigest() != view["content_sha256"]:
        raise RetrievalError("content_mismatch")
    return body, {k: view[k] for k in ("project_id", "doc_id", "content_revision", "revision_sha256", "content_sha256")}


def rank(rows, words, backend):
    if backend == "lexical":
        counts = []
        for ref, (text, _) in rows.items():
            frequencies = Counter(terms(text))
            counts.append((sum(frequencies[t] for t in words), ref))
    elif backend == "sqlite":
        # Per-query postings; a revoked credential cannot reuse an old index.
        with closing(sqlite3.connect(":memory:")) as con:
            con.execute("CREATE TABLE posting(ref TEXT, term TEXT, n INTEGER)")
            for ref, (text, _) in rows.items():
                con.executemany("INSERT INTO posting VALUES(?,?,?)",
                                ((ref, t, n) for t, n in Counter(terms(text)).items() if t in words))
            counts = con.execute("SELECT sum(n), ref FROM posting GROUP BY ref").fetchall()
    else:
        raise RetrievalError("unknown_backend")
    return sorted(((-n, ref) for n, ref in counts if n))


def retrieve(gateway, token, refs, query, backend, skill_path=None):
    if not isinstance(query, str) or len(query) > 512 or not (words := set(terms(query))):
        raise RetrievalError("invalid_query")
    if not 1 <= len(refs) <= 16 or len(set(refs)) != len(refs):
        raise RetrievalError("invalid_document_selection")
    binding = load_skill(skill_path) if skill_path else None
    rows = {ref: read_document(gateway, token, ref) for ref in refs}
    ranked = rank(rows, words, backend)
    if not ranked:
        raise RetrievalError("no_match")
    _, selected = ranked[0]
    body, citation = read_document(gateway, token, selected)
    if citation != rows[selected][1]:
        raise RetrievalError("document_changed")
    result = {"citation": citation, "text": body}
    if binding:
        skill, digest = binding
        values = {}
        for field in skill["fields"]:
            found = [line[len(field)+1:].strip() for line in body.splitlines() if line.startswith(field + ":")]
            if len(found) != 1 or not found[0]:
                raise RetrievalError("domain_field_unavailable")
            values[field] = found[0]
        result.update(skill=skill["skill"], skill_sha256=digest, fields=values)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--token-file", default=os.environ.get("AWO_AGENT_TOKEN_FILE"))
    parser.add_argument("--backend", choices=("lexical", "sqlite"), default="lexical")
    parser.add_argument("--doc", required=True, action="append", help="Published document ID; repeat up to 16 times")
    parser.add_argument("--query", required=True)
    parser.add_argument("--skill", help="Optional declarative domain skill JSON")
    args = parser.parse_args(argv)
    if not args.token_file:
        parser.error("supply --token-file or set AWO_AGENT_TOKEN_FILE")
    try:
        gateway = ServiceBackend(args.state, args.project_root, extensions_enabled=True)
        result = retrieve(gateway, read_token_file(args.token_file), args.doc, args.query, args.backend, args.skill)
    except Exception as exc:
        print("project retrieve: " + (str(exc) if isinstance(exc, RetrievalError) else safe_error(exc).code), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
