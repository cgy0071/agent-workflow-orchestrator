# Initialization, Window Selection, and Handoffs

English | [Chinese](setup-and-switching.zh-CN.md)

## 1. Choose roles, clients, and actual targets

Start from `examples/workflow.json`. A role's `harness` is descriptive; it does not select or launch a window. For each role, discover the actual client session through a verified interface and save its adapter specification. Any role can use any supported client.

For Herdr, use its installed CLI's read-only discovery and identity commands. Record the executable, session, pane, actual agent kind, terminal ID, and launch directory. For another CLI/API, provide commands that return the identity/status contract in [harness-adapters.md](harness-adapters.md). The package does not guess provider flags or launch interactive clients.

Extract the `worker` and `reviewer` objects from your edited `request.json` into `worker.json` and `planner.json`. Remove `baseline_completion_seq` from `worker.json`; keep it in the per-task request. Keep adapter specifications otherwise identical. A Paper Lead can have another reviewer specification. Names, models, and window titles alone do not establish identity.

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
