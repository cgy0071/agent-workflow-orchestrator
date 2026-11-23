"""Select a manager and export an explicitly shared, published document revision."""
import json
from pathlib import Path

from mcp_gateway import ServiceBackend, read_token_file
from project_cli_inputs import check_paths
from project_retrieve import read_document
import service_access


def required(obj, key, kind=str):
    value = obj.get(key) if isinstance(obj, dict) else None
    if not isinstance(value, kind) or not value:
        raise ValueError("Missing or invalid routing field: " + key)
    return value


def directory(project):
    path = Path(required(project, "root"))
    if not path.is_absolute() or not path.is_dir():
        raise ValueError("Project root must be an absolute, existing directory")
    return path.resolve()


def select(catalog, project, material, spec, thread):
    """The local operator supplies the directory. Task specs cannot grant exports."""
    thread = Path(thread).resolve()
    data = json.loads(Path(catalog).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("Unsupported manager directory version")
    projects = required(data, "projects", dict)
    origin = required(projects, project, dict)
    origin_root = directory(origin)
    if Path(spec["repository"]).resolve() != origin_root:
        raise ValueError("Task repository does not match requesting project")
    # A thread contains the result clone. It cannot live in another project.
    roots = [directory(item) for item in projects.values()]
    if any(a == b or a.is_relative_to(b) or b.is_relative_to(a)
           for i, a in enumerate(roots) for b in roots[:i]):
        raise ValueError("Project roots overlap")
    if any(thread.is_relative_to(root) or root.is_relative_to(thread) for root in roots):
        raise ValueError("Conversation directory overlaps a registered project")
    receipt = {"project": project, "manager": required(origin, "manager")}
    if not material:
        return receipt, None
    export = required(required(data, "exports", dict), material, dict)
    if required(export, "recipient") != project:
        raise ValueError("Material is not shared with the requesting project")
    source_id = required(export, "source")
    source = required(projects, source_id, dict)
    source_root = directory(source)
    state = Path(required(source, "state"))
    token_file = Path(required(export, "token_file"))
    if not state.is_absolute() or not token_file.is_absolute():
        raise ValueError("Material state and credential paths must be absolute")
    state = state.resolve()
    if thread.is_relative_to(state) or state.is_relative_to(thread):
        raise ValueError("Conversation directory overlaps the material source state")
    if token_file.resolve().is_relative_to(origin_root):
        raise ValueError("Source credential must be outside the requesting repository")
    doc_id = required(export, "doc_id")
    revision = required(export, "revision_sha256")
    check_paths(state, source_root)
    token = read_token_file(token_file)
    try:
        identity = service_access.principal(state, str(source_root), token)
        if identity.get("knowledge_id") != doc_id:
            raise ValueError("Credential must be scoped to the selected document")
        body, citation = read_document(ServiceBackend(state, str(source_root), extensions_enabled=True),
                                       token, doc_id)
    except Exception as exc:
        raise ValueError("Source denied access to the selected published document") from exc
    if citation["revision_sha256"] != revision:
        raise ValueError("Published revision differs from the export grant")
    receipt["material"] = {"source": source_id, **citation}
    return receipt, body
