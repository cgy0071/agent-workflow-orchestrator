# First real run: from a fresh clone to one reviewed task

[Chinese](first-real-run.zh-CN.md)

This page gives the shortest copyable route from a fresh clone to a real,
review-before-accept run, and states what the result does and does not prove.
Choose one route:

| Route | Needs | Real model turns |
|---|---|---|
| Native one-shot (recommended first check) | Python 3.10+, installed Codex CLI | 3 |
| Native session resume | The same, plus Codex session resume | 3 |
| Herdr panes (optional) | Python, Herdr CLI, two live panes | Per your task |
| Git worktree code loop (optional) | Python 3.10+, Git, installed and authenticated Codex CLI | 5 |
| Offline demo (mechanics only) | Python | 0 |

The routes are separate. The native and worktree routes do not start Herdr and
refuse to run while `HERDR_ENV=1` is set. The Herdr route does not use the
native validators.

The [Git worktree code loop](worktree-code-loop.md) is the full code-task route:
five real Codex turns from worktree execution through independent review and
the local Merge Queue to downstream materialization.

## Before you start

- **Python 3.10+.** Check the interpreter before anything else:

  ```sh
  python -c "import sys; print(sys.executable, sys.version)"
  ```

  A Microsoft Store `python` alias can look installed and fail. Use the real
  interpreter, or the full path to it.

- **Run from the repository root.** The scripts import sibling modules from
  `scripts/` and copy them into the run directory.
- **Codex CLI installed and already authenticated.** Check with
  `codex --version`. The routes use your CLI default provider and model; they
  do not set, change or read provider credentials. If `codex` is not on
  `PATH`, pass `--executable C:/path/to/codex.cmd`.
- **A new output directory for every run.** The validators and the demo refuse
  an existing directory.
- **No Herdr environment for the native routes.** In PowerShell:

  ```powershell
  Remove-Item Env:HERDR_ENV -ErrorAction SilentlyContinue
  ```

  In a POSIX shell, prefix the command with `env -u HERDR_ENV`.
- **Network and model quota.** The native routes make three real model turns.
  Token usage is recorded; monetary cost is recorded as unknown, not zero.

## Route A: native one-shot

This is the minimal real check. From the repository root:

```powershell
Remove-Item Env:HERDR_ENV -ErrorAction SilentlyContinue
python -B scripts/validate_v2_native_loop.py --directory runtime/repro/native-oneshot-1 --harness codex --mode one-shot --timeout 240
```

Options: `--harness codex|pi|omp`, `--executable PATH`, `--mode one-shot|session`,
`--timeout SECONDS`. The validator creates its own disposable project, state,
credentials and delivery directories under `--directory`; it needs no workflow
configuration and no user state.

A pass prints a JSON result with `"status": "V2_NATIVE_LOOP_PASS"` and exits 0.
Any other outcome exits 2. The run directory keeps `result.json`, `trace.json`,
`usage.json`, `provenance.json`, the sealed delivery and the independent
review.

Who does what:

| Step | Actor |
|---|---|
| Create a disposable project, state and scoped fixture credentials | Validator |
| Dispatch task A once, with task ID and nonce | Validator acting as controller |
| Execute A in a disposable directory workspace | Installed Codex CLI, worker |
| Reconcile the completion, seal evidence, request independent review | Validator |
| Review A in a separate Codex process | Reviewer role, not the worker |
| Close A as accepted, then dispatch dependent task B | Validator |
| Check the sealed evidence and decide | You, if you need to trust the result |

What a pass proves:

- Real installed Codex CLI turns ran and reported completion.
- The dependency gate kept B unassigned until A was accepted.
- Review happened in a separate process before A was accepted.
- One-shot mode used distinct Codex sessions.
- Two service-level refusals: the service refused a review call made with the
  fixture's worker credential, and the issuer refused to issue a reviewer
  credential for the executing binding. These are protocol checks around the
  real model turns, not model behavior.
- A third refusal is **fixture-only fault injection**, not a model turn. The
  validator temporarily changes the disposable fixture ledger's executor
  binding mode so a mis-issued reviewer credential can be synthesized, confirms
  the service rejects it (`forbidden_role`), restores the binding record
  verbatim, and revokes the token. This exercises the same-agent guard in a
  disposable service database; it is not evidence that a real worker tried to
  review its own work.

