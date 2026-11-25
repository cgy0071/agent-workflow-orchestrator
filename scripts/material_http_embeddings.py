"""Explicit, single-destination embedding scorer. Standard library only."""
import http.client
import json
import math
import os
from urllib.parse import urlsplit


class EmbeddingError(ValueError):
    pass


def http_scorer(endpoint, model, allow_remote=False):
    u = urlsplit(endpoint or "")
    local = u.hostname in ("127.0.0.1", "::1", "localhost")
    if (not model or not model.strip() or not u.hostname or u.username is not None
            or u.password is not None or u.query or u.fragment or not u.path.endswith("/v1/embeddings")
            or not (local and u.scheme in ("http", "https") or allow_remote and u.scheme == "https")):
        raise EmbeddingError("Invalid embedding configuration; remote HTTPS requires --allow-remote-embedding.")

    def rank(texts, query, provider):
        body = json.dumps({"model": model, "input": [query, *texts], "encoding_format": "float"})
        headers = {"Content-Type": "application/json"}
        if key := os.environ.get("AWO_EMBEDDING_API_KEY"):
            headers["Authorization"] = "Bearer " + key
        cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        conn = cls(u.hostname, u.port, timeout=15)
        try:
            conn.request("POST", u.path, body, headers)
            response = conn.getresponse()
            if response.status != 200:
                raise ValueError
            raw = response.read(32 * 1024**2 + 1)
            if len(raw) > 32 * 1024**2:
                raise ValueError
            rows = json.loads(raw)["data"]
            count = len(texts) + 1
            if not isinstance(rows, list) or len(rows) != count:
                raise ValueError
            vectors = {}
            for row in rows:
                idx, v = row["index"], row["embedding"]
                if (type(idx) is not int or not 0 <= idx < count or idx in vectors
                        or not isinstance(v, list) or not 1 <= len(v) <= 16384
                        or not all(type(x) in (float, int) and math.isfinite(x) for x in v)):
                    raise ValueError
                length = math.hypot(*v)
                if not length or not math.isfinite(length):
                    raise ValueError
                vectors[idx] = [x / length for x in v]
            q = vectors[0]
            if any(len(v) != len(q) for v in vectors.values()):
                raise ValueError
            return [sum(x*y for x, y in zip(q, vectors[i])) for i in range(1, count)]
        except (OSError, http.client.HTTPException, ValueError, TypeError, KeyError, OverflowError) as exc:
            raise EmbeddingError("Embedding service unavailable or invalid response; no fallback performed.") from exc
        finally:
            conn.close()
    return rank
