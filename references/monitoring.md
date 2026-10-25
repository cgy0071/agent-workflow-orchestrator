# Status Monitoring and Automatic Notification

English | [Chinese](monitoring.zh-CN.md)

The monitor checks lifecycle and delivery, then notifies a reviewer. It does not create tasks, approve prototypes, or read project-specific files. Business acceptance remains with the configured reviewer.

For initialization, actual window selection, conversation policies, handoffs, and review gates, use [setup-and-switching.md](setup-and-switching.md). Set workflow_state in the request for ledger-backed operation. A harness label never selects the target. The final request is frozen at registration/monitoring startup; reviewer handoffs use versioned bindings without editing old requests.

## Request file

Start with `examples/request.herdr.json`. Use a new directory for each task. Freeze the request before submission, then record dispatch_confirmed after the Worker starts, retaining the real pre-dispatch completion sequence. The monitor writes monitor_status, monitor_summary, completion_receipt, and notification_attempt/result; it does not overwrite task delivery.

All adapters use task_id, nonce, cwd, worker, reviewer, completion, and authorization. Role names and harness labels are editable metadata.

| Setting | Meaning |
|---|---|
| worker.expected_identity | Verified session/job identity fields and directory |
| worker.baseline_completion_seq | Pre-dispatch sequence; a new completion must exceed it |
| completion.artifacts | Required files; objects may specify nonempty and a known sha256 |
| completion.binding_artifact | Markdown or structured file containing this task ID and nonce |
| completion.marker | Marker required at the selected source; omit only when another reliable contract is used |
| completion.marker_source | artifact: binding file; output: bounded terminal text; both: require both. Legacy default is output; new example uses artifact |
| monitor.interval_seconds | Default 300; configurable from 1 to 86400 seconds |
| monitor.command_timeout_seconds | Timeout for each adapter command; default 15 seconds |
| monitor.settle_grace_seconds | Grace for delivery after idle/done; default 120 seconds |
| monitor.max_consecutive_errors | Consecutive read-only query error budget; default 3, positive integer |
| monitor.retry_interval_seconds | Delay between read-only retries; default 5 seconds |
| monitor.overall_timeout_seconds | Default null: wait until completion/exception; optionally impose a total monitor limit |
| status_map | Map adapter states to working/idle/done/blocked/exited/failed |
| authorization.notify_reviewer | Existing user authorization to notify once; source must also be nonempty |
| reviewer.context_policy | Default inbox_only; direct policies need verified context evidence |
| workflow_state | Optional absolute directory of an initialized workflow ledger |

Change the default three Markdown deliverables if needed. Without expected output hashes, the monitor records actual hashes but does not judge business correctness.

## Start monitoring

```sh
python scripts/monitor_task.py --request /absolute/task/request.json --check
python scripts/monitor_task.py --request /absolute/task/request.json --preflight
python scripts/monitor_task.py --request /absolute/task/request.json --notify
```

--check contacts no agents and returns STRUCTURE_OK. --preflight queries live identities and the baseline sequence without sending. Ledger requests must be registered before monitoring. --notify requires authorization and the appropriate context policy. Monitoring depends on the local process, reviewer session, and transport; network, quota, and session failures are exceptions.

Windows background example; replace paths with verified local paths:

```powershell
Start-Process -FilePath 'python.exe' -ArgumentList @(
  'C:/skills/agent-workflow-orchestrator/scripts/monitor_task.py',
  '--request', 'C:/tasks/TASK-ID/request.json', '--notify'
) -WindowStyle Hidden -RedirectStandardOutput 'C:/tasks/TASK-ID/monitor.stdout.log' `
  -RedirectStandardError 'C:/tasks/TASK-ID/monitor.stderr.log'
