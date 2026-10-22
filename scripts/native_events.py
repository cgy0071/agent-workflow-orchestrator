"""Native turn evidence, independent of process exit and worker orchestration."""
import hashlib
import json


def inspect_turn(harness, raw):
    if harness not in ("codex", "pi"):
        raise ValueError("unsupported native completion contract")
    data = raw if isinstance(raw, bytes) else raw.encode("utf-8")
    raw = data.decode("utf-8", errors="replace")
    result = {"status": "incomplete", "text": "", "native_session_id": None,
              "provider": None, "model": None,
              "output_sha256": hashlib.sha256(data).hexdigest()}
    completed = False
    pending_error = False
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue  # Includes a truncated final JSON event; it cannot complete a turn.
        if not isinstance(event, dict): continue
        kind = event.get("type")
        if kind == "error" and harness == "codex":
            # Codex also emits error events during transport retries. Only a
            # subsequent completed turn can establish that it recovered.
            pending_error = True
            result.setdefault("diagnostics", []).append(event.get("message", "native error"))
        elif kind in ("error", "turn.failed"):
            result.update(status="failed", error="native harness returned a failure event")
        if kind in ("turn.started", "agent_start"):
            completed = False
            result["text"] = ""
        if harness == "codex":
            if kind == "thread.started": result["native_session_id"] = event.get("thread_id")
            item = event.get("item") or {}
            if kind == "item.completed" and item.get("type") == "agent_message":
                result["text"] = item.get("text", "")
            if kind == "turn.completed":
                completed = True
                pending_error = False
        else:
            if kind == "session": result["native_session_id"] = event.get("id")
            message = event.get("message") or {}
            if message.get("role") == "assistant":
                for key in ("provider", "model"):
                    if message.get(key): result[key] = message[key]
                if kind == "message_end":
                    if message.get("stopReason") in ("error", "aborted"):
                        result.update(status="failed", error="native assistant turn did not complete")
                    result["text"] = "".join(item.get("text", "") for item in message.get("content", [])
                                             if isinstance(item, dict) and item.get("type") == "text")
            if kind == "agent_end": completed = True
    if pending_error and result["status"] != "failed":
        result.update(status="failed", error="native harness error without subsequent turn completion")
    if result["status"] != "failed":
        if completed and isinstance(result["text"], str) and result["text"].strip():
            result["status"] = "completed"
        else:
            result["error"] = "missing native completion event or final assistant message"
    return result


def extract(harness, raw):
    turn = inspect_turn(harness, raw)
    if turn["status"] != "completed": raise ValueError(turn["error"])
    return turn["text"], turn["native_session_id"]
