# Agent Workflow Orchestrator (AWO)

English | [Chinese](README.zh-CN.md)

AWO coordinates work between AI agents. You set the goal and scope. A Planner assigns the work, an Executor delivers it, and an independent Reviewer checks the evidence. A local ledger records each step. AWO works on its own or with an optional secretary such as MAID.

## How it works

![From request to independent review](docs/images/workflow-overview.svg)

The Planner sends each task once. A monitor reports delivery; the Reviewer accepts it, requests repairs or rejects it. Independent tasks can run in parallel, while dependent steps wait for acceptance.

## Inside the workflow

### Roles, dependencies and independent review

Give the Planner a goal, a scope and an acceptance check. It can assign independent tasks in parallel and hold dependent work until the required reviews pass.
The Executor delivers files and evidence; the Reviewer examines them and records acceptance, repair or rejection.

### Isolated workspaces and the local merge queue

For code changes, give each assignment its own Git worktree so workers do not edit the same checkout.
An accepted change can then enter the local merge queue; if the target has moved or conflicts, check the recorded result before treating it as merged.

## What connects to the workflow

![Where clients, authorized knowledge, status and notifications connect](docs/images/integrations-overview.svg)

### Background monitoring and the dashboard

A local script watches for delivery and stalls without repeatedly asking a model to reread the conversation.
Open the dashboard to see which tasks are running, awaiting review or blocked; its default view is read-only.

### Client adapters

Use a supported client for each role instead of moving everyone to the same tool.
Check the adapter guide for the installed version: dispatch, resume and direct terminal interaction need not be supported equally.

### Project knowledge and cited search

Extract passages from Markdown or text PDFs without changing the source, then search authorized documents and research excerpts by topic without supplying IDs; results point back to the source location or PDF page.
Bilingual expansion helps match a query to English text, and a project glossary can restrict translations; access is checked before ranking, and revocation blocks subsequent reads.

### External inbox and notifications

An external channel can submit a request to the inbox for the Planner to inspect.
Outgoing alerts tell the Owner when a worker needs permission or a task is blocked, and tell the Reviewer when evidence is ready.

## Five safety rules

1. Configuration is not permission to dispatch, notify or keep running.
2. Delivery is not acceptance; acceptance is not a merge.
3. Send each task once. Inspect an uncertain send before taking action.
4. Only the current controller acts; an Executor cannot review its own work.
5. An incoming message cannot authorize work or answer an approval prompt.

## Let your AI set this up

~~~text
Help me set up Agent Workflow Orchestrator (AWO) using docs/setup.md in the agent-workflow-orchestrator repository.
Check Git, Python 3.10+ and installed clients.
Ask for a destination; use the documented release in a fresh subdirectory.
Run only the demo_scheduler.py and workflow_control.py status checks using new disposable demo state.
Do not modify my projects, accounts or keys, make paid calls, or dispatch work.
Report results and the next command, then stop.
~~~

## Guides

[Manual setup and documentation index](docs/setup.md) | [Material search and glossaries](references/material-packets.md)