```

Use an authorized execution context without changing system policy or answering approval dialogs automatically. Linux/macOS users can use an existing process manager; this skill installs no background service.

## Completion and continuation

While working, query status only. After fresh completion and valid delivery, read bounded output for the marker and save a receipt. A new completion sequence gets its own delivery grace; working clears old idle timers. Invalid sequence values are response errors, never truncated.

Ledger mode persists inbox events before optional notification, even without --notify. Default inbox_only sends no prompt; standalone mode writes pending_event.json when --notify is authorized. Direct notifications recheck identity/context policy. Sending is not acknowledgment or acceptance. The setup guide explains acknowledgment, REVIEW.md registration, and continuation gates. Messages are not new Owner instructions.

An exclusive notification-attempt file is written before sending. Delivery timeouts do not trigger automatic retries. Inspect the actual session after delivery_uncertain. monitor.lock prevents duplicate monitors. If a crash leaves the lock, inspect the process before deliberate recovery. Never reuse a monitor directory for a different task.

Ledger mode records attempts transactionally in SQLite instead of a task-local attempt file. Each attempt binds the target, request/authorization hashes, and UTC time. Read-only query failures have bounded retries; identity changes, blocked states, unknown states, and failures stop for inspection. After inspection, --resume preserves the old summary as history and resumes unchanged frozen inputs. It never retries a previously attempted prompt or resumes review-ready delivery into new execution.

## Other adapters

Herdr responses use completion_seq when present, otherwise state_change_seq. Missing or invalid counters are errors, not zero. Copy the observed normalized value into baseline_completion_seq before dispatch. state_change_seq records every state change, so its advance is only freshness evidence: idle/done, task/nonce-bound artifacts, and the configured marker are still required. Its delivery grace starts at the first fresh idle/done observation and is not renewed by counter churn, including intervening working states. A true completion_seq retains a new grace period for each completion. Receipts record sequence_source.

For new tasks, put the exact marker in HANDOFF.md and select marker_source=artifact (as in the example). Terminal text can include echoed commands and is not a trustworthy authorship signal. A file marker strengthens the delivery contract but does not authenticate its author or prove acceptance; independent review remains required. Existing frozen requests keep the legacy output default; do not edit them in place.

- command: worker.status_argv emits canonical JSON; output_argv emits bounded completion text. Reviewer identity_argv emits identity, and prompt_argv accepts one standalone `{message}`. Arguments are executed without shell interpolation.
- receipt: worker.status_path points to task-bound JSON and output_path to stdout. Use `run_job.py` for verified one-shot CLI commands; see examples/job.json. Set baseline_completion_seq to 0 for a new job. Do not treat a persistent TUI process as a one-shot job.

See harness-adapters.md for details. Every role may use any harness, but actual notification needs a verified identity and precise session-delivery interface. Report ADAPTER_NOT_VERIFIED when that interface is absent.

Creating STOP_MONITOR in the task directory stops monitoring on its next iteration, sends no continuation notification, and does not cancel the Worker. Handle actual cancellation through authorized scheduling. Preserve failures and parent-task records.

## Dashboard

The standard-library-only local dashboard binds to loopback, prints a per-process token URL, and opens it in the default browser (`--no-browser` only prints it). Double-click `dashboard.cmd` (Windows) or `dashboard.command` (macOS), or run:

```sh
python -B scripts/dashboard.py --state /absolute/workflow-state
```

Without `--state`, the page asks for a state directory and lists up to 10 recent ones from `~/.agent-workflow-orchestrator/dashboard-recent.json`; the file stores paths only. **Switch state** changes the viewed directory while dispatch is disabled. If the default port 8765 is busy, the server picks a free port. Set `AWO_PYTHON` to choose the launcher's interpreter.

The page reads the ledger without changing it or querying agents. It shows bindings, task phases and evidence, DAG dependencies, inbox events, resource claims, and alerts. Automatic refresh reads only the ledger; **Check workers now** explicitly makes bounded live status queries. The server checks its token, Host, and POST Origin; it uses a restrictive CSP and renders ledger text as text. Do not expose or forward the token URL.

To enable the form, add `--allow-dispatch --actor INSTANCE --version 1 --context main --tasks-root /absolute/tasks` with `--state` at startup; the state is then fixed. The actor must be the active controller and `main` is the operator's explicit declaration. Preview checks a live idle Worker and returns a task-bound request, nonce, message, and digest without writing files. Confirm rechecks the frozen preview and baseline, creates a new task directory, and delegates one send to `workflow_scheduler.dispatch`; uncertain sends are never retried. After an independent observation, write `EXECUTION.md` yourself and run the displayed `reconcile` command, then start the monitor. The page does not perform reconcile, review, binding changes, or DAG ticks.
