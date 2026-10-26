# Initialization, Window Selection, and Handoffs

English | [Chinese](setup-and-switching.zh-CN.md)

## 1. Choose roles, clients, and actual targets

Start from `examples/workflow.json`. A role's `harness` is descriptive; it does not select or launch a window. For each role, discover the actual client session through a verified interface and save its adapter specification. Any role can use any supported client.

Terminology: **pane** in this guide refers specifically to a Herdr terminal split. Windows and sessions are the general concepts; other clients use their own verified interfaces and identifiers. Herdr pane commands do not apply to those clients.

For Herdr, use its installed CLI's read-only discovery and identity commands. Record the executable, session, pane, actual agent kind, terminal ID, and launch directory. For another CLI/API, provide commands that return the identity/status contract in [harness-adapters.md](harness-adapters.md). Harness launch arguments must come from the user's selected runtime.

Open or select a client session, then verify its identity and current tasks before binding.

### Open and verify a session

1. Open a dedicated window/session in the selected client (a pane when using Herdr) and enter the task workspace.
2. Start the installed harness and select the intended provider, model, and thinking level where applicable.
3. Check the actual runtime settings, then discover the session identity and cwd through the verified adapter; for Herdr, also record pane and terminal identities.
4. Save the role's adapter specification and continue with section 2. Capture the live sequence baseline immediately before dispatch.

After verification, extract the `worker` and `reviewer` objects from your edited `request.json` into `worker.json` and `planner.json`. Remove `baseline_completion_seq` from `worker.json`; keep it in the per-task request. Keep adapter specifications otherwise identical. A Paper Lead can have another reviewer specification. Names, models, and window titles alone do not establish identity.

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

Ledger bind/switch stores the selected policy in the adapter spec and fills a missing context_declaration from --reason for dedicated_window. The standalone request still needs its own declaration. Binding and preflight check direct Herdr targets with read-only queries against the conditions Herdr's native prompt enforces (as of Herdr 0.9.3): the reported `agent` label must be a native Herdr agent kind (such as pi, codex, or claude), `launch_pending` must not be true, and `agent_status` must not be blocked. Sending rechecks identity and these conditions. No test prompt is sent. The optional `agent_session` field is session metadata and is neither required nor sufficient. Herdr itself checks that the agent is still the foreground process when the prompt arrives, so passing these checks is not a guarantee of delivery; timeout/uncertainty rules still apply.

A custom label such as `custom:dsh-tui` can report status, but Herdr's native prompt rejects it with agent_not_ready. For such targets, use `inbox_only` or a separately verified command bridge. Do not infer compatibility from a harness name or nullable name field, and do not rename a label to pass the check.

When a binding becomes active (bind or activate), AWO reads the live Herdr pane name. An empty name or a stale `awo:` name is set to `awo:<role>`; a custom name is kept. When a role moves, the old pane is cleared only if its name is exactly the old role's AWO name. `label-panes` applies the same rules to active panes and clears AWO names on unbound panes; `--force` also replaces custom names on active panes. Results report each pane's original name and action. A query or rename failure is reported as `label_error` without blocking the binding. Display names are never stored in the ledger or used for identity checks; identity still uses pane, terminal ID, agent kind, and launch directory. The dashboard reads actual names only when you click **Check agents now**.

```sh
python scripts/workflow_control.py --state STATE roles        # role -> pane, identity check, unbound agents
python scripts/workflow_control.py --state STATE label-panes  # restore missing names, clear stale awo: names
python scripts/workflow_control.py --state STATE label-panes --force  # explicitly replace custom names on bound panes
```

`roles` is read-only; "terminal not found" means the agent was closed or restarted and must be rebound.

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

The register-task command records work submitted through an existing controller. Alternatively, use [durable dispatch and DAG scheduling](scheduling.md) to reserve and send once, confirm execution, and enforce workspace/resource claims within the shared state database. The monitor requires an identical confirmed registered request and writes `frozen_request.json`. Follow-up registration checks the sealed parent review; DAG dependencies separately require accepted evidence. Business review remains an authorized reviewer responsibility.

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
