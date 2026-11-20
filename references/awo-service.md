# Opt-in V2 service boundary

`scripts/awo_service.py` is an optional foundation for the V2 application-service
boundary. It is additive and disabled by default. It does not replace the
existing controller, dispatcher, monitor, evidence, review, or claim logic.

## Working usage

A local state database can be inspected without writing:

```sh
python -B scripts/awo_service.py --state STATE status
```

The migration is a read-only dry run unless `--apply` is present. Applying
requires the active controller and writes the additive v2 projection in one
transaction:

```sh
python -B scripts/awo_service.py --state STATE migrate
python -B scripts/awo_service.py --state STATE migrate --apply \
  --actor INSTANCE --version 1 --context main
```

After applying, a controller can freeze dispatch, unfreeze after resolving V2
slot/uncertain records, or switch authority. `freeze` and `unfreeze` are separate
commands; `enable` and `disable` are separate commands:

```sh
python -B scripts/awo_service.py --state STATE freeze \
  --actor INSTANCE --version 1 --context main --reason "..."
python -B scripts/awo_service.py --state STATE unfreeze \
  --actor INSTANCE --version 1 --context main
python -B scripts/awo_service.py --state STATE enable \
  --actor INSTANCE --version 1 --context main
python -B scripts/awo_service.py --state STATE disable \
  --actor INSTANCE --version 1 --context main
```

With V2 enabled, the controller can create and update draft tasks through the
envelope. The envelope supplies an idempotency key, the tool, the current
controller epoch, and the payload; the principal and project come from the
verified local binding and ledger:

```json
{
  "command_id": "cmd-demo-0001",
  "tool": "task.create",
  "expected_epoch": 1,
  "payload": {
    "task_id": "T-DEMO",
    "title": "Draft task",
    "body": "Local draft only.",
    "priority": "P3"
  }
}
```

```sh
python -B scripts/awo_service.py --state STATE command \
  --envelope envelope.json --actor INSTANCE --version 1 --context main
```

Reads are available without enabling V2:

```sh
python -B scripts/awo_service.py --state STATE task-get --task T-DEMO \
  --actor INSTANCE --version 1 --context main
python -B scripts/awo_service.py --state STATE task-list \
  --actor INSTANCE --version 1 --context main
```

## What is implemented

- Additive `v2_*` SQLite tables: project, stable task, assignment, evidence,
  review, legacy mapping, command log, event log, service state, migration run,
  and migration-uncertain findings.
- A conservative legacy-to-v2 projection:
  - `migrate` is a read-only dry run by default.
  - `migrate --apply` requires the active controller and writes in one
    `BEGIN IMMEDIATE` transaction.
  - Applying is repeat-safe and does not drop or rewrite legacy tables. Re-applying
    refreshes legacy-owned projections (task state, assignment slot, evidence and
    review integrity) while preserving sealed original hashes and V2-owned draft
    fields, so changed evidence cannot remain accepted in the enabled projection.
  - Enabling recomputes a fresh source snapshot under its lock and refuses a
    stale/incomplete migration; a new closed legacy task must be re-applied
    first, never silently omitted at authority handover.
  - Every legacy task row is included, including registered-only rows with no
    dispatch. Registered-only rows get a `historical_registered` assignment that
    preserves the recorded request generation.
  - `resolved` never implies `accepted`. Acceptance reuses the existing
    `workflow_scheduler.acceptance()` gate and additionally checks the reviewed
    event, the recomputed frozen request digest, task/nonce binding, and sealed
    review/artifact hashes. A corrupted frozen request is uncertain, never
    accepted or review_ready.
  - Missing, changed, unreadable, or unsealed evidence/reviews become
    `migration_uncertain`, never `review_ready` or `accepted`.
  - A resolved Task is the authoritative closure. `release()` never writes
    `dispatches.status = resolved`, so the legacy dispatch status is preserved
    separately as provenance and a closed accepted Task is not shown as a
    running Assignment.
  - A valid independently recorded termination note (path, hash, task/nonce in
    the audit) releases a slot even without acceptance; the Task remains
    `migration_uncertain` because no acceptance is fabricated. Missing, changed,
    or unbound termination notes, heartbeat loss, timeout, and silence do not
    release a slot. Acceptance and quiescence are independent.
  - History has no usage rows; the foundation does not invent zero cost.
