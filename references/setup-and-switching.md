# Initialization, Window Selection, and Handoffs

English | [Chinese](setup-and-switching.zh-CN.md)

## 1. Choose roles, clients, and actual targets

Start from `examples/workflow.json`. A role's `harness` is descriptive; it does not select or launch a window. For each role, discover the actual client session through a verified interface and save its adapter specification. Any role can use any supported client.

Terminology: **pane** in this guide refers specifically to a Herdr terminal split. Windows and sessions are the general concepts; other clients use their own verified interfaces and identifiers. Herdr pane commands do not apply to those clients.

For Herdr, use its installed CLI's read-only discovery and identity commands. Record the executable, session, pane, actual agent kind, terminal ID, and launch directory. For another CLI/API, provide commands that return the identity/status contract in [harness-adapters.md](harness-adapters.md). Harness launch arguments must come from the user's selected runtime; see the optional provisioning recipe below.

Choose either creation method below. Both lead to the same identity verification, binding, and preflight steps. An existing available session can be reused after checking its identity and current tasks.

### 1.1 Manual creation

1. Open a dedicated window/session in the selected client (a pane when using Herdr) and enter the task workspace.
2. Start the installed harness and select the intended provider, model, and thinking level where applicable.
3. Check the actual runtime settings, then discover the session identity and cwd through the verified adapter; for Herdr, also record pane and terminal identities.
4. Save the role's adapter specification and continue with section 2. Capture the live sequence baseline immediately before dispatch.

This method suits an existing interactive session or a client without a verified creation interface. A manually created session can still use automatic monitoring, notification, and authorized follow-up.

### 1.2 Automatic creation

