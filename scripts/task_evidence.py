"""Shared read-only material search and exact task evidence. No persistent cache."""
import hashlib
import json
import re

import material_packets as materials
from service_access import principal


def checksum(value):
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                     allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[\w.-]{1,160}", value):
        raise ValueError("unsupported identifier")
    return value


def sha(value):
    if not isinstance(value, str) or not re.fullmatch("[a-f0-9]{64}", value):
        raise ValueError("invalid SHA-256")
    return value


def reference(text, cite):
    authority = cite["authority"]
    if authority == "awo":
        key = cite["doc_id"]
        revision = {"sha256": sha(cite["revision_sha256"])}
        locator = {"char_start": cite["char_start"], "char_end": cite["char_end"]}
    elif authority == "awo-kb":
        key = cite["record_id"]
        revision = {"version": identifier(cite["version"]), "file_sha256": sha(cite["file_sha256"])}
        revision["kb_revision"] = sha(cite["kb_revision"])
        # The immutable SRC record binds its byte range or unique PDF occurrence.
        pdf = cite["verification"] == "kb.evidence.pdf"
        label = cite["locator"]
        locator = {"kind": "pdf" if pdf else "note", "record_locator_sha256": materials.digest(label)}
        if pdf:
            match = re.fullmatch(r"p\.([1-9]\d{0,8})", label)
            if not match:
                raise ValueError("unsupported PDF page locator")
            locator["physical_page"] = int(match[1])
        elif re.fullmatch(r"line [1-9]\d*(?:-[1-9]\d*)?", label):
            locator["position"] = label
    else:
        raise ValueError("unknown authority")
    return dict(authority=authority, source_project_id=identifier(cite["project_id"]),
                source_id=identifier(key), pinned_revision=revision, locator=locator,
                excerpt_sha256=materials.digest(text))


class KnowledgeReadFacade:
    def __init__(self, api, credentials, task=None):
        self.api = api
        self.credentials = dict(credentials)
        self.task = identifier(task) if task is not None else None

    def identity(self):
        identities = set()
        for kind, token in self.credentials.items():
            who = principal(self.api.state, self.api.project_root, token)
            if kind not in ("doc", "note", "pdf") or (not self.task and kind != "doc"):
                raise PermissionError("task required for plugin sources")
            if who.get("task_id") and who["task_id"] != self.task:
                raise PermissionError("credential outside read scope")
            identities.add((who["project_id"], who["principal_id"], who.get("agent_id")))
        if len(identities) != 1:
            raise PermissionError("one recipient required")
        return identities.pop()[0]

    def read(self, kind, key, *, precise=True):
        if kind not in self.credentials:
            raise PermissionError("source unavailable")
        return materials.load(self.api, self.credentials[kind], kind, key, self.task, precise=precise)

    def search(self, query, refs=None, provider="lsa", limit=5, *, scorer=materials.score, legacy=False):
        self.identity()
        auto = refs is None
        if auto:
            refs = materials.discover(self.api, self.credentials, self.task)
        reader = lambda kind, key: self.read(kind, key, precise=not legacy)
        result = materials.packet(reader, refs, query, provider, limit, scorer=scorer,
                                  automatic=auto)
        if auto and materials.discover(self.api, self.credentials, self.task) != refs:
            raise ValueError("discovery changed during search")
        return result

    @staticmethod
    def selection(hits):
        if not 1 <= len(hits) <= 32:
            raise ValueError("select 1 to 32 excerpts")
        items = []
        for hit in hits:
            ref = reference(hit["text"], hit["citation"])
            if ref["excerpt_sha256"] != hit["excerpt_sha256"] or ref in items:
                raise ValueError("invalid or duplicate selection")
            items.append(ref)
        return [dict(evidence_id=f"E{i}", **ref) for i, ref in enumerate(items, 1)]

    def exact(self, item, project):
        if item["source_project_id"] != project:
            raise PermissionError("foreign source")
        kind = "doc" if item["authority"] == "awo" else item["locator"]["kind"]
        key = identifier(item["source_id"])
        for text, cite in self.read(kind, key):
            expected = dict(evidence_id=item["evidence_id"], **reference(text, cite))
            if checksum(expected) == checksum(item):
                return text
        raise ValueError("pinned revision, locator or excerpt unavailable")

    def pin(self, hits, draft_digest, expected_selection_digest):
        project = self.identity()
        if not self.task:
            raise PermissionError("task required to freeze evidence")
        items = self.selection(hits)
        if checksum(items) != expected_selection_digest:
            raise ValueError("selection differs from preview")
        for item in items:
            self.exact(item, project)
        body = dict(schema="awo.task-evidence.v1", project_id=identifier(project),
                    task_id=self.task, draft_digest=sha(draft_digest), items=items)
        return dict(**body, packet_sha256=checksum(body))

    def resolve(self, manifest, expected_packet_digest):
        project = self.identity()
        body = dict(manifest)
        if (body.pop("packet_sha256") != expected_packet_digest or
                checksum(body) != sha(expected_packet_digest)):
            raise ValueError("packet digest mismatch")
        if (set(body) != {"schema", "project_id", "task_id", "draft_digest", "items"} or
                body["schema"] != "awo.task-evidence.v1" or not self.task or
                body["project_id"] != project or body["task_id"] != self.task):
            raise PermissionError("unexpected evidence scope")
        sha(body["draft_digest"])
        if not 1 <= len(body["items"]) <= 32:
            raise ValueError("invalid evidence count")
        result = []
        for i, item in enumerate(body["items"], 1):
            if item["evidence_id"] != f"E{i}":
                raise ValueError("invalid evidence order")
            result.append(dict(evidence_id=item["evidence_id"], text=self.exact(item, project)))
        return result
