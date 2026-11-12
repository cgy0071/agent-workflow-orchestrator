"""Loopback HTTP/JSON and WebSocket application transports (not MCP HTTP)."""
from __future__ import annotations

import argparse
import base64
import hashlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
import http.client
import ipaddress
import json
import re
import socket
from socketserver import ThreadingMixIn
import struct
import sys
import threading
import time

from mcp_gateway import (GatewayError, MAX_INPUT, MAX_OUTPUT, MAX_TOKEN,
                         ServiceBackend, decode_json, encode_json, safe_error, valid_id)


MAX_HEADERS = 16384
MAX_REQUEST_LINE = 4096
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
HTTP_ERRORS = {"unauthenticated": 401, "project_mismatch": 403,
               "forbidden_role": 403, "not_found": 404,
               "invalid_request": 400, "invalid_arguments": 400,
               "command_id_required": 400, "revision_conflict": 409,
               "idempotency_conflict": 409, "state_conflict": 409,
               "resource_exhausted": 429, "unavailable": 503,
               "timeout": 504, "response_too_large": 502}


def application_call(backend, token, value, cancel=None):
    """Identical application envelope and argument forwarding for HTTP and WS."""
    request_id = value.get("id") if isinstance(value, dict) else None
    if (not isinstance(value, dict) or set(value) != {"id", "tool", "arguments"}
            or not valid_id(request_id) or not isinstance(value.get("tool"), str)
            or not isinstance(value.get("arguments"), dict)):
        return 400, {"id": request_id if valid_id(request_id) else None,
                     "error": GatewayError("invalid_request").as_dict()}
    try:
        result = backend.call(token, value["tool"], value["arguments"], cancel)
        response = {"id": request_id, "result": result}
        encode_json(response)
        return 200, response
    except Exception as exc:
        error = safe_error(exc)
        if error.code in ("response_too_large", "internal"):
            error.uncertain = True
        return HTTP_ERRORS.get(error.code, 500), {"id": request_id, "error": error.as_dict()}


class WebSocketError(ValueError):
    def __init__(self, close_code=1002):
        self.close_code = close_code
        super().__init__("Invalid WebSocket frame")


def read_exact(stream, length):
    parts = bytearray()
    while len(parts) < length:
        part = stream.read(length - len(parts))
        if not part:
            raise EOFError()
        parts.extend(part)
    return bytes(parts)


def read_frame(stream, max_payload=MAX_INPUT):
    """RFC 6455 client frames: masked, canonical lengths, no RSV/extensions."""
    first, second = read_exact(stream, 2)
    fin, opcode, masked = bool(first & 128), first & 15, bool(second & 128)
    if first & 112 or not masked or opcode not in (0, 1, 2, 8, 9, 10):
        raise WebSocketError()
    length = second & 127
    if opcode >= 8 and (not fin or length > 125):
        raise WebSocketError()
    if length == 126:
        length = struct.unpack("!H", read_exact(stream, 2))[0]
        if length < 126:
            raise WebSocketError()
    elif length == 127:
        length = struct.unpack("!Q", read_exact(stream, 8))[0]
        if length < 65536 or length >= 2 ** 63:
            raise WebSocketError()
    if length > max_payload:
        raise WebSocketError(1009)
    mask = read_exact(stream, 4)
    payload = read_exact(stream, length)
    return fin, opcode, bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))


def write_frame(stream, opcode, payload=b""):
    if len(payload) > MAX_OUTPUT or (opcode >= 8 and len(payload) > 125):
        raise WebSocketError(1009)
    head = bytes([128 | opcode])
    if len(payload) < 126:
        head += bytes([len(payload)])
    elif len(payload) <= 65535:
        head += bytes([126]) + struct.pack("!H", len(payload))
    else:
        head += bytes([127]) + struct.pack("!Q", len(payload))
    stream.write(head + payload)
    stream.flush()


