# AWO priority scheduler

[Chinese](awo-scheduler.zh-CN.md) | [Service](awo-service.md) | [Budget](awo-budget.md)

## What runs now

The opt-in V2 scheduler queues Controller-approved work, reserves one Assignment
per approved Task slot, and reuses command/Herdr transports, receipts and independent review.
Task is the stable objective; each attempt has a new Assignment, Run, generation,
attempt ID and nonce. Queue rows and send intents are metadata in the same
ledger, not another Task authority.

Enqueue uses existing directory contracts. Ordinary Tasks keep single execution
in slot 0; an explicit collaboration policy enables bounded fanout or ensemble
slots through the same native runtime. The scheduler
does not provision worktrees; that belongs to the separate workspace service.
Steering, preemption and reviewed-code materialization
are unavailable in this scheduler stage. Accepted code
dependencies are blocked pending materialization. Accepted non-code artifacts
can unlock downstream work after their bytes and independent review are
revalidated. A directory claim is not an OS sandbox.

## Setup and commands

Use Python 3.10+. Follow the [setup guide](setup-and-switching.md) to initialize
and verify Controller/Worker/Reviewer bindings; then migrate and explicitly
enable V2 using the [service guide](awo-service.md). Existing V2-owned state must
use guarded schema-upgrade for additive changes, not legacy import replay.

Before enqueue, record supported send/status/collect capabilities with readable
evidence hashes and matching harness version, host and OS. Additional required
capabilities are cumulative; unknown fails admission. The initial execution
identity must be the registry Agent imported from the exact Worker binding.
A same-harness alias or newly enrolled Agent cannot own that native session.
Enrollment needs a separately implemented, Controller-confirmed binding bridge.

If a Controller attaches a managed Workspace after reservation, dispatch lazily
checks its Assignment/host/fingerprint and marks it active in the transaction
that persists attempted. The second send guard permits that active state only
for the same attempted intent. Delivery request.cwd remains separate. Managed
review calls workspace_runtime.review_gate with the verdict and records its
inventory; accept requires valid scope. Accepted closure and dependency
consumption call verify_reviewed_workspace to detect post-review changes.
Unmanaged directory contracts do not invoke these optional hooks.

    python scripts/awo_service.py --state runtime/example command --actor CONTROLLER_ID --version 1 --envelope enqueue.json
    python scripts/awo_service.py --state runtime/example command --actor CONTROLLER_ID --version 1 --envelope claim.json
    python scripts/awo_service.py --state runtime/example scheduler-queue --actor CONTROLLER_ID --version 1
    python scripts/awo_service.py --state runtime/example assignment-dispatch --actor CONTROLLER_ID --version 1 --assignment ASSIGNMENT_ID --expected-epoch EPOCH
    python scripts/awo_service.py --state runtime/example assignment-reconcile --actor CONTROLLER_ID --version 1 --assignment ASSIGNMENT_ID --expected-epoch EPOCH

Replace IDs, epoch and version with actual values. Each command envelope has
command_id, expected_epoch, tool and payload. Repeating the identical envelope
retrieves the idempotent response. Claim reserves only; dispatch is separate.
Never dispatch the original request again through the legacy CLI.

## Integration contract

The application service owns authentication, delegated claim authorization,
controller version/context/epoch, command replay, and the outer BEGIN IMMEDIATE
transaction. Scheduler writes share that connection:

    create_scheduler_schema(con)
    apply_command(con, project_id, controller_actor, tool, payload)
    queue_status(con, project_id, now=None)
    next_task(con, project_id, agent_id=None, now=None)
    assignment_get(con, project_id, assignment_id)
    claim_for_agent(con, project_id, agent_id, payload=None)
    complete_for_agent(con, project_id, agent_id, assignment_id, *, transport=None)
    review_in_transaction(con, project_id, actor, version, context,
                          event_id, evidence_path, verdict)
    reservation_status(con, project_id, assignment_id)
    reconcile_in_transaction(con, project_id, controller_actor, assignment_id,
                             *, transport=None)

controller_actor is a verified Controller binding ID, not a Worker token or
an actor supplied in an MCP payload. The service must authorize a Worker claim
against the Controller-approved queue and inject its authenticated agent_id.
apply_command rechecks the active Controller and enabled/frozen project.
It does not commit. Query functions never write or poll a harness.

