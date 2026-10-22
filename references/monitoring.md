# Status Monitoring and Automatic Notification

English | [Chinese](monitoring.zh-CN.md)

The monitor checks lifecycle and delivery, then notifies a reviewer. It does not create tasks, approve prototypes, or read project-specific files. Business acceptance remains with the configured reviewer.

## Request file

Start with `examples/request.herdr.json`. Use a new directory for each task. Freeze the request before submission, then record dispatch_confirmed after the Worker starts, retaining the real pre-dispatch completion sequence. The monitor writes monitor_status, monitor_summary, completion_receipt, and notification_attempt/result; it does not overwrite task delivery.

All adapters use task_id, nonce, cwd, worker, reviewer, completion, and authorization. Role names and harness labels are editable metadata.

| Setting | Meaning |
|---|---|
| worker.expected_identity | Verified session/job identity fields and directory |
| worker.baseline_completion_seq | Pre-dispatch sequence; a new completion must exceed it |
| completion.artifacts | Required files; objects may specify nonempty and a known sha256 |
| completion.binding_artifact | Markdown or structured file containing this task ID and nonce |
| completion.marker | Marker required in bounded completion output; omit only when another reliable contract is used |
| monitor.interval_seconds | Default 300; configurable from 1 to 86400 seconds |
| monitor.command_timeout_seconds | Timeout for each adapter command; default 15 seconds |
| monitor.settle_grace_seconds | Grace for delivery after idle/done; default 120 seconds |
| monitor.overall_timeout_seconds | Default null: wait until completion/exception; optionally impose a total monitor limit |
| status_map | Map adapter states to working/idle/done/blocked/exited/failed |
| authorization.notify_reviewer | Existing user authorization to notify once; source must also be nonempty |

Change the default three Markdown deliverables if needed. Without expected output hashes, the monitor records actual hashes but does not judge business correctness.

## Start monitoring

```sh
python scripts/monitor_task.py --request /absolute/task/request.json --check
python scripts/monitor_task.py --request /absolute/task/request.json --notify
```

--check contacts no agents. --notify also requires request authorization. Monitoring depends on the local process, reviewer session, and transport remaining available; network, quota, and session failures are recorded as exceptions.

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

While working, query status only. After a fresh completion and complete delivery, read bounded output for the marker, save a receipt, and notify the verified reviewer once. The message requests independent review, a decision, and continuation within existing authorization; it explicitly is not a new Owner instruction.

An exclusive notification-attempt file is written before sending. Delivery timeouts do not trigger automatic retries. Inspect the actual session after delivery_uncertain. monitor.lock prevents duplicate monitors. If a crash leaves the lock, inspect the process before deliberate recovery. Never reuse a monitor directory for a different task.

## Other adapters

- command: worker.status_argv emits canonical JSON; output_argv emits bounded completion text. Reviewer identity_argv emits identity, and prompt_argv accepts one standalone `{message}`. Arguments are executed without shell interpolation.
- receipt: worker.status_path points to task-bound JSON and output_path to stdout. Use `run_job.py` for verified one-shot CLI commands; see examples/job.json. Set baseline_completion_seq to 0 for a new job. Do not treat a persistent TUI process as a one-shot job.

See harness-adapters.md for details. Every role may use any harness, but actual notification needs a verified identity and precise session-delivery interface. Report ADAPTER_NOT_VERIFIED when that interface is absent.

Creating STOP_MONITOR in the task directory stops monitoring on its next iteration, sends no continuation notification, and does not cancel the Worker. Handle actual cancellation through authorized scheduling. Preserve failures and parent-task records.