When the user authorizes creating a session and the installed client exposes a verified interface, the controller creates a dedicated session in the task workspace, launches the harness with the selected arguments, waits for registration, and verifies the actual runtime settings and identity. With Herdr, this creates a pane; the Herdr-only command example and bounded-wait requirements are in [section 8](#8-provision-an-executor-when-needed). Other clients require their own verified creation interfaces. This route requires a verified controller or launcher; bind and preflight do not create sessions themselves.

After either method, extract the `worker` and `reviewer` objects from your edited `request.json` into `worker.json` and `planner.json`. Remove `baseline_completion_seq` from `worker.json`; keep it in the per-task request. Keep adapter specifications otherwise identical. A Paper Lead can have another reviewer specification. Names, models, and window titles alone do not establish identity.

## 2. Initialize and bind

Run commands from the Skill repository; use absolute paths in real deployments. The state directory must be new. Keep it private and outside the published repository.

```sh
python scripts/monitor_task.py --check-workflow workflow.json
python scripts/workflow_control.py --state /absolute/workflow-state init --config workflow.json
python scripts/workflow_control.py --state /absolute/workflow-state bind --role planner --spec planner.json --mode controller --policy inbox_only
python scripts/workflow_control.py --state /absolute/workflow-state bind --role executor --spec worker.json --mode worker
python scripts/workflow_control.py --state /absolute/workflow-state bindings
```

Binding performs a live identity query. Save the returned `instance_id` and `version` for the controller. A workflow has one active controller. Other roles can be `worker`, `reviewer`, or `advisor`; a reviewer can record decisions without dispatching. The default permissions are controller=dispatch/review, reviewer=review, worker/advisor=none. An explicit role `permissions` list can restrict these actions.

The SQLite ledger stores configuration, versioned bindings, frozen tasks, pending events, notification attempts, reviews, and audit records. Workflow configuration is a snapshot at initialization. Edit the source configuration and initialize a new state directory for a revised workflow; preserve old state and explicitly hand off existing tasks. Runtime changes of role targets use the switch protocol below.

## 3. Choose a conversation policy

| Policy | Required evidence | Delivery behavior |
|---|---|---|
| `inbox_only` | Verified role/window identity | Persist the event; no prompt is sent. Default. |
| `exact_thread` | Real, pinned and observed `conversation_id` **and** `branch_id` | Recheck both before direct notification. |
| `dedicated_window` | Explicit operator declaration that this window is exclusively the controlling main conversation | Direct notification after identity recheck. Do not open side conversations there. |

If a client cannot expose thread and branch IDs, the Skill cannot reliably recognize side conversations from a terminal ID or title. Use `inbox_only`, or explicitly reserve a dedicated window. For direct legacy monitoring, set the request reviewer's `context_policy`; `dedicated_window` also needs `context_declaration`. For ledger delivery, use binding `--policy` and an explicit `--reason` for a dedicated window.

Ledger bind/switch stores the selected policy in the adapter spec and fills a missing context_declaration from --reason for dedicated_window. The standalone request still needs its own declaration. Binding and preflight check direct Herdr targets for agent_session using read-only queries; sending rechecks identity and this prerequisite. No test prompt is sent. Presence is a capability prerequisite, not a guarantee of delivery; timeout/uncertainty rules still apply.

In the reported Windows / Herdr 0.9.3 evaluation, Pi exposed agent_session and received prompts; dsh-tui did not. Use inbox_only for such a target, or a separately verified command bridge. Do not infer compatibility from harness names or the nullable name field: other versions and Codex/Claude/Grok require their own live checks.

**Trust boundary:** `--context main|side|unknown` is supplied by a trusted runtime bridge or an informed operator. It is not automatic branch detection or authentication. The gate rejects declared side/unknown contexts and stale bindings. It cannot stop an arbitrary process with ledger write access from falsely declaring `main`. A bridge should pass `main` only after checking actual caller metadata against the binding. Without that capability, use inbox-only operation. Do not invent branch IDs or infer main status from pane focus.

Side conversations may inspect and advise; they do not acknowledge events, record acceptance, or dispatch follow-ups. A copied completion message does not transfer controller authority. A newly opened/restarted client requires a new verified binding if identity changed.

## 4. Preflight, dispatch, register, and monitor

Set `workflow_state` in each request to the absolute ledger directory. Supply actual task paths, identities, nonce, authorization source, and pre-dispatch completion sequence. Check before submission:

```sh
python scripts/monitor_task.py --request /absolute/task/request.json --check
python scripts/monitor_task.py --request /absolute/task/request.json --preflight
```

`--check` is offline structural validation. `--preflight` performs read-only Worker/reviewer queries and verifies the captured completion sequence. It does not send a task. Confirm the Worker is available and check directory ownership through the existing scheduler.

Submit the approved task once using the verified interface and existing submission ledger. After confirming execution started, set `dispatch_confirmed` to true, register the final request, and keep it unchanged:

```sh
python scripts/workflow_control.py --state /absolute/workflow-state register-task --request /absolute/task/request.json --actor CONTROLLER_INSTANCE --version 1 --context main
python scripts/monitor_task.py --request /absolute/task/request.json --notify
```

The control tool registers tasks; it does not submit them or own external directory leases. The monitor requires an identical registered request and writes `frozen_request.json`. Follow-up registration also checks the sealed parent review. Full workflow dispatch and business review remain agent responsibilities under user authorization.

## 5. Receive, acknowledge, review, and continue

Completion is persisted before notification. A successful send, acknowledgment, and acceptance are three different states. If a prompt lands in a side conversation or the reviewer is offline, the pending event remains recoverable:

```sh
python scripts/workflow_control.py --state /absolute/workflow-state inbox
python scripts/workflow_control.py --state /absolute/workflow-state ack --event EVENT_ID --actor CONTROLLER_INSTANCE --version 1 --context main
```

Independently read the frozen request, delivery, changes, and raw evidence. Write a review, for example:

```markdown
# TASK-EXAMPLE Review
Task ID: TASK-EXAMPLE
Nonce: the-actual-task-nonce
Verdict: accept

Evidence checked: ...
Unexecuted checks / remaining limits: ...
Decision and next authorized action: ...
```

```sh
python scripts/workflow_control.py --state /absolute/workflow-state review --event EVENT_ID --file /absolute/task/REVIEW.md --verdict accept --actor CONTROLLER_INSTANCE --version 1 --context main
python scripts/workflow_control.py --state /absolute/workflow-state next-check --event EVENT_ID --actor CONTROLLER_INSTANCE --version 1 --context main
```

Put the returned `parent_review` object and its `task_id` as `parent_task` in the next request. The gate verifies the parent ledger entry and current file hash. Default continuation verdicts are `accept` and `repair`; configure `workflow.continuation_verdicts` to change them. A passing gate permits the configured next stage within existing authorization; it does not itself send a task or prove technical acceptance. Distinct reviewer and controller roles can perform review and continuation respectively.

## 6. Switch a harness/window safely

For a controller/reviewer, switch the same role to a freshly verified specification:

```sh
python scripts/workflow_control.py --state /absolute/workflow-state switch --role planner --spec new-planner.json --mode controller --policy inbox_only --reason "Handoff to the new main session"
```

The version increases. Old controller instance/version pairs immediately lose authority. Frozen task requests retain their original identities; pending events remain assigned to the reviewer role. The new role binding may acknowledge them. If no notification attempt exists, an authorized explicit `deliver --event EVENT_ID` can target the current binding. Submitted/uncertain sends are never blindly resent. Check inbox after every handoff.

For a Worker still running or uncertain, request a deferred switch:

```sh
python scripts/workflow_control.py --state /absolute/workflow-state switch --role executor --spec new-worker.json --mode worker --defer --reason "Replace the Worker after the current task ends"
python scripts/workflow_control.py --state /absolute/workflow-state activate --role executor
```

Activation is explicit, rechecks the pending identity, and refuses while old work is running/uncertain. A valid completion or observed failure/exit ends the task. An adapter failure, blocked state, timeout, or missing delivery does not prove termination. Inspect old execution and external leases; if independently verified stopped, record a task/nonce-bound termination note and use `resolve-task --task TASK_ID --evidence /absolute/TERMINATION.md` with the current controller's actor/version/context. This records your judgment; it does not cancel work or establish acceptance. Then activate and register future tasks against the new binding.

## 7. Resume an inspected monitor

Read-only adapter queries retry up to a configurable consecutive-error budget (default 3). Sending prompts is never retried automatically. A fresh completion gets its own delivery grace period. After inspecting a terminal monitor anomaly, preserve inputs and resume explicitly:

```sh
python scripts/monitor_task.py --request /absolute/task/request.json --resume --notify
```

The old summary is retained as a history file; frozen inputs and notification attempts remain. Ready-for-review delivery cannot be resumed into a second execution. A stale `monitor.lock` needs deliberate process verification before removal. Do not reuse task IDs/directories or overwrite old evidence.

## 8. Provision an executor when needed

**Herdr-only example:** this section uses Herdr panes and CLI commands. For another client, use its verified window/session creation interface.

The snippet below illustrates the command sequence. It checks exit codes for pane split and pane run and validates the returned pane ID. It does not implement shell-readiness polling, registration polling, per-command timeouts, or exit-code handling for every read/query command. The comments mark steps for the operator or controller to perform; copying the snippet alone does not implement automatic provisioning. A controller must supply those waits, checks, and runtime-setting verification before binding or dispatch. Version-scoped reports are recorded in [the adapter field observations](harness-adapters.md#versioned-field-observations).

A pre-created pane is optional. Within the authorized session and workspace, an operator or verified controller can create a dedicated pane and launch an already-installed harness with the user's chosen arguments. This does not require installing providers or changing configuration. Inspect the installed CLI help first; the following primitives were reported working on Windows with Herdr 0.9.3. The same evaluation reported agent_pane_busy from agent start in idle cmd/PowerShell shells. Treat that as a version-specific compatibility report, not a universal Windows limitation. Use pane run on a newly created, verified shell pane in that environment.

```powershell
$herdrExe = 'C:/absolute/path/to/herdr.exe'
$workflowSession = 'OWNER-AUTHORIZED-SESSION'
$parentPane = 'VERIFIED-PARENT-PANE'
$taskCwd = 'E:/absolute/task-workspace'
# Supply the exact installed CLI command and provider/model/thinking arguments selected by the user.
$launchCommand = 'USER-SELECTED-HARNESS-AND-ARGUMENTS'
$created = & $herdrExe --session $workflowSession pane split --pane $parentPane --direction down --cwd $taskCwd --no-focus
if ($LASTEXITCODE -ne 0) { throw 'Pane creation failed; inspect before retrying' }
$workerPane = ($created | ConvertFrom-Json).result.pane.pane_id
if (-not $workerPane) { throw 'No pane identity returned; inspect session before proceeding' }
& $herdrExe --session $workflowSession pane read $workerPane --source recent-unwrapped --lines 3
# Confirm the new pane is an available shell before issuing the launch exactly once.
& $herdrExe --session $workflowSession pane run $workerPane $launchCommand
if ($LASTEXITCODE -ne 0) { throw 'Launch failed or uncertain; inspect pane, do not replay blindly' }
& $herdrExe --session $workflowSession agent get $workerPane
& $herdrExe --session $workflowSession pane read $workerPane --source recent-unwrapped --lines 6
```

These are provisioning primitives, not a cross-version turnkey launcher. A controller automating them must bound each subprocess (for example 15 seconds), poll shell readiness and registration with a deadline (for example 30 seconds each), and keep the returned pane ID as an owned resource. Inspect a timeout before retrying any creation or launch; leave pre-existing panes alone. Do not infer readiness from agent_status=unknown, process existence, or launch exit code.

After registration, verify actual agent kind, terminal, pane, and cwd with agent get. Read the harness's current runtime status, not its echoed launch command, to check provider, model, and thinking against the user's selection. If those values cannot be observed, record unverified and stop before binding/dispatch; a successful launch is insufficient. Then build the adapter spec and capture the live sequence baseline immediately before dispatch. The reported approximately 1.2-second provisioning measurement is one environment's observation, not a timeout or performance guarantee. No provisioning command is run by bind or preflight.
