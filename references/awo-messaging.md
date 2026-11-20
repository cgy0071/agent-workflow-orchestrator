# P1-1 durable agent messaging

English | [Chinese](awo-messaging.zh-CN.md)

`scripts/agent_messages.py` implements the messaging domain in the existing AWO
SQLite database. The application service remains the authority for authentication,
controller epoch/freeze policy, command idempotency, the outer command event, and
transaction commit/rollback. This module adds no endpoint or schema-version
change of its own; the application service registers the messaging tools above
the domain functions below.

## Integration contract

```python
create_message_schema(con)
apply_command(con, project_id, principal, tool, payload)
read_messages(con, project_id, principal, filters=None)
expire_deliveries(con, project_id, principal)
```

- `con` is a `sqlite3.Connection` to the authoritative database with foreign keys
  enabled. Both default tuple rows and `sqlite3.Row` work in the domain. The parent
  replay helpers require `sqlite3.Row`.
- Schema creation, commands, and expiry maintenance require an already active
  transaction. The caller should use `BEGIN IMMEDIATE`, then commit the domain
  result, command log, and outer event together, or roll everything back on any
  exception. The module never opens a connection, starts/commits a transaction,
  calls `executescript`, or changes the shared schema version.
- The parent creates the accepted service/registry schema first, then calls
  `create_message_schema` in its authorized schema transaction. The helper is
  repeatable and adds only `v2_message`, `v2_message_delivery`, and their indexes.
- `principal` is a service-derived plain dict with `agent_id`, `role`,
  `principal_id`, and `project_id`. All four are required. `principal_id` is the
  parent's opaque audit/replay identity; it is not a credential. The domain checks
  the project and active registry entry. Its role must match, except that the
  accepted access service may downgrade an existing agent to observer access.
  That identity can only read its own inbox; it gains no write rights.
  It cannot authenticate a dictionary supplied directly by an untrusted caller.
- Commands accept only `message.send` and `message.ack`. They return plain dicts
  or raise the parent's `ServiceError`, imported lazily, with a code prefix such
  as `invalid_request`, `permission_denied`, `state_conflict`,
  `revision_conflict`, or `transaction_required`.
- Domain events use the existing `awo_service._event` lazily, with the principal
  ID as actor, source `v2`, and `source_seq=None`. The module never writes the
  command log or replays a command. The parent validates auth/epoch before looking
  up `(project_id, principal_id, command_id)`, verifies the original request hash,
  and returns the recorded response on replay without calling the domain again.

## Send

Required payload fields are `to`, `kind`, and `body`. `to` is a list of 1..50
registered agent IDs in the same project. Recipients must be active; duplicates
are collapsed into one delivery. The body must be nonempty UTF-8 text, at most
65,536 encoded bytes, without NUL. The seven supported kinds are:

| Kind | Domain behavior |
|---|---|
| `question` | Reject self recipients; permit at most one answer. |
| `answer` | Require `reply_to` a live question addressed to this sender; route only to the question's sender. |
| `handoff` | Deliver context; do not transfer assignment ownership. |
| `notice` | Deliver information, including claimed status; do not apply that status. |
| `consult` | Deliver a request for advice; do not transfer ownership. |
| `state_request` | Deliver a request for a status report; the recipient may reply with `notice`. |
| `steer` | Record a direction request for a current running assignment; never inject it. |

Optional fields are `task_id`, `assignment_id`, `generation`, `thread_id`,
`reply_to`, `priority` (`P0`..`P4`, default `P3`), and `expires_at` (a future ISO
timestamp with timezone, normalized to UTC). Null optional binding fields mean
unbound. `generation` is a positive integer, not a boolean; it is mandatory with
`assignment_id` and forbidden without it. Assignment-only messages infer
`task_id`; a supplied task must match. A newer assignment generation in the same
task slot invalidates a message targeting the older generation.
The assignment's owner must also be a registered agent in the same project.

Workers may originate bound messages only for their own assignment/task.
Controller and reviewer may originate messages about any task in the project.
A recipient may reply in the exact binding of a received message without owning
that assignment. This permits a peer or reviewer to answer a worker's question.
Replies inherit task, assignment, generation, and thread; conflicting values are
rejected. A reply cannot restore a superseded assignment generation.

Controller, worker, and reviewer may send. Observer cannot send. For steering,
only controller/reviewer may send, and `to` must contain exactly the assigned
agent of the matching, slot-holding, running assignment. All kinds, including
answers containing approval or completion language, leave Task, Assignment,
review, ownership, slot, and budget state unchanged.

New root messages default to their own message ID as thread ID. A caller may
provide a new thread ID. Joining an existing thread requires prior sender or
recipient participation in that project. Thread membership does not grant read
access to messages addressed to other agents.

Successful send returns:

```text
{
  status: "CREATED", message_id, thread_id, revision: 1, event_id,
  deliveries: [{delivery_id, recipient_agent_id, state: "held", revision: 1}, ...]
}
```

An answer atomically inserts its message/delivery and sets the answering
recipient's question delivery to `answered`, incrementing that delivery's
revision. Other recipients' deliveries retain their receipt status. The
`v2_message_one_answer` partial unique index prevents a second answer even if a
caller bypasses the Python precheck. An answer is a response only to `question`;
other conversations can use `notice`/`consult` with `reply_to`.

## Delivery, ACK, TTL, and replay

