"""Search authorized topics or rank selected excerpts. No network, durable index or source writes."""
import argparse
from collections import Counter
import hashlib
import json
import math
import re
import sys
import unicodedata

from service_access import principal

import material_glossary
from material_http_embeddings import EmbeddingError, http_scorer
from mcp_gateway import ServiceBackend, read_token_file
from project_cli_inputs import check_paths
from project_retrieve import read_document


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def tokens(text):
    words = re.findall(r"[\u3400-\u9fff]+|[^\W_\u3400-\u9fff]+", unicodedata.normalize("NFKC", text).casefold())
    out = []
    for word in words:
        if "\u3400" <= word[0] <= "\u9fff":
            out.extend(word)
            out.extend(word[i:i+2] for i in range(len(word)-1))
        else:
            out.append(word)
            if len(word) >= 5 and word.isascii() and word.isalpha():
                out.extend("~" + word[i:i+3] for i in range(len(word)-2))
    return out


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
    norm = lambda v: math.sqrt(sum(x*x for x in v))
    def cosine(v, q):
        return float(sum(x*y for x, y in zip(v, q)) / (norm(q)*norm(v) or 1))
    baseline = [cosine(v, q) for v in rows]
    if provider == "lsa":
        import numpy as np
        matrix = np.asarray(rows)
        _, values, vt = np.linalg.svd(matrix, full_matrices=False)
        dims = min(32, max(1, math.isqrt(len(values))), int(sum(values > 1e-10)))
        basis = vt[:dims].T
        rows, q = matrix @ basis, np.asarray(q) @ basis
        return [0.5 * (raw + cosine(v, q)) for raw, v in zip(baseline, rows)]
    elif provider != "lexical":
        raise ValueError("unknown provider")
    return baseline


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
    project = principal(api.state, api.project_root, token)["project_id"]
    if rec["project"] != project:
        raise PermissionError("foreign source")
    if rec["id"] != key or rec["type"] != "SRC" or rec["active"] is not True:
        raise ValueError("inactive source")
    op = "kb.evidence.pdf" if kind == "pdf" else "kb.evidence.text"
    if api.call(token, op, args) is not True:
        raise ValueError("unverified excerpt")
    return [(rec["quote"], dict(authority="awo-kb", project_id=rec["project"], task_id=task,
                                record_id=key, version=rec["version"], locator=rec["locator"],
                                author=rec["author"], verification=op))]


def discover(api, creds, task):
    refs = []
    if "doc" in creds:
        token = creds["doc"]
        who = principal(api.state, api.project_root, token)
        scoped = who.get("knowledge_id")
        after = ""
        while True:
            if scoped:
                rows = [api.call(token, "doc_get", {"payload": {"doc_id": scoped}})]
                cursor = None
            else:
                page = api.call(token, "doc_list", {"payload": {"after_id": after, "limit": 8}})
                rows, cursor = page["items"], page["next_after_id"]
            refs.extend(("doc", r["doc_id"]) for r in rows if r["state"] == "published"
                        and r["usable"] and r["project_id"] == who["project_id"])
            if cursor is None:
                break
            if cursor <= after:
                raise ValueError("invalid cursor")
            after = cursor
    if "note" in creds:
        from kb_host_bridge import material_grants
        refs.extend(material_grants(api.state, api.project_root, creds["note"], task))
    return refs


def packet(reader, refs, query, provider="lsa", limit=5, *, scorer=score, automatic=False):
    if not query.strip() or len(query) > 512 or len(refs) > (256 if automatic else 16) or (not automatic and not refs) or len(set(refs)) != len(refs):
        raise ValueError("invalid selection")
    snapshots = {ref: reader(*ref) for ref in refs}
    items = [item for group in snapshots.values() for item in group]
    if len(items) > 256 or (not automatic and not items):
        raise ValueError("excerpt count outside bounds")
    if type(limit) is not int or not 1 <= limit <= 32:
        raise ValueError("invalid result limit")
    if provider == "http":
        for ref in refs:
            if reader(*ref) != snapshots[ref]:
                raise ValueError("source changed before embedding")
    scores = scorer([t for t, _ in items], query, provider) if items else []
    if len(scores) != len(items) or not all(math.isfinite(s) for s in scores):
        raise ValueError("invalid provider scores")
    indices = sorted(range(len(items)), key=lambda i: (-round(scores[i], 12), i))
    result = [dict(text=items[i][0], relevance=round(scores[i], 12), citation=items[i][1], excerpt_sha256=digest(items[i][0]))
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
    p.add_argument("--provider", choices=("lexical", "lsa", "http"), default="lsa")
    p.add_argument("--top-k", type=int, default=5)
    p.add_argument("--expand", action="append", default=[])
    p.add_argument("--strict-glossary", action="store_true")
    p.add_argument("--no-auto-expand", action="store_true")
    p.add_argument("--expand-for", nargs=2, action="append", default=[], metavar=("SOURCE", "TERM"))
    p.add_argument("--embedding-url")
    p.add_argument("--embedding-model")
    p.add_argument("--allow-remote-embedding", action="store_true")
    a = p.parse_args(argv)
    try:
        if not a.query.strip() or len(a.expand) > 12 or any(not x.strip() or len(x) > 96 for x in a.expand):
            raise ValueError("invalid query expansion")
        scorer = score
        if a.provider == "http":
            scorer = http_scorer(a.embedding_url, a.embedding_model, a.allow_remote_embedding)
        elif a.embedding_url or a.embedding_model or a.allow_remote_embedding:
            raise EmbeddingError("Embedding settings require --provider http.")
        check_paths(a.state, a.project_root)
        api = ServiceBackend(a.state, a.project_root, extensions_enabled=True)
        refs = [(k, key) for k in ("doc", "note", "pdf") for key in getattr(a, k)]
        auto = not refs
        creds = {}
        if a.doc_token:
            creds["doc"] = read_token_file(a.doc_token)
        if a.kb_token:
            if not a.task:
                raise ValueError("task required")
            creds["note"] = creds["pdf"] = read_token_file(a.kb_token)
        if not creds:
            raise ValueError("credential required")
        identities = [principal(a.state, a.project_root, t) for t in set(creds.values())]
        if len({(p["project_id"], p["principal_id"], p.get("agent_id"))
                for p in identities}) != 1:
            raise ValueError("mixed recipient identities")
        configured = material_glossary.rules(a.state, identities[0]["project_id"])
        query, expansion = material_glossary.expand(a.query, configured, a.expand, a.expand_for,
                                                   a.strict_glossary, not a.no_auto_expand)
        if auto:
            refs = discover(api, creds, a.task)
        res = packet(lambda k, key: load(api, creds[k], k, key, a.task), refs, query,
                     a.provider, a.top_k, scorer=scorer, automatic=auto)
        res["expansion"] = expansion
        if auto and discover(api, creds, a.task) != refs:
            raise ValueError("discovery changed during search")
    except EmbeddingError as exc:
        print(str(exc), file=sys.stderr)
        return 2
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
