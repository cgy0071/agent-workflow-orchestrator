# AWO gateway and application transports

English | [Chinese](awo-gateway.zh-CN.md)

`scripts/mcp_gateway.py` implements legacy MCP over stdio. `scripts/service_http.py`
implements application HTTP/JSON and WebSocket calls. These adapters use Python
3.10+ standard-library modules. Neither transport opens SQLite, implements domain
authorization, issues credentials, performs migrations, or retries commands.

## Service boundary and enablement

The parent application service provides exactly these adapter entry points:

```python
gateway_principal(state, project_root, token)
gateway_tool_specs(messages_enabled=False)
gateway_call(state, project_root, token, tool, arguments)
```

Authentication returns a nonempty principal mapping or raises. The service owns
token expiry/revocation, root binding, roles, enrollment, assignment ownership,
revision/epoch checks, command deduplication, transactions, journal and outbox.
The transport only validates protocol structure and the service's published
schemas. A tool's presence in discovery does not authorize its use.

Production calls execute the real service in a fresh, bounded subprocess. No
database or service implementation is substituted. The credential crosses the
private child's stdin, not its command line. Authentication is repeated on
discovery and calls, including every call on an existing WebSocket connection.
The principal is not exposed as response data. Exceptions expose stable machine
codes through `.code`; unclassified exceptions return `internal` with a trace ID,
without their text, traceback or filesystem paths. A typed service rejection at
the principal-authentication boundary defaults to `unauthenticated` when the
service has not provided a more specific code. No exception prose is parsed.

Discovery fails closed if service entry points or the complete catalog are
missing. The default P0 catalog has exactly these 13 tools:

| Family | Tools |
| --- | --- |
| Task | `awo_task_get`, `awo_task_list`, `awo_task_create`, `awo_task_next` |
| Assignment | `awo_assignment_claim`, `awo_assignment_get`, `awo_assignment_complete` |
| Agent | `awo_agent_register`, `awo_agent_heartbeat` |
| Evidence/review | `awo_evidence_record`, `awo_review_submit` |
| Resource | `awo_resource_claim`, `awo_resource_release` |

`--messages-enabled` explicitly requests the final 16-tool catalog, including
`awo_message_send`, `awo_message_read`, and `awo_message_ack`. The service must
actually supply all 16 implemented tools. An importable message module does not
enable this flag or establish message acceptance. The default stays at 13.
The gateway exposes no acceptance override, controller takeover, or implicit
Assignment cancellation. Completion does not mean acceptance or landing.

Every write schema must accept and require flat `command_id` and `expected_epoch`.
Updates also require `expected_revision`; task/agent creation and message send
are creation operations. Read schemas reject `command_id`. Schemas use explicit
object properties and `additionalProperties: false`. The adapter implements a
bounded JSON Schema subset: primitive types, required/properties, local `$defs`
and `$ref`, arrays, bounds, enum/const, pattern, date-time/URI formats and boolean
combinations. Unsupported keywords/references fail catalog validation instead
of being silently ignored. Domain policy is still checked by the service.

## Explicit protocol version

The supported MCP version is **2025-11-25, legacy initialize lifecycle**. This is
an explicit compatibility target, not a claim about the latest specification.
On 2026-11-10 the pinned official lifecycle, transport, tools, and cancellation
pages were retrieved. The implementation checkpoint separately identifies
modern `2026-07-28`; that era's `server/discover` lifecycle is not implemented.
The future fixture string `2026-11-25` is not treated as a supported revision.

Supported version requests echo `2025-11-25`. Other requested versions negotiate
to `2025-11-25`, and a client that cannot support it must disconnect. Simply
returning another date would not implement that date's protocol.

Supported methods are `initialize`, `ping`, `tools/list`, and `tools/call`;
notifications are `notifications/initialized` and `notifications/cancelled`.
Only the tools capability is advertised. There are no resources, prompts,
sampling, elicitation, task extensions, subscriptions, or list-change promises.
Input/output is UTF-8 NDJSON; each response occupies one line. Batch requests,
duplicate JSON keys, nonfinite numbers, invalid UTF-8, and excessive nesting
are rejected. Notifications never execute tools or produce responses.

Official references:

- [Legacy lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle)
- [Legacy transports](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)
- [Legacy tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)
- [Legacy cancellation](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/cancellation)
- [WebSocket RFC 6455](https://www.rfc-editor.org/rfc/rfc6455)

## Local setup and first read

Use an already initialized, enabled test project and an explicitly issued,
root-scoped access-token file from the controller's service CLI. Do not pass a
token string on a command line or put it in harness configuration. See
[service configuration](awo-service.md) and [agent registry](awo-registry.md).
The gateway does not bootstrap or migrate a production ledger.

From the repository root, replace the example paths with the issued paths:

```powershell
python -B scripts/mcp_gateway.py --state E:/awo-test/state --project-root E:/awo-test/project --token-file E:/awo-test/secrets/access.txt
```

`AWO_AGENT_TOKEN_FILE` can supply the token-file path when `--token-file` is
omitted. `--state` and `--project-root` remain required; neither falls back to the
working directory. A root-scoped enrollment grant may authenticate a bootstrap
session only as allowed by the service; it authorizes registration alone, not
ordinary reads or writes. Other operations require explicitly issued access.

Use an NDJSON client that keeps stdin open while waiting for call responses.
The following lines show a first read. Send initialize, read its response,
then send initialized and tools/list before calling a tool:

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"local-driver","version":"1"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"awo_task_list","arguments":{}}}
```

Take write fields from the returned schema. Read current revisions/epoch through
the service before writing; do not guess them. Preserve the exact arguments and
original `command_id` when recovering an uncertain transport response. A new
intent or changed payload requires a new command ID after reconciliation.

## HTTP and WebSocket application calls

These are **application transports, not MCP Streamable HTTP**. There is no
`/mcp` route, SSE, MCP HTTP session, or browser-cookie authentication.

```powershell
python -B scripts/service_http.py --state E:/awo-test/state --project-root E:/awo-test/project --host 127.0.0.1 --port 8765
```

Binding accepts literal loopback IPs only (including `::1`). Nonloopback binds,
wildcards, and hostname binds fail. There is no LAN switch, TLS termination,
proxy trust, external-origin exception or automatic exposure. Use a configured
port when needed; `--port 0` chooses an ephemeral port and reports only that port
to stderr. A missing service remains unavailable; startup itself grants access
to no caller.

`POST /call` requires `Authorization: Bearer <token>`, `Content-Type:
application/json`, and one canonical `Content-Length`. Headers must contain the
bound authority as Host (or localhost with the same port). If Origin is present,
it must exactly equal `http://` plus that Host. Cross-origin requests, wildcard
CORS, cookies/query credentials, duplicate security headers, transfer encoding,
Expect, obsolete header folding and invalid body encoding are rejected.