def validate_close(payload):
    if len(payload) == 1:
        raise WebSocketError()
    if len(payload) >= 2:
        code = struct.unpack("!H", payload[:2])[0]
        if (code not in {1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010, 1011, 1012, 1013, 1014}
                and not 3000 <= code <= 4999):
            raise WebSocketError()
        try:
            payload[2:].decode("utf-8", "strict")
        except UnicodeError:
            raise WebSocketError(1007) from None


class _HeaderReader:
    def __init__(self, source):
        self.source, self.remaining = source, MAX_HEADERS

    def readline(self, size=-1):
        value = self.source.readline(min(self.remaining + 1, size if size >= 0 else MAX_HEADERS + 1))
        self.remaining -= len(value)
        if self.remaining < 0:
            raise http.client.LineTooLong("header budget")
        # Obsolete folding has ambiguous security semantics; reject it.
        if value.startswith((b" ", b"\t")):
            raise http.client.HTTPException("folded header")
        return value


class ApplicationHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False
    request_queue_size = 16

    def __init__(self, address, backend, *, idle_timeout=10, connection_timeout=60,
                 max_connections=16):
        host, port = address
        try:
            parsed = ipaddress.ip_address(host)
        except ValueError:
            raise ValueError("host must be a literal loopback IP address") from None
        if not parsed.is_loopback:
            raise ValueError("only loopback binding is supported")
        if not (0 < idle_timeout <= 30 and 0 < connection_timeout <= 300
                and 1 <= max_connections <= 64):
            raise ValueError("invalid connection limits")
        self.address_family = socket.AF_INET6 if parsed.version == 6 else socket.AF_INET
        self.backend = backend
        self.idle_timeout, self.connection_timeout = idle_timeout, connection_timeout
        self._slots = threading.BoundedSemaphore(max_connections)
        self._active = set()
        self._active_lock = threading.Lock()
        super().__init__(address, ApplicationHandler)
        bound_host, bound_port = self.server_address[:2]
        authority = "[" + bound_host + "]" if parsed.version == 6 else bound_host
        self.allowed_hosts = {authority + ":" + str(bound_port), "localhost:" + str(bound_port)}
        if bound_port == 80:
            self.allowed_hosts.update({authority, "localhost"})

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        with self._active_lock:
            self._active.add(request)
        try:
            super().process_request(request, client_address)
        except Exception:
            with self._active_lock:
                self._active.discard(request)
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._active_lock:
                self._active.discard(request)
            self._slots.release()

    def handle_error(self, request, client_address):
        pass  # Never print header/token/exception material.

    def server_close(self):
        with self._active_lock:
            connections = list(self._active)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        super().server_close()


class ApplicationHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AWO"
    sys_version = ""

    def setup(self):
        self.request.settimeout(self.server.idle_timeout)
        self.cancel = threading.Event()
        super().setup()
        self._timer = threading.Timer(self.server.connection_timeout, self._expire)
        self._timer.daemon = True
        self._timer.start()

    def _expire(self):
        self.cancel.set()
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def finish(self):
        self.cancel.set()
        try:
            self._drain_request_body()
        finally:
            self._timer.cancel()
        try:
            super().finish()
        except OSError:
            pass

    def _drain_request_body(self):
        """Consume an unread declared body so closing sends FIN, not RST.

        A rejection can be written before the body is read. Closing with
        unread bytes makes the stack send RST, which can discard the rejection
        response the client is about to read, so a bad Origin or token would
        surface as a connection abort instead of the structured 403/401.

        Draining is bounded by wall time, not by a per-read gap: a client that
        dribbles bytes must not keep the request thread and its connection
        slot alive. `read1` performs at most one socket read per call so the
        deadline is not stretched inside a buffered read.
        """
        if getattr(self, '_body_consumed', False):
            return
        # An empty request line (connect and close, idle timeout, or _expire)
        # returns before parse_request sets `command`; a bare attribute read
        # here would skip the timer cancel and leak one Timer per connection.
        if getattr(self, 'command', None) not in ('POST', 'PUT', 'PATCH', 'DELETE'):
            return
        headers = getattr(self, 'headers', None)
        if headers is None:
            return
        length = headers.get('Content-Length', '')
        remaining = int(length) if re.fullmatch(r'[0-9]{1,7}', length) else 65536
        deadline = time.monotonic() + 0.25
        try:
            while remaining > 0:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self.request.settimeout(min(0.1, left))
                chunk = self.rfile.read1(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            pass

    def log_message(self, format, *args):
        pass

    def handle_one_request(self):
        try:
            self.raw_requestline = self.rfile.readline(MAX_REQUEST_LINE + 1)
            if len(self.raw_requestline) > MAX_REQUEST_LINE:
                self.requestline, self.request_version, self.command = "", "HTTP/1.1", None
                self.send_error(414)
                return
            if not self.raw_requestline:
                self.close_connection = True
                return
            source = self.rfile
            self.rfile = _HeaderReader(source)
            try:
                parsed = self.parse_request()
            finally:
                self.rfile = source
            if not parsed:
                return
            if self.request_version != "HTTP/1.1":
                self.send_error(505)
                return
            handler = getattr(self, "do_" + self.command, None)
            if handler is None:
                self.send_error(405)
                return
            handler()
            self.wfile.flush()
        except (OSError, EOFError, ValueError, http.client.HTTPException):
            self.close_connection = True

    def send_error(self, code, message=None, explain=None):
        # BaseHTTPRequestHandler's messages can contain untrusted request text.
        self._json(code, {"id": None, "error": GatewayError("invalid_request").as_dict()})

    def handle_expect_100(self):
        self.send_error(417)
        return False

    def _json(self, status, value):
        self.close_connection = True
        wire = encode_json(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(wire)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if status == 401:
            self.send_header("WWW-Authenticate", "Bearer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(wire)

    def _reject(self, status, code="invalid_request"):
        self._json(status, {"id": None, "error": GatewayError(code).as_dict()})

    def _authorize(self):
        for name in ("Host", "Origin", "Authorization", "Content-Length",
                     "Content-Type", "Transfer-Encoding", "Expect",
                     "Upgrade", "Connection", "Sec-WebSocket-Version",
                     "Sec-WebSocket-Key", "Sec-WebSocket-Protocol",
                     "Sec-WebSocket-Extensions"):
            if len(self.headers.get_all(name, [])) > 1:
                self._reject(400)
                return None
        host = self.headers.get("Host", "").lower()
        if host not in self.server.allowed_hosts:
            self._reject(403)
            return None
        origin = self.headers.get("Origin")
        if origin is not None and origin != "http://" + host:
            self._reject(403)
            return None
        if self.headers.get("Transfer-Encoding") is not None or self.headers.get("Expect") is not None:
            self._reject(400)
            return None
        auth = self.headers.get("Authorization", "")
        if not re.fullmatch(r"(?i:Bearer) [!-~]{1,16384}", auth):
            self._reject(401, "unauthenticated")
            return None
        token = auth[7:]
        try:
            self.server.backend.principal(token)
        except Exception as exc:
            error = safe_error(exc)
            self._json(HTTP_ERRORS.get(error.code, 500), {"id": None, "error": error.as_dict()})
            return None
        return token

    def do_POST(self):
        token = self._authorize()
        if token is None:
            return
        if self.path != "/call":
            self._reject(404, "not_found")
            return
        length = self.headers.get("Content-Length", "")
        if not re.fullmatch(r"0|[1-9][0-9]{0,9}", length):
            self._reject(411)
            return
        length = int(length)
        if length > MAX_INPUT:
            self._reject(413)
            return
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            self._reject(415)
            return
        try:
            body = read_exact(self.rfile, length)
            self._body_consumed = True
            value = decode_json(body)
        except (ValueError, UnicodeError, RecursionError):
            self._reject(400)
            return
        except (OSError, EOFError):
            self._reject(408)
            return
        status, result = application_call(self.server.backend, token, value, self.cancel)
        self._json(status, result)

    def do_GET(self):
        token = self._authorize()
        if token is None:
            return
        if self.path != "/ws":
            self._reject(404, "not_found")
            return
        if (self.headers.get("Upgrade", "").lower() != "websocket"
                or "upgrade" not in {x.strip().lower() for x in self.headers.get("Connection", "").split(",")}
                or self.headers.get("Sec-WebSocket-Version") != "13"
                or self.headers.get("Content-Length", "0") != "0"
                or self.headers.get("Sec-WebSocket-Extensions") is not None
                or self.headers.get("Sec-WebSocket-Protocol") is not None):
            self._reject(400)
            return
        key = self.headers.get("Sec-WebSocket-Key", "")
        try:
            decoded = base64.b64decode(key, validate=True)
            if len(decoded) != 16 or base64.b64encode(decoded).decode("ascii") != key:
                raise ValueError("key")
        except (ValueError, UnicodeError):
            self._reject(400)
            return
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode("ascii")
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        self._websocket(token)

    def _websocket(self, token):
        fragments = None
        frame_count = 0
        try:
            while not self.cancel.is_set():
                fin, opcode, payload = read_frame(self.rfile)
                if opcode == 8:
                    validate_close(payload)
                    write_frame(self.wfile, 8, payload)
                    return
                if opcode == 9:
                    write_frame(self.wfile, 10, payload)
                    continue
                if opcode == 10:
                    continue
                if opcode == 2:
                    raise WebSocketError(1003)
                if opcode == 1:
                    if fragments is not None:
                        raise WebSocketError()
                    fragments, frame_count = bytearray(), 0
                elif fragments is None:
                    raise WebSocketError()
                frame_count += 1
                if frame_count > 1024 or len(fragments) + len(payload) > MAX_INPUT:
                    raise WebSocketError(1009)
                fragments.extend(payload)
                if not fin:
                    continue
                try:
                    bytes(fragments).decode("utf-8", "strict")
                except UnicodeError:
                    raise WebSocketError(1007) from None
                try:
                    value = decode_json(bytes(fragments))
                except (ValueError, RecursionError):
                    write_frame(self.wfile, 1, encode_json({"id": None,
                        "error": GatewayError("invalid_request").as_dict()}))
                    fragments = None
                    continue
                fragments = None
                _, response = application_call(self.server.backend, token, value, self.cancel)
                write_frame(self.wfile, 1, encode_json(response))
        except WebSocketError as exc:
            write_frame(self.wfile, 8, struct.pack("!H", exc.close_code))
        except (OSError, EOFError):
            return

    def do_OPTIONS(self):
        self._reject(403)

    def do_HEAD(self):
        if self._authorize() is not None:
            self._reject(405)


def create_server(state, project_root, *, host="127.0.0.1", port=0,
                  messages_enabled=False, extensions_enabled=False, call_timeout=30, idle_timeout=10,
                  connection_timeout=60, max_connections=16):
    backend = ServiceBackend(state, project_root, messages_enabled=messages_enabled,
                             extensions_enabled=extensions_enabled,
                             call_timeout=call_timeout)
    return ApplicationHTTPServer((host, port), backend, idle_timeout=idle_timeout,
                                 connection_timeout=connection_timeout, max_connections=max_connections)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--messages-enabled", action="store_true")
    parser.add_argument("--extensions-enabled", action="store_true",
                        help="enable project extensions; MCP core catalog is unchanged")
    parser.add_argument("--call-timeout", type=float, default=30)
    parser.add_argument("--idle-timeout", type=float, default=10)
    parser.add_argument("--connection-timeout", type=float, default=60)
    args = parser.parse_args(argv)
    try:
        server = create_server(args.state, args.project_root, host=args.host, port=args.port,
                               messages_enabled=args.messages_enabled, call_timeout=args.call_timeout,
                               extensions_enabled=args.extensions_enabled,
                               idle_timeout=args.idle_timeout, connection_timeout=args.connection_timeout)
    except Exception:
        sys.stderr.write("awo-http: invalid configuration\n")
        return 2
    try:
        sys.stderr.write("awo-http: loopback port " + str(server.server_address[1]) + "\n")
        server.serve_forever(poll_interval=.1)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
