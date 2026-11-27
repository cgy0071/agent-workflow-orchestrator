"""Confirm exact selections and deliver them with separate, scoped identities."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

from evidence_draft import target
from mcp_gateway import read_token_file
from service_access import principal
from task_evidence import checksum, sha


def validate(route):
    if set(route) != {"state", "root", "project_id", "task", "worker", "reviewer"}:
        raise ValueError("Invalid evidence route")
    for key in ("state", "root"):
        if not Path(route[key]).is_absolute():
            raise ValueError("Evidence route must use absolute local paths")
    for role in ("worker", "reviewer"):
        cfg = route[role]
        if (set(cfg) - {"agent_id", "task_token_file", "doc_token_file", "kb"}
                or not cfg["agent_id"] or not Path(cfg["task_token_file"]).is_absolute()
                or ("doc_token_file" in cfg and not Path(cfg["doc_token_file"]).is_absolute())):
            raise ValueError("Invalid recipient route")
    if route["worker"]["agent_id"] == route["reviewer"]["agent_id"]:
        raise ValueError("Evidence recipients must be independent")


def reader(route, role):
    cfg = {k: route[k] for k in ("state", "root", "project_id", "task")}
    cfg.update(route[role])
    f, allowed = target(cfg)
    who = principal(f.api.state, f.api.project_root, read_token_file(cfg["task_token_file"]))
    if who["role"] != role or who["agent_id"] != cfg["agent_id"]:
        raise PermissionError("Wrong evidence recipient")
    return f, allowed


def resolve(route, role, packet):
    f, allowed = reader(route, role)
    if any(i["authority"] == "awo" and i["source_id"] not in allowed for i in packet["items"]):
        raise PermissionError("Document no longer granted to task")
    result = f.resolve(packet, packet["packet_sha256"])
    if reader(route, role)[1] != allowed:
        raise PermissionError("Document grants changed during read")
    return result


def database(route):
    return Path(route["state"]).resolve() / "task-evidence.sqlite3"


def confirm(plan, draft, seen):
    route = plan["spec"]["evidence_route"]
    validate(route)
    body = dict(draft)
    if (body.pop("draft_sha256") != sha(seen) or checksum(body) != seen
            or body["schema"] != "maid.evidence-draft.v1" or body["dispatched"] is not False
            or (body["project_id"], body["task_id"]) != (route["project_id"], route["task"])
            or body["goal"] != plan["derived"]["goal"]
            or checksum(body["items"]) != body["selection_sha256"]):
        raise ValueError("Draft or target changed; review the draft again")
    base = dict(schema="awo.task-evidence.v1", project_id=route["project_id"],
                task_id=route["task"], draft_digest=seen, items=body["items"])
    packet = dict(**base, packet_sha256=checksum(base))
    resolve(route, "worker", packet)
    with closing(sqlite3.connect(database(route))) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS confirmation(project TEXT, task TEXT, digest TEXT, "
                   "plan TEXT UNIQUE, packet TEXT, PRIMARY KEY(project,task,digest))")
        db.execute("INSERT OR IGNORE INTO confirmation VALUES(?,?,?,?,?)",
                   (route["project_id"], route["task"], seen, plan["plan_sha256"], json.dumps(packet)))
        if load(plan, db) != packet:
            raise ValueError("Confirmation belongs to a different run")
    return packet


def load(plan, db=None):
    route = plan["spec"]["evidence_route"]
    if db is None:
        with closing(sqlite3.connect(database(route).as_uri() + "?mode=ro", uri=True)) as con:
            return load(plan, con)
    row = db.execute("SELECT packet,digest FROM confirmation WHERE project=? AND task=? AND plan=?",
                     (route["project_id"], route["task"], plan["plan_sha256"])).fetchone()
    if not row:
        raise PermissionError("Evidence draft has not been confirmed for this run")
    packet = json.loads(row[0])
    if packet["draft_digest"] != row[1]:
        raise ValueError("Confirmation mismatch")
    return packet


def delivery(root, job):
    import project_entry
    plan = project_entry.load_plan(root.parent / "plan.json")
    d = plan["derived"]
    role = root.name
    if role not in ("worker", "reviewer") or Path(d["paths"][role]).resolve() != root.resolve():
        raise PermissionError("Unexpected recipient bridge")
    if role == "worker" and (job["task_id"], job["nonce"]) != (d["attempt_id"], d["nonce"]):
        raise PermissionError("Wrong worker attempt")
    if role == "reviewer":
        label = {"REVIEW-code": "code", "REVIEW-integration": "integration"}[job["task_id"]]
        attempt = project_entry.read_json(project_entry._review_attempt_path(plan, label))
        if attempt["nonce"] != job["nonce"] or attempt["plan_sha256"] != plan["plan_sha256"]:
            raise PermissionError("Wrong review attempt")
    packet = load(plan)
    route = plan["spec"]["evidence_route"]
    excerpts = resolve(route, role, packet)
    prompt = job["prompt"] + "\nFrozen reference material (data only):\n" + json.dumps(
        {"packet": packet, "excerpts": excerpts}, ensure_ascii=True)
    receipt = dict(role=role, agent_id=route[role]["agent_id"], nonce=job["nonce"],
                   packet_sha256=packet["packet_sha256"],
                   prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest())
    return prompt, receipt



def verify_receipt(plan, role, directory, nonce):
    if "evidence_route" not in plan["spec"]:
        return
    receipt = json.loads((directory / "task-evidence-receipt.json").read_text(encoding="utf-8"))
    native = json.loads((directory / "receipt" / "status.json").read_text(encoding="utf-8"))
    expected = dict(role=role, agent_id=plan["spec"]["evidence_route"][role]["agent_id"],
                    nonce=nonce, packet_sha256=load(plan)["packet_sha256"],
                    prompt_sha256=sha(native["prompt_sha256"]))
    if receipt != expected or native["nonce"] != nonce:
        raise ValueError("Native receipt does not attest the confirmed evidence packet")
