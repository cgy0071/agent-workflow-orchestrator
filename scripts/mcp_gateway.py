"""Bounded legacy MCP stdio adapter for the AWO application service.

No database access or business authorization lives here. Production service
calls run in killable subprocesses; tests may inject a protocol-only backend.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid


PROTOCOL_VERSION = "2025-11-25"
MAX_INPUT = 1024 * 1024
MAX_OUTPUT = 2 * 1024 * 1024
MAX_WORKER_INPUT = MAX_INPUT + 32768
MAX_TOKEN = 16384
MAX_DEPTH = 32
P0_TOOLS = frozenset({
    "awo_task_get", "awo_task_list", "awo_task_create", "awo_task_next",
    "awo_assignment_claim", "awo_assignment_get", "awo_assignment_complete",
    "awo_agent_register", "awo_agent_heartbeat", "awo_evidence_record",
    "awo_review_submit", "awo_resource_claim", "awo_resource_release",
})
MESSAGE_TOOLS = frozenset({"awo_message_send", "awo_message_read", "awo_message_ack"})
READ_TOOLS = frozenset({"awo_task_get", "awo_task_list", "awo_task_next",
                        "awo_assignment_get", "awo_message_read"})
ERROR_MESSAGES = {
    "unauthenticated": "Authentication failed.",
    "project_mismatch": "The credential does not authorize this project.",
    "forbidden_role": "The principal may not perform this operation.",
    "not_found": "The requested entity is unavailable.",
    "revision_conflict": "Read the current revision before changing the command.",
    "idempotency_conflict": "The command ID was already used for different input.",
    "state_conflict": "The current state does not permit this operation.",
    "resource_exhausted": "Capacity is currently unavailable.",
    "uncertain_send": "The external outcome is unknown; do not automatically resend.",
    "invalid_arguments": "Arguments do not match the tool schema.",
    "invalid_request": "Invalid application request.",
    "command_id_required": "A command ID is required.",
    "timeout": "The call deadline expired; reconcile using the original command ID.",
    "cancelled": "The call was cancelled; reconcile using the original command ID.",
    "unavailable": "The service API or complete tool catalog is unavailable.",
    "response_too_large": "The service response exceeds the transport limit.",
    "internal": "The service could not complete this request.",
}


class GatewayError(ValueError):
    """Safe, bounded transport error; never include arbitrary exception text."""

    def __init__(self, code, *, uncertain=False):
        self.code = code if isinstance(code, str) and code in ERROR_MESSAGES else "internal"
        self.uncertain = bool(uncertain)
        self.trace_id = uuid.uuid4().hex
        super().__init__(ERROR_MESSAGES[self.code])

    def as_dict(self):
        return {"code": self.code, "message": str(self),
                "retryable": not self.uncertain and self.code in
                {"revision_conflict", "resource_exhausted"},
                "uncertain": self.uncertain, "trace_id": self.trace_id}


def safe_error(exc):
    if isinstance(exc, GatewayError):
        return exc
    # Service errors opt into stable machine codes; raw strings are not echoed.
    code = getattr(exc, "code", "internal")
    return GatewayError(code, uncertain=code == "uncertain_send")


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _json_shape(value, depth=0):
    if depth > MAX_DEPTH:
        raise ValueError("JSON nesting limit")
    if isinstance(value, dict):
        for key, item in value.items():
            key.encode("utf-8", "strict")
            _json_shape(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _json_shape(item, depth + 1)
    elif isinstance(value, str):
        value.encode("utf-8", "strict")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("nonfinite JSON number")


def decode_json(data, limit=MAX_INPUT):
    if len(data) > limit:
        raise ValueError("input limit")
    def invalid_constant(_):
        raise ValueError("nonfinite JSON number")
    value = json.loads(data.decode("utf-8", "strict"), object_pairs_hook=_pairs,
                       parse_constant=invalid_constant)
    _json_shape(value)
    return value


def encode_json(value, limit=MAX_OUTPUT):
    _json_shape(value)
    data = json.dumps(value, ensure_ascii=True, allow_nan=False,
                      separators=(",", ":")).encode("ascii")
    if len(data) > limit:
        raise GatewayError("response_too_large")
    return data


def valid_id(value):
    return ((isinstance(value, str) and len(value) <= 128)
            or (type(value) is int and -(2 ** 53) < value < 2 ** 53))


SCHEMA_KEYS = frozenset({
    "$schema", "$id", "$defs", "$ref", "type", "description", "title",
    "default", "examples", "properties", "required", "additionalProperties",
    "minProperties", "maxProperties", "items", "minItems", "maxItems",
    "uniqueItems", "minLength", "maxLength", "pattern", "format", "minimum",
    "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "enum",
    "const", "anyOf", "oneOf", "allOf", "not",
})


def check_schema(schema, root=None, depth=0):
    """Fail closed on schema keywords this stdlib validator does not implement."""
    if type(schema) is bool:
        return
    if not isinstance(schema, dict) or depth > MAX_DEPTH or set(schema) - SCHEMA_KEYS:
        raise GatewayError("unavailable")
    root = schema if root is None else root
    types = schema.get("type", [])
    types = [types] if isinstance(types, str) else types
    if (not isinstance(types, list) or any(t not in
            ("object", "array", "string", "integer", "number", "boolean", "null") for t in types)):
        raise GatewayError("unavailable")
    for key in ("minProperties", "maxProperties", "minItems", "maxItems", "minLength", "maxLength"):
        if key in schema and (type(schema[key]) is not int or schema[key] < 0):
            raise GatewayError("unavailable")
    for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"):
        if key in schema and (type(schema[key]) not in (int, float) or not math.isfinite(schema[key])):
            raise GatewayError("unavailable")
    if "multipleOf" in schema and schema["multipleOf"] <= 0:
        raise GatewayError("unavailable")
    for key in ("required", "enum", "allOf", "anyOf", "oneOf"):
        if key in schema and not isinstance(schema[key], list):
            raise GatewayError("unavailable")
    if any(not isinstance(key, str) for key in schema.get("required", [])):
        raise GatewayError("unavailable")
    if "$ref" in schema:
        ref = schema["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/"):
            raise GatewayError("unavailable")
        target = root
        try:
            for part in ref[2:].split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
            check_schema(target, root, depth + 1)
        except (KeyError, TypeError):
            raise GatewayError("unavailable") from None
    if "pattern" in schema:
        try:
            re.compile(schema["pattern"])
        except (re.error, TypeError):
            raise GatewayError("unavailable") from None
    if schema.get("format") not in (None, "date-time", "uri", "uri-reference"):
        raise GatewayError("unavailable")
    for key in ("properties", "$defs"):
        for child in schema.get(key, {}).values():
            check_schema(child, root, depth + 1)
    for key in ("items", "additionalProperties", "not"):
        if key in schema:
            check_schema(schema[key], root, depth + 1)
    for key in ("allOf", "anyOf", "oneOf"):
        for child in schema.get(key, []):
            check_schema(child, root, depth + 1)


def validate_schema(value, schema, root=None, depth=0):
    """Validate the explicitly supported JSON Schema subset, without coercion."""
    def fail():
        raise GatewayError("invalid_arguments")
    if depth > MAX_DEPTH or schema is False:
        fail()
    if schema is True:
        return
    root = schema if root is None else root
    if "$ref" in schema:
        target = root
        for part in schema["$ref"][2:].split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        validate_schema(value, target, root, depth + 1)
    types = {"object": isinstance(value, dict), "array": isinstance(value, list),
             "string": isinstance(value, str), "integer": type(value) is int,
             "number": type(value) in (int, float), "boolean": type(value) is bool,
             "null": value is None}
    wanted = schema.get("type")
    if wanted and not any(types.get(t, False) for t in
                          (wanted if isinstance(wanted, list) else [wanted])):
        fail()
    # JSON boolean and number equality must not inherit Python's True == 1.
    same = lambda a, b: (encode_json(a) == encode_json(b))
    if "const" in schema and not same(value, schema["const"]):
        fail()
    if "enum" in schema and not any(same(value, x) for x in schema["enum"]):
        fail()
    for key in ("allOf", "anyOf", "oneOf", "not"):
        if key not in schema:
            continue
        options = [schema[key]] if key == "not" else schema[key]
        matches = 0
        for option in options:
            try:
                validate_schema(value, option, root, depth + 1)
                matches += 1
            except GatewayError:
                pass
        if ((key == "allOf" and matches != len(options)) or
                (key == "anyOf" and matches == 0) or
                (key == "oneOf" and matches != 1) or (key == "not" and matches)):
            fail()
    if isinstance(value, dict):
        if (len(value) < schema.get("minProperties", 0) or
                len(value) > schema.get("maxProperties", MAX_INPUT) or
                any(key not in value for key in schema.get("required", []))):
            fail()
        props = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            validate_schema(item, props.get(key, extra), root, depth + 1)
    elif isinstance(value, list):
        if (len(value) < schema.get("minItems", 0) or
                len(value) > schema.get("maxItems", MAX_INPUT)):
            fail()
        if schema.get("uniqueItems") and len({encode_json(x) for x in value}) != len(value):
            fail()
        for item in value:
            validate_schema(item, schema.get("items", True), root, depth + 1)
    elif isinstance(value, str):
        if (len(value) < schema.get("minLength", 0) or
                len(value) > schema.get("maxLength", MAX_INPUT) or
                ("pattern" in schema and not re.search(schema["pattern"], value))):
            fail()
        if schema.get("format") == "date-time":
            try:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}[Tt].+[Zz]|\d{4}-\d{2}-\d{2}[Tt].+[+-]\d{2}:\d{2}", value):
                    fail()
                datetime.fromisoformat(value.replace("z", "+00:00").replace("Z", "+00:00"))
            except ValueError:
                fail()
        if schema.get("format") == "uri" and not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value):
            fail()
        if schema.get("format") in ("uri", "uri-reference") and re.search(r"[\s\x00-\x1f]", value):
            fail()
    elif type(value) in (int, float):
        if (value < schema.get("minimum", -math.inf) or
                value > schema.get("maximum", math.inf) or
                value <= schema.get("exclusiveMinimum", -math.inf) or
                value >= schema.get("exclusiveMaximum", math.inf)):
            fail()
        if "multipleOf" in schema and value % schema["multipleOf"] != 0:
            fail()


def validate_catalog(specs, messages_enabled=False):
    expected = P0_TOOLS | (MESSAGE_TOOLS if messages_enabled else frozenset())
    if (not isinstance(specs, list) or len(specs) != len(expected)
            or any(not isinstance(s, dict) for s in specs)
            or {s.get("name") for s in specs} != expected):
        raise GatewayError("unavailable")
    for spec in specs:
        schema = spec.get("inputSchema")
        if (not isinstance(spec.get("description"), str) or
                not isinstance(schema, dict) or schema.get("type") != "object" or
                not isinstance(spec.get("annotations"), dict)):
            raise GatewayError("unavailable")
        if schema.get("additionalProperties") is not False:
            raise GatewayError("unavailable")
        props = schema.get("properties", {})
        if spec["name"] in READ_TOOLS:
            if "command_id" in props or spec["annotations"].get("readOnlyHint") is not True:
                raise GatewayError("unavailable")
        else:
            required = {"command_id", "expected_epoch"}
            if spec["name"] not in {"awo_task_create", "awo_agent_register", "awo_message_send"}:
                required.add("expected_revision")
            if (not required.issubset(props) or not required.issubset(schema.get("required", []))
                    or spec["annotations"].get("readOnlyHint") is not False):
                raise GatewayError("unavailable")
        check_schema(schema)
    encode_json(specs)
    return specs


def validate_tool_call(specs, tool, arguments):
    if not isinstance(tool, str) or not isinstance(arguments, dict):
        raise GatewayError("invalid_arguments")
    spec = next((s for s in specs if s["name"] == tool), None)
    if spec is None:
        raise GatewayError("invalid_arguments")
    validate_schema(arguments, spec["inputSchema"])


def read_token_file(path):
    try:
        with Path(path).open("rb") as stream:
            data = stream.read(MAX_TOKEN + 1)
        if not data or len(data) > MAX_TOKEN:
            raise ValueError("token size")
        token = data.decode("utf-8", "strict").strip()
        if not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise ValueError("token characters")
        return token
    except (OSError, ValueError):
        raise GatewayError("unauthenticated") from None


class ServiceBackend:
    """Only production backend: invoke unmodified awo_service in a subprocess."""

    def __init__(self, state, project_root, *, messages_enabled=False, extensions_enabled=False,
                 call_timeout=30.0, max_calls=8):
        if not 0 < call_timeout <= 30 or not 1 <= max_calls <= 64:
            raise ValueError("invalid call limits")
        self.state = str(state)
        self.project_root = str(project_root)
        self.messages_enabled = bool(messages_enabled)
        self.extensions_enabled = bool(extensions_enabled)
        self.call_timeout = float(call_timeout)
        self._slots = threading.BoundedSemaphore(max_calls)

    def _invoke(self, operation, token, tool=None, arguments=None, cancel=None):
        if not isinstance(token, str) or not token or len(token) > MAX_TOKEN:
            raise GatewayError("unauthenticated")
        if cancel is not None and cancel.is_set():
            raise GatewayError("cancelled")
        request = {"operation": operation, "state": self.state,
                   "project_root": self.project_root, "token": token,
                   "messages_enabled": self.messages_enabled,
                   "extensions_enabled": self.extensions_enabled,
                   "tool": tool, "arguments": arguments}
        try:
            wire = encode_json(request, MAX_WORKER_INPUT) + b"\n"
        except GatewayError:
            raise GatewayError("invalid_request") from None
        if not self._slots.acquire(blocking=False):
            raise GatewayError("resource_exhausted")
        process = None
        deadline = time.monotonic() + self.call_timeout
        started = False
        try:
            process = subprocess.Popen(
                [sys.executable, "-B", str(Path(__file__).resolve()), "--service-worker"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            started = True
            first = True
            while True:
                reason = ("cancelled" if cancel is not None and cancel.is_set()
                          else "timeout" if time.monotonic() >= deadline else None)
                if reason:
                    raise GatewayError(reason, uncertain=operation == "call")
                try:
                    out, _ = process.communicate(wire if first else None,
                                                 timeout=min(.05, max(.001, deadline - time.monotonic())))
                    break
                except subprocess.TimeoutExpired:
                    first = False
            if process.returncode != 0 or len(out) > MAX_OUTPUT:
                raise GatewayError("internal", uncertain=operation == "call")
            # Worker output limit is larger than the client input limit.
            result = json.loads(out.decode("utf-8"), object_pairs_hook=_pairs)
            if not isinstance(result, dict) or type(result.get("ok")) is not bool:
                raise ValueError("worker result")
            if not result["ok"]:
                error = result.get("error", {})
                raise GatewayError(error.get("code"), uncertain=error.get("uncertain", False))
            return result["result"]
        except GatewayError:
            raise
        except Exception:
            raise GatewayError("internal", uncertain=started and operation == "call") from None
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)
            self._slots.release()

    def principal(self, token):
        return self._invoke("principal", token)

    def specs(self, token):
        specs = self._invoke("specs", token)
        if not self.extensions_enabled:
            return validate_catalog(specs, self.messages_enabled)
        expected = P0_TOOLS | (MESSAGE_TOOLS if self.messages_enabled else frozenset())
        validate_catalog([s for s in specs if s['name'] in expected], self.messages_enabled)
        for spec in specs:
            check_schema(spec['inputSchema'])
        return specs

    def call(self, token, tool, arguments, cancel=None):
        return self._invoke("call", token, tool, arguments, cancel)


def service_worker():
    """Private one-shot child. Secrets use stdin; service output is suppressed."""
    invoked = False
    try:
        data = sys.stdin.buffer.readline(MAX_WORKER_INPUT + 1)
        request = decode_json(data, MAX_WORKER_INPUT)
        if not isinstance(request, dict):
            raise GatewayError("invalid_request")
        with open(os.devnull, "w", encoding="utf-8") as sink:
            with redirect_stdout(sink), redirect_stderr(sink):
                import awo_service as service
                if not all(callable(getattr(service, name, None)) for name in
                           ("gateway_call", "gateway_principal", "gateway_tool_specs")):
                    raise GatewayError("unavailable")
                args = (request["state"], request["project_root"], request["token"])
                try:
                    principal = service.gateway_principal(*args)
                except service.ServiceError as exc:
                    # The service already rejected authentication. This is error
                    # mapping only: never inspect a credential or exception prose.
                    raise GatewayError(getattr(exc, "code", "unauthenticated")) from None
                if not isinstance(principal, dict) or not principal:
                    raise GatewayError("unauthenticated")
                operation = request["operation"]
                if operation == "principal":
                    result = {"authenticated": True}
                elif operation in ("specs", "call"):
                    specs = validate_catalog(service.gateway_tool_specs(
                        messages_enabled=request["messages_enabled"]), request["messages_enabled"])
                    if request.get('extensions_enabled') is True:
                        import service_extensions
                        specs += service_extensions.tool_specs()
                    if operation == "specs":
                        result = specs
                    else:
                        validate_tool_call(specs, request["tool"], request["arguments"])
                        invoked = True
                        if request["tool"] in P0_TOOLS | MESSAGE_TOOLS:
                            result = service.gateway_call(*args, request["tool"], request["arguments"])
                        else:
                            result = service_extensions.call(*args, request["tool"], request["arguments"])
                else:
                    raise GatewayError("invalid_request")
        response = {"ok": True, "result": result}
        wire = encode_json(response)
        if request["token"].encode("ascii") in wire:
            raise GatewayError("internal", uncertain=invoked)
    except Exception as exc:
        error = safe_error(exc)
        if invoked and error.code in ("internal", "response_too_large"):
            error.uncertain = True
        wire = encode_json({"ok": False, "error": error.as_dict()})
    sys.stdout.buffer.write(wire + b"\n")
    sys.stdout.buffer.flush()
    return 0


def rpc_error(request_id, code, message, data=None):
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def tool_result(result=None, error=None):
    structured = {"error": error.as_dict()} if error else result
    # MCP structuredContent must be an object.
    if not isinstance(structured, dict):
        structured = {"result": structured}
    text = encode_json(structured).decode("ascii")
    return {"content": [{"type": "text", "text": text}],
            "structuredContent": structured, "isError": error is not None}


class ProtocolSession:
    """One legacy MCP connection, with bounded asynchronous tool invocations."""

    def __init__(self, backend, token, emit, *, max_calls=8):
        self.backend, self.token, self.emit = backend, token, emit
        self.max_calls = max_calls
        self.initializing = False
        self.initialized = False
        self.closed = False
        self.pending = {}
        self.tool_names = set()
        self.lock = threading.RLock()

    def _send(self, response):
        with self.lock:
            if not self.closed:
                try:
                    encode_json(response)
                    self.emit(response)
                except GatewayError as exc:
                    self.emit(rpc_error(response.get("id"), -32603, "Response limit exceeded.",
                                        GatewayError(exc.code, uncertain=True).as_dict()))
                except (OSError, ValueError):
                    self.closed = True
                    for cancel, _ in self.pending.values():
                        cancel.set()

    def handle(self, message):
        if self.closed:
            return
        if (not isinstance(message, dict) or message.get("jsonrpc") != "2.0"
                or not isinstance(message.get("method"), str)
                or set(message) - {"jsonrpc", "id", "method", "params"}
                or ("id" in message and not valid_id(message["id"]))):
            self._send(rpc_error(None, -32600, "Invalid Request"))
            return
        request_id = message.get("id")
        method, params = message["method"], message.get("params", {})
        notification = "id" not in message
        if not isinstance(params, dict):
            if not notification:
                self._send(rpc_error(request_id, -32602, "Invalid params"))
            return
        if notification:
            if (method == "notifications/initialized" and self.initializing
                    and not set(params) - {"_meta"}):
                self.initialized = True
            elif method == "notifications/cancelled" and valid_id(params.get("requestId")):
                with self.lock:
                    pending = self.pending.get(params["requestId"])
                    if pending:
                        pending[0].set()
            return  # Notifications never produce responses or execute tools.
        with self.lock:
            if request_id in self.pending:
                self._send(rpc_error(request_id, -32600, "Request ID already active"))
                return
        try:
            if method == "initialize":
                if (self.initializing or not isinstance(params.get("protocolVersion"), str)
                        or not params["protocolVersion"] or len(params["protocolVersion"]) > 64
                        or not isinstance(params.get("capabilities"), dict)
                        or not isinstance(params.get("clientInfo"), dict)
                        or not isinstance(params["clientInfo"].get("name"), str)
                        or not isinstance(params["clientInfo"].get("version"), str)):
                    self._send(rpc_error(request_id, -32602, "Invalid initialization"))
                    return
                specs = self.backend.specs(self.token)  # authenticates and verifies complete catalog
                self.tool_names = {spec["name"] for spec in specs}
                self.initializing = True
                result = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
                          "serverInfo": {"name": "awo-gateway", "version": "1.0.0"}}
            elif method == "ping":
                self.backend.principal(self.token)
                result = {}
            elif not self.initialized:
                self._send(rpc_error(request_id, -32002, "Initialization required"))
                return
            elif method == "tools/list":
                if set(params) - {"_meta"}:
                    self._send(rpc_error(request_id, -32602, "Invalid params"))
                    return
                result = {"tools": self.backend.specs(self.token)}
                self.tool_names = {spec["name"] for spec in result["tools"]}
            elif method == "tools/call":
                if (set(params) - {"name", "arguments", "_meta"}
                        or not isinstance(params.get("name"), str)
                        or not isinstance(params.get("arguments", {}), dict)):
                    self._send(rpc_error(request_id, -32602, "Invalid params"))
                    return
                if params["name"] not in self.tool_names:
                    self._send(rpc_error(request_id, -32602, "Unknown tool"))
                    return
                with self.lock:
                    if len(self.pending) >= self.max_calls:
                        raise GatewayError("resource_exhausted")
                    cancel = threading.Event()
                    worker = threading.Thread(target=self._call, args=(request_id, params, cancel),
                                              daemon=True, name="awo-mcp-call")
                    self.pending[request_id] = (cancel, worker)
                    worker.start()
                return
            else:
                self._send(rpc_error(request_id, -32601, "Method not found"))
                return
            self._send({"jsonrpc": "2.0", "id": request_id, "result": result})
        except Exception as exc:
            self._send(rpc_error(request_id, -32000, "Service request failed", safe_error(exc).as_dict()))

    def _call(self, request_id, params, cancel):
        try:
            result = tool_result(self.backend.call(self.token, params["name"],
                                                  params.get("arguments", {}), cancel))
        except Exception as exc:
            result = tool_result(error=safe_error(exc))
        # Cancellation is a notification: suppress a late response when observed.
        # It does not establish rollback of a service commit or cancel an Assignment.
        if not cancel.is_set():
            self._send({"jsonrpc": "2.0", "id": request_id, "result": result})
        with self.lock:
            self.pending.pop(request_id, None)

    def close(self):
        with self.lock:
            self.closed = True
            workers = list(self.pending.values())
            for cancel, _ in workers:
                cancel.set()
        deadline = time.monotonic() + 6
        for _, worker in workers:
            worker.join(max(0, deadline - time.monotonic()))


def run_stdio(backend, token, source=None, sink=None):
    source = sys.stdin.buffer if source is None else source
    sink = sys.stdout.buffer if sink is None else sink
    def emit(message):
        sink.write(encode_json(message) + b"\n")
        sink.flush()
    session = ProtocolSession(backend, token, emit)
    try:
        while True:
            line = source.readline(MAX_INPUT + 1)
            if not line:
                break
            if len(line) > MAX_INPUT:
                emit(rpc_error(None, -32600, "Input limit exceeded"))
                return 2  # Do not drain an unbounded peer stream.
            try:
                message = decode_json(line)
            except (ValueError, UnicodeError, RecursionError):
                emit(rpc_error(None, -32700, "Parse error"))
                continue
            session.handle(message)
    finally:
        session.close()
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["--service-worker"]:
        return service_worker()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--token-file", default=os.environ.get("AWO_AGENT_TOKEN_FILE"))
    parser.add_argument("--messages-enabled", action="store_true")
    parser.add_argument("--call-timeout", type=float, default=30)
    args = parser.parse_args(argv)
    try:
        if not args.token_file:
            raise GatewayError("unauthenticated")
        token = read_token_file(args.token_file)
        backend = ServiceBackend(args.state, args.project_root,
                                 messages_enabled=args.messages_enabled, call_timeout=args.call_timeout)
        backend.principal(token)  # No unauthenticated stdio lifecycle.
        return run_stdio(backend, token)
    except (Exception, KeyboardInterrupt) as exc:
        code = safe_error(exc).code
        sys.stderr.write("awo-gateway: " + code + "\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
