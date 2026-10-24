---
name: agent-workflow-orchestrator
description: Operating procedure for the bundled local multi-agent orchestration tool (scripts/). Use it to configure editable roles, architecture rules, and automation policies, then dispatch once, monitor status, record completion events, and gate independent review through the tool's ledger. Planner, Executor, and Paper Lead are defaults; any role can run in Codex, Pi, Claude, dsh-tui, Grok, or another harness through Herdr, command, or receipt adapters.
---

# Agent Workflow Orchestrator

English | [Chinese](SKILL.zh-CN.md)

Coordinate planning, execution, monitoring, delivery, review, and follow-up tasks. This skill is the operating procedure for the tool in `scripts/`: the scripts enforce single dispatch, identity checks, deduplicated notification, and review gates; this document tells you when and how to run them. Both are independent of any project, path, model, or harness. Planner, Executor, and Paper Lead are editable defaults.

## Load the workflow

Read the user-selected workflow configuration. If none exists, start with `examples/workflow.json`. Establish the objective, role responsibilities, writable scope, project constraints, and automation authorization. Existing project protocols are optional inputs chosen by the user; this skill needs no external role document or other skill.

Users may change role names, count and permissions; architecture constraints; stages and admission gates; dispatch and review roles; polling intervals; completion criteria; exception handling; follow-up actions; and stop conditions. Configuration does not grant new permission. Current user instructions take precedence.

## Separate roles from harnesses

Read [roles.md](references/roles.md) for the default roles. Rename, combine, add, or replace them as needed. Do not apply default Planner restrictions to a role the user has explicitly redefined.

Any role can run in Codex, Pi, Claude, dsh-tui, Grok, or another harness. Record the role, harness, model, and control interface separately. Select a verified Herdr, command, or job-receipt interface using [harness-adapters.md](references/harness-adapters.md). Do not infer CLI capabilities from a nickname. Report an unavailable interface rather than claiming a connection.

## Dispatch and execution

For durable single-attempt submission or DAG branches/joins, read [scheduling.md](references/scheduling.md). Use its scheduler with explicit per-task dispatch authorization and verified worker commands. It persists reservations before sending, enforces shared-state workspace/resource claims, and gates dependencies on accepted evidence. Use [capabilities.md](references/capabilities.md) to record versioned operation evidence; labels alone do not establish client support.

Before first dispatch or any harness/window change, read [setup-and-switching.md](references/setup-and-switching.md). Initialize role bindings, verify actual targets, and record controller instance/version. Side conversations are advisory-only. Default to inbox-only notification; exact-thread routing needs real conversation/branch metadata. A dedicated main window requires an explicit declaration. Context flags are trusted adapter/operator metadata, not automatic branch detection or authentication.

1. Recover the objective, current tasks, accepted results, and open gates from configured records. Do not infer completed work from chat alone.
2. Write a reviewable task with an ID, objective, directory, allowed changes, frozen inputs, acceptance criteria, deliverables, and stop conditions. Adapt [task-and-review.md](references/task-and-review.md) to the project.
3. Recheck the target identity and working directory, active tasks, and directory leases. Serialize shared writable directories; parallel tasks need independent directories. Reuse an existing dispatch ledger instead of creating competing controllers.
4. Save a unique task ID and nonce, the verified target, task specification, and submission attempt. Submit once, confirm execution began, then start monitoring. Inspect uncertain delivery before considering any follow-up.
5. The Executor performs the configured work and delivers the agreed evidence. RESULT, EVIDENCE, and HANDOFF Markdown files are defaults; the task may specify another format.

Herdr is optional. Reuse its identity and lease controls when already present; otherwise use a verified official CLI/API adapter. Do not install or alter providers, credentials, or model settings as part of dispatch.

## Monitoring and notifications

`scripts/monitor_task.py` reads status without repeatedly fetching conversations while work is running. The default interval is 300 seconds and is configurable. Each command has a finite timeout; overall monitoring continues until completion or an exception unless a total limit is configured. See [monitoring.md](references/monitoring.md). Do not start duplicate monitors for one task.

Completion must match the task identity, worker identity, a fresh completion sequence, and the configured artifact/marker contract. `review_ready` means ready for review, not accepted. Record artifact hashes and, under existing notification authorization, notify the verified reviewer once. The reviewer may use any harness and need not be named Planner.

With workflow_state enabled, register the frozen request before monitoring and persist events before notification. On a handoff, recover pending inbox events through the new verified role binding. A sent prompt is not acknowledgment. Query failures have a finite retry budget; uncertain prompt delivery is never automatically retried. Resume inspected anomalies explicitly with unchanged inputs.

Record blocked states, missing delivery, changed identities, failures, and uncertain notifications separately. Never answer approval dialogs or blindly resend. Stopping the monitor does not stop the worker; handle cancellation separately under the configured authorization.

## Review and continuation

The reviewer reads the frozen request, delivery, changes, and raw acceptance evidence; records accept, repair, reject, or missing evidence; and updates current state. Advance only through configured stages. Engineering admission and paper-contribution judgment can remain separate gates.

For ledger-backed workflows, acknowledge the event, record a task/nonce-bound REVIEW.md, run next-check, and seal the returned parent_review into the follow-up request. Stale controllers and declared side/unknown contexts cannot perform these operations. Worker switches wait for old execution to end and require a fresh identity check; monitoring failure alone does not establish termination.

Within continuous-execution authorization, create the next task or a narrow repair with a new ID and nonce linked to the parent. Report work outside the objective, budget, or permissions. Stop when the configured goal, pause, cancellation, or termination condition is reached. Do not invent additional work just to keep the loop running.

By default, Paper Lead owns the research problem, literature gap, contributions, and claim-evidence reasoning. Send reviewed engineering results to that role and route evidence requests back through planning. Users may replace this role with product review, quality review, or another responsibility.

## Local validation

Before using a new or changed adapter, validate identity, fresh completion, missing output, blocked states, and notification deduplication offline. Then perform an authorized connection check. Run `python -B -m unittest discover -s scripts -p "test_*.py" -v` and `python scripts/check_encoding.py`. Offline tests do not establish live compatibility for every client version. README contains setup, configuration, and usage examples.
