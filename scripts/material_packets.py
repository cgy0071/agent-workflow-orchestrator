"""Rank explicitly authorized excerpts. No network, durable index or source writes."""
import argparse
from collections import Counter
import hashlib
import json
import math
import re
import sys

from mcp_gateway import ServiceBackend, read_token_file
from project_cli_inputs import check_paths
from project_retrieve import read_document


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def tokens(text):
    return re.findall(r"[\u3400-\u9fff]|[^\W_\u3400-\u9fff]+", text.casefold())


def score(texts, query, provider):
    counts = [Counter(tokens(t)) for t in texts]
    vocab = sorted(set().union(*counts))
    if not vocab:
        return [0.] * len(texts)
    if len(vocab) > 8192:
        raise ValueError("vocabulary limit exceeded")
    weights = [math.log((1 + len(texts)) / (1 + sum(k in c for c in counts))) + 1 for k in vocab]

    def vector(c):
        return [(1 + math.log(c[k])) * w if c[k] else 0. for k, w in zip(vocab, weights)]

    rows, q = [vector(c) for c in counts], vector(Counter(tokens(query)))
    if provider == "lsa":
        import numpy as np
        matrix = np.asarray(rows)
        _, values, vt = np.linalg.svd(matrix, full_matrices=False)
        dims = min(32, max(1, math.isqrt(len(values))), int(sum(values > 1e-10)))
        basis = vt[:dims].T
        rows, q = matrix @ basis, np.asarray(q) @ basis
    elif provider != "lexical":
        raise ValueError("unknown provider")
    norm = lambda v: math.sqrt(sum(x*x for x in v))
    return [float(sum(x*y for x, y in zip(v, q)) / (norm(q)*norm(v) or 1)) for v in rows]


def load(api, token, kind, key, task):
    if kind == "doc":
        body, cite = read_document(api, token, key)
        out, pos = [], 0
        for line in body.splitlines(keepends=True):
            for start in range(0, len(line), 800):
                part = line[start:start+800]
                if part.strip():
                    out.append((part, dict(cite, authority="awo", char_start=pos+start,
                                           char_end=pos+start+len(part))))
            pos += len(line)
        return out
    if not task or kind not in ("note", "pdf"):
        raise ValueError("expected a task-bound note or pdf")
    args = {"task_id": task, "record_id": key}
    rec = api.call(token, "kb.pull", args)
    if rec["id"] != key or rec["type"] != "SRC" or rec["active"] is not True:
        raise ValueError("inactive source")
    op = "kb.evidence.pdf" if kind == "pdf" else "kb.evidence.text"
    if api.call(token, op, args) is not True:
        raise ValueError("unverified excerpt")
    return [(rec["quote"], dict(authority="awo-kb", project_id=rec["project"], task_id=task,
                                record_id=key, version=rec["version"], locator=rec["locator"],
                                author=rec["author"], verification=op))]


def packet(reader, refs, query, provider="lsa", limit=5, *, scorer=score):
    if not query.strip() or len(query) > 512 or not 1 <= len(refs) <= 16 or len(set(refs)) != len(refs):
        raise ValueError("invalid selection")
    snapshots = {ref: reader(*ref) for ref in refs}
    items = [item for group in snapshots.values() for item in group]
    if not 1 <= len(items) <= 256:
        raise ValueError("excerpt count outside bounds")
    if type(limit) is not int or not 1 <= limit <= 32:
        raise ValueError("invalid result limit")
    scores = scorer([t for t, _ in items], query, provider)
    if len(scores) != len(items) or not all(math.isfinite(s) for s in scores):
        raise ValueError("invalid provider scores")
    indices = sorted(range(len(items)), key=lambda i: (-round(scores[i], 12), i))
    result = [dict(text=items[i][0], citation=items[i][1], excerpt_sha256=digest(items[i][0]))
              for i in indices[:limit] if scores[i] > 1e-8]
    # Even a non-winning source can influence the embedding and ordering.
    for ref in refs:
        if reader(*ref) != snapshots[ref]:
            raise ValueError("source changed during search")
    return dict(schema="awo.material-packet.v1", provider=provider, materials=result)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("state", "project-root", "query"):
        p.add_argument("--"+name, required=True)
    for name in ("doc", "note", "pdf"):
        p.add_argument("--"+name, action="append", default=[])
    for name in ("doc-token", "kb-token", "task"):
        p.add_argument("--"+name)
    p.add_argument("--provider", choices=("lexical", "lsa"), default="lsa")
    a = p.parse_args(argv)
    try:
        check_paths(a.state, a.project_root)
        api = ServiceBackend(a.state, a.project_root, extensions_enabled=True)
        refs = [(k, key) for k in ("doc", "note", "pdf") for key in getattr(a, k)]
        creds = {k: read_token_file(a.doc_token if k == "doc" else a.kb_token) for k, _ in refs}
        if "doc" in creds and len(creds) > 1:
            from service_access import principal
            identities = [principal(a.state, a.project_root, t) for t in set(creds.values())]
            if len({(p["project_id"], p["principal_id"], p.get("agent_id"))
                    for p in identities}) != 1:
                raise ValueError("mixed recipient identities")
        res = packet(lambda k, key: load(api, creds[k], k, key, a.task), refs, a.query, a.provider)
    except ImportError:
        print("Optional provider or plugin unavailable; check dependencies.", file=sys.stderr)
        return 2
    except Exception:
        print("Material packet unavailable; verify scope, revision and input bounds.", file=sys.stderr)
        return 2
    print(json.dumps(res, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
