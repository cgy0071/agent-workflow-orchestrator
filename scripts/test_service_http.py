"""Wire-level transport tests and separately labelled real-service integration."""
from __future__ import annotations

import base64
import hashlib
import http.client
import io
import json
import socket
import struct
import threading
import time
import unittest

import mcp_gateway as gateway
import service_http as transport
import test_mcp_gateway as fixtures


def client_frame(opcode, payload=b"", fin=True):
    mask = b"1234"
    first = (128 if fin else 0) | opcode
    if len(payload) < 126:
        head = bytes([first, 128 | len(payload)])
    elif len(payload) <= 65535:
        head = bytes([first, 254]) + struct.pack("!H", len(payload))
    else:
        head = bytes([first, 255]) + struct.pack("!Q", len(payload))
    return head + mask + bytes(value ^ mask[index % 4] for index, value in enumerate(payload))


def server_frame(source):
    first, second = transport.read_exact(source, 2)
    if second & 128:
        raise AssertionError("server frame must not be masked")
    length = second
    if length == 126:
        length = struct.unpack("!H", transport.read_exact(source, 2))[0]
    if length == 127:
        length = struct.unpack("!Q", transport.read_exact(source, 8))[0]
    if length > gateway.MAX_OUTPUT:
        raise AssertionError("server frame exceeds limit")
    return first & 15, transport.read_exact(source, length)


class FrameTests(unittest.TestCase):
    def test_masked_frames_all_length_encodings(self):
        for length in (0, 5, 125, 126, 65535, 65536):
            payload = b"x" * length
            self.assertEqual(transport.read_frame(io.BytesIO(client_frame(1, payload))),
                             (True, 1, payload))

    def test_unmasked_rsv_unknown_opcode_and_fragmented_control_rejected(self):
        for frame in (b"\x81\x00", b"\xc1\x80", b"\x83\x80", b"\x09\x80", b"\x89\xfe"):
            with self.subTest(frame=frame), self.assertRaises(transport.WebSocketError):
                transport.read_frame(io.BytesIO(frame))

    def test_noncanonical_highbit_and_oversize_lengths_rejected_before_payload(self):
        for frame in (b"\x81\xfe\x00\x01", b"\x81\xff" + struct.pack("!Q", 126),
                      b"\x81\xff" + struct.pack("!Q", 2 ** 63),
                      b"\x81\xff" + struct.pack("!Q", gateway.MAX_INPUT + 1)):
            with self.subTest(frame=frame), self.assertRaises(transport.WebSocketError):
                transport.read_frame(io.BytesIO(frame))

    def test_close_code_reason_and_length_validation(self):
        transport.validate_close(b"")
        transport.validate_close(struct.pack("!H", 1000) + b"bye")
        for payload in (b"x", struct.pack("!H", 1005), struct.pack("!H", 999),
                        struct.pack("!H", 5000), struct.pack("!H", 1000) + b"\xff"):
            with self.subTest(payload=payload), self.assertRaises(transport.WebSocketError):
                transport.validate_close(payload)

    def test_truncated_payload_raises_eof(self):
        with self.assertRaises(EOFError):
            transport.read_frame(io.BytesIO(client_frame(1, b"hello")[:-1]))

    def test_server_frame_roundtrip_is_unmasked_and_bounded(self):
        for payload in (b"hello", b"x" * 126, b"x" * 65536):
            stream = io.BytesIO()
            transport.write_frame(stream, 1, payload)
            stream.seek(0)
            self.assertEqual(server_frame(stream), (1, payload))
        with self.assertRaises(transport.WebSocketError):
            transport.write_frame(io.BytesIO(), 9, b"x" * 126)


