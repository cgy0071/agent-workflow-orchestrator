# Collaboration runtime: fanout, ensemble and bounded repair

English | [Chinese](awo-collaboration.zh-CN.md)

`scripts/collaboration_runtime.py` implements the P1-3/P1-4 domain sidecar.
SQLite remains the authority. One stable native Task owns distinct Assignments,
generations, slots, legacy attempts and nonces; no business Task is cloned.
The module does not dispatch model workers, accept Tasks, merge changes, stop
competitors, or release workspace/resource/budget claims. Parent implementation
owns scheduler/service integration and final integrated acceptance.

## API and transactions

```python
create_collaboration_schema(con)
apply_command(con, project_id, principal, tool, payload)
query(con, project_id, principal, query, payload)
prepare_enqueue(con, project_id, principal, task_id, slot_id,
                contract, expected_revision)
check_admission(con, project_id, principal, ticket_id)
admit_in_transaction(con, project_id, principal, ticket_id)
review_guard(con, project_id, principal, task_id)
acceptance_guard(con, project_id, principal, assignment_id, event_id)
closure_projection(con, project_id, principal, assignment_id, outcome)
run_verification(con, project_id, principal, verification_id)
```

Domain writes and schema creation require a caller transaction, normally
`BEGIN IMMEDIATE`. They never commit or roll back. Parent must roll back the
whole command on any failure, including native budget failure after Assignment
insertion. Do not catch a failed command and commit partial work. Schema creation
uses transactional statements, not `executescript`, and leaves native versions alone.

`run_verification` is the explicit external-effect exception: a dedicated
**idle** connection, disk-backed SQLite, journaling, and `synchronous=FULL` or
`EXTRA` are required. It owns two short transactions around the process and
cannot execute inside the service command transaction.

Trusted principal: `{"role": ..., "agent_id": ..., "principal_id": ...}`.
Parent authenticates it and enforces scopes, epoch, replay and envelope revisions.
Module checks active project registration, role, Task/Assignment/slot ownership,
policy revision and independent review. Optional `principal.project_id` must
match. Payload fields cannot impersonate principals. Domain errors are
`CollaborationError(ValueError)` with `code`; native gate failures use `native_gate`.

## Commands, queries and limits

Names allow an optional `awo_` prefix; unknown fields are rejected.

| Command | Payload | Result |
|---|---|---|
| `collaboration_configure` | `task_id, expected_revision, mode, slots, max_attempts, max_repairs, verifier` | Policy/native Task revisions and config hash |
| `collaboration_verify` | `task_id, expected_revision, assignment_id, event_id` | `verification_id, status=READY, process_started=false, revision` |
| `collaboration_propose` | `task_id, expected_revision, assignment_id, event_id`; optional `contributors` | Unique proposal ID, manifest hash, `accepted=false`, revision |
| `collaboration_withdraw` | `task_id, expected_revision, reason` | Retained withdrawn proposal ID and revision |
| `collaboration_cancel_ticket` | `ticket_id, expected_revision, reason` | Cancelled unadmitted ticket ID and revision |

Configure checks **native Task** revision and is allowed only on draft/ready
Tasks before any Assignment or native queue entry. The resulting policy is
immutable. All other revisions are **collaboration policy** revisions. Enqueue,
admission, verification approval/attempt/receipt also advance policy revision.
Native Task revision remains a separate parent-envelope concern.

`slots` is a list of `{"slot_id": int, "agent_id": str}`. Single uses slot 0.
Fanout uses contiguous competitor slots 1..N, N=2..8. Ensemble also requires an
explicitly approved synthesis worker in slot 0; native `fanout_slots` is N,
excluding synthesis. All approved workers need distinct native physical identities.

`verifier` requires `argv` (list of strings), `timeout_seconds`,
`max_output_bytes` and `max_runs_per_assignment`. Example:

```json
{
  "argv": ["python", "-B", "-m", "unittest", "test_deliverable"],
  "timeout_seconds": 60,
  "max_output_bytes": 65536,
  "max_runs_per_assignment": 2
}
```

This argv is explicit Controller approval, not an automatic default. Executable
path/hash is frozen at configure time; relative arguments resolve in delivery
cwd. Shell batch files are rejected. Libraries, environment and scripts remain
trusted Controller inputs: executable hashing is not an OS sandbox or a
hermetic build guarantee.

Maximums: 128 admitted/queued attempts per Task, 64 consumed repairs, 256 retained
approval tickets, 8 verifier approvals per Assignment, 64 argv elements,
300 seconds per verifier and 1 MiB total retained stdout/stderr. Configured
limits may be lower. Cancelled initial tickets retain identity/history but do
not count as executed attempts. Cancelled repair tickets never unconsume reviews.

| Query | Payload | Read-only result |
|---|---|---|
| `collaboration_get` | `task_id` | Task, policy/config, tickets, verifier intent states/hashes, current proposal |
| `collaboration_history` | `task_id`; optional `after_seq=0, limit=100` (1..100) | Audit events and `next_after_seq` |

Queries never write, reconcile, expire, release or launch. Workers can query only
Tasks with a slot approved for them. Active controller/reviewer/observer
principals can query their own project.

## Scheduler integration

1. In `_enqueue` construct the native contract, then call `prepare_enqueue`.
   Required contract keys: `task_id, agent_id, request, message, budget`.
   Optional native keys: `required_capabilities, resources, dependencies,
   worker_binding, workspace_mode, task_kind, dag_run, dag_node,
   approved_controller_id, approved_controller_version, approved_epoch`.
   The hook fixes generation/binding and supplies native defaults, returning
   ticket ID, slot, generation, revision, frozen contract and hash.