Delivery IDs are generated once from message ID plus recipient agent ID and
persisted with a unique `(message_id, recipient_agent_id)` constraint. Repeated
inbox reads, reconnects, and parent command replay expose the same IDs. A lost
ACK leaves the same durable delivery available for retransmission; no replacement
message or delivery is created. A distinct send command creates a distinct
message even when its body is identical.

`message.ack` requires exactly `delivery_id`, `state` (`read` or `acknowledged`),
and `expected_revision` (a positive integer). The domain rechecks revision in
addition to any parent validation. Only the delivery's recipient may ACK it,
including when the caller is a controller or reviewer; observer cannot ACK.
Unread deliveries can become `read` or directly `acknowledged`, and `read` can
become `acknowledged`. Each change increments revision and writes a domain event.
An ACK of the same state/current revision is a no-op (`status: "UNCHANGED"`).
Backward transitions, expired/answered deliveries, and stale revisions fail.

ACK success returns `status: "UPDATED"`, `message_id`, `delivery_id`, `state`,
the new `revision`, and `event_id`. The no-op response omits `event_id`. Parent
replay of a committed ACK returns the original result even though its original
expected revision is now stale; a fresh command using that stale revision fails.

TTL expires at `now >= expires_at`. An unread delivery (`held`, `sending`,
`pushed`, or `notified`) then has effective state `expired`. Messages are retained.
The deadline persists in SQLite; pure reads project expiry without writing.
`expire_deliveries` lets an active controller persist unread expiry for its
project, increment delivery revisions, and write `message.expired` events in the
caller's transaction. It returns `{status: "EXPIRED", expired_count}`. Repeating
maintenance is a no-op for already expired deliveries.

Read, acknowledged, and answered deliveries are not expired by maintenance.
Nevertheless, a question past its deadline cannot receive an answer, even if
already read or acknowledged. A previously read delivery may still be
acknowledged after its message's deadline; that records receipt, not an answer.
Historical delivery ACK validates recipient/revision, not renewed assignment
execution authority.

## Pure inbox reads

`read_messages` returns `{messages: [...], limit, offset}`. Each result includes
the stored message columns, `delivery_id`, `recipient_agent_id`, `state` (effective
TTL state), `stored_state`, `revision` (delivery revision, for ACK),
`message_revision`, receipt timestamps, and `message_expired`.

Supported filters are `message_id`, `delivery_id`, `thread_id`, `task_id`,
`assignment_id`, `kind`, `state`, `limit` (1..500; default 100), and `offset`
(a nonnegative signed 64-bit integer; default 0). Order is stable by delivery creation timestamp and ID.
State filtering precedes pagination. Every role sees only its own addressed
deliveries. There is no sender outbox or implicit controller-wide inbox access.
Foreign/unaddressed IDs yield an empty result. The read works on a SQLite
read-only connection and never marks read, refreshes heartbeats, expires stored
rows, writes events, or changes revisions. `mark_read`, recipient overrides, and
unknown fields are rejected.

## Steering capability boundary

The domain reads the recipient's `steer` capability from the accepted registry.
A supported record must match current harness version, OS, and host, have an
eligible evidence source and a nonfuture timezone-qualified check time, and have
an existing evidence file whose SHA-256 still matches. Missing, unknown, stale,
self-declared, or unreadable evidence stays `capability_level: "unknown"` in the
message snapshot. The registry itself is not rewritten.

- Supported evidence: `steer_status: "queued"`, `capability_level: "supported"`.
- Missing/unverified evidence: `steer_status: "queued"`, `capability_level: "unknown"`.
- A context-matching unsupported record: `steer_status: "unsupported"`,
  `capability_level: "unsupported"`.

The response also includes `capability_reason`. Every steering message remains
a durable inbox delivery. `queued` means available for recipient consumption,
not sent to a harness or applied to a process. No transport, subprocess,
provider call, fake injection, task transition, or automatic retry is performed.

## Scope and verification

Direct registered agent IDs are implemented. Human/task/run address aliases,
channels, gates/approval execution, push transport, and long polling are not
implemented; unsupported payload/address forms fail explicitly. Representing
an Owner requires a service-authorized registered identity. Identity, command
IDs, epoch, and gate fields in message payloads are rejected. The application
service wires these tools (`awo_message_send`, `awo_message_read`,
`awo_message_ack`) through the existing MCP/HTTP catalog, the `api` CLI, and the
command replay path. A task- or run-scoped credential can only act on a delivery
whose recorded assignment matches exactly; a task-only message with a NULL
assignment cannot be read, acknowledged, replied to or sent to through such a
credential, and durable replay cannot bypass that check.

```powershell
python -B scripts/awo_service.py --state STATE api --project-root ROOT --token-file TOKEN --tool awo_message_send --arguments ARGS.json
python -B scripts/awo_service.py --state STATE api --project-root ROOT --token-file TOKEN --tool awo_message_read --arguments ARGS.json
```

Run the real SQLite tests with:

```sh
python -B -m unittest discover -s scripts -p "test_agent_messages.py" -v
python -B scripts/check_encoding.py
```

Tests cover the four-role matrix, invalid principal/recipient/binding, strict
ACLs, all seven kinds, unique-answer SQL enforcement and competing connections,
TTL/read-only purity, ACK revisions, atomic rollback with events, truthful
capabilities, parent replay scope, and unread/ACK/replay persistence across a
fresh Python process. These are offline domain tests, not claims of a live
harness injection or an installed service endpoint.