class ServerFixture:
    def start_server(self, backend, **limits):
        self.backend = backend
        self.server = transport.ApplicationHTTPServer(("127.0.0.1", 0), backend, **limits)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.authority = "127.0.0.1:" + str(self.port)
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def request(self, body=None, headers=None, method="POST", path="/call"):
        wire = gateway.encode_json(body if body is not None else {
            "id": "call-1", "tool": "awo_task_list", "arguments": {}})
        values = {"Authorization": "Bearer " + self.token, "Content-Type": "application/json"}
        if headers:
            values.update(headers)
        client = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            client.request(method, path, wire, values)
            response = client.getresponse()
            body = response.read()
            return response.status, json.loads(body) if body else None, dict(response.getheaders())
        finally:
            client.close()

    def raw_request(self, headers, body=b"", request_line="POST /call HTTP/1.1"):
        client = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        self.addCleanup(client.close)
        client.sendall((request_line + "\r\n" + "\r\n".join(headers) + "\r\n\r\n").encode("ascii") + body)
        response = http.client.HTTPResponse(client)
        response.begin()
        result = response.status, response.read()
        response.close()
        client.close()
        return result

    def websocket(self, overrides=None):
        client = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        self.addCleanup(client.close)
        key = base64.b64encode(b"0123456789abcdef").decode()
        values = {"Host": self.authority, "Authorization": "Bearer " + self.token,
                  "Upgrade": "websocket", "Connection": "Upgrade",
                  "Sec-WebSocket-Key": key, "Sec-WebSocket-Version": "13"}
        values.update(overrides or {})
        request = "GET /ws HTTP/1.1\r\n" + "\r\n".join(k + ": " + v for k, v in values.items()) + "\r\n\r\n"
        client.sendall(request.encode("ascii"))
        source = client.makefile("rb")
        self.addCleanup(source.close)
        status = source.readline().decode("ascii").split()[1]
        headers = {}
        while True:
            line = source.readline()
            if line == b"\r\n":
                break
            if not line:
                raise AssertionError("incomplete handshake")
            key_name, value = line.decode("ascii").split(":", 1)
            headers[key_name.lower()] = value.strip()
        return client, source, int(status), headers


