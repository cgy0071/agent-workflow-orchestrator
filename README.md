# Agent Workflow Orchestrator

English | [Chinese](README.zh-CN.md)

**Two AI agents split the work, and they hand tasks over through a ledger on disk rather than through chat.**
One plans and reviews, the other executes. Each task is dispatched once, and nothing counts as done until an
independent review accepts it. `scripts/` enforces those rules; prompts do not.

**[Install the Windows knowledge base plugin (0.4.1)](https://github.com/cgy0071/agent-workflow-orchestrator/releases/tag/kb-v0.4.1)** - Try a sample record with Python 3.10+ (including Anaconda).

**[Try the Windows host with task-scoped knowledge](https://github.com/cgy0071/agent-workflow-orchestrator/releases/tag/kb-host-v0.1.0-preview.1)** - Download the named Windows ZIP from the preview release (not GitHub's automatic Source code archive) and follow its three commands. The demo prints the project, revision, quote and `AWO KB HOST PREVIEW PASS`.

**[Download the Windows project commands 0.1.0](https://github.com/cgy0071/agent-workflow-orchestrator/releases/tag/project-cli-v0.1.0)** - Under Assets, download `AWO-Windows-Project-CLI-0.1.0.zip`, extract it, and run `python -B try_project.py` there with Python 3.10+.

[Project commands in this source checkout](docs/project-commands.md)

## Try it on Windows (no Git required)

**[Download AWO for Windows x64 (portable release candidate)](https://github.com/cgy0071/agent-workflow-orchestrator/releases/download/v1.0.0-rc.1/AWO-Windows-x64-rc-check.zip)** | [Release notes and checksum](https://github.com/cgy0071/agent-workflow-orchestrator/releases/tag/v1.0.0-rc.1)

Extract all files, then double-click the only `.cmd` file in the top folder. The offline demo runs two local workers and shows three accepted tasks: A, B, and JOIN. Open it again to see the saved result. Python is included; no account or API key is needed for the demo.

## Is this for you

**Yes, if any of these is true:**

- You already run two or more CLI agents (Claude Code, Codex, ...) and copy tasks between them by hand.
- You want tasks that run for tens of minutes without you watching.
- You need to show, afterwards, who dispatched this step, what evidence was delivered, and who approved it.

**No, if any of these is true:**

- You use one CLI only. Its built-in subagents are enough; no cross-process bridge needed.
- You want "give a goal, get it done." Every dispatch, notification and continuation needs your approval.
  That is a design choice, not missing work.

![Workflow overview: you give a goal, the Planner dispatches once, the Executor delivers files, monitor_task.py checks them, the ledger records one inbox event, and the Reviewer decides](docs/images/workflow-overview.svg)

## What changed in v1.0.0-rc.1?

| If you used... | What changes for you |
| --- | --- |
| An earlier source checkout | The Windows ZIP includes Python. Extract it and double-click to run the demo without cloning the repo or setting up an interpreter. The fix for writing task receipts under deep Windows paths has been included since preview.1. |
| The v1.0.0-preview.1 portable ZIP | Launch, offline execution, and reopening saved results work as before. RC1 adds the portable build scripts to source control so the same commit and pinned Python archive can reproduce the ZIP. It also tests the case where OMP times out and Windows cannot confirm process cleanup. |

If preview.1 already runs your workflow, there is no feature-driven need to upgrade. This candidate ships the **Windows orchestration tool**. Dispatch, review, and the A/B/JOIN demo already existed; the full Native Agent Framework is outside this ZIP. [Compare the two tags](https://github.com/cgy0071/agent-workflow-orchestrator/compare/v1.0.0-preview.1...v1.0.0-rc.1).

Let one AI agent plan and review while another does the work. You set the goal and say what is allowed; the agents hand tasks to each other through a local ledger, and nothing counts as done until it is reviewed.

## Supported features

The Windows ZIP runs the offline demo immediately. Real clients need configuration and authorization. To use an optional feature such as project knowledge, follow its setup guide for the project that needs it.

| Feature | What you can do | Access |
| --- | --- | --- |
| Offline demo | Run A and B with local workers, review JOIN and reopen saved results. | [Offline demo](references/manual-workflow.md) |
| Roles and review | Configure roles, inspect delivery evidence, then accept, request repair or reject. | [Setup](references/task-and-review.md) |
| Scheduling | Declare dependencies, dispatch independent work in parallel, and continue stalled tasks. | [Setup](references/scheduling.md) |
| Client adapters | Bind and switch workers; select command, receipt or optional Herdr transport based on verified client capabilities. | [Setup](references/harness-adapters.md) |
| Dashboard and alerts | Inspect progress and stalls, preview management actions, and send authorized notifications. | [Setup](references/monitoring.md) |
| External requests and remote access | Receive signed requests for review; optionally use a remote terminal from a phone. | [Setup](references/owner-intake.md) / [Details](references/remote-access.md) |
| Repository work | Prepare tasks in an existing repository, execute in isolated worktrees, review and land changes locally. | [Setup](references/project-entry.md) / [Details](references/worktree-code-loop.md) |
| Service and identity | Preview migrations, freeze writes, enroll agents, inspect capabilities and heartbeats, and revoke access. | [Enable for this project](references/awo-service.md) / [Details](references/awo-registry.md) |
| Budget and capacity | Reserve and reconcile usage; admit queued work by priority, available slots and resources. | [Enable for this project](references/awo-budget.md) / [Details](references/awo-scheduler.md) |
| Workspaces and merge queue | Assign an isolated directory or worktree, pin the baseline and land reviewed changes by ticket. | [Enable for this project](references/awo-workspaces.md) / [Details](references/awo-merge.md) |
| Messaging and collaboration | Send, reply to and acknowledge task messages; coordinate multiple workers with shared budgets and bounded repair. | [Enable for this project](references/awo-messaging.md) / [Details](references/awo-collaboration.md) |
| Project knowledge and documents | List authorized entries and read content, sources, revisions and review history by ID; check usability before reuse. | [Install the plugin](https://github.com/cgy0071/agent-workflow-orchestrator/releases/tag/kb-v0.4.1) / [Service API](references/awo-knowledge.md) |
| Application integrations | Call enabled service tools over MCP stdio, HTTP or WebSocket. | [Enable for this project](references/awo-gateway.md) |
| Supervision and audit | Inspect local advice and paired drafts, view project runs and export audit receipts. | [Enable for this project](references/awo-supervision.md) / [Details](references/awo-observability.md) |

## Offline demo (mechanics only)

Needs Python 3.10+, no agent account or network. Run from the repository root, with a new directory for each run:

```sh
python -B scripts/demo_scheduler.py --directory runtime/first-run
python -B scripts/workflow_control.py --state runtime/first-run/state status --summary --format text
```

Tasks A and B run in parallel on two fake workers; JOIN starts only after both are reviewed and accepted. The first command prints `OFFLINE_DEMO_PASS`, and the status shows all three as `accepted`. This proves local DAG, dependency and review mechanics with Python child processes, not Harness compatibility.

## How it works

1. The **Planner** writes a task and sends it once to the **Executor**.
2. A background script checks the Executor's status. It never asks a model to reread the conversation, so watching costs almost nothing.
3. When the expected files are delivered, the **Reviewer** gets one update, reads the evidence and decides: accept, repair or reject.
4. With your permission, the next task follows. Without it, the agents stop and report to you.

Roles are not tied to a client: the Planner can run in Codex and the Executor in Claude, or another supported mix. Support is verified per client version, not inferred from a harness name; see [harness-adapters.md](references/harness-adapters.md). The rules (send once, review before done, one controller at a time) are enforced by the scripts in `scripts/`, not by prompts. `SKILL.md` teaches the controlling agent when to run them.

## Optional: install as an agent skill from source

**You need first:**

- Two terminal windows, each able to start one CLI agent: one Planner, one Executor.
- Both agents signed in and able to run commands unattended.
- A writable project directory (parallel tasks need separate directories).

1. **Install.** Clone the repository into your client's Skill folder:

   ```sh
   git clone https://github.com/cgy0071/agent-workflow-orchestrator.git "$HOME/.codex/skills/agent-workflow-orchestrator"   # Codex
   git clone https://github.com/cgy0071/agent-workflow-orchestrator.git "$HOME/.claude/skills/agent-workflow-orchestrator"  # Claude Code
   ```

   Other clients (Pi, dsh-tui, Grok, ...) load `SKILL.md` through their own Skill or context mechanism.

2. **Open one window per role** and start each client, for example Claude for the Planner and Codex for the Executor.

3. **Tell the Planner what to do.** In Codex use `$agent-workflow-orchestrator`, in Claude Code `/agent-workflow-orchestrator`, then give a goal and a scope:

   ```text
   Set up a Planner/Executor workflow for: add pagination to the /users endpoint.
   You may dispatch, monitor and review within this goal. Ask me before anything else.
   ```

   The agent creates the workflow config, binds the windows, writes the task, dispatches it and watches it. It asks you when it needs a decision.

4. **Watch and decide.** Ask the agent to "open the dashboard", or double-click `dashboard.cmd` (Windows) / `dashboard.command` (macOS). It shows open tasks, updates waiting for review and alerts. It is read-only unless started with action options.

![Dashboard after the offline demo: goal, assigned roles, open and recent tasks, and the task plan](docs/images/dashboard-overview.png)

What stays with you: the goal and how far the agents may go, approval prompts in each agent's own window, and any review you have not delegated. To run every step by hand instead, see the [manual walkthrough](references/manual-workflow.md).

## First real run

Needs Python 3.10+ and an installed, authenticated Codex CLI. Run from the repository root, with a new directory for each run. This makes three real model turns on a disposable fixture and stops if any review gate fails:

```powershell
Remove-Item Env:HERDR_ENV -ErrorAction SilentlyContinue
python -B scripts/validate_v2_native_loop.py --directory runtime/repro/native-oneshot-1 --harness codex --mode one-shot --timeout 240
```

A pass prints `V2_NATIVE_LOOP_PASS` and exits 0. It proves the installed client, one-shot sessions, dependency gating and independent review on a directory-artifact task. It does not prove Herdr, steering or restart recovery, cost, provider/model identity, or worktree/code-task behavior. The [first-real-run guide](references/first-real-run.md) also covers session resume and the optional Herdr pane route. Git worktree and code-task behavior are covered separately by the [worktree code loop](references/worktree-code-loop.md).

## Optional extras

- **Dashboard management console** (`--allow-manage`): confirm starts, continue a stalled worker once, close tasks, change limits, assign windows, and see statistics. Every action is previewed, then confirmed once.
- **Alerts to you** (`owner_notify.py`): permission prompts, worker questions, failures and long waits, by file, webhook or command.
- **Requests from you** (`owner_intake.py`): a chat bot or other channel drops signed requests into an inbox; the Planner gets only a request ID.

## Constraints you accept

These are not optional switches. They are things it refuses to do by design:

- **Configuration is not permission.** Dispatch, notification and continuous running each need your explicit
  approval.
- **Done means reviewed.** An idle window or a zero exit code is never acceptance.
- **Each task is sent once.** An uncertain send is recorded for inspection, never retried blindly. Approval
  prompts are never answered automatically.
- **One controller, no self-review.** An old window loses authority after a switch; a worker cannot review its
  own task.
- **Outside requests are data.** A request from a chat channel cannot start work without your direct approval.

A pass certifies only what it ran. Directory-artifact validation does not certify Git worktrees or code tasks; priority is not live steering; budget admission and settlement do not stop a call in progress; legacy knowledge origin can remain unknown.

## Known limits

- Adapters watch a target CLI's window state and session identity. That is a **UI contract, not an API
  contract**, so a client upgrade can break them. The project states that offline tests do not establish live
  compatibility for every client version.
- Verified harness combinations are limited. Herdr is optional; the command adapter works without it. See
  [client adapters](references/harness-adapters.md).
- All state lives on the local filesystem; nothing aggregates across machines.

## Documentation

**Not sure where to start: read the [manual walkthrough](references/manual-workflow.md) first.** It covers the
core concepts, config files, the full safety model and the repository layout.

| Topic | Guide |
|---|---|
| Doing every step by hand; concepts, config files, full safety model, repository layout | [manual-workflow.md](references/manual-workflow.md) |
| Setup, bindings, switching windows, recovery | [setup-and-switching.md](references/setup-and-switching.md) |
| First real run: native one-shot, session resume, optional Herdr route, limits | [first-real-run.md](references/first-real-run.md) |
| Monitoring, dashboard, alerts, Owner intake | [monitoring.md](references/monitoring.md) |
| Dispatch, parallel task plans, continuation | [scheduling.md](references/scheduling.md) |
| Client adapters (Herdr, command, receipt) | [harness-adapters.md](references/harness-adapters.md) |
| Roles, task and review templates, task states | [roles.md](references/roles.md), [task-and-review.md](references/task-and-review.md), [task-state-machine.md](references/task-state-machine.md) |
| Optional remote command line from a phone (Windows, SSH over Tailscale) | [remote-access.md](references/remote-access.md) |
| Channel contract for Owner requests | [owner-intake.md](references/owner-intake.md) |
| Opt-in V2 service boundary: additive schema, dry-run migration, freeze/epoch | [awo-service.md](references/awo-service.md) |
| Agent registry: enrollment grants, capability evidence, heartbeat, revocation | [awo-registry.md](references/awo-registry.md) |
| Budget ledger: scoped reservations, usage/exposure, authoritative close | [awo-budget.md](references/awo-budget.md) |
| Priority scheduler: queue order, collaboration slots, holds, admission gates | [awo-scheduler.md](references/awo-scheduler.md) |
| Assignment workspaces: isolated roots, Git worktrees, frozen source | [awo-workspaces.md](references/awo-workspaces.md) |
| Durable agent messaging: task/run scoped inbox, reply, acknowledgement | [awo-messaging.md](references/awo-messaging.md) |
| Local knowledge and documents: scoped registration and retrieval | [awo-knowledge.md](references/awo-knowledge.md) |
| Gateway and application transports: handlers, routing, replay rules | [awo-gateway.md](references/awo-gateway.md) |
| Collaboration runtime: fanout, ensemble, shared budget, bounded repair | [awo-collaboration.md](references/awo-collaboration.md) |
| Merge queue: accepted bases, materialization gate, ticket-bound intents | [awo-merge.md](references/awo-merge.md) |
| End-to-end Git worktree code loop: real worktree execution, independent review, local Merge Queue, materialization | [worktree-code-loop.md](references/worktree-code-loop.md) |
| Project entry for an existing repository: prepare, run, independent review, local landing, recovery | [project-entry.md](references/project-entry.md) |
| Local supervision and paired external drafts (local label protocol, not certified A2A) | [awo-supervision.md](references/awo-supervision.md) |
| Read-only runtime views and isolated audit receipts | [awo-observability.md](references/awo-observability.md) |
| What changed and when | [CHANGELOG.md](CHANGELOG.md) |

English entry points are ASCII-only; Chinese translations live in `*.zh-CN.md` and are updated together.

## License

The RC1 tag includes the [MIT license](https://github.com/cgy0071/agent-workflow-orchestrator/blob/v1.0.0-rc.1/LICENSE). The current main branch does not yet include a root LICENSE file.
