"""Protocol fixtures are isolated tests, not service or harness certification.

RealServiceSubprocessTests exercise unmodified service APIs in child processes.
No production service function is patched; missing parent APIs fail the tests.
"""
from __future__ import annotations

import copy
import io
import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

import mcp_gateway as gateway


def protocol_specs(messages_enabled=False):
    """Deliberately small schemas for protocol-only tests, not a domain catalog."""
    specs = []
    for name in sorted(gateway.P0_TOOLS | (gateway.MESSAGE_TOOLS if messages_enabled else set())):
        read = name in gateway.READ_TOOLS
        props = {"value": {"type": "integer", "minimum": 1, "maximum": 10}}
        required = []
        if not read:
            props.update(command_id={"type": "string", "minLength": 8, "maxLength": 128},
                         expected_epoch={"type": "integer", "minimum": 0},
                         expected_revision={"type": "integer", "minimum": 1})
            required = ["command_id", "expected_epoch", "expected_revision"]
        specs.append({"name": name, "description": "Protocol-only test fixture",
                      "inputSchema": {"type": "object", "properties": props,
                                      "required": required, "additionalProperties": False},
                      "annotations": {"readOnlyHint": read, "idempotentHint": read}})
    return specs


class ProtocolOnlyBackend:
    """Injected protocol unit-test backend; never installed in production."""

    def __init__(self, invoke=None):
        self.calls = []
        self.messages_enabled = False
        self.invoke = invoke

    def principal(self, token):
        if token != "protocol-test-secret":
            raise gateway.GatewayError("unauthenticated")
        return {"authenticated": True}

    def specs(self, token):
        self.principal(token)
        return gateway.validate_catalog(protocol_specs(self.messages_enabled), self.messages_enabled)

    def call(self, token, tool, arguments, cancel=None):
        gateway.validate_tool_call(self.specs(token), tool, arguments)
        self.calls.append((tool, copy.deepcopy(arguments)))
        if self.invoke:
            return self.invoke(tool, arguments, cancel)
        return {"received": arguments}