class ProtocolHTTPTests(ServerFixture, unittest.TestCase):
    """Real sockets, injected backend: validates protocol only, not business logic."""

    def setUp(self):
        self.token = "protocol-test-secret"
        self.start_server(fixtures.ProtocolOnlyBackend(), idle_timeout=.5, connection_timeout=5)

    def test_http_envelope_preserves_id_command_revision_and_headers(self):
        args = {"command_id": "retained-command-id", "expected_epoch": 0, "expected_revision": 3}
        status, value, headers = self.request({"id": "c", "tool": "awo_task_create", "arguments": args})
        self.assertEqual(status, 200)
        self.assertEqual(value, {"id": "c", "result": {"received": args}})
        self.assertEqual(self.backend.calls, [("awo_task_create", args)])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_host_origin_and_duplicate_security_headers_never_call_service(self):
        for headers in ({"Host": "evil.example"}, {"Host": "127.0.0.1:9999"},
                        {"Origin": "https://evil.example"}, {"Origin": "null"},
                        {"Origin": "http://" + self.authority + "/"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request(headers=headers)[0], 403)
        status, _ = self.raw_request(["Host: " + self.authority, "Host: evil.example", "Content-Length: 0"])
        self.assertEqual(status, 400)
        self.assertEqual(self.backend.calls, [])

    def test_same_origin_allowed_without_cors_response(self):
        self.assertEqual(self.request(headers={"Origin": "http://" + self.authority})[0], 200)

    def test_no_unauthenticated_health_or_cookie_query_auth(self):
        for headers, path in (({"Authorization": ""}, "/call"),
                              ({"Authorization": "Bearer forged"}, "/call"),
                              ({"Authorization": "", "Cookie": "token=" + self.token}, "/health"),
                              ({"Authorization": ""}, "/ws?token=" + self.token)):
            with self.subTest(path=path):
                status, value, _ = self.request(headers=headers, path=path)
                self.assertEqual(status, 401)
                self.assertNotIn(self.token, json.dumps(value))
        self.assertEqual(self.backend.calls, [])

    def test_unknown_route_and_options_do_not_mutate(self):
        self.assertEqual(self.request(path="/call?token=not-allowed")[0], 404)
        self.assertEqual(self.request(method="OPTIONS")[0], 403)
        self.assertEqual(self.request(method="HEAD")[0], 405)
        self.assertEqual(self.backend.calls, [])

    def test_invalid_application_body_and_schema_do_not_mutate(self):
        for body in ([], {"id": True, "tool": "awo_task_list", "arguments": {}},
                     {"id": 1, "tool": "awo_task_list", "arguments": {}, "token": "forged"},
                     {"id": 1, "tool": "awo_task_list", "arguments": {"project_root": "elsewhere"}}):
            with self.subTest(body=body):
                self.assertEqual(self.request(body)[0], 400)
        self.assertEqual(self.backend.calls, [])

    def test_http_duplicate_json_nan_and_invalid_utf8(self):
        base = ["Host: " + self.authority, "Authorization: Bearer " + self.token,
                "Content-Type: application/json"]
        for body in (b'{"id":1,"id":2}', b'{"id":NaN}', b'"\xff"'):
            status, _ = self.raw_request(base + ["Content-Length: " + str(len(body))], body)
            self.assertEqual(status, 400)
        self.assertEqual(self.backend.calls, [])

    def test_http_size_length_content_type_and_transfer_encoding(self):
        base = ["Host: " + self.authority, "Authorization: Bearer " + self.token]
        for fields, expected in ((["Content-Length: " + str(gateway.MAX_INPUT + 1)], 413),
                                 (["Content-Length: -1"], 411), ([], 411),
                                 (["Content-Length: 0"], 415),
                                 (["Transfer-Encoding: chunked"], 400),
                                 (["Content-Length: 0", "Content-Length: 0"], 400),
                                 (["Expect: 100-continue", "Content-Length: 0"], 417)):
            with self.subTest(fields=fields):
                self.assertEqual(self.raw_request(base + fields)[0], expected)
        self.assertEqual(self.backend.calls, [])

    def test_header_budget_and_obsolete_folding(self):
        base = ["Host: " + self.authority]
        self.assertEqual(self.raw_request(base + ["X-Large: " + "x" * transport.MAX_HEADERS])[0], 431)
        self.assertEqual(self.raw_request(base + [" folded"])[0], 431)
        self.assertEqual(self.backend.calls, [])

    def test_slow_or_truncated_body_cannot_mutate(self):
        base = ["Host: " + self.authority, "Authorization: Bearer " + self.token,
                "Content-Type: application/json", "Content-Length: 100"]
        started = time.monotonic()
        status, _ = self.raw_request(base, b"{")
        self.assertEqual(status, 408)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(self.backend.calls, [])

    def test_transport_errors_redact_exception_and_do_not_retry(self):
        def fail(tool, args, cancel):
            raise RuntimeError(self.token + " sensitive traceback")
        self.backend.invoke = fail
        status, value, _ = self.request()
        self.assertEqual(status, 500)
        self.assertNotIn(self.token, json.dumps(value))
        self.assertTrue(value["error"]["uncertain"])
        self.assertEqual(len(self.backend.calls), 1)

    def test_websocket_auth_handshake_and_same_application_call(self):
        client, source, status, headers = self.websocket()
        self.assertEqual(status, 101)
        key = base64.b64encode(b"0123456789abcdef").decode()
        expected = base64.b64encode(hashlib.sha1((key + transport.WS_GUID).encode()).digest()).decode()
        self.assertEqual(headers["sec-websocket-accept"], expected)
        payload = {"id": "ws-1", "tool": "awo_task_list", "arguments": {}}
        client.sendall(client_frame(1, gateway.encode_json(payload)))
        opcode, reply = server_frame(source)
        self.assertEqual(opcode, 1)
        self.assertEqual(json.loads(reply), {"id": "ws-1", "result": {"received": {}}})
        client.sendall(client_frame(8, struct.pack("!H", 1000)))
        self.assertEqual(server_frame(source)[0], 8)

    def test_websocket_bad_auth_origin_key_version_and_extensions(self):
        for overrides, expected in (({"Authorization": ""}, 401),
                                     ({"Origin": "http://evil.example"}, 403),
                                     ({"Host": "evil.example"}, 403),
                                     ({"Sec-WebSocket-Key": "bad"}, 400),
                                     ({"Sec-WebSocket-Version": "12"}, 400),
                                     ({"Sec-WebSocket-Extensions": "permessage-deflate"}, 400)):
            with self.subTest(overrides=overrides):
                client, source, status, _ = self.websocket(overrides)
                self.assertEqual(status, expected)
                source.close()
                client.close()
        self.assertEqual(self.backend.calls, [])

    def test_websocket_fragmentation_ping_and_message_size(self):
        client, source, status, _ = self.websocket()
        self.assertEqual(status, 101)
        body = gateway.encode_json({"id": 1, "tool": "awo_task_list", "arguments": {}})
        client.sendall(client_frame(1, body[:10], False) + client_frame(9, b"ping") + client_frame(0, body[10:]))
        self.assertEqual(server_frame(source), (10, b"ping"))
        self.assertIn("result", json.loads(server_frame(source)[1]))
        client.sendall(b"\x81\xff" + struct.pack("!Q", gateway.MAX_INPUT + 1))
        self.assertEqual(server_frame(source), (8, struct.pack("!H", 1009)))

    def test_websocket_invalid_frames_cannot_call_service(self):
        for wire, close_code in ((b"\x81\x00", 1002), (client_frame(0, b"no-start"), 1002),
                                 (client_frame(1, b"\xff"), 1007), (client_frame(2, b"binary"), 1003),
                                 (client_frame(8, b"x"), 1002)):
            with self.subTest(wire=wire):
                client, source, status, _ = self.websocket()
                self.assertEqual(status, 101)
                client.sendall(wire)
                self.assertEqual(server_frame(source), (8, struct.pack("!H", close_code)))
                source.close()
                client.close()
        self.assertEqual(self.backend.calls, [])

    def test_websocket_malformed_json_recovers_without_calling(self):
        client, source, _, _ = self.websocket()
        client.sendall(client_frame(1, b"invalid-json"))
        self.assertEqual(json.loads(server_frame(source)[1])["error"]["code"], "invalid_request")
        self.assertEqual(self.backend.calls, [])

    def test_websocket_reauthenticates_each_call(self):
        client, source, _, _ = self.websocket()
        # Token revocation simulated only in the protocol test backend.
        self.backend.principal = lambda token: (_ for _ in ()).throw(gateway.GatewayError("unauthenticated"))
        client.sendall(client_frame(1, gateway.encode_json({"id": 1, "tool": "awo_task_list", "arguments": {}})))
        self.assertEqual(json.loads(server_frame(source)[1])["error"]["code"], "unauthenticated")
        self.assertEqual(self.backend.calls, [])

    def test_connection_absolute_lifetime_limits_trickling_headers(self):
        self.server.connection_timeout = .2
        client = socket.create_connection(("127.0.0.1", self.port), timeout=2)
        self.addCleanup(client.close)
        client.sendall(b"POST /call HTTP/1.1\r\nHost: ")
        started = time.monotonic()
        self.assertEqual(client.recv(1), b"")
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(self.backend.calls, [])

    def test_nonloopback_or_hostname_binding_is_rejected(self):
        for host in ("0.0.0.0", "192.168.1.5", "::", "localhost", "evil.example"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                transport.ApplicationHTTPServer((host, 0), self.backend)

    def test_websocket_cumulative_fragment_limit(self):
        client, source, _, _ = self.websocket()
        half = gateway.MAX_INPUT // 2
        client.sendall(client_frame(1, b"x" * half, False))
        client.sendall(client_frame(0, b"x" * (half + 1)))
        self.assertEqual(server_frame(source), (8, struct.pack("!H", 1009)))
        self.assertEqual(self.backend.calls, [])

    def test_websocket_new_text_before_continuation_rejected(self):
        client, source, _, _ = self.websocket()
        client.sendall(client_frame(1, b"{", False) + client_frame(1, b"{}"))
        self.assertEqual(server_frame(source), (8, struct.pack("!H", 1002)))
        self.assertEqual(self.backend.calls, [])

    def test_connection_capacity_rejection_is_bounded(self):
        # Occupy every connection slot without any authenticated application call.
        server = transport.ApplicationHTTPServer(("127.0.0.1", 0), self.backend,
                                                  max_connections=1, connection_timeout=2)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        clients = []
        try:
            first = socket.create_connection(server.server_address, timeout=2)
            clients.append(first)
            first.sendall(b"GET /ws HTTP/1.1\r\n")
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                with server._active_lock:
                    if server._active:
                        break
                time.sleep(.01)
            second = socket.create_connection(server.server_address, timeout=2)
            clients.append(second)
            self.assertEqual(second.recv(1), b"")
            self.assertEqual(self.backend.calls, [])
        finally:
            for client in clients:
                client.close()
            server.shutdown()
            server.server_close()
            thread.join(3)


class RealServiceHTTPTests(ServerFixture, unittest.TestCase):
    def setUp(self):
        self.fixture, self.token_file, self.token, self.issued = fixtures.real_fixture(self)
        self.start_server(gateway.ServiceBackend(self.fixture.state, self.fixture.root))

    def test_real_http_ws_share_service_and_reject_forged_token(self):
        self.assertEqual(self.request()[0], 200)
        client, source, status, _ = self.websocket()
        self.assertEqual(status, 101)
        client.sendall(client_frame(1, gateway.encode_json({"id": 2, "tool": "awo_task_list", "arguments": {}})))
        self.assertIn("result", json.loads(server_frame(source)[1]))
        self.assertEqual(self.request(headers={"Authorization": "Bearer forged-token"})[0], 401)

    def test_http_errors_leave_real_database_unchanged(self):
        with self.fixture.db() as con:
            before = list(con.iterdump())
        for headers in ({"Host": "evil.example"}, {"Origin": "null"}, {"Authorization": ""}):
            self.assertNotEqual(self.request(headers=headers)[0], 200)
        self.assertEqual(self.request({"id": 1, "tool": "awo_task_create", "arguments": {}})[0], 400)
        with self.fixture.db() as con:
            self.assertEqual(before, list(con.iterdump()))

    def test_rejected_post_drains_body_before_orderly_close(self):
        # Closing with an unread body makes the stack send RST, which can
        # discard the rejection the client is reading. An RST is timing
        # dependent, so repeat the rejection instead of sampling it once.
        body = b'{"id": 1, "tool": "awo_task_list", "arguments": {"padding": "' + b'x' * (1 << 20) + b'"}}'
        for _ in range(5):
            client = socket.create_connection(("127.0.0.1", self.port), timeout=3)
            try:
                client.sendall(
                    b"POST /call HTTP/1.1\r\nHost: evil.example\r\nAuthorization: Bearer " +
                    self.token.encode("ascii") +
                    b"\r\nContent-Type: application/json\r\nContent-Length: " +
                    str(len(body)).encode("ascii") + b"\r\n\r\n" + body)
                response = http.client.HTTPResponse(client)
                response.begin()
                self.assertEqual(response.status, 403)
                self.assertEqual(json.loads(response.read())["error"]["code"], "invalid_request")
                client.settimeout(3)
                self.assertEqual(client.recv(1), b"")
            finally:
                client.close()

    def test_rejected_post_with_dribbled_body_is_closed_within_a_bound(self):
        # A client that declares a large body and dribbles it must not pin the
        # request thread or its connection slot after the rejection is written.
        client = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        self.addCleanup(client.close)
        client.sendall(b"POST /call HTTP/1.1\r\nHost: evil.example\r\nAuthorization: Bearer " +
                       self.token.encode("ascii") +
                       b"\r\nContent-Type: application/json\r\nContent-Length: 9999999\r\n\r\n")
        stop = threading.Event()

        def dribble():
            while not stop.is_set():
                try:
                    client.sendall(b"x")
                except OSError:
                    return
                time.sleep(0.03)

        sender = threading.Thread(target=dribble, daemon=True)
        sender.start()
        self.addCleanup(stop.set)
        started = time.monotonic()
        closed = False
        client.settimeout(0.2)
        try:
            while time.monotonic() - started < 5:
                try:
                    if client.recv(65536) == b"":
                        closed = True
                        break
                except socket.timeout:
                    continue
                except OSError:
                    closed = True
                    break
        finally:
            stop.set()
            sender.join(2)
        self.assertTrue(closed, 'server kept a dribbled rejected request open')
        self.assertLess(time.monotonic() - started, 3)

    def test_empty_connections_do_not_leave_connection_timers(self):
        # A connect-and-close connection has no request line, so `command` is
        # never set. finish() must still cancel the per-connection timer; a
        # leaked timer retains the handler and socket for the full timeout.
        def timers():
            return sum(1 for item in threading.enumerate()
                       if isinstance(item, threading.Timer))

        before = timers()
        for _ in range(20):
            socket.create_connection(("127.0.0.1", self.port), timeout=3).close()
        deadline = time.monotonic() + 5
        while timers() > before + 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertLessEqual(timers(), before + 2)

    def test_http_ws_and_stdio_share_durable_command_replay(self):
        arguments = fixtures.task_arguments(self.fixture)
        request = {"id": "http-create", "tool": "awo_task_create", "arguments": arguments}
        status, first, _ = self.request(request)
        self.assertEqual(status, 200, first)
        self.assertFalse(first["result"]["replayed"])
        client, source, status, _ = self.websocket()
        self.assertEqual(status, 101)
        client.sendall(client_frame(1, gateway.encode_json(dict(request, id="ws-replay"))))
        replay = json.loads(server_frame(source)[1])
        self.assertTrue(replay["result"]["replayed"])
        protocol = fixtures.StdioClient(self.fixture, self.token_file)
        try:
            protocol.initialize()
            replay = protocol.call("awo_task_create", arguments)["result"]
            self.assertFalse(replay["isError"], replay)
            self.assertTrue(replay["structuredContent"]["replayed"])
        finally:
            protocol.close()
        fixtures.assert_single_command(self, self.fixture, arguments)

    def test_real_revoked_credential_rejected_on_existing_websocket(self):
        import service_access as access
        client, source, status, _ = self.websocket()
        self.assertEqual(status, 101)
        access.revoke_token(self.fixture.state, *self.fixture.actor, self.fixture.envelope(
            "access.revoke", {"token_id": self.issued["token_id"], "expected_revision": 1}))
        client.sendall(client_frame(1, gateway.encode_json({
            "id": "revoked", "tool": "awo_task_list", "arguments": {}})))
        self.assertIn("error", json.loads(server_frame(source)[1]))

    def test_http_service_idempotency_conflict_is_structured_409(self):
        arguments = fixtures.task_arguments(self.fixture)
        value = {"id": "first", "tool": "awo_task_create", "arguments": arguments}
        self.assertEqual(self.request(value)[0], 200)
        status, error, _ = self.request(dict(value, id="conflict", arguments=dict(arguments, title="different")))
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "idempotency_conflict")
        self.assertFalse(error["error"]["retryable"])
        fixtures.assert_single_command(self, self.fixture, arguments)


if __name__ == "__main__":
    unittest.main()