What a pass does not prove:

- Herdr, pane behavior, or a persistent terminal session.
- In-flight steering, queued input, or restart recovery.
- The provider or model identity: recorded as `null` / `cli_default`.
- Cost: recorded as unknown.
- Git worktree or code-task behavior. The fixture uses a directory workspace
  and an artifact task; see [workspaces](awo-workspaces.md) for worktrees.
- That your own task is correct or accepted. This is a compatibility and
  protocol check on a disposable fixture, not a delivery of your work.

## Route B: native session resume

```powershell
Remove-Item Env:HERDR_ENV -ErrorAction SilentlyContinue
python -B scripts/validate_v2_native_loop.py --directory runtime/repro/native-session-1 --harness codex --mode session --timeout 240
```

Session mode resumes the first worker's Codex session for the dependent task.
The validator requires both turns to report the same native session ID and
requires the second turn to recall a secret that only the first turn saw. The
reviewer still runs as a separate one-shot process.

This proves resume after a clean process exit. It does not prove a persistent
process, in-flight steering, or recovery after a crash. `--resume` is not this
mode: in the older validator it resumes an interrupted validation run, not a
model session.

A recorded session-mode pass validates the validator source it ran and the
protocol path it exercised. It is not a rerun of every file in the current
revision. Run the command on your own clone to validate your revision.

## Route C: Herdr panes (optional)

This is the live-pane route through the local ledger. It needs a running Herdr
session with one pane per role and a real, editable task. Herdr itself rechecks
that the agent is the foreground process when a prompt arrives, so a passing
preflight is not a delivery guarantee.

**Authorization comes from you, not from a file.** Paths below assume `C:/awo`
and a task named `TASK-001` with nonce `0123456789abcdef`; substitute your own
absolute paths and a fresh nonce, and keep every path consistent. Before this
route can send anything, the Owner must authorize the specific task, and you
must record that decision in `request.json`:

- `authorization.source`: a truthful description of the Owner's authorization
  (what was approved, when, and where). It is required, and it is copied into
  the ledger and any notification attempt.
- `authorization.dispatch_worker: true`: the Owner's permission to send this
  one task. Loading a graph, choosing a harness, editing `workflow.json`, or
  copying an example does not grant it. The examples omit this field, so
  copying one does not authorize dispatch.
- `authorization.notify_reviewer: true`: only if the Owner also authorizes one
  direct notification. Even then, an `inbox_only` reviewer binding records the
  event without prompting the reviewer; see step 6.

1. **Discover the actual panes** (read-only) and record each pane's agent kind,
   terminal ID, launch directory, pane ID and completion sequence:

   ```sh
   herdr --session SESSION agent list
   herdr --session SESSION agent get PANE
   ```

   Copy the agent object's fields into the spec's expected identity:
   `agent` to `expected_identity.kind`, `terminal_id` to
   `expected_identity.terminal`, `cwd` to `expected_identity.launch_cwd`, and
   `pane_id` to `pane`. Record `completion_seq` as the pre-dispatch baseline;
   when it is null, the adapter uses `state_change_seq` instead. Do not rename a
   label or invent an identity to pass a check.

   As of Herdr 0.9.3, the native prompt accepts native agent labels such as
   `codex`, `claude` or `pi`. Custom labels are rejected with
   `agent_not_ready`; use `inbox_only` or a separately verified command bridge
   instead.