HTTP request and WebSocket text-message body:

```json
{"id":"query-1","tool":"awo_task_list","arguments":{}}
```

The response is `{"id":"query-1","result":...}` or
`{"id":"query-1","error":{"code":...,"message":...,"retryable":false,
"uncertain":false,"trace_id":...}}`. No field is moved into or out of
`arguments`; HTTP and WS use the same `application_call` function. Invalid
transport input never invokes a domain command. The adapter never retries.
Successful calls return HTTP 200, authentication failures 401/403, invalid input
400, conflicts 409, capacity 429, unavailable service 503, and deadlines 504.

For WebSocket, use `GET /ws` with the same bearer header and a version-13 RFC 6455
upgrade. Use a client that can supply Authorization; the browser JavaScript
WebSocket constructor cannot supply an arbitrary authorization header. Query
tokens, cookies and subprotocol credentials are not alternate authentication.
The server validates the key, upgrade fields, masking, reserved bits/opcodes,
canonical lengths, continuation order, cumulative message size, UTF-8, and
close codes/reasons. Text fragmentation, ping/pong, and close are supported;
binary messages, extensions and negotiated subprotocols are rejected. Calls on
one WS connection run serially. Reconnect with the same command ID after an
uncertain disconnect. The transport does not offer event streaming.

## Limits, cancellation, and outcomes

| Limit | Value/default |
| --- | --- |
| Client JSON line/body/message | 1 MiB |
| Encoded response | 2 MiB |
| JSON nesting | 32 levels |
| Token file | 16 KiB; service may impose a stricter credential limit |
| MCP request ID | string up to 128 characters, or safe integer |
| Concurrent MCP calls/service children | 8 |
| HTTP request line/headers | 4 KiB / 16 KiB total |
| HTTP/WS connections | 16 |
| Call timeout | 30 s; `--call-timeout` may lower it |
| HTTP/WS idle timeout | 10 s; configurable up to 30 s |
| Absolute HTTP/WS connection lifetime | 60 s; configurable up to 300 s |
| Fragments per WS text message | 1024 |

The absolute connection limit also bounds trickling headers, fragments and
control frames. Oversized stdio input closes that session rather than draining
an unbounded peer stream. Stdin EOF cancels pending calls. Reconnect after the
finite WebSocket lifetime. Message calls currently share the 30-second limit;
there is no claim of implemented 60-second cancellable message long polling.

A deadline or cancellation kills that service invocation, not the Assignment
or any independently running worker. SQLite rolls back an uncommitted child
transaction, but a commit could precede termination. Therefore a call timeout,
broken child, or lost response is **outcome-uncertain**, not proof of rollback.
Timeouts return `retryable: false` and `uncertain: true`; cancelled MCP calls
suppress late responses, following the pinned cancellation specification.
Reconcile with the service and reuse the original command ID for the identical
command. Never automatically resend an uncertain external dispatch.

Stdout is reserved for protocol output; diagnostic text is limited to safe codes
on stderr. Service stdout/stderr is suppressed in child calls. Arbitrary
exception text is never returned; a response echoing its bearer token is
rejected. Tokens are not OS isolation: another process with the same filesystem
permissions may read token files or the ledger.

## Verification boundaries

```powershell
python -B -m unittest discover -s scripts -p "test_mcp_gateway.py" -v
python -B -m unittest discover -s scripts -p "test_service_http.py" -v
python scripts/check_encoding.py
```

`ProtocolOnlyBackend` is an explicitly labelled test fixture. Its tests establish
framing, schema enforcement, error redaction and envelope forwarding, not real
domain behavior. `RealServiceSubprocessTests` and `RealServiceHTTPTests` use
isolated SQLite state, actual service APIs and real child processes/sockets.
They compose `test_service_access.ServiceAccessTests` and test auth/root binding,
role rejection, restart replay, cross-transport command replay, revocation,
deadline/cancellation and no-mutation failures. Missing parent gateway APIs fail
these integration tests. The parent must run the final full service/integration
suite after all domain work is ready.

Raw worker evidence is stored outside the repository under the implementation
workspace's `P0-5/worker-evidence` and `P1-6/worker-evidence` directories. Version
probes are not live harness compatibility: Codex/Claude/Pi/DSH/OMP connections
remain unverified unless separately exercised with the configured real client.
