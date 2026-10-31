# Durable dispatch and DAG scheduling

English | [Chinese](scheduling.zh-CN.md)

Use `scripts/workflow_scheduler.py` for single-attempt task submission, isolated
parallel branches, and joins gated by independent review. The existing monitor
and review commands remain the delivery and acceptance interfaces.

## State and migration

New `workflow_control.py init` states include the scheduler tables. Upgrade an
existing state explicitly with its current controller; migration is additive:

The orthogonal states and event envelope are defined in [task-state-machine.md](task-state-machine.md).

```sh
python scripts/workflow_scheduler.py --state STATE migrate --actor INSTANCE --version 1 --context main
python scripts/workflow_control.py --state STATE status
python scripts/workflow_control.py --state STATE inspect --task TASK-ID
```

Status/inspect open SQLite read-only, without migration or live client queries.
They expose bindings, execution, dispatch attempts, delivery events, review,
notifications, observations, retry counts, journal, claims, DAG dependencies,
and the latest scheduling deferral. Observations are timestamped snapshots,
not proof that a disconnected process has stopped.

Use `status --summary --format text` (or `inspect --task TASK-ID --format text`)
for adapter-neutral phase, harness/adapter/identity, next role/action, observation
age, DAG dependencies, dispatch status, claim, review verdicts, blocking reason,
warnings, resource usage against capacity and held claims. JSON summaries use `--summary`. `--stale-after 900` controls
the observation warning; it does not infer failure, cancellation or heartbeat.
Queued DAG nodes are included before registration. Summary reads contact no
adapter and work with Command, Receipt and optional Herdr bindings.

## Single-attempt dispatch

Bind the real worker with a verified `command` or `herdr` interface. A command
worker additionally needs `prompt_argv` with one standalone `{message}`.
Task requests need `workflow_state`, `dispatch_confirmed: false`, a positive
integer `generation` (default 1), and explicit
`authorization.dispatch_worker: true` with its actual authorization source.
The message must contain the task ID and nonce. No authorization is implied
by loading a graph or choosing a harness label.

```sh
python scripts/workflow_scheduler.py --state STATE dispatch --request request.json --message-file task.md --actor INSTANCE --version 1 --context main
```

The controller queries identity, mapped idle status and completion baseline, then
atomically registers the unconfirmed request and reserves workspace/resources.
It rechecks the controller and target before one bounded send. The final query
and send share a SQLite write transaction, serializing handoffs and submissions.
A successful send records `submitted`; execution still needs confirmation.
A crash leaves `reserved`; errors after reservation record `uncertain`. Neither
state can be automatically resent. The worker and its claims remain occupied.

Both admission queries apply the request's `status_map`, as does monitoring.
If the verified client/version reports `done` as a reusable between-turn state,
set `"status_map": {"done": "idle"}` before freezing requests or loading a DAG.
Without this explicit mapping, dispatch still requires `idle`; the package does
not assume every harness's `done` state is reusable. Raw `working`, `blocked`,
`exited` and `failed` remain ineligible even if mapped to `idle`. Identity,
completion baseline, ledger occupancy, review gates and resource claims still
apply, including the final check immediately before sending.

Independently inspect the actual session. Write a note containing the exact task
ID, nonce, `Outcome: running`, and evidence that execution began (it may already
have completed). Then create a new confirmed request file:

```sh
python scripts/workflow_scheduler.py --state STATE reconcile --task TASK-ID --outcome running --evidence EXECUTION.md --request-out confirmed.json --actor INSTANCE --version 1 --context main
python scripts/monitor_task.py --request confirmed.json
```

Only the confirmation flag changes in the stored dispatch request. The journal
retains the original request hash and evidence hash. Never edit a monitor's
frozen request. If writing the output file was interrupted, repeat reconciliation
with a new output filename. An existing output file is never overwritten.
`not_sent` or `terminated` requires a matching `Outcome:` line and independent
evidence; either closes that attempt and frees its claims. Retry work uses a new
task ID and nonce, with a new generation when appropriate. Reconciliation
records an operator judgment; it does not cancel a real process.

## Isolation and resource quotas

Configure workflow-level `scheduler.max_concurrency` (default 1) and optional
positive integer capacities, for example `"resources": {"gpu": 1}`.
Node `resources` reserves units from these capacities. Claims cover both the
artifact directory (`cwd`) and the pinned worker `launch_cwd`, including nested
paths and resolved symlinks. Separate artifact folders cannot conceal a shared
source workspace. Legacy task registration also respects scheduler claims.

