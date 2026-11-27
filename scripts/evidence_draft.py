"""Protocol 1 evidence-draft: transient preview and target-authorized draft."""
import argparse
import json
from pathlib import Path
import sys

import material_glossary
from mcp_gateway import ServiceBackend, read_token_file
from contextlib import closing
import awo_service as service
import service_access
from service_access import principal
from task_evidence import KnowledgeReadFacade, checksum


def target(config):
    api = ServiceBackend(config["state"], config["root"], extensions_enabled=True)
    token = read_token_file(config["task_token_file"])
    with closing(service._connect(service._db_path(api.state), readonly=True)) as con:
        who = service_access.authenticate(con, api.project_root, token)
        task = config["task"]
        if not task or who.get("task_id") != task or who["project_id"] != config["project_id"]:
            raise PermissionError("Target task credential required.")
        service_access.require_scope(con, who, task, who.get("assignment_id"))
        body = con.execute("SELECT body FROM v2_task WHERE project_id=? AND task_id=?",
                           (who["project_id"], task)).fetchone()[0]
    credentials = {}
    allowed = []
    if "doc_token_file" in config:
        doc = read_token_file(config["doc_token_file"])
        ident = principal(api.state, api.project_root, doc)
        if (ident["project_id"], ident["principal_id"]) != (who["project_id"], who["principal_id"]):
            raise PermissionError("Different target document recipient.")
        credentials["doc"] = doc
        grants = json.loads(body or "{}").get("evidence_document_ids", [])
        if not isinstance(grants, list) or not all(isinstance(key, str) for key in grants):
            raise ValueError("Invalid task document grants.")
        allowed = [key for key in grants if ident.get("knowledge_id") in (None, key)]
    if config.get("kb", False):
        credentials.update(note=token, pdf=token)
    facade = KnowledgeReadFacade(api, credentials, task)
    facade.identity()
    return facade, allowed


def personal(facade, ref, mapping):
    """Bind a personal selection only through an independently authorized KB record."""
    from task_evidence import identifier, sha
    if set(ref) != {"authority", "source_id", "pinned_revision", "locator", "excerpt_sha256"}:
        raise ValueError("Unexpected personal reference fields.")
    identifier(ref["source_id"])
    sha(ref["excerpt_sha256"])
    if set(ref["pinned_revision"]) != {"sha256"}:
        raise ValueError("Unexpected personal revision fields.")
    kind, key = mapping["kind"], mapping["record_id"]
    if kind not in ("note", "pdf"):
        raise ValueError("Expected a task source record.")
    from kb_host_bridge import source_position
    from task_evidence import reference
    for text, cite in facade.read(kind, key):
        current = reference(text, cite)
        if (current["pinned_revision"]["file_sha256"] != ref["pinned_revision"]["sha256"]
                or current["excerpt_sha256"] != ref["excerpt_sha256"]):
            continue
        pos = source_position(facade.api, facade.credentials[kind], facade.task, kind, key,
                              current["pinned_revision"]["kb_revision"])
        if checksum(ref["locator"]) != checksum(pos):
            continue
        return current
    raise ValueError("Target excerpt revision or location changed.")


def handle(config, request):
    facade, allowed = target(config)
    project = facade.identity()
    scope = {"project_id": project, "task_id": facade.task}
    if "expected_target" in request and request["expected_target"] != scope:
        raise PermissionError("Target changed after preview.")
    if "query" in request:
        rules = material_glossary.rules(facade.api.state, project)
        q, expansion = material_glossary.expand(request["query"], rules, [], [], False, True)
        refs = [("doc", key) for key in allowed]
        if "note" in facade.credentials:
            from kb_host_bridge import material_grants
            refs.extend(material_grants(facade.api.state, facade.api.project_root,
                                        facade.credentials["note"], facade.task))
        from material_packets import packet
        hits = packet(facade.read, refs, q, "lexical", automatic=True)["materials"]
        if target(config)[1] != allowed:
            raise PermissionError("Target document grant changed.")
        refs = facade.selection(hits) if hits else []
        return {"status": "preview" if hits else "no_hits", "dispatched": False, "target": scope,
                "expansion": expansion, "items": [dict(ref=r, text=h["text"]) for r,h in zip(refs,hits)]}

    picked = request["selected"]
    if not request["goal"].strip() or not 1 <= len(picked) <= 32:
        raise ValueError("Choose a goal and one to 32 original excerpts.")
    items, origins = [], []
    for i, incoming in enumerate(picked, 1):
        ref = dict(incoming)
        ref.pop("evidence_id", None)
        if ref["authority"] == "personal":
            origin = ref
            ref = personal(facade, origin, config.get("personal_sources", {})[origin["source_id"]])
            origins.append({"evidence_id": f"E{i}", "source": origin})
        if ref["authority"] == "awo" and ref["source_id"] not in allowed:
            raise PermissionError("Document outside target task grant.")
        if ref["authority"] not in ("awo", "awo-kb") or ref in items:
            raise ValueError("Unsupported or duplicate source.")
        items.append(ref)
    items = [dict(evidence_id=f"E{i}", **ref) for i,ref in enumerate(items, 1)]
    excerpts = [facade.exact(item, project) for item in items]
    if target(config)[1] != allowed:
        raise PermissionError("Target task grant changed.")
    draft = dict(schema="maid.evidence-draft.v1", project_id=project, task_id=facade.task,
                 goal=request["goal"], items=items, personal_origins=origins,
                 selection_sha256=checksum(items), dispatched=False)
    return dict(status="draft", draft=dict(**draft, draft_sha256=checksum(draft)), excerpts=excerpts)


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = handle(json.loads(args.target.read_text(encoding="utf-8")), json.load(sys.stdin))
        print(json.dumps(result))
        return 0
    except Exception as exc:
        print("Draft blocked: verify target task read permission, source revision and exact location. "
              f"({type(exc).__name__})", file=sys.stderr)
        return 2
