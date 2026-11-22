"""Offline measurement for the trusted host after authorization and rendering.

The host supplies exact rendered UTF-8 bytes and an opaque identifier for the
effective authorization scope. Configure the tokenizer in trusted host code.
The returned JSON-compatible receipt contains no injection text.
"""

from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
import json
import re


@dataclass(frozen=True)
class Tokenizer:
    name: str
    version: str
    count: Callable[[bytes], int | None]

    def __post_init__(self):
        if not all(isinstance(v, str) and v.strip() for v in (self.name, self.version)):
            raise ValueError("tokenizer name and version are required")
        if not callable(self.count):
            raise TypeError("tokenizer count must be callable")


def measure_injection_budget(core: bytes, full: bytes, *, kb_revision: str,
                             profiles: list[str] | tuple[str, ...], scope_id: str,
                             tokenizer: Tokenizer | None = None) -> dict:
    """Measure both injections exactly as received. Missing counts stay unmeasured.

    Full includes the host's rendered core and selected profiles. Tokenizer
    errors propagate to the host. The receipt SHA-256 identifies the complete
    measurement; authentication and authorization remain with the host.
    """
    if not isinstance(kb_revision, str) or not re.fullmatch("[0-9a-f]{64}", kb_revision):
        raise ValueError("expected a SHA-256 knowledge revision")
    if not isinstance(scope_id, str) or not scope_id.strip():
        raise ValueError("effective authorization scope ID is required")
    if (not isinstance(profiles, (tuple, list))
            or not all(isinstance(v, str) and v.strip() for v in profiles)):
        raise ValueError("expected a collection of profile names")
    if tokenizer is not None and not isinstance(tokenizer, Tokenizer):
        raise TypeError("expected host tokenizer configuration")
    for payload in (core, full):
        if type(payload) is not bytes:
            raise TypeError("expected immutable UTF-8 bytes")
        payload.decode("utf-8", errors="strict")

    receipt = {"kb_revision": kb_revision, "profiles": sorted(set(profiles)),
               "scope_id": scope_id, "tokenizer": None if tokenizer is None else
               {"name": tokenizer.name, "version": tokenizer.version}}
    for key, payload, limit in (("core_tokens", core, 1500), ("profile_tokens", full, 4000)):
        measured = tokenizer.count(payload) if tokenizer else None
        status = "unmeasured"
        if measured is not None:
            if type(measured) is not int or measured < 0:
                raise ValueError("tokenizer returned an invalid count")
            status = "over" if measured > limit else "within"
        receipt[key] = {"sha256": sha256(payload).hexdigest(), "limit": limit,
                        "measured": measured, "status": status}
    encoded = json.dumps(receipt, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    receipt["receipt_sha256"] = sha256(encoded).hexdigest()
    return receipt
