"""Render an authorized KB index as compact, sorted-key UTF-8 JSON v1.

The delivered object contains only terms and judgments. JSON uses literal
Unicode, no BOM, no optional whitespace and no trailing newline. The task
envelope, packet digest and pulled source evidence are outside the injection.
Deliver full_bytes directly; core_bytes is the corresponding core-only block.

Host integration (after access.authenticate, require_scope, project match,
deployment admission, and the v2_task body SHA-256 check in kb_host_bridge):

    host = PacketBudgetHost(reader, Tokenizer("local-tokenizer", version, count_bytes))
    receipt = host.inject(principal["principal_id"], task_from_bound_body,
                          append_context_bytes)

The host owns the tokenizer and downstream callback. append_context_bytes
receives one immutable UTF-8 block synchronously. Use a fresh binding for
each delivery. Store the returned measurement receipt with its task ID.
Budgets report within/over/unmeasured; install a host budget policy separately
if dispatch requires enforcement. No model call is required for measurement.
"""

from copy import copy, deepcopy
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
import json

from awo_kb.service import Reader
from kb_injection_budget import Tokenizer, measure_injection_budget


def _json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _render(packet):
    index = packet["index"]
    fields = {"terms": {"id", "canonical", "definition", "forbidden", "note_refs"},
              "judgments": {"id", "holder", "stance", "subject", "source_ref", "as_of",
                            "review_by", "locator", "stale", "disputes", "summary", "scope",
                            "detail_required"}}
    if not isinstance(index, dict) or index.keys() != fields.keys():
        raise ValueError("unsupported KB injection index")
    for kind, allowed in fields.items():
        if not isinstance(index[kind], list):
            raise ValueError("invalid KB injection rows")
        for row in index[kind]:
            if not isinstance(row, dict) or not row.keys() <= allowed:
                raise ValueError("unexpected KB injection field")
            for key, value in row.items():
                if key in {"disputes", "forbidden", "note_refs"}:
                    valid = isinstance(value, list) and all(isinstance(v, str) for v in value)
                elif key in {"stale", "detail_required"}:
                    valid = isinstance(value, bool)
                else:
                    valid = isinstance(value, str)
                if not valid:
                    raise ValueError("invalid KB injection field")
    return _json_bytes(index)


@dataclass(frozen=True)
class Injection:
    core_bytes: bytes
    full_bytes: bytes
    receipt: dict


@dataclass(frozen=True)
class PacketBudgetHost:
    reader: Reader
    tokenizer: Tokenizer | None = None

    def inject(self, actor, task, append_context_bytes, /, *, today=None):
        """Hand the measured bytes to the host's context builder and return the receipt."""
        if not callable(append_context_bytes):
            raise TypeError("expected host context callback")
        prepared = self.prepare(actor, task, today=today)
        append_context_bytes(prepared.full_bytes)
        return prepared.receipt

    def prepare(self, actor, task, /, *, today=None):
        if not isinstance(task, dict) or set(task) != {"project", "profiles", "kb_revision"}:
            raise ValueError("expected project, profiles and kb_revision")
        task = deepcopy(task)
        today = date.today().isoformat() if today is None else today
        if date.fromisoformat(today).isoformat() != today:
            raise ValueError("expected ISO calendar date")
        core_task = dict(task, profiles=[])
        # Each packet reads the same private policy snapshot. A transient grant
        # cannot widen its content between the access check and store read.
        policy = deepcopy(self.reader.current_policy())
        snapshot = copy(self.reader)
        snapshot.current_policy = lambda: policy
        def note_grants():
            provider = getattr(self.reader, "note_refs", None)
            return self.reader._notes(actor, task) if provider is not None else frozenset()

        notes = note_grants()
        if getattr(snapshot, "note_refs", None) is not None:
            # The core projection is derived locally, not a separately registered task.
            snapshot.note_refs = lambda who, pinned: notes if who == actor and pinned in (core_task, task) else ()

        def scopes(reader):
            return [reader._scope(actor, t["project"], t["profiles"]) for t in (core_task, task)]

        authorized = scopes(snapshot)

        def reauthorize():
            if scopes(self.reader) != authorized or note_grants() != notes:
                raise PermissionError("KB read scope changed during preparation")

        reauthorize()
        core = _render(snapshot.packet(actor, core_task, today))
        reauthorize()
        full = _render(snapshot.packet(actor, task, today))
        scope_id = sha256(_json_bytes({"actor": actor, "project": task["project"],
                                       "read_projects": authorized[1][0],
                                       "profiles": sorted(set(task["profiles"]))})).hexdigest()
        reauthorize()
        receipt = measure_injection_budget(core, full, kb_revision=task["kb_revision"],
                                           profiles=task["profiles"], scope_id=scope_id,
                                           tokenizer=self.tokenizer)
        reauthorize()
        return Injection(core, full, receipt)