2. **Write the four files** (you, the operator). Start from
   `examples/workflow.json` and `examples/request.herdr.json`, then edit them.
   This extraction creates the two binding specs from the example request:

   ```sh
   python -B -c "import json; r=json.load(open('examples/request.herdr.json')); r['worker'].pop('baseline_completion_seq'); json.dump(r['worker'], open('C:/awo/worker.json','w'), indent=2); json.dump(r['reviewer'], open('C:/awo/planner.json','w'), indent=2)"
   ```

   | File | Content and consistency rule |
   |---|---|
   | `workflow.json` | Goal, roles, transitions, scheduler limits. Role names must match the request: `executor` executes, `planner` reviews and controls in this example. |
   | `planner.json` | The reviewer/controller spec to bind; use `request.reviewer` as the source (adapter spec, expected identity, `context_policy`). `bind` stores the policy from `--policy`, so pass the same policy you put in the request. Keep both copies consistent. |
   | `worker.json` | Exactly `request.worker` without `baseline_completion_seq`. Dispatch compares the request's worker (minus that field) with the active binding and refuses a mismatch. |
   | `request.json` | Task ID, fresh nonce, absolute `cwd`, absolute `workflow_state`, generation, authorization, both specs, completion contract and monitor settings. |

   - `cwd` is an existing task directory where the worker writes; the
     completion paths are relative to it. `workflow_state` is a new private
     ledger directory outside this repository.
   - Keep `dispatch_confirmed: false` until step 5. Use a new task ID and nonce
     for each attempt; a nonce is not a password, but it binds the message,
     delivery and review.
   - Copy the live `completion_seq` into `worker.baseline_completion_seq`
     immediately before dispatch.
   - The completion contract names the files and the exact marker the worker
     must write:

     ```json
     "completion": {
       "artifacts": ["RESULT.md", "EVIDENCE.md"],
       "binding_artifact": "HANDOFF.md",
       "marker_source": "artifact",
       "marker": "TASK_DONE TASK-001 0123456789abcdef"
     }
     ```

     The monitor reads these files relative to `cwd`; `HANDOFF.md` must contain
     the task ID, the nonce, and the marker line exactly.
   - The authorization block must record the Owner's actual decision:

     ```json
     "authorization": {
       "source": "Owner approved this task on 2026-01-01: <what was approved>",
       "dispatch_worker": true,
       "notify_reviewer": false
     }
     ```

3. **Initialize and bind** (Planner/controller runs these):

   ```sh
   python scripts/monitor_task.py --check-workflow C:/awo/workflow.json
   python scripts/workflow_control.py --state C:/awo/state init --config C:/awo/workflow.json
   python scripts/workflow_control.py --state C:/awo/state bind --role planner --spec C:/awo/planner.json --mode controller --policy inbox_only
   python scripts/workflow_control.py --state C:/awo/state bind --role executor --spec C:/awo/worker.json --mode worker
   python scripts/workflow_control.py --state C:/awo/state bindings
   ```

   `bind` queries the live pane and refuses a missing or mismatched identity.
   Its JSON output contains the new binding's `instance_id` and `version`;
   `bindings` shows the same pair as the planner row's `id` and `version`.
   Those exact values are `--actor` and `--version` in steps 4, 5 and 7, with
   `--context main`. They are generated per state, so copy them from the
   output; a stale, retired, or wrong-context binding is rejected. `--context
   main` is an operator declaration, not proof from pane focus.

   Use `--policy inbox_only` unless you deliberately authorize direct
   notification. `dedicated_window` needs a real exclusive-window declaration
   in `planner.json` (`context_declaration`) plus that `--policy`; the example
   request uses `inbox_only`. One agent cannot hold both the executor binding
   and the controller/reviewer binding; `bind` refuses it.

4. **Write the task message, check, and dispatch once** (Planner/controller).
   The message is the prompt the worker receives. It must contain the task ID
   and nonce; this is enforced by dispatch. A valid `C:/awo/task/task.md`:

   ````text
   Task ID: TASK-001
   Nonce: 0123456789abcdef
   Cwd: C:/awo/task
   Deliverables: RESULT.md, EVIDENCE.md, HANDOFF.md.
   Before the final line, end HANDOFF.md with a fenced awo-result block such as
   ```awo-result {"outcome": "done", "summary": "one line", "remaining": []} ```
   The final line of HANDOFF.md must be exactly:
   TASK_DONE TASK-001 0123456789abcdef
   There is no time limit; finish the task before stopping and save partial
   results as you go.
   ````

   Then check and send:

   ```sh
   python scripts/monitor_task.py --request C:/awo/task/request.json --check
   python scripts/monitor_task.py --request C:/awo/task/request.json --preflight
   python scripts/workflow_scheduler.py --state C:/awo/state dispatch --request C:/awo/task/request.json --message-file C:/awo/task/task.md --actor INSTANCE_ID --version 1 --context main
   ```

   `INSTANCE_ID` stands for the `instance_id` printed by `bind`; `1` is the
   `version` of the first binding in a new state, so use the printed version if
   it differs.

   `--check` validates request structure and ledger role links. `--preflight`
   queries live identities and compares the exact completion baseline; it sends
   nothing. Neither checks `authorization.dispatch_worker` or the message, so a
   request can pass both and still be refused here. Dispatch rechecks identity,
   the message's task/nonce binding and the authorization, reserves the state,
   and sends once. `SUBMITTED` records that one send was attempted; it is not a
   confirmed start and not a delivery.