class JsonSchemaTests(unittest.TestCase):
    def test_json_rejects_ambiguous_nonfinite_and_invalid_unicode(self):
        for wire in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e400}', b'"\xff"',
                     b'"\\ud800"', b'\xef\xbb\xbf{}', b'[' * 40 + b']' * 40):
            with self.subTest(wire=wire), self.assertRaises((ValueError, UnicodeError)):
                gateway.decode_json(wire)

    def test_json_input_and_output_are_bounded(self):
        with self.assertRaises(ValueError):
            gateway.decode_json(b" " * (gateway.MAX_INPUT + 1))
        with self.assertRaises(gateway.GatewayError):
            gateway.encode_json("x" * 100, 10)

    def test_types_required_unknown_and_ranges(self):
        spec = protocol_specs()[0]["inputSchema"]
        valid = {"command_id": "cmd-12345", "expected_epoch": 0, "expected_revision": 1, "value": 2}
        gateway.validate_schema(valid, spec)
        for update in ({"value": True}, {"value": 0}, {"value": 11}, {"value": "2"},
                       {"extra": 1}, {"expected_revision": False}, {"command_id": "x"}):
            with self.subTest(update=update), self.assertRaises(gateway.GatewayError):
                gateway.validate_schema(dict(valid, **update), spec)
        with self.assertRaises(gateway.GatewayError):
            gateway.validate_schema({}, spec)

    def test_shared_defs_ref_schema(self):
        schema = {"type": "object", "$defs": {"command": {"type": "string", "minLength": 8}},
                  "properties": {"command_id": {"$ref": "#/$defs/command"}},
                  "required": ["command_id"], "additionalProperties": False}
        gateway.check_schema(schema)
        gateway.validate_schema({"command_id": "cmd-12345"}, schema)
        with self.assertRaises(gateway.GatewayError):
            gateway.validate_schema({"command_id": 3}, schema)

    def test_unsupported_schema_does_not_silently_skip_validation(self):
        for schema in ({"type": "object", "unevaluatedProperties": False},
                       {"$ref": "https://invalid/schema"}, {"pattern": "["},
                       {"format": "unimplemented"}, {"$ref": "#/$defs/missing"}):
            with self.subTest(schema=schema), self.assertRaises(gateway.GatewayError):
                gateway.check_schema(schema)

    def test_arrays_dates_patterns_and_combinations(self):
        schema = {"type": "array", "minItems": 1, "maxItems": 2,
                  "uniqueItems": True, "items": {"type": "string", "pattern": "^[a-z]+$"}}
        gateway.validate_schema(["one", "two"], schema)
        for value in ([], ["one", "one"], ["one", "two", "three"], ["UPPER"]):
            with self.assertRaises(gateway.GatewayError):
                gateway.validate_schema(value, schema)
        gateway.validate_schema("2026-11-10T00:00:00Z", {"type": "string", "format": "date-time"})
        with self.assertRaises(gateway.GatewayError):
            gateway.validate_schema("2026-99-00", {"type": "string", "format": "date-time"})
        with self.assertRaises(gateway.GatewayError):
            gateway.validate_schema(True, {"enum": [1]})
        gateway.validate_schema(1, {"oneOf": [{"type": "integer"}, {"type": "string"}]})
        with self.assertRaises(gateway.GatewayError):
            gateway.validate_schema(1, {"not": {"type": "integer"}})

    def test_exact_catalog_and_explicit_message_gate(self):
        self.assertEqual(len(gateway.validate_catalog(protocol_specs())), 13)
        self.assertEqual(len(gateway.validate_catalog(protocol_specs(True), True)), 16)
        for specs in (protocol_specs(True), protocol_specs()[:-1],
                      protocol_specs()[:-1] + protocol_specs()[:1]):
            with self.assertRaises(gateway.GatewayError):
                gateway.validate_catalog(specs)

    def test_read_schema_never_accepts_command_id(self):
        specs = protocol_specs()
        read = next(s for s in specs if s["name"] == "awo_task_list")
        read["inputSchema"]["properties"]["command_id"] = {"type": "string"}
        with self.assertRaises(gateway.GatewayError):
            gateway.validate_catalog(specs)

    def test_no_exception_text_or_token_in_errors(self):
        error = gateway.safe_error(RuntimeError("sensitive-token-and-path"))
        self.assertEqual(error.code, "internal")
        self.assertNotIn("sensitive", json.dumps(error.as_dict()))

    def test_token_file_bounded_and_no_path_echo(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "secret"
            path.write_text("test-secret\n", encoding="utf-8")
            self.assertEqual(gateway.read_token_file(path), "test-secret")
            for token in ("", "two words", "x" * (gateway.MAX_TOKEN + 1)):
                path.write_text(token, encoding="utf-8")
                with self.assertRaises(gateway.GatewayError):
                    gateway.read_token_file(path)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.backend = ProtocolOnlyBackend()
        self.responses = queue.Queue()
        self.session = gateway.ProtocolSession(self.backend, "protocol-test-secret", self.responses.put)
        self.addCleanup(self.session.close)

    def send(self, method, params=None, request_id=1):
        message = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if request_id is not None:
            message["id"] = request_id
        self.session.handle(message)

    def initialize(self, version=gateway.PROTOCOL_VERSION):
        self.send("initialize", {"protocolVersion": version, "capabilities": {},
                                 "clientInfo": {"name": "protocol-test", "version": "1"}})
        response = self.responses.get(timeout=3)
        self.send("notifications/initialized", request_id=None)
        return response

    def test_legacy_lifecycle_and_thirteen_tools(self):
        result = self.initialize()["result"]
        self.assertEqual(result["protocolVersion"], "2025-11-25")
        self.assertEqual(result["capabilities"], {"tools": {}})
        self.send("tools/list")
        tools = self.responses.get()["result"]["tools"]
        self.assertEqual({t["name"] for t in tools}, gateway.P0_TOOLS)

    def test_future_or_modern_version_negotiates_supported_legacy(self):
        result = self.initialize("2026-11-25")["result"]
        self.assertEqual(result["protocolVersion"], gateway.PROTOCOL_VERSION)

    def test_initialized_notification_required(self):
        self.send("tools/list")
        self.assertEqual(self.responses.get()["error"]["code"], -32002)
        self.send("initialize", {"protocolVersion": gateway.PROTOCOL_VERSION, "capabilities": {},
                                 "clientInfo": {"name": "test", "version": "1"}})
        self.responses.get()
        self.send("tools/list")
        self.assertEqual(self.responses.get()["error"]["code"], -32002)

    def test_ping_is_authenticated_before_initialize(self):
        self.send("ping")
        self.assertEqual(self.responses.get()["result"], {})
        self.session.token = "wrong"
        self.send("ping")
        self.assertEqual(self.responses.get()["error"]["data"]["code"], "unauthenticated")

    def test_bad_initialization_and_duplicate_initialize(self):
        self.send("initialize", {"protocolVersion": 5})
        self.assertEqual(self.responses.get()["error"]["code"], -32602)
        self.initialize()
        self.send("initialize", {"protocolVersion": gateway.PROTOCOL_VERSION})
        self.assertEqual(self.responses.get()["error"]["code"], -32602)

    def test_notification_never_invokes_tool_or_responds(self):
        self.initialize()
        self.send("tools/call", {"name": "awo_task_list", "arguments": {}}, request_id=None)
        self.assertEqual(self.backend.calls, [])
        self.assertTrue(self.responses.empty())

    def test_unknown_methods_and_invalid_request_forms(self):
        self.initialize()
        self.send("server/discover")
        self.assertEqual(self.responses.get()["error"]["code"], -32601)
        for message in ([], {}, {"jsonrpc": "2.0", "id": True, "method": "ping"},
                        {"jsonrpc": "1.0", "id": 1, "method": "ping"}):
            self.session.handle(message)
            self.assertEqual(self.responses.get()["error"]["code"], -32600)

    def test_unknown_tool_is_protocol_error(self):
        self.initialize()
        self.send("tools/call", {"name": "awo_controller_takeover", "arguments": {}})
        self.assertEqual(self.responses.get()["error"]["code"], -32602)
        self.assertEqual(self.backend.calls, [])

    def test_empty_string_request_id_is_preserved(self):
        self.send("ping", request_id="")
        self.assertEqual(self.responses.get()["id"], "")

    def test_malformed_call_params_not_dispatched(self):
        self.initialize()
        self.send("tools/call", {"name": "awo_task_list", "arguments": []})
        self.assertEqual(self.responses.get()["error"]["code"], -32602)
        self.assertEqual(self.backend.calls, [])

    def test_schema_errors_are_structured_and_do_not_invoke(self):
        self.initialize()
        self.send("tools/call", {"name": "awo_task_list", "arguments": {"principal": "controller"}})
        result = self.responses.get(timeout=3)["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error"]["code"], "invalid_arguments")
        self.assertEqual(self.backend.calls, [])

    def test_command_id_and_revision_preserved_without_retry(self):
        self.initialize()
        arguments = {"command_id": "original-command-id", "expected_epoch": 0, "expected_revision": 4}
        self.send("tools/call", {"name": "awo_task_create", "arguments": arguments}, "correlation")
        response = self.responses.get(timeout=3)
        self.assertEqual(response["id"], "correlation")
        self.assertEqual(self.backend.calls, [("awo_task_create", arguments)])

    def test_cancellation_terminates_only_request_without_late_reply(self):
        started = threading.Event()
        finished = threading.Event()
        def wait_for_cancel(tool, args, cancel):
            started.set()
            if not cancel.wait(2):
                raise AssertionError("cancel not delivered")
            finished.set()
            raise gateway.GatewayError("cancelled", uncertain=True)
        self.backend.invoke = wait_for_cancel
        self.initialize()
        self.send("tools/call", {"name": "awo_task_list"}, 7)
        self.assertTrue(started.wait(2))
        self.send("notifications/cancelled", {"requestId": 7}, None)
        self.assertTrue(finished.wait(2))
        self.session.close()
        self.assertTrue(self.responses.empty())
        self.assertEqual(len(self.backend.calls), 1)

    def test_duplicate_active_id_and_capacity_are_bounded(self):
        entered, release = threading.Event(), threading.Event()
        def slow(tool, args, cancel):
            entered.set()
            release.wait(2)
            return {}
        self.backend.invoke = slow
        self.session.max_calls = 1
        self.initialize()
        self.send("tools/call", {"name": "awo_task_list"}, 2)
        self.assertTrue(entered.wait(2))
        self.send("tools/call", {"name": "awo_task_list"}, 2)
        self.assertEqual(self.responses.get()["error"]["code"], -32600)
        self.send("tools/call", {"name": "awo_task_list"}, 3)
        self.assertEqual(self.responses.get()["error"]["data"]["code"], "resource_exhausted")
        release.set()
        self.responses.get(timeout=3)
        self.assertEqual(len(self.backend.calls), 1)

    def test_stdio_ndjson_malformed_and_oversize(self):
        sink = io.BytesIO()
        source = io.BytesIO(b'not json\n{"jsonrpc":"2.0","id":2,"method":"ping"}\n')
        self.assertEqual(gateway.run_stdio(self.backend, "protocol-test-secret", source, sink), 0)
        lines = [json.loads(x) for x in sink.getvalue().splitlines()]
        self.assertEqual(lines[0]["error"]["code"], -32700)
        self.assertEqual(lines[1]["result"], {})
        sink = io.BytesIO()
        result = gateway.run_stdio(self.backend, "protocol-test-secret",
                                   io.BytesIO(b"x" * (gateway.MAX_INPUT + 1)), sink)
        self.assertEqual(result, 2)
        self.assertEqual(len(sink.getvalue().splitlines()), 1)


