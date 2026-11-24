"""Native turn evidence, independent of process exit and worker orchestration."""
import hashlib
import json
import math

CLAUDE_DS_MODEL = "ccs-claude-kedaya-ai-ds-2--deepseek-flash"
CLAUDE_DEFAULT_TOOL_TURNS = 12


def valid_tool_turns(value):
    return type(value) is int and 1 <= value <= 64


def valid_cost(value):
    return (type(value) is int and value >= 0 or
            type(value) is float and math.isfinite(value) and value >= 0)

CLAUDE_TOOL_PROFILES = {
    "project_worker": ("Read", "Edit", "Write", "Grep", "Glob"),
    "review_only": ("Read", "Grep", "Glob"),
}


def inspect_turn(harness, raw):
    if harness not in ("codex", "pi", "omp", "claude"):
        raise ValueError("unsupported native completion contract")
    data = raw if isinstance(raw, bytes) else raw.encode("utf-8")
    if harness == "omp":
        return inspect_omp(data)
    if harness == "claude":
        return inspect_claude(data)
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
        elif kind in ("error", "turn.failed") or (harness == "claude" and kind == "result" and event.get("is_error") is True):
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
        elif harness == "claude":
            if kind == "result":
                result["text"] = event.get("result", "")
                result["provider"] = "kedaya"
                models = event.get("modelUsage") or {}
                result["model"] = next(iter(models), None) if isinstance(models, dict) else None
                if event.get("session_id"):
                    result["native_session_id"] = event["session_id"]
                if event.get("is_error") is False:
                    completed = True
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


def inspect_claude(data):
    """Accept Claude's final JSON envelope only when DS identity and cost exist."""
    result = {"status": "incomplete", "text": "", "native_session_id": None,
              "provider": None, "model": None,
              "output_sha256": hashlib.sha256(data).hexdigest()}
    try:
        event = json.loads(data.decode("utf-8"))
    except (UnicodeError, ValueError):
        result.update(status="failed", error="invalid Claude JSON result")
        return result
    if not isinstance(event, dict) or event.get("type") != "result":
        result.update(status="failed", error="Claude result envelope is missing")
        return result
    if event.get("is_error") is not False:
        result.update(status="failed", error="Claude reported an unsuccessful result")
        return result
    text = event.get("result")
    usage = event.get("modelUsage")
    cost = event.get("total_cost_usd")
    if not isinstance(text, str) or not text.strip() or not isinstance(usage, dict) or len(usage) != 1:
        result.update(status="failed", error="Claude result lacks text or a single model identity")
        return result
    model = next(iter(usage))
    if model != CLAUDE_DS_MODEL:
        result.update(status="failed", error="Claude model identity is not the pinned Kedaya DS Flash model")
        return result
    if event.get("session_id") is not None and (not isinstance(event["session_id"], str)
                                                  or not event["session_id"].strip()):
        result.update(status="failed", error="Claude session identity is invalid")
        return result
    if not valid_cost(cost):
        result.update(status="failed", error="Claude reported missing or invalid turn cost")
        return result
    result.update(status="completed", text=text, provider="kedaya", model=model,
                  native_session_id=event.get("session_id"), reported_cost_usd=cost)
    return result


def extract(harness, raw):
    turn = inspect_turn(harness, raw)
    if turn["status"] != "completed": raise ValueError(turn["error"])
    return turn["text"], turn["native_session_id"]