Prepare separate clones/worktrees and bind each parallel worker to its own
verified directory. The scheduler enforces claims in one state database; all
controllers sharing writable sources/resources must share this database.
OS sandboxing, worktree creation, resource discovery and merging remain operator
or existing scheduler responsibilities. Claims have no automatic expiration.

Failed or disconnected work retains claims until termination is inspected.
Accepted claims can be released explicitly with `release --task TASK-ID` and
actor/version/context. A DAG tick releases its valid accepted claims before
selecting downstream work. Review and delivery files must remain available.

Serial nodes may reuse a worker and its execution directory. Keep their evidence
in separate per-task cwd directories, or use distinct task-specific artifact,
binding and review paths within a shared cwd. Never overwrite earlier accepted
files: releasing a claim does not end their retention requirement, and dependency
checks rehash those bytes. Loading a DAG allows shared cwd because separate
artifact paths can preserve evidence there. Loading rejects overlaps among
planned evidence files. Registration permanently reserves registered delivery,
binding and review paths in the shared ledger, including after release or
termination. Old requests receive the same collision checks. This protects
ledger-mediated allocation; it is not an OS write barrier or cross-ledger lock.

Generate task-specific paths before freezing a new graph:

```sh
python scripts/workflow_scheduler.py prepare-dag --file draft-dag.json --root /absolute/new-delivery --output prepared-dag.json
```

The allocator creates exclusive `task-<hash-of-id-and-nonce>/` directories, pins
REVIEW.md and preserves relative delivery paths. Message placeholders
`{task_cwd}`, `{review_path}` and `{marker}` are replaced before graph loading.
Existing directories/output files are not overwritten. Inspect the generated
message and bind any changed execution directory before dispatch. Never relocate
a registered request.

## Frozen DAGs and review gates

A graph contains `nodes`; each node has `id`, `needs`, an unconfirmed `request`,
a task/nonce-bound `message`, and optional resource units. IDs are unique,
dependencies must exist, and cycles are rejected. All task requests and messages
are frozen when loaded; run IDs cannot be overwritten or share task IDs.
See `examples/dag.json` for the graph structure. Replace all placeholders and
bind the actual roles before loading it. The template uses a second Worker role;
declare that role and set the desired concurrency before initializing the state.

Each template node provides a task/nonce-bound `marker` and instructs the worker
to place it in HANDOFF.md. `marker_source` selects where to search; only a
nonempty `marker` enables that additional check. Marker-free requests remain
valid for compatibility and still require the normal file/identity/nonce checks.
A file marker constrains delivery content; it does not authenticate its author
or establish task acceptance. Independent review remains required.

```sh
python scripts/workflow_scheduler.py --state STATE dag-load --run RUN-ID --file dag.json --actor INSTANCE --version 1 --context main
python scripts/workflow_scheduler.py --state STATE tick --run RUN-ID --actor INSTANCE --version 1 --context main
```

Each tick submits the currently ready nodes within capacity. Different workers
may continue asynchronously after their individual submissions. Dependent
nodes wait for *all* upstream nodes to have `accept` reviews of `review_ready`
events with task/nonce-bound receipts. Every consumption rehashes both REVIEW.md
and the delivered files. `repair` does not satisfy a DAG dependency. Rejection,
failure, invalid evidence or inspected termination blocks downstream work;
unaffected branches continue. Direct dispatch and legacy registration cannot
bypass a frozen graph's dependencies. Review remains an explicit operation.

For DAG tasks, the completion baseline is captured when the node is ready.
The dispatched request records its frozen template hash in `dag_binding`.
Only that baseline is refreshed; task identity, authorization and adapter are
unchanged. Use reconciliation's exported request for the monitor. This allows
a persistent worker to execute several sequential graph nodes without guessing
future completion counters.

A tick is one finite pass. An existing controller can call it again after
recording reviews or recovering an anomaly. It never creates agent sessions,
automatically supplies a review verdict, or retries an uncertain send. For
rework, load a new scoped run with fresh task identities.

Controllers with `scripts/` on their Python import path can use the same state
directory APIs directly:

```python
from workflow_scheduler import dag_view, snapshot, tick

graph, node_states = dag_view(state, "RUN-ID")
status = snapshot(state)
result = tick(state, "RUN-ID", actor, version, "main")
```

`dag_view(state, run)` and `snapshot(state)` are read-only and contact no clients.
`dag_view` returns the frozen graph and the node-state dictionary. The private
`_dag_view(connection, run)` is used only inside existing transactions.

## Monitor recovery

