"""Native turn evidence, independent of process exit and worker orchestration."""
import hashlib
import json


def inspect_turn(harness, raw):
    if harness not in ("codex", "pi", "omp"):
        raise ValueError("unsupported native completion contract")
    data = raw if isinstance(raw, bytes) else raw.encode("utf-8")
    if harness == "omp":
        return inspect_omp(data)
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


def inspect_omp(data):
    """Validate OMP's JSON print stream, including its terminal agent_end."""
    result = {"status": "incomplete", "text": "", "native_session_id": None,
              "provider": None, "model": None,
              "output_sha256": hashlib.sha256(data).hexdigest()}
    try:
        raw = data.decode("utf-8")
        if not raw.endswith("\n"):
            raise ValueError("JSONL stream has no complete final line")
        events = [json.loads(line) for line in raw.splitlines()]
        if not events or any(not isinstance(item, dict) for item in events):
            raise ValueError("JSONL stream contains a non-event")
    except (UnicodeError, ValueError) as exc:
        result.update(status="failed", error="invalid OMP JSONL: " + str(exc))
        return result
    terminal = None
    for event in events:
        kind = event.get("type")
        if kind == "session": result["native_session_id"] = event.get("id")
        if kind in ("error", "turn.failed"):
            result.update(status="failed", error="OMP reported an error event")
        if kind == "agent_start": terminal = None
        if kind == "agent_end": terminal = event
    if result["status"] == "failed": return result
    if terminal is None:
        result["error"] = "missing OMP agent_end event"
        return result
    if terminal.get("isTerminal") is not True:
        result["error"] = "OMP agent_end is not terminal"
        return result
    messages = terminal.get("messages")
    if not isinstance(messages, list):
        result["error"] = "OMP agent_end lacks messages"
        return result
    assistants = [item for item in messages if isinstance(item, dict) and item.get("role") == "assistant"]
    if not assistants:
        result["error"] = "OMP agent_end lacks assistant response"
        return result
    message = assistants[-1]
    if message.get("stopReason") != "stop":
        result.update(status="failed", error="OMP assistant did not stop normally")
        return result
    content = message.get("content")
    if not isinstance(content, list):
        result["error"] = "OMP assistant content is missing"
        return result
    result["text"] = "".join(item.get("text", "") for item in content
                             if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str))
    result["provider"] = message.get("provider")
    result["model"] = message.get("model")
    if result["text"].strip():
        result["status"] = "completed"
    else:
        result["error"] = "OMP final assistant response is empty"
    return result


def extract(harness, raw):
    turn = inspect_turn(harness, raw)
    if turn["status"] != "completed": raise ValueError(turn["error"])
    return turn["text"], turn["native_session_id"]