| Tool | Payload |
|---|---|
| scheduler.enqueue | Required: task_id, expected_revision, request, message, agent_id, budget. Optional: required_capabilities, resources, dependencies, workspace_mode, task_kind, due_at, dag_run, dag_node, slot_id. A configured collaboration Task also requires expected_collaboration_revision. |
| scheduler.claim | {}, or optional agent_id and paired task_id, expected_revision. Optional slot_id requires that Task/revision pair. Filters can only narrow the approved queue. |
| scheduler.priority | task_id, expected_revision, priority, reason; expires_at is mandatory for P0. |
| assignment.close | assignment_id, expected_revision, outcome; review outcomes require event_id, cancellation requires evidence_path. Outcomes: accepted, repair_required, rejected, cancelled. |

request is the existing complete request object with dispatch_confirmed=false,
explicit dispatch authorization, frozen bindings and completion paths.
request.task_id is the attempt ID and may differ from the stable task_id.
Use a fresh nonce and delivery directory each time. generation defaults to the
next generation and must match if supplied. message must bind attempt ID/nonce.
budget maps dimensions to estimates, for example {"tokens.input": 1000}; every
bounded dimension requires an estimate. dependencies contains stable Task IDs.
workspace_mode defaults to directory; task_kind defaults to artifact and also
accepts code. dag_run and dag_node must be supplied together for a frozen DAG.

MCP task.next maps to next_task. It checks durable approval, dependency,
evidence, capability, capacity and budget eligibility without writing or polling.
CANDIDATE explicitly awaits a live Worker check; it does not promise admission.
Final claim repeats all checks in one transaction, including idle identity,
completion baseline and actual budget reservation.

MCP worker assignment.claim maps to claim_for_agent. It accepts only optional
paired task_id/expected_revision and optional slot_id, resolves the frozen Controller approval and
revalidates its binding version/epoch. Controller proxy claims use
scheduler.claim. A successful response is RESERVED
with assignment_id, run_id, generation, slot_id, revision, task_revision and budget
reservation_ids. WAITING includes reasons. Neither response sends a task.

MCP assignment.complete maps to complete_for_agent. It checks authenticated
Agent ownership and observes the actual adapter, identity, fresh sequence and
frozen artifacts. It cannot accept a caller's completion assertion. Scope must
come from the authenticated principal, not a payload actor.

There is no separate mutable resource Claim table. reservation_status exposes
frozen resources/directories from the intent and existing budget reservations.
Gateway claim may confirm these; release must reject while holds_slot=1.

## Collaboration slots and independent selection

Install/upgrade the collaboration domain schema through the application service
before using these hooks. A Controller configures the Task while draft/ready;
configuration binds distinct verified workers, finite attempts/repairs and an
approved verifier. Setting execution_mode alone does not enable collaboration.

Enqueue each approved slot separately. Example additional payload fields:

    {"slot_id": 1, "expected_revision": 2, "expected_collaboration_revision": 1}

These are not a complete enqueue payload. expected_revision is the current Task
revision; expected_collaboration_revision is the separate policy revision. Read
both again after each mutation. A successful enqueue also returns
collaboration_ticket_id and collaboration_revision. Generation is task-wide,
including queued tickets, so distinct slots can be admitted in either order.
Omit request.generation on a new repair when adopting a legacy follow-up; the
Controller queue supplies the next task-wide generation. A supplied generation
must match. Each repair still consumes the exact last same-slot sealed repair
review, after authoritative closure, and uses a fresh attempt ID and nonce.

Queue keys are (task_id,slot_id). Schema upgrade preserves older approvals as
slot 0, including their bytes, age and notification flags, in the caller
transaction. Queued slots remain visible while siblings run or await review.
Priority affects pending slots only; it never interrupts a running worker.
Scheduler-owned progression carries forward only matching revision fences; an
unrelated Task edit remains stale even when a sibling progresses. All slots
share Task dependencies and artifact classification. Delivery/worker directories
must be disjoint, and every slot pays its own cumulative budget reservation.

The scheduler calls prepare_enqueue, check_admission and admit_in_transaction
on collaboration_runtime. The last operation creates the sole Assignment and
budget reservation; native tasks/maps/send intent are added in the same
transaction. Any rejection rolls back the entire candidate. An admitted slot
rechecks its immutable ticket and native safety gates before the one send.
There is no alternate dispatcher. A selected winner fences unsent competitors
without releasing them.