5. **Confirm the start** (Planner/controller). Use the implemented
   `confirm-start` path; it observes the worker and writes the evidence note
   only when a start is actually visible:

   ```sh
   python scripts/workflow_scheduler.py --state C:/awo/state confirm-start --task TASK-001 --request-out C:/awo/task/confirmed.json --wait 120 --actor INSTANCE_ID --version 1 --context main
   ```

   It polls the bound worker every five seconds for up to `--wait`. Only when
   it observes `working`, a completion sequence after the baseline, or the task
   ID and nonce in the worker output does it write `C:/awo/task/EXECUTION.md`,
   reconcile the dispatch as `running`, and write `confirmed.json`. If it
   prints `NOT_OBSERVED`, nothing was written and the ledger did not change:
   inspect the pane and retry only after a real start. `IDENTITY_CHANGED` means
   rebind the role, not retry the send. If `EXECUTION.md` or `confirmed.json`
   already exists, inspect it; the command refuses to overwrite.

   The manual path is for an observation you made yourself and can describe:
   write `EXECUTION.md` with the task ID, the nonce, a line exactly
   `Outcome: running`, and what you actually saw and where; then run:

   ```sh
   python scripts/workflow_scheduler.py --state C:/awo/state reconcile --task TASK-001 --outcome running --evidence C:/awo/task/EXECUTION.md --request-out C:/awo/task/confirmed.json --actor INSTANCE_ID --version 1 --context main
   ```

   The ledger stores the evidence hash. Never write that note without an
   observation.

6. **Monitor and notify** (a background `monitor_task.py` process that you or
   the Planner starts):

   ```sh
   python scripts/monitor_task.py --request C:/awo/task/confirmed.json --notify
   ```

   With `workflow_state` set, the monitor records an inbox event even when
   `notify_reviewer` is false. `--notify` then behaves according to
   authorization and policy:

   - `inbox_only`: the event is recorded; the reviewer is never prompted.
   - `dedicated_window` or `exact_thread`, with `notify_reviewer: true` in the
     request: after an identity recheck, one notification attempt is made and
     recorded. There is no blind retry.

   A successful monitor run is neither delivery nor acceptance. The monitor
   exits with `review_ready` only after it has read task-bound artifacts whose
   bytes it recorded.

7. **Receive and review** (Reviewer reads the inbox event, the frozen request,
   the delivery and the raw evidence, then writes `REVIEW.md`; the
   Planner/controller records the decision):

   ```sh
   python scripts/workflow_control.py --state C:/awo/state inbox
   python scripts/workflow_control.py --state C:/awo/state settle --event EVENT_ID --file C:/awo/task/REVIEW.md --verdict accept --actor INSTANCE_ID --version 1 --context main
   ```

   `REVIEW.md` must contain the task ID, the nonce, and an exact verdict line.
   A valid file:

   ```text
   # Review

   Task: TASK-001
   Nonce: 0123456789abcdef
   Verdict: accept

   ## Findings
   ...
   ```

   The verdict must be one of the decisions configured in
   `workflow.transitions` (`accept`, `repair`, `reject`, `blocked` in
   `examples/workflow.json`), and `--verdict` must equal the line after
   `Verdict:`. `accept` is allowed only for a `review_ready` delivery. Any
   trailing text on that line, a missing line, or an unconfigured verdict is
   refused. If `request.review_path` is declared, the file must be that exact
   path.

   `--actor` must be the active binding for the event's reviewer role
   (`planner` here) in `main` context. `settle` records the review and closes
   the attempt, so that binding must hold both review and dispatch permission:
   bind the reviewer role with `--mode controller`, not `--mode reviewer`. In
   this example the planner binding is both controller and reviewer; if you
   bind a separate reviewer role, bind it in controller mode and use its
   `instance_id` and `version`.
   `settle` records the reviewer's decision; it does not make the delivery
   accepted by itself or land it in your project. A non-accept verdict records
   the configured transition (in `examples/workflow.json`, `repair` maps to
   `new_scoped_repair_task`), but `settle` itself does not create or resend a
   task. A follow-up is a separate new request, with its own authorization.

