"""Bind a native one-shot prompt to the current, authenticated KB task."""
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path

import awo_service as service
import kb_host_bridge as bridge
from kb_injection_budget import Tokenizer
from kb_packet_injection import PacketBudgetHost


def input_digest(job):
    fields = {k: job.get(k) for k in ("task_id", "nonce", "prompt", "cwd", "execution_cwd", "sandbox")}
    raw = json.dumps(fields, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    return sha256(raw).hexdigest()


def _tokenizer(spec):
    if spec is None:
        return None
    from tokenizers import Tokenizer as LocalTokenizer
    path = Path(spec["file"])
    if not path.is_absolute():
        raise ValueError("expected absolute tokenizer file")
    raw = path.read_bytes()
    digest = sha256(raw).hexdigest()
    if digest != spec["sha256"]:
        raise ValueError("approved tokenizer changed")
    tokenizer = LocalTokenizer.from_str(raw.decode("utf-8"))
    return Tokenizer(spec["name"], digest, lambda blob: len(tokenizer.encode(
        blob.decode("utf-8"), add_special_tokens=False).ids))


def prepare(config, job):
    if config["mode"] != "one-shot":
        raise ValueError("KB execution requires a fresh one-shot context")
    settings = config["kb_context"]
    state = Path(settings["state"])
    project_root = Path(settings["project_root"])
    token_path = Path(settings["token_file"])
    if not all(p.is_absolute() for p in (state, project_root, token_path)):
        raise ValueError("KB context configuration requires absolute paths")
    deployed = bridge.deployment(state)
    if not deployed or "kb.packet" not in deployed[1]:
        raise PermissionError("KB packet deployment unavailable")

    def current():
        with closing(service._connect(service._db_path(state), readonly=True)) as con:
            con.execute("BEGIN")
            bound = bridge._binding(con, state, str(project_root), token_path.read_text().strip(),
                                    "kb.packet", job["task_id"], deployed)
            project, actor, task, reader = bound
            spec = bridge._config(state)["projects"][project]["tasks"][job["task_id"]]["execution"]
            if spec["input_sha256"] != input_digest(job):
                raise PermissionError("KB executor input differs from registered task")
            scope = reader._scope(actor, project, task["profiles"])
            notes = reader._notes(actor, task) if getattr(reader, "note_refs", None) is not None else frozenset()
            return (actor, task, reader.root.resolve(), scope, notes, spec), reader

    identity, reader = current()
    actor, task, _, _, _, spec = identity
    injection = PacketBudgetHost(reader, _tokenizer(spec.get("tokenizer"))).prepare(actor, task)
    if any(injection.receipt[k]["status"] == "over" for k in ("core_tokens", "profile_tokens")):
        raise ValueError("KB context exceeds the configured injection budget")
    if spec.get("require_measured", False) and injection.receipt["profile_tokens"]["status"] != "within":
        raise ValueError("KB context requires a measured token budget")
    if current()[0] != identity:
        raise PermissionError("KB executor scope changed during preparation")
    prompt = job["prompt"] + "\n\n" + injection.full_bytes.decode("utf-8")
    return prompt, dict(injection.receipt, task_id=job["task_id"], input_sha256=spec["input_sha256"])
