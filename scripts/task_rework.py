"""Prepare one linked follow-up from the exact independently reviewed commit."""
import hashlib
import json
from pathlib import Path

import project_entry as e
from task_evidence import checksum


def snapshot(plan):
    d = plan["derived"]
    root = Path(d["paths"]["root"])
    state = Path(d["paths"]["state"])
    j = e.load_journal(root / "run.json") or {}
    if j.get("status") != "REPAIR_REQUIRED":
        raise ValueError("Only a sealed repair decision permits rework.")
    assignment = e._latest_assignment(state, d)
    records = e._ledger_reviews(state, j["event_id"])
    if len(records) != 1 or records[0]["verdict"] != "repair":
        raise ValueError("No unique repair decision.")
    commit = j["source_commit"]
    r = e.validate_ledger_review(plan, state, j["event_id"], assignment, records[0], commit)
    w = Path(d["paths"]["workspace"])
    ws = e._workspace_row(state, assignment["workspace_id"])
    if (Path(ws["path"]).resolve() != w.resolve() or ws["base_commit"] != d["base"]
            or e.git_text(w, "rev-parse", "HEAD") != commit):
        raise ValueError("Reviewed workspace ownership, base or HEAD changed.")
    dirty = e.status_paths(w)
    if dirty:
        raise ValueError("Working tree conflict; left unchanged: " + json.dumps(dirty))
    e._validate_source_evidence(plan, w, j)
    patch = e.git(w, "diff", "--binary", d["base"], commit, "--")["stdout"]
    patch_path = e._review_out_dir(plan, "code") / "patch.diff"
    if patch_path.read_bytes() != patch:
        raise ValueError("Reviewed patch differs from committed changes.")
    evidence = Path(d["paths"]["evidence"])
    result = Path(d["paths"]["delivery"]) / "RESULT.md"
    row = e._one(state, "SELECT sealed_sha256 FROM v2_evidence WHERE assignment_id=? "
                 "AND path=? AND kind='artifact' AND sealed=1",
                 (assignment["assignment_id"], str(result.resolve())))
    if not row or e.sha_file(result) != row["sealed_sha256"]:
        raise ValueError("Delivered RESULT.md differs from the evidence ledger.")
    return dict(task=d["task_id"], attempt=d["attempt_id"], plan_sha256=plan["plan_sha256"],
                event=j["event_id"], base=d["base"], commit=commit,
                repository=str(w), patch_path=str(patch_path),
                result_path=str(result), result_sha256=row["sealed_sha256"],
                verify_path=str(evidence / "verify-1.json"),
                receipt_path=str(evidence / "worker-receipt.json"),
                patch_sha256=hashlib.sha256(patch).hexdigest(), review_path=r["path"],
                review_sha256=r["sha256"], verify_sha256=e.sha_file(evidence / "verify-1.json"),
                receipt_sha256=e.sha_file(evidence / "worker-receipt.json"))


def prepare(plan, fresh_route=None):
    from awo_app_operations import handle
    d = plan["derived"]
    source = snapshot(plan)
    root = Path(d["paths"]["root"])
    area = root / "rework"
    child = area / "run"
    spec = e.read_json(root / "spec.json")
    material = bool(spec.get("evidence_route") or d.get("route"))
    if material:
        from evidence_confirmation import validate, load, resolve
        if not fresh_route:
            raise PermissionError("Provide a new evidence route and task grants for rework.")
        validate(fresh_route)
        old = spec.get("evidence_route")
        if not old or any(fresh_route[k] != old[k] for k in ("state", "root", "project_id")) or fresh_route["task"] == old["task"]:
            raise PermissionError("A new scoped task in the same evidence project is required.")
        packet = dict(load(plan))
        packet.pop("packet_sha256")
        packet["task_id"] = fresh_route["task"]
        packet["packet_sha256"] = checksum(packet)
        for role in ("worker", "reviewer"):
            resolve(fresh_route, role, packet)
        spec["evidence_route"] = fresh_route
    elif fresh_route:
        raise ValueError("Unexpected evidence route.")
    receipt = area / "link.json"
    if receipt.is_file():
        saved = e.read_json(receipt)
        prepared = e.load_plan(child / "plan.json")
        if (saved["previous"] != source or prepared["plan_sha256"] != saved["child_plan"]
                or prepared["spec"].get("evidence_route") != fresh_route):
            raise ValueError("Historical snapshot or child plan changed.")
        return saved, True
    # Exclusive reservation; an interrupted preparation is inspected, never replaced.
    review = Path(source["review_path"]).read_text(encoding="utf-8")
    reason = review.partition("Independent reviewer: ")[2].split("\n```", 1)[0].strip()
    if not reason:
        raise ValueError("Missing review instructions.")
    # For protected material never reuse free text from the old review or prompt.
    items = (protected_items(review) if material
             else [line.strip() for line in reason.splitlines() if line.strip()])
    reviewed_paths = {p for change in e.name_status(Path(source["repository"]), source["base"],
                                                    source["commit"]) for p in change[1:]}
    scope = [p for p in d["allowed_paths"] if p in reviewed_paths]
    if not scope:
        raise ValueError("No reviewed path within the original authorized scope.")
    spec.update(repository=d["paths"]["workspace"], work_root=str(child), allowed_paths=scope,
                target_ref="refs/heads/rework-" + source["review_sha256"][:16],
                rework_origin={"snapshot_sha256": checksum(source)},
                goal="Address only these independent review findings:\n" +
                     "\n".join(f"{i}. {v}" for i, v in enumerate(items, 1)))
    spec["worker"].pop("session_mode", None)
    try:
        area.mkdir(exist_ok=False)
    except FileExistsError:
        raise ValueError("Rework already being prepared or interrupted; inspect the existing attempt.") from None
    e.save_json(area / "spec.json", spec)
    handle("prepare", ["--thread", str(child), "--spec", str(area / "spec.json")])
    new = e.load_plan(child / "plan.json")
    if new["derived"]["base"] != source["commit"] or snapshot(plan) != source:
        raise ValueError("Parent changed during preparation; inspect the reserved attempt.")
    link = dict(previous=source, child_plan=new["plan_sha256"], nonce=new["derived"]["nonce"],
                thread=str(child), items=items, dispatched=False)
    e.write_new(receipt, json.dumps(link, sort_keys=True))
    return link, False