Status of this route: the AWO stage taskbooks use it. Their frozen requests
bind Herdr panes, dispatch returns `SUBMITTED` after Herdr's prompt call, and
their settlements record accepted reviews. Those are examples from project
runs, not a test of your panes, and this page does not claim that live panes
were re-executed for every current source file. The `herdr_invoked=false`
field in a native-validator run describes only that inner disposable fixture,
not this outer route. Herdr still rechecks the foreground process; inspect
your actual panes and task.

## Offline demo: mechanics only

Run from the repository root. Use a new directory each run:

```sh
python -B scripts/demo_scheduler.py --directory runtime/first-run
python -B scripts/workflow_control.py --state runtime/first-run/state status --summary --format text
```

Tasks A and B are independent nodes submitted in parallel on two fake workers.
JOIN depends on both and starts only after both upstream nodes have accepted
reviews. The demo compares each delivered `RESULT.md` with expected bytes,
records the review, and asserts that all three nodes end as `accepted`.

This proves the local DAG, dependency, review and acceptance mechanics. It does
not prove Harness compatibility, model output, provider or model selection,
notification delivery, cost, or worktree/code-task behavior: the workers are
local Python child processes, not AI clients.

## Evidence status and your own files

| Route | Recorded evidence | What it does not establish |
|---|---|---|
| Native one-shot | A real Codex CLI run reported `V2_NATIVE_LOOP_PASS` on the reviewed validator source, with 3 recorded model jobs. | That the entire current revision was rerun, or that your task is correct. |
| Native session resume | Session mode has a passing recorded run on the reviewed validator bytes, with the same native session ID across both turns and secret recall in the result. | A rerun of every current source file, crash recovery, or live steering. |
| Herdr panes | Project stage taskbooks use frozen Herdr bindings and record `SUBMITTED` dispatches and accepted settlements. | That your panes behave the same, or that live panes were re-executed for this page. |
| Offline demo | The deterministic demo records `OFFLINE_DEMO_PASS` and three accepted nodes. | Harness compatibility, model output, or notification delivery. |

These are example runs from the project. For your own clone, run the command
and read the files it writes; do not rely on someone else's summary.

Where your own evidence appears:

- **Native routes**: under `--directory`, `result.json`, `trace.json`,
  `usage.json` and `provenance.json`.
- **Herdr route**: the dispatch command's JSON; `EXECUTION.md` and
  `confirmed.json` in the task directory (written by `confirm-start` or by your
  inspection); `monitor_status.json`, `monitor_summary.json`,
  `completion_receipt.json` and `frozen_request.json` in the request
  directory; the event shown by `workflow_control.py inbox`; and the recorded
  review shown by `workflow_control.py inspect --task TASK-001`.
- **Offline demo**: the JSON printed by `demo_scheduler.py` and the ledger
  summary from `workflow_control.py status`.

A recorded pass validates only what ran. It is not a promise about unrelated
files in the current revision, and it is not an acceptance of your work.

## Limits a pass does not remove

- **Directory-artifact validation is not worktree or code-task validation.** It
  certifies declared artifact bytes in a managed directory, not Git worktrees,
  branches, patches, merges, or code-task checks.
- **Accepted is not landed.** Acceptance records the reviewer's decision; it
  does not merge or materialize code.
- **Priority is not live steering.** Queue order and admission gates do not
  interrupt a model call already running.
- **Budget admission and settlement are not forced stopping.** They are ledger
  measurements; they do not stop a call in progress, and the ledger does not
  create a provider hard cap.
- **Legacy knowledge origin can remain unknown.** An unauthenticated legacy
  record is not the same as a missing or authenticated one.

## Troubleshooting

| Symptom | Cause and next safe action |
|---|---|
| `native CLI executable not found` | `codex` is not on `PATH`; pass `--executable` with the exact `.cmd` path |
| Refusal because the directory exists | Use a new `--directory`; do not reuse a completed run |
| Refusal about `HERDR_ENV` | Clear `HERDR_ENV` for the native route |
| `python` starts a Store alias | Use the real interpreter path |
| A model turn fails or times out | AWO does not retry an uncertain send or change credentials; inspect the run directory and solve the client/account cause |

Running any route never authorizes dispatch, notification or continuous
running for your own tasks. Those permissions stay with you.
