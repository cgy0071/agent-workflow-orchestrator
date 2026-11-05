# Agent Workflow Orchestrator

English | [Chinese](README.zh-CN.md)

Let one AI agent plan and review while another does the work. You set the goal and say what is allowed; the agents hand tasks to each other through a local ledger, and nothing counts as done until it is reviewed.

![Workflow overview: you give a goal, the Planner dispatches once, the Executor delivers files, monitor_task.py checks them, the ledger records one inbox event, and the Reviewer decides](docs/images/workflow-overview.svg)

## How it works

1. The **Planner** writes a task and sends it once to the **Executor**.
2. A background script checks the Executor's status. It never asks a model to reread the conversation, so watching costs almost nothing.
3. When the expected files are delivered, the **Reviewer** gets one update, reads the evidence and decides: accept, repair or reject.
4. With your permission, the next task follows. Without it, the agents stop and report to you.

Roles are not tied to a client: the Planner can run in Codex and the Executor in Claude, or any other mix. The rules (send once, review before done, one controller at a time) are enforced by the scripts in `scripts/`, not by prompts. `SKILL.md` teaches the controlling agent when to run them.

## Get started

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

## Try it offline in 30 seconds

Needs Python 3.10+, no agent account or network:

```sh
python -B scripts/demo_scheduler.py --directory runtime/first-run
python -B scripts/workflow_control.py --state runtime/first-run/state status --summary --format text
```

Tasks A and B run in parallel on two fake workers; JOIN starts only after both are reviewed and accepted. The first command prints `OFFLINE_DEMO_PASS`, and the status shows all three as `accepted`.

## Safety in short

- **Configuration is not permission.** Dispatch, notification and continuous running each need your explicit approval.
- **Done means reviewed.** An idle window or a zero exit code is never acceptance.
- **Each task is sent once.** An uncertain send is recorded for inspection, never retried blindly. Approval prompts are never answered automatically.
- **One controller, no self-review.** An old window loses authority after a switch; a worker cannot review its own task.
- **Outside requests are data.** A request from a chat channel cannot start work without your direct approval.

## Optional extras

- **Dashboard management console** (`--allow-manage`): confirm starts, continue a stalled worker once, close tasks, change limits, assign windows, and see statistics. Every action is previewed, then confirmed once.
- **Alerts to you** (`owner_notify.py`): permission prompts, worker questions, failures and long waits, by file, webhook or command.
- **Requests from you** (`owner_intake.py`): a chat bot or other channel drops signed requests into an inbox; the Planner gets only a request ID.

## Documentation

| Topic | Guide |
|---|---|
| Doing every step by hand; concepts, config files, full safety model, repository layout | [manual-workflow.md](references/manual-workflow.md) |
| Setup, bindings, switching windows, recovery | [setup-and-switching.md](references/setup-and-switching.md) |
| Monitoring, dashboard, alerts, Owner intake | [monitoring.md](references/monitoring.md) |
| Dispatch, parallel task plans, continuation | [scheduling.md](references/scheduling.md) |
| Client adapters (Herdr, command, receipt) | [harness-adapters.md](references/harness-adapters.md) |
| Roles, task and review templates, task states | [roles.md](references/roles.md), [task-and-review.md](references/task-and-review.md), [task-state-machine.md](references/task-state-machine.md) |
| Channel contract for Owner requests | [owner-intake.md](references/owner-intake.md) |
| What changed and when | [CHANGELOG.md](CHANGELOG.md) |

English entry points are ASCII-only; Chinese translations live in `*.zh-CN.md` and are updated together.