def protected_items(review):
    """Only pass positional references; a reviewer's free text may quote a source."""
    import re
    _, sep, tail = review.rpartition("\n```json\n")
    data = json.loads(tail.rsplit("\n```", 1)[0]) if sep else {}
    items = []
    for check in data.get("checks", []):
        c, ref = check.get("claim_id"), check.get("evidence_id")
        if (check.get("supported") is not True and isinstance(c, str) and
                isinstance(ref, str) and re.fullmatch(r"C[1-9][0-9]{0,2}", c) and
                re.fullmatch(r"E[1-9][0-9]?", ref)):
            items.append(f"Recheck {c} against independently resolved excerpt {ref}.")
    return items or ["Rebuild the missing or unsupported evidence chain using the authorized excerpts."]


def validate_child(plan):
    root = Path(plan["derived"]["paths"]["root"])
    if root.name != "run" or root.parent.name != "rework":
        raise ValueError("Rework plan outside its linked attempt.")
    link = e.read_json(root.parent / "link.json")
    parent = e.load_plan(root.parent.parent / "plan.json")
    source = snapshot(parent)
    if (source != link["previous"] or checksum(source) != plan["spec"]["rework_origin"]["snapshot_sha256"]
            or link["child_plan"] != plan["plan_sha256"] or link["nonce"] != plan["derived"]["nonce"]
            or plan["derived"]["base"] != source["commit"]):
        raise ValueError("Historical evidence or rework binding changed before start.")
    if "evidence_route" in plan["spec"]:
        from evidence_confirmation import load, resolve
        packet = load(plan)
        if packet["items"] != load(parent)["items"]:
            raise ValueError("Rework requires the reviewed selection.")
        for role in ("worker", "reviewer"):
            resolve(plan["spec"]["evidence_route"], role, packet)


def linked_child(plan):
    area = Path(plan["derived"]["paths"]["root"]) / "rework"
    saved = e.read_json(area / "link.json")
    child = area / "run" / "plan.json"
    prepared = e.load_plan(child)
    if (prepared["plan_sha256"] != saved["child_plan"] or
            prepared["spec"]["rework_origin"]["snapshot_sha256"] != checksum(saved["previous"])
            or saved["nonce"] != prepared["derived"]["nonce"]
            or saved["previous"]["plan_sha256"] != plan["plan_sha256"]):
        raise ValueError("Rework link no longer matches this run.")
    return child, saved


def card(plan):
    child, saved = linked_child(plan)
    status = e.status(child)["continuity"]
    return {"previous_commit": saved["previous"]["commit"], "previous_review": "repair",
            "state": status["state"], "review": status["review"], "nonce": saved["nonce"]}


def native_resume(root, config, payload, broker=None):
    """Require a live, exclusive session attestation for this exact destination."""
    import sqlite3
    import subprocess
    import task_session
    child = e.load_plan(root.parent / "plan.json")
    d = child["derived"]
    if ("rework_origin" not in child["spec"] or Path(d["paths"]["worker"]).resolve() != root.resolve()
            or (d["attempt_id"], d["nonce"]) != (payload["task_id"], payload["nonce"])
            or Path(payload["execution_cwd"]).resolve() != Path(d["paths"]["workspace"]).resolve()
            or config["identity"] != d["worker_spec"]["expected_identity"]):
        raise ValueError("Unexpected rework executor binding")
    if ("evidence_route" in child["spec"] or d.get("route") or "kb_context" in config
            or config["harness"] != "codex"):
        return None
    parent = e.load_plan(root.parent.parent.parent / "plan.json")
    if parent["derived"].get("route") or "evidence_route" in parent["spec"]:
        return None
    try:
        original = task_session.binding(parent)
        original_root = Path(parent["derived"]["paths"]["worker"])
        prior_job = original_root / "jobs" / task_session.task_key(
            {"task_id": original["attempt"], "nonce": original["nonce"]}) / "job.json"
        if ("kb_context" in e.read_json(original_root / "config.json")
                or e.read_json(prior_job).get("kb_context_required")):
            return None
        identity = dict(original, destination={"project": d["project_id"], "task": d["task_id"],
                        "nonce": d["nonce"], "instance": d["worker"]["instance_id"],
                        "workspace": str(Path(payload["execution_cwd"]).resolve())})
        reply = (broker or task_session.host)("inspect-rework", identity)
        if (reply.get("binding") == identity and reply.get("exclusive") is True
                and reply.get("isolated_workspace") is True and reply.get("no_private_context") is True):
            return original["session"]
    except (OSError, KeyError, ValueError, TypeError, sqlite3.Error, subprocess.SubprocessError):
        pass
    return None