def real_fixture(test):
    """Compose ServiceAccessTests and real APIs; never bypass service setup."""
    import awo_service as service
    test.assertTrue(all(callable(getattr(service, name, None)) for name in
                        ("gateway_principal", "gateway_call", "gateway_tool_specs")),
                    "parent gateway service APIs are required")
    import test_service_access as access_tests
    access_fixture = access_tests.ServiceAccessTests()
    access_fixture.setUp()
    test.addCleanup(access_fixture.doCleanups)
    test.access_fixture = access_fixture
    fixture = access_fixture.f
    service.set_enabled(fixture.state, *fixture.actor, True)
    fixture.epoch = service.service_status(fixture.state)["controller_epoch"]
    result, token, _ = access_fixture.issue("controller")
    return fixture, Path(result["token_file"]), token, result


def task_arguments(fixture):
    return {"command_id": "cmd-" + uuid.uuid4().hex, "expected_epoch": fixture.epoch,
            "task_id": "T-" + uuid.uuid4().hex[:12], "title": "Transport regression"}


class StdioClient:
    """Real subprocess protocol driver; not an installed harness client."""

    def __init__(self, fixture, token_file):
        self.process = subprocess.Popen([sys.executable, "-B", gateway.__file__,
            "--state", str(fixture.state), "--project-root", str(fixture.root),
            "--token-file", str(token_file)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        self.responses = queue.Queue()
        self.stdout, self.sent = [], []
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        while True:
            line = self.process.stdout.readline(gateway.MAX_OUTPUT + 2)
            if not line:
                return
            self.stdout.append(line)
            self.responses.put(line)

    def send(self, value):
        wire = gateway.encode_json(value) + b"\n"
        self.sent.append(wire)
        self.process.stdin.write(wire)
        self.process.stdin.flush()

    def receive(self, timeout=10):
        return json.loads(self.responses.get(timeout=timeout))

    def initialize(self):
        self.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": gateway.PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "real-subprocess-protocol-driver", "version": "1"}}})
        response = self.receive()
        if "result" not in response:
            raise AssertionError(response)
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return response

    def call(self, tool, arguments, request_id=2):
        self.send({"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                   "params": {"name": tool, "arguments": arguments}})
        return self.receive()

    def close(self):
        if not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.reader.join(3)
        self.stderr = self.process.stderr.read()
        self.process.stdout.close()
        self.process.stderr.close()


def assert_single_command(test, fixture, arguments):
    with fixture.db() as con:
        test.assertEqual(con.execute("SELECT count(*) FROM v2_task WHERE task_id=?",
                                     (arguments["task_id"],)).fetchone()[0], 1)
        test.assertEqual(con.execute("SELECT count(*) FROM v2_command_log WHERE command_id=?",
                                     (arguments["command_id"],)).fetchone()[0], 1)


class RealServiceSubprocessTests(unittest.TestCase):
    def test_missing_state_cli_is_fail_closed_no_stdout_secrets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            token_path = root / "token"
            token_path.write_text("a-secret-not-for-stdout", encoding="utf-8")
            result = subprocess.run([sys.executable, "-B", gateway.__file__, "--state", str(root / "absent"),
                                     "--project-root", str(root), "--token-file", str(token_path)],
                                    input=b"", capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, b"")
            self.assertNotIn(b"a-secret-not-for-stdout", result.stderr)
            self.assertFalse((root / "absent").exists())

    def test_real_process_deadline_is_finite(self):
        backend = gateway.ServiceBackend("missing-state", "missing-root", call_timeout=.000001)
        started = time.monotonic()
        with self.assertRaises(gateway.GatewayError) as caught:
            backend.call("not-a-real-secret", "awo_task_list", {})
        self.assertEqual(caught.exception.code, "timeout")
        self.assertTrue(caught.exception.uncertain)
        self.assertLess(time.monotonic() - started, 5)

    def test_pre_cancel_never_starts_invocation(self):
        cancel = threading.Event()
        cancel.set()
        backend = gateway.ServiceBackend("missing-state", "missing-root")
        with self.assertRaises(gateway.GatewayError) as caught:
            backend.call("test-secret", "awo_task_list", {}, cancel)
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(caught.exception.code, "cancelled")

    def test_real_catalog_authentication_and_root_binding(self):
        fixture, path, token, _ = real_fixture(self)
        backend = gateway.ServiceBackend(fixture.state, fixture.root)
        self.assertEqual(backend.principal(token), {"authenticated": True})
        self.assertEqual({s["name"] for s in backend.specs(token)}, gateway.P0_TOOLS)
        backend.call(token, "awo_task_list", {})
        with self.assertRaises(gateway.GatewayError):
            backend.principal("invalid-forged-token")
        with self.assertRaises(gateway.GatewayError):
            gateway.ServiceBackend(fixture.state, fixture.secrets).principal(token)

    def test_real_worker_role_cannot_create_task(self):
        fixture, path, token, _ = real_fixture(self)
        _, worker_token, _ = self.access_fixture.issue("worker")
        backend = gateway.ServiceBackend(fixture.state, fixture.root)
        arguments = task_arguments(fixture)
        with self.assertRaises(gateway.GatewayError) as caught:
            backend.call(worker_token, "awo_task_create", arguments)
        self.assertEqual(caught.exception.code, "forbidden_role")
        with fixture.db() as con:
            self.assertEqual(con.execute("SELECT count(*) FROM v2_task").fetchone()[0], 0)

    def test_real_schema_failure_never_writes_a_command_or_event(self):
        fixture, path, token, _ = real_fixture(self)
        backend = gateway.ServiceBackend(fixture.state, fixture.root)
        with fixture.db() as con:
            before = list(con.iterdump())
        arguments = task_arguments(fixture)
        arguments["expected_epoch"] = True
        with self.assertRaises(gateway.GatewayError) as caught:
            backend.call(token, "awo_task_create", arguments)
        self.assertEqual(caught.exception.code, "invalid_arguments")
        with fixture.db() as con:
            self.assertEqual(before, list(con.iterdump()))

    def test_real_message_catalog_requires_explicit_enablement(self):
        fixture, path, token, _ = real_fixture(self)
        backend = gateway.ServiceBackend(fixture.state, fixture.root)
        with self.assertRaises(gateway.GatewayError) as caught:
            backend.call(token, "awo_message_read", {})
        self.assertEqual(caught.exception.code, "invalid_arguments")
        enabled = gateway.ServiceBackend(fixture.state, fixture.root, messages_enabled=True)
        self.assertEqual({s["name"] for s in enabled.specs(token)},
                         gateway.P0_TOOLS | gateway.MESSAGE_TOOLS)

    def test_real_root_scoped_bootstrap_grant_only_registers_and_replays(self):
        import awo_service as service
        fixture, path, token, _ = real_fixture(self)
        grant_file = fixture.secrets / "transport-grant.txt"
        service._enroll_agent(fixture.state, *fixture.actor, fixture.envelope("agent.enroll", {
            "project_root": str(fixture.root), "role": "worker", "allowed_capabilities": ["status"]}),
            str(grant_file))
        grant = grant_file.read_text(encoding="utf-8").strip()
        backend = gateway.ServiceBackend(fixture.state, fixture.root)
        self.assertEqual(backend.principal(grant), {"authenticated": True})
        with self.assertRaises(gateway.GatewayError) as caught:
            backend.call(grant, "awo_task_list", {})
        self.assertEqual(caught.exception.code, "forbidden_role")
        arguments = {"handle": "gateway-bootstrap", "harness": "offline-test-driver",
                     "harness_version": "1", "host": "local", "os": sys.platform,
                     "capabilities": ["status"], "command_id": "cmd-" + uuid.uuid4().hex,
                     "expected_epoch": fixture.epoch}
        first = backend.call(grant, "awo_agent_register", arguments)
        replay = backend.call(grant, "awo_agent_register", arguments)
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        # Avoid credentials in assertion diffs or raw evidence on failure.
        secret = first["access_token"]
        self.assertTrue(secret == replay["access_token"], "registration replay changed credential")
        self.assertEqual(backend.principal(secret), {"authenticated": True})
        with fixture.db() as con:
            snapshot = "\n".join(con.iterdump())
            self.assertTrue(secret not in snapshot and grant not in snapshot,
                            "plaintext credentials must not be stored in the ledger")

    def test_real_worker_review_attempt_is_denied_without_entity_creation(self):
        fixture, path, token, _ = real_fixture(self)
        _, worker_token, _ = self.access_fixture.issue("worker")
        backend = gateway.ServiceBackend(fixture.state, fixture.root)
        args = {"command_id": "cmd-" + uuid.uuid4().hex, "expected_epoch": fixture.epoch,
                "expected_revision": 1, "assignment_id": "not-owned", "task_id": "not-owned",
                "event_id": "not-owned", "evidence_ids": ["not-owned"], "verdict": "accept",
                "review_note_path": "REVIEW.md", "review_note_sha256": "0" * 64}
        with self.assertRaises(gateway.GatewayError) as caught:
            backend.call(worker_token, "awo_review_submit", args)
        self.assertEqual(caught.exception.code, "forbidden_role")
        with fixture.db() as con:
            self.assertEqual(con.execute("SELECT count(*) FROM v2_review").fetchone()[0], 0)

    def test_real_stdio_subprocess_lifecycle(self):
        fixture, path, token, _ = real_fixture(self)
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2026-11-25", "capabilities": {},
                "clientInfo": {"name": "real-subprocess-protocol-driver", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "ping"},
        ]
        result = subprocess.run([sys.executable, "-B", gateway.__file__, "--state", str(fixture.state),
                                 "--project-root", str(fixture.root), "--token-file", str(path)],
                                input=b"".join(gateway.encode_json(m) + b"\n" for m in messages),
                                capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(replies), 3)
        self.assertEqual(replies[0]["result"]["protocolVersion"], gateway.PROTOCOL_VERSION)
        self.assertEqual(len(replies[1]["result"]["tools"]), 13)
        self.assertEqual(result.stderr, b"")
        self.assertNotIn(token.encode(), result.stdout)

    def test_real_stdio_write_replay_survives_gateway_restart(self):
        fixture, path, token, _ = real_fixture(self)
        arguments = task_arguments(fixture)
        for attempt in range(2):
            client = StdioClient(fixture, path)
            try:
                client.initialize()
                result = client.call("awo_task_create", arguments)["result"]
                self.assertFalse(result["isError"], result)
                self.assertEqual(result["structuredContent"]["replayed"], bool(attempt))
            finally:
                client.close()
            self.assertEqual(client.process.returncode, 0)
            self.assertEqual(client.stderr, b"")
            self.assertNotIn(token.encode(), b"".join(client.stdout))
        assert_single_command(self, fixture, arguments)

    def test_real_command_conflict_and_epoch_rejection_preserve_task(self):
        fixture, path, token, _ = real_fixture(self)
        arguments = task_arguments(fixture)
        backend = gateway.ServiceBackend(fixture.state, fixture.root)
        backend.call(token, "awo_task_create", arguments)
        for changed, code in ((dict(arguments, title="changed payload"), "idempotency_conflict"),
                              (dict(arguments, expected_epoch=fixture.epoch + 1), "revision_conflict")):
            with self.assertRaises(gateway.GatewayError) as caught:
                backend.call(token, "awo_task_create", changed)
            self.assertEqual(caught.exception.code, code)
        task = backend.call(token, "awo_task_get", {"task_id": arguments["task_id"]})
        self.assertEqual(task["task"]["title"], arguments["title"])
        assert_single_command(self, fixture, arguments)

    def test_real_sqlite_lock_deadline_rolls_back_and_original_command_can_retry(self):
        fixture, path, token, _ = real_fixture(self)
        arguments = task_arguments(fixture)
        backend = gateway.ServiceBackend(fixture.state, fixture.root, call_timeout=.3)
        with fixture.db() as con:
            con.execute("BEGIN IMMEDIATE")
            with self.assertRaises(gateway.GatewayError) as caught:
                backend.call(token, "awo_task_create", arguments)
            self.assertEqual(caught.exception.code, "timeout")
            self.assertTrue(caught.exception.uncertain)
            self.assertEqual(con.execute("SELECT count(*) FROM v2_task WHERE task_id=?",
                                         (arguments["task_id"],)).fetchone()[0], 0)
        backend = gateway.ServiceBackend(fixture.state, fixture.root)
        self.assertFalse(backend.call(token, "awo_task_create", arguments)["replayed"])
        self.assertTrue(backend.call(token, "awo_task_create", arguments)["replayed"])
        assert_single_command(self, fixture, arguments)

    def test_real_sqlite_lock_cancellation_stops_subprocess_without_mutation(self):
        fixture, path, token, _ = real_fixture(self)
        arguments = task_arguments(fixture)
        backend = gateway.ServiceBackend(fixture.state, fixture.root)
        cancel = threading.Event()
        with fixture.db() as con:
            con.execute("BEGIN IMMEDIATE")
            timer = threading.Timer(.2, cancel.set)
            timer.start()
            try:
                with self.assertRaises(gateway.GatewayError) as caught:
                    backend.call(token, "awo_task_create", arguments, cancel)
                self.assertEqual(caught.exception.code, "cancelled")
                self.assertTrue(caught.exception.uncertain)
            finally:
                timer.cancel()
                timer.join(1)
            self.assertEqual(con.execute("SELECT count(*) FROM v2_task WHERE task_id=?",
                                         (arguments["task_id"],)).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