review_guard excludes every frozen competitor and synthesis identity, including
historical aliases. Original reviews/events stay authoritative. A completed or
independently reviewed competitor is not automatically accepted: the Controller
must obtain a successful bounded verification and explicitly propose one winner.
For ensemble, only the synthesis slot can be selected, with distinct frozen
contributor receipts. collaboration_verify records an intent only; the separate
run_verification runner commits attempted before launch. The scheduler never
launches the verifier or retries an uncertain verification.

Controller assignment.close calls acceptance_guard and closure_projection, then
rechecks native review, artifact hashes, managed workspace seal and actual worker
quiescence. Closing one slot releases only that slot. A loser's completion,
repair, rejection or cancellation cannot overwrite the accepted winner or its
evidence. Other slots keep their claims and unknown budget until their own
authoritative close/usage settlement. No automatic loser termination or merge
is performed. See the collaboration domain reference for configuration,
verification and proposal commands.

## Queue order and waiting reasons

Queries and claims use one deterministic comparator: P0 first, native P1 before
promoted P1, then FIFO, stable Task ID and slot ID. Timestamps require a timezone and
compare in UTC. Restart preserves enqueue time.

| Base priority | Aging interval | Promotion ceiling |
|---|---|---|
| P1 | 480 minutes | P1 |
| P2 | 240 minutes | P1 |
| P3 | 120 minutes | P1 |
| P4 | 60 minutes | P2 |

A deadline within 24 hours promotes at most to P1. A separate seven-day watchdog
promotes remaining lower tasks to P1, ahead of ordinary promotions and behind
native P1. P0 requires a reason and future expiry; expiry restores its former
base priority without a query writing. Claim records deadline/watchdog events
once per queued attempt. This module starts no background polling or
notification delivery loop.

WAITING is not failure. Reasons identify independent review still pending,
changed evidence, unverified capabilities, an occupied Worker/directory, full
capacity or blocked budgets. Resolve the named condition or obtain fresh
Controller approval; priority cannot bypass it or interrupt a running call.

Capacity uses scheduler.max_concurrency, scheduler.per_host,
scheduler.per_harness and scheduler.resources in the workflow configuration.
Limits are positive integers. Ownership is v2_assignment.holds_slot, even if
legacy status says finished/resolved. Admission atomically reserves Run, Task
and workflow budget; failure rolls back Assignment, budgets, legacy records and
send intent together.

## Runtime and lifecycle ownership

    dispatch_assignment(state, assignment_id, actor, version, context="main",
                        *, expected_epoch, transport=None)
    reconcile_assignment(state, assignment_id, actor, version, context="main",
                         *, expected_epoch, transport=None)

These wrappers own their transactions; do not call them inside a service
transaction. dispatch_assignment is the sole send entry point. Reconcile writes
the existing legacy inbox event and journal, and updates the authoritative V2
Task/Assignment projection. review_ready retains slot/claims.

Dispatch persists attempted before the external send. Process death or send
failure after this point is uncertain; retry returns ALREADY_ATTEMPTED without
sending. Reconcile uses the original monitor. Disconnect, timeout, missing
heartbeat or cancellation request never releases claims.

Independent review uses the original reviews/events tables.
review_in_transaction supports the gateway within the service transaction;
legacy acknowledge/record_review remains compatible. Recording a verdict does
not accept the V2 Task or release claims, and cannot overwrite an existing
review. Only Controller assignment.close rechecks the sealed review, artifact
hashes, original Worker identity, idle status and reviewed completion sequence,
then releases ownership. Existing API evidence IDs are reused only when
Assignment, hash, bytes and seal match; any mismatch rejects closure.

Cancellation requires a task/nonce-bound note with a standalone
Outcome: terminated line and actual idle confirmation; it does not kill a
process. Repair requires the last sealed repair review and a fresh attempt,
preserving Assignment/Evidence/Review history. Accepted is not landed.
Budget settlement remains the budget service's separate authoritative
measurement operation; closing an Assignment never invents zero usage.

## Validation

    python -B -m unittest discover -s scripts -p test_priority_scheduler.py -v
    python scripts/check_encoding.py

Tests use isolated SQLite and real local subprocesses for completion/review,
competing claims and process death after send. Collaboration regressions cover
same-slot and different-slot process races, sibling revisions, atomic budget
rollback, repairs, verified winner/ensemble selection and loser closure.
The timeout probe explicitly
uses a synthetic transport. These validate local protocol/transaction behavior,
not live Codex/Pi/Claude/OMP or cross-host execution. The fixtures use a
directory workspace and an artifact task, so they do not certify Git worktree
or code-task behavior. Parent integration and independent review remain
separate gates.