`monitor_checkpoint.json` persists the request hash, start time, delivery grace
timer and read-error count. Restart after inspecting/removing a stale lock
restores these values. Explicit `--resume` resets the overall monitoring budget
and read-error budget, retaining the delivery grace history. Read-only query
retries use capped exponential backoff (`retry_interval_seconds`, default 5;
`retry_max_seconds`, default 60). Sends are never retried by this policy.

An already-written final summary is replayed into the event inbox on restart,
including its receipt, even without `--notify`. Event IDs deduplicate replay.
Overall monitor timeout stops observation; it never terminates the worker.
Wall-clock deadlines can be affected by clock changes; they cannot grant review
or dispatch permission.

## Stalls, permission prompts and bounded continuation

`incomplete` (worker idle without a valid delivery, e.g. after a quota,
rate-limit or network error), `blocked` (usually a harness permission/approval
prompt or usage limit) and `adapter_error` (queries keep failing) are pauses,
not ends. The monitor publishes one event and one notification per stall
episode, then keeps watching the same attempt; a later delivery still becomes
`review_ready`. The overall monitor timeout also applies while stalled. When
the controller closes the attempt (`reconcile --outcome terminated` or
`release`), the monitor stops without another event.

A stall event cannot be reviewed while the attempt is open, so a verdict on a
pause cannot outlive the real delivery. For `incomplete`, the controller may
send one bounded continuation once the worker can run again:

```sh
python scripts/workflow_scheduler.py --state STATE continue --task TASK-ID --actor INSTANCE --version 1 --context main
```

It requires a confirmed running dispatch, a latest observation of
`incomplete`, the unchanged worker binding, the live identity, and an idle
worker that can receive input. It sends the same task ID and nonce, never a new
task, at most once per stall episode and at most
`scheduler.max_continue_prompts` times per attempt (default 2, 0 disables). The
attempt is journaled (`continue_reserved`) before the send; an uncertain send
counts and is never retried. `blocked` and `adapter_error` are never continued:
the Owner answers permission prompts in the worker's own harness, and the
monitor resumes when the worker does. If the harness denied a permission, a
continuation cannot help; change the harness permissions or close the attempt.
Dispatch to a worker that shows a permission prompt is refused before anything
is sent, with that reason.

## Follow-up tasks and observed starts

After a task has a sealed review, prepare the next request and standard message without writing the ledger or sending to a worker:

```sh
python scripts/workflow_scheduler.py --state STATE follow-up --parent-event EVENT_ID --task-id NEXT-ID --taskbook /abs/TASK.md --cwd /abs/next --source "Owner authorization" --out /abs/next/request.json --message-out /abs/next/message.txt --actor INSTANCE --version 1 --context main
python scripts/workflow_scheduler.py --state STATE dispatch --request /abs/next/request.json --message-file /abs/next/message.txt --actor INSTANCE --version 1 --context main
python scripts/workflow_scheduler.py --state STATE confirm-start --task NEXT-ID --request-out /abs/next/confirmed.json --wait 60 --actor INSTANCE --version 1 --context main
```

`follow-up` checks the parent review hash and current worker/reviewer bindings, captures a fresh completion baseline, copies `TASK.md` into the new task directory, and creates a new nonce and exact completion marker. Output paths must not already exist. `confirm-start` reads worker status and, where available, recent output every five seconds. It writes `EXECUTION.md` and reconciles only after observing `working`, a newer completion sequence, or both task ID and nonce in output. `NOT_OBSERVED` and `IDENTITY_CHANGED` leave files and ledger unchanged; neither command resends a task. You can still use manual `reconcile` after inspecting the worker yourself.

The monitor keeps the configured full `interval_seconds` check. Between full checks it queries status every `fast_poll_seconds` (default 30, capped at the interval) and runs a full check as soon as status or completion sequence changes. Set `fast_poll_seconds: 0` to use the original single wait. Fast queries never send messages or consume the error budget.

## Local demo

```sh
python -B scripts/demo_scheduler.py --directory runtime/new-demo
python -B scripts/demo_scheduler.py --directory runtime/new-done-demo --completion-status done
```

The demo refuses an existing directory. It uses real local Python subprocesses
as workers, executes A/B and JOIN, compares results with expected bytes,
records reviews and writes `dag.json`, `demo-result.json` and the ledger.
It creates no external agents and sends no external notifications. The demo
shows local orchestration behavior only; record actual client compatibility
using the [capability contract](capabilities.md).

The second demo leaves the same worker at `done` after each turn and uses the
explicit mapping to reuse its binding for A and JOIN. It never resets the
worker between passes.
