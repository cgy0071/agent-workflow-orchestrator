"""Project-bound query expansion; no network or knowledge-store access."""
import json
from pathlib import Path
import re
import unicodedata


# Conservative offline secretary vocabulary. Unknown concepts stay untranslated.
HINTS = {
    "\u63a8\u7406": "inference",
    "\u541e\u5410\u91cf": "throughput",
    "\u541e\u5410\u7387": "throughput",
    "\u5ef6\u8fdf": "latency",
    "\u529f\u8017": "power consumption",
    "\u5185\u5b58": "memory",
}


def norm(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def matches(term, text):
    # English aliases must match whole words; adjacent Chinese needs no spaces.
    pattern = (r"(?<![a-z0-9])" if term[0].isascii() and term[0].isalnum() else "")
    pattern += re.escape(term)
    if term[-1].isascii() and term[-1].isalnum():
        pattern += r"(?![a-z0-9])"
    return re.finditer(pattern, text)


def pairs(items):
    result = dict(items)
    if len(result) != len(items):
        raise ValueError("duplicate glossary keys")
    return result


def rules(state, project):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", project):
        raise ValueError("invalid project identifier")
    path = Path(state) / "glossaries" / (project + ".json")
    try:
        with path.open("rb") as f:
            raw = f.read(65537)
    except FileNotFoundError:
        return {}
    if len(raw) > 65536:
        raise ValueError("oversize glossary")
    rows = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    if not isinstance(rows, dict) or len(rows) > 256:
        raise ValueError("invalid glossary")
    result = {}
    for alias, rule in rows.items():
        key = norm(alias)
        if (not 1 <= len(key) <= 96 or key in result or not isinstance(rule, dict)
                or set(rule) - {"terms", "avoid"}):
            raise ValueError("invalid glossary entry")
        for name in ("terms", "avoid"):
            terms = rule.get(name, [])
            if (not isinstance(terms, list) or len(terms) > 12
                    or not all(isinstance(t, str) and 0 < len(t.strip()) <= 96 for t in terms)):
                raise ValueError("invalid glossary terms")
        if not rule.get("terms"):
            raise ValueError("missing glossary terms")
        result[key] = rule
    return result


def expand(query, glossary, free, scoped, strict, automatic):
    text = norm(query)
    if not text or len(text) > 512 or len(free) + len(scoped) > 12:
        raise ValueError("invalid expansion")
    covered = [False] * len(text)
    chosen, banned, extras = [], [], []
    for alias in sorted(glossary, key=lambda k: (-len(k), k)):
        for hit in matches(alias, text):
            a, b = hit.span()
            if any(covered[a:b]):
                continue
            covered[a:b] = [True] * (b-a)
            chosen.extend(glossary[alias]["terms"])
            banned.extend(norm(t) for t in glossary[alias].get("avoid", []))
    def forbidden(term):
        return any(any(matches(t, norm(term))) for t in banned)
    if any(forbidden(term) for term in chosen):
        raise ValueError("conflicting glossary translations")
    remaining = "".join("\0" if used else c for c, used in zip(text, covered))
    def add(term):
        if not isinstance(term, str) or not 0 < len(term.strip()) <= 96:
            raise ValueError("invalid suggestion")
        if not forbidden(term) and norm(term) not in seen and len(extras) < 12:
            extras.append(term.strip())
            seen.add(norm(term))
    canonical = {}
    for term in chosen:
        canonical.setdefault(norm(term), term.strip())
    chosen = list(canonical.values())
    seen = set(canonical)
    if not strict:
        for source, term in scoped:
            source = norm(source)
            if source and "\0" not in source and any(matches(source, remaining)):
                add(term)
        if not chosen:
            for term in free:
                add(term)
        if automatic:
            for source, term in HINTS.items():
                if any(matches(source, remaining)):
                    add(term)
    base = query
    if banned:
        base = text
        for term in sorted(set(banned), key=len, reverse=True):
            for hit in reversed(list(matches(term, base))):
                base = base[:hit.start()] + " " + base[hit.end():]
    effective = " ".join([base.strip(), *chosen, *extras])
    if len(effective) > 512:
        raise ValueError("expanded query too long")
    return effective, {"glossary": chosen, "secretary": extras, "strict": strict}