2. Queue/admit by **ticket**, not an overwriteable single Task queue row.
   Generations include pending tickets and historical Assignments; admission
   order may differ across slots.
3. Retain final native authorization, epoch, DAG, capability, live-idle,
   workspace, capacity and resource gates. Do not apply the old single-only
   candidate or max-admitted-generation-plus-one check. `check_admission` is
   read-only, never a reservation.
4. In the final transaction, `admit_in_transaction` rechecks domain gates,
   inserts native Assignment and calls native `budget._reserve_in_transaction`.
   It returns `assignment_id, run_id, reservation_ids, generation, contract,
   revision, sent=false`. Parent persists legacy task/map/reservation/dispatch
   intent and native Task runtime state in the **same transaction**; send only
   after commit.
5. Call `review_guard` before native `review_in_transaction`. It excludes every
   approved competitor/synthesis identity and historical Assignment/frozen
   binding, including aliases and losing workers.
6. Call `acceptance_guard` before accepting the selected candidate. Use
   `closure_projection` before closing an Assignment and apply its Task state
   instead of the old unconditional single-Assignment overwrite. Native
   termination/quiescence checks and actual per-Assignment releases remain
   mandatory. The projection itself releases nothing.

Unique winner proposal requires current completed evidence and successful
verification; it never accepts or chooses the first finisher. Withdraw before
authorizing/admitting more work. Ensemble pins synthesis output plus at least
two distinct competitor event snapshots; the independent review covers the
synthesis. Source outputs are not implicitly accepted. Any changed candidate
or contributor hash invalidates acceptance.

## Bounded repair

Consume the existing `reviews/events/tasks` sealed chain and an already prepared
native `workflow_scheduler.follow_up` request/message. No alternate review store
or follow-up file generation exists here. Parent must be the latest same-slot
legacy attempt in this stable Task; exact review fields must authorize repair.

Prior Assignment requires `holds_slot=0, review_status=repair`, a closed native
intent, matching native review/map seal and the original closure's authoritative
quiescence/identity observation. TTL, heartbeat loss, uncertain status, cancellation
requests and invented closure objects do not qualify. Changed/dead reviews,
wrong Task/nonce/hash and reused parent events fail. A cancelled repair approval
does not return its single-use parent event.

Competitors and repairs share native Task/workflow cumulative budgets. Existing
unknown usage, repair-count policy and reservation settlement semantics remain
unchanged. Finite local attempt/repair caps supplement native gates. No held or
unknown reservation is released. Partial failure/cancellation preserves other
slots and any accepted Task.

## Durable verification and crash behavior

1. `collaboration_verify` writes immutable approval and returns READY inside the
   caller transaction; no subprocess. Caller commits.
2. Parent explicitly invokes `run_verification` on a dedicated idle connection.
   Runner rechecks intent owner, native controller/epoch, enabled/frozen state,
   executable and completion hashes, then commits `attempted` with a unique
   token **before** `Popen(shell=False)`.
3. Runner bounds process timeout and retained output; records actual PID, exit,
   start/end times, observed/retained output byte counts/hashes and before/after
   artifact snapshots; commits immutable receipt and `recorded`.
4. Recorded reentry replays the receipt. Attempted reentry returns UNCERTAIN
   without launching. Pending/uncertain verification blocks acceptance and
   another verification approval for that Assignment.

A crash between attempt commit and Popen cannot be distinguished from a launched
process with a lost receipt; both stay uncertain even if no effect occurred.
Receipt persistence failure is also uncertain. No reset or automatic retry API
exists. Parent must inspect actual processes and filesystem effects; SQLite
cannot undo them. Normal recorded failure permits another explicit approval
within the finite run cap.

Timeout/output overflow kills the direct verifier; missing pipe EOF fails
closed. This does not prove descendant or worker process-tree quiescence:
external inspection may still be needed. Verifier exit never releases claims.
Use bounded read-only commands with controlled subprocess behavior.

## Schema, write ACL and evidence

Additive `v2_collaboration_` tables: `meta, policy, ticket, proposal,
verify_intent, verification, audit`. Policy/approval identities are immutable;
tickets and verifier attempts cannot reset. Withdrawn proposals are retained.
Receipts/audit are append-only. Native schedule/evidence/review schema is unchanged.

Every command, enqueue/admission, acceptance, closure and explicit runner is
controller-only. Review guard allows controller/reviewer. Queries use the
project/worker ACL above. Configure writes native Task mode/slots/revision;
admission inserts native Assignment and uses existing budget authority.
All other writes remain in collaboration tables.

## Application CLI

`scripts/awo_service.py` exposes the bounded verification runner:

```powershell
python -B scripts/awo_service.py --state STATE collaboration-run --project-root ROOT --token-file TOKEN --verification VERIFICATION_ID --expected-epoch EPOCH
```

The token must be a project-wide controller credential; scoped credentials are
rejected. The command holds one dedicated connection with `synchronous=FULL`,
authenticates, checks the current epoch plus frozen/enabled state, rolls back,
and calls `run_verification` on that same idle connection. The runner commits
`attempted` before launching the verifier and never retries an uncertain attempt.
If the process is lost, inspect the recorded receipt and external effects and
reconcile with the original command_id instead of launching again.

```powershell
python -X utf8 -B -m unittest discover -s scripts -p test_collaboration_runtime.py -v
```

Tests use real SQLite, competing independent processes, local verifiers and Git.
The worker adapter is a labeled synthetic fixture; parent persistence seam is
fixture wiring, not proof of integrated fanout dispatch. Crash-before-launch
explicitly injects `os._exit` at the effect seam; another test kills a real runner
after a verifier file write. No provider call, production migration, commit or
push is part of this validation. Final parent dispatch/close acceptance is separate.