- Service commands with a command envelope:
  - `command_id`, `tool`, `expected_epoch`, `payload`.
  - The principal and project come from the verified local binding and the
    workflow ledger; a body-supplied role is never trusted.
  - Replaying the same principal/command/payload returns the stored response;
    the same command with different input conflicts.
  - For Task lifecycle metadata, `task.create` and `task.update` are the only
    implemented write tools. The registry adds Agent/enrollment/capability/
    heartbeat writes (see [awo-registry.md](awo-registry.md)); those do not
    dispatch work or change any Task/Assignment lifecycle.
  - `task.update` requires `expected_revision` and only edits draft tasks.
- Freeze/epoch controls:
  - `freeze` blocks legacy register/dispatch, the final send in dispatch, and
    the final send in `continue_task`.
  - `unfreeze` and `disable` refuse while a V2 assignment holds a slot or is
    uncertain.
  - Freezing never claims that a running external process stopped; reads,
    evidence collection, review, settlement, and inspected closure remain
    available.
  - V2 is disabled by default. Enabling requires an applied migration and no
    running task or genuinely unclosed dispatch. Existing claim closure is
    `tasks.status = resolved`; legacy `release()` does not write
    `dispatches.status = resolved`, so resolved tasks are treated as closed.
  - When V2 is enabled, legacy dispatch/register is blocked. V2 execution uses
    the opt-in priority scheduler and its Application command boundary; enabling
    the service does not itself dispatch work or replace a Controller decision.

`status` is read-only and does not create schema or WAL files. Migration dry-run
is also read-only. `migrate --apply`, freeze, unfreeze, enable, disable, and
service writes require the active controller and the existing dispatch
permission; an explicit empty permission list grants no new authority.

## Field authority

- Legacy `tasks`, `dispatches`, `events`, `reviews`, and `audit` remain the
  execution-safety authority for existing attempts: nonces, frozen request
  hashes, dispatch state, review originals, and claim closure are read from
  them, not re-derived.
- V2 Task/Assignment tables are read-only unless V2 is enabled; the registry
  tables (Agent/enrollment/capability) are writable once the additive schema is
  applied, independent of `v2_enabled`. Registry admission never dispatches work.
- The service writes only V2 draft-task rows and registry rows. It does not write
  legacy lifecycle rows. When V2 is enabled, legacy dispatch/register is blocked;
  the legacy rows remain provenance and execution-safety evidence.
- Registry authentication differs by entry point: controller commands use the
  active controller binding; `agent-register` uses the one-time grant secret
  file; `agent-self-register` is an unapproved pending request; heartbeat and
  status use the Agent token. See [awo-registry.md](awo-registry.md).

## Limits

The service boundary itself does not schedule, enforce budgets, send messages,
create worktrees, execute merges, or serve MCP/UI. Those capabilities are
separate opt-in modules documented under `references/awo-*.md`; `enable` is a
write gate, not a working replacement for the scheduler. Do not treat the empty
v2 tables as implemented capabilities.

## Tests

`scripts/test_awo_service.py` covers dry-run non-mutation, transaction rollback,
repeat migration, registered-only rows and generation preservation, resolved
rejection, accepted with sealed evidence, missing/changed artifacts and reviews,
non-review-ready accept rows, uncertain slot retention, accepted-but-unclosed
slot retention, closed accepted history, role permission fencing, idempotency,
revision conflict, stale epoch, freeze blocking register/dispatch/continuation,
malformed receipts, and a real CLI subprocess exercise over temporary SQLite
states.

## Other limits

- Not applied to any production or shared control state. The migration only runs
  on an explicit local `--apply` against the given state.
- No full bidirectional lifecycle routing; that is why V2 stays disabled by
  default and enabling blocks legacy dispatch.
- No live Harness capability claim is made by this foundation.
