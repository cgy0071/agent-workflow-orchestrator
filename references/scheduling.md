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
artifact paths can preserve evidence there; it does not reserve permanent
filenames for historical deliveries.

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

## Reproducible local fixture

```sh
python -B scripts/demo_scheduler.py --directory runtime/new-demo
python -B scripts/demo_scheduler.py --directory runtime/new-done-demo --completion-status done
python -B -m unittest discover -s scripts -p "test_*.py" -v
```

The demo refuses an existing directory. It uses real local Python subprocesses
as fixture workers, executes A/B and JOIN, compares results with expected bytes,
records fixture reviews and writes `dag.json`, `demo-result.json` and the ledger.
It creates no external agents and sends no external notifications. These results
establish local orchestration behavior; record actual client compatibility using
the [capability contract](capabilities.md).

The second demo leaves the same fixture worker at `done` after each turn and
uses the explicit mapping to reuse its binding for A and JOIN. It never resets
the worker between passes. This regression fixture does not establish live Pi
compatibility; the corresponding external report is version-scoped.
