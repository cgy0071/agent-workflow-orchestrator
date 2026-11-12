# Budget ledger and reservations

The optional V2 service includes a local budget ledger behind the same
application-service envelope. It records admissions and usage; it does not
activate the scheduler, call a provider, or certify a provider hard cap.

## Scopes and dimensions

A budget scope is one of `run`, `task`, or `workflow`. Run/task/workflow scopes
are derived from the actual Assignment, its Task and the project, not chosen by
a caller:

- a run must be linked to exactly one `(assignment_id, generation)`; a run
  cannot later move to another assignment or settle another run;
- the Task is the Assignment's task; the Workflow is that Task's project.

Dimensions are named keys such as `tokens.input`, `tokens.output`, `cost.usd`,
`turns`, `tool_calls`, or `wall_clock_sec`. Limits and amounts must be finite
nonnegative numbers; booleans, NaN, Infinity, and numeric strings are rejected.
No limit means unlimited, not free execution. Every bounded dimension requires
an estimate when reserving.

## Reservation

A reservation is admitted across every derived scope/dimension in one
`BEGIN IMMEDIATE`; any failure rolls back all scopes, reservations and the run
link. A caller-owned in-transaction API is available for a later scheduler.

Reservations are keyed by `(budget_id, assignment_id, generation, dimension)`.
The same amount is a no-op even with a new command key, including at the exact
cap; a different amount or state conflicts. Unknown usage blocks admission by
default (`on_unknown: "block"`); a policy may set `warn` explicitly. Frozen
service state rejects new reservations.
Closed runs cannot receive new reservations, including previously unreserved
dimensions. Replaying a successful pre-close command remains a no-op.

## Usage, exposure, and close

Samples are per run/dimension and either a delta or a cumulative snapshot. A
cumulative sample replaces the delta total up to its sequence; deltas after it
add. Sample sequence numbers must be nonnegative integers and generations
positive integers; booleans, strings and fractional values are rejected.
Until a dimension settles, its effective usage is the maximum of that
run's reservation, stream total and reported measurement/estimate. These are
overlapping observations, not separate charges. Sum the per-run maxima across
runs; never take one workflow-wide maximum.

Estimated totals are separate from known actual totals. An estimated close is
not actual: it retains conservative unknown reservation/exposure and blocks new
admission by default until an authoritative reconciliation. A warned policy may
relax the unknown-data gate, but still enforces the numeric cap against retained
exposure. With a USD10 cap and USD10 reservation, an unknown or estimated USD1
close leaves zero nominal capacity even with `on_unknown: "warn"`. An estimate
of USD12 instead exposes USD12, not USD10 or USD22. Unknown stays unknown per
dimension; a known token total with unknown
USD is valid and is not free execution. Missing or unknown per-run dimensions
are reported as unknown, including historical imported Assignments with no usage.
Stream-only partial closes are projected as `partial_actual`, with provenance
and residual uncertainty; the partial observed value is not a final total.

Only an authoritative dimension whose usage state is `actual` settles and
replaces its stream/reservation once, even when another dimension is unknown.
Use whole-close state `partial` for mixed usage. An authoritative report does
not make its unknown/estimated dimensions actual. A non-authoritative actual
measurement is observable in `known_actual` but does not prove settlement; its
reservation remains unknown and bounded incomplete totals block new admission
by default. Stream-only `partial_actual` is likewise not a final total.

Both `authoritative` and `reconcile` require JSON booleans. Whole-close
`actual` requires a nonempty set of actual dimensions; whole-close `unknown`
cannot contain measured dimensions. Replay adds no charge. Reconciliation
requires an authoritative close, the current close revision, and the same
run/assignment/generation. It requires an existing close; first closure must
not use reconcile. A run cannot be rebound or settled twice. A changed
authority or price-table reference is not a duplicate. An uncertain correction
cannot discard or reduce an earlier measurement; supply an authoritative actual
replacement to release capacity. Close events retain the prior snapshot for audit.

Task/Workflow attempt and repair caps are derived from durable Assignment and
review records, not caller-provided zero values. Admission counts a prospective
assignment once; a new Assignment cannot reset the attempt total. Token,
wall-clock, attempt and repair aggregates are preserved across Assignments;
resource units remain separate occupancy claims and are never auto-released by
budget expiry or disconnection.

The status field `effective_usage` is the same total used by admission, warnings
and `nominal_remaining = max(0, limit - effective_usage)`. Do not add the
`known_actual`, `estimated`, `held`, `unknown_reserved` or raw
`stream_exposure` display fields to it: they overlap. Nominal remaining is
known-only and actual remaining may be lower; it is not a guaranteed lower bound.

## Existing databases

R1 databases need no schema change. Reads filter historical `authority_json`
entries against actual usage, so incorrectly listed unknown/estimated dimensions
retain exposure. Legacy reservation labels alone are not settlement evidence.
For an older database without authority metadata, reads conservatively retain
exposure without modifying the schema; use the existing authorized migration
command before writes, then reconcile with an authoritative actual report.
Missing authority is not inferred from a whole-close label or an estimate.

## Actions and authority

Only the active controller can set limits, approve, reserve, close, or record
warnings. Revision CAS, project binding, controller epoch and idempotency apply.
Warning, pause-request, approval-required and cancel-request are ledger states
and requests only; they do not prove a worker paused or terminated. Admission,
reservation and settlement are ledger measurements; they do not stop or
interrupt a model call already in progress. Provider
hard caps are an independent verified capability; this ledger does not create
one. Resource units remain occupancy claims, separate from spend, and are not
released by budget expiry or disconnection.

## CLI

```sh
python -B scripts/awo_service.py --state STATE budget-status \
  --scope-type task --scope-id TASK-ID

python -B scripts/awo_service.py --state STATE budget-limit \
  --envelope limit.json --actor INSTANCE --version 1 --context main

python -B scripts/awo_service.py --state STATE budget-reserve \
  --envelope reserve.json --actor INSTANCE --version 1 --context main

python -B scripts/awo_service.py --state STATE budget-sample \
  --envelope sample.json --actor INSTANCE --version 1 --context main

python -B scripts/awo_service.py --state STATE budget-close \
  --envelope close.json --actor INSTANCE --version 1 --context main

python -B scripts/awo_service.py --state STATE budget-action \
  --envelope action.json --actor INSTANCE --version 1 --context main

python -B scripts/awo_service.py --state STATE budget-approve \
  --envelope approve.json --actor INSTANCE --version 1 --context main
```

Example reservation envelope:

```json
{
  "command_id": "cmd-budget-reserve-0001",
  "tool": "budget.reserve",
  "expected_epoch": 0,
  "payload": {
    "run_id": "run-0001",
    "assignment_id": "asg-0001",
    "generation": 1,
    "dimensions": {"tokens.input": 10000, "cost.usd": 2.0},
    "basis": "estimate"
  }
}
```

Example authoritative close:

```json
{
  "command_id": "cmd-budget-close-0001",
  "tool": "budget.close",
  "expected_epoch": 0,
  "payload": {
    "run_id": "run-0001",
    "assignment_id": "asg-0001",
    "generation": 1,
    "state": "partial",
    "authoritative": true,
    "usage": {
      "tokens.input": {"state": "actual", "value": 9000, "source": "harness-result"},
      "cost.usd": {"state": "unknown", "reason": "provider_did_not_report", "source": "run-close"}
    }
  }
}
```

## Limits

No scheduler activation, messaging, provider setup, MCP server, fanout, worktree,
merge, provider hard cap, or live Harness certification. Agents and resource
claims are separate from this ledger. Reads do not create or migrate state.
