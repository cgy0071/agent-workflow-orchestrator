"""Allocate task-specific delivery directories and preserve ledger-owned evidence."""
import copy
import hashlib
import json
from pathlib import Path


def task_key(request):
    raw = json.dumps([request["task_id"], request["nonce"]], ensure_ascii=True).encode()
    return "task-" + hashlib.sha256(raw).hexdigest()[:24]


def delivery_paths(request):
    cwd = Path(request["cwd"]).resolve()
    completion = request["completion"]
    items = completion.get("artifacts", []) + [completion["binding_artifact"]]
    paths = {(cwd / (item if isinstance(item, str) else item["path"])).resolve() for item in items}
    if request.get("review_path"):
        paths.add(Path(request["review_path"]).resolve())
    return paths


def owned_paths(con):
    owned = []
    for row in con.execute("SELECT id,data FROM tasks"):
        request = json.loads(row["data"])["request"]
        owned.extend((row["id"], path) for path in delivery_paths(request))
    for row in con.execute("SELECT data FROM reviews"):
        review = json.loads(row[0])
        owned.append((review["task_id"], Path(review["path"]).resolve()))
    for row in con.execute("SELECT task,data FROM events"):
        receipt = json.loads(row["data"]).get("receipt") or {}
        for item in receipt.get("artifacts", []) + [receipt.get("binding_artifact", {})]:
            if item.get("path"):
                owned.append((row["task"], Path(item["path"]).resolve()))
    return owned


def check_paths(con, request):
    from workflow_scheduler import overlap
    for owner, old in owned_paths(con):
        if owner != request["task_id"] and any(overlap(old, new) for new in delivery_paths(request)):
            raise ValueError("evidence path is permanently owned by task " + owner + ": " + str(old))


def check_review_path(con, task, path):
    from workflow_scheduler import overlap
    for owner, old in owned_paths(con):
        if owner != task and overlap(old, path):
            raise ValueError("review path is owned by another task: " + owner)


def allocate(request, root):
    """Prepare a new request before freezing; never relocate a registered request."""
    from monitor_task import validate_request
    validate_request(request, require_dispatch=False)
    if request["dispatch_confirmed"]:
        raise ValueError("allocate paths before dispatch confirmation")
    result = copy.deepcopy(request)
    old_cwd = Path(request["cwd"]).resolve()
    directory = Path(root).resolve() / task_key(request)
    directory.mkdir(parents=True, exist_ok=False)
    result["cwd"] = str(directory)
    completion = result["completion"]
    for key in ("binding_artifact", "artifacts"):
        items = completion.get(key, []) if key == "artifacts" else [completion[key]]
        updated = []
        for item in items:
            value = {"path": item} if isinstance(item, str) else dict(item)
            value["path"] = (old_cwd / value["path"]).resolve().relative_to(old_cwd).as_posix()
            updated.append(value["path"] if isinstance(item, str) else value)
        completion[key] = updated if key == "artifacts" else updated[0]
    completion.setdefault("marker", "TASK_DONE " + request["task_id"] + " " + request["nonce"])
    completion.setdefault("marker_source", "artifact")
    result["review_path"] = str(directory / "REVIEW.md")
    result["evidence_layout"] = {"version": 1, "key": task_key(request)}
    validate_request(result, require_dispatch=False)
    return result


def prepare_graph(graph, root):
    from workflow_scheduler import validate_dag
    validate_dag(graph)
    result = copy.deepcopy(graph)
    for node in result["nodes"]:
        node["request"] = allocate(node["request"], root)
        for key, value in (("{task_cwd}", node["request"]["cwd"]),
                           ("{review_path}", node["request"]["review_path"]),
                           ("{marker}", node["request"]["completion"]["marker"])):
            node["message"] = node["message"].replace(key, value)
    return result


def check_graph_paths(graph):
    from workflow_scheduler import overlap
    prior = []
    for node in graph["nodes"]:
        paths = delivery_paths(node["request"])
        for owner, old in prior:
            if any(overlap(old, new) for new in paths):
                raise ValueError("DAG evidence paths overlap between " + owner + " and " + node["id"])
        prior.extend((node["id"], path) for path in paths)
