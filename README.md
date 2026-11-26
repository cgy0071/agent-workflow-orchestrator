# Agent Workflow Orchestrator (AWO)

English | [Chinese](README.zh-CN.md)

Coordinating multiple AI agents on real code or research projects usually breaks down in three ways: agents edit outside their scope, workers declare victory without real checks, and task state gets lost across terminals. **AWO is a local multi-agent workflow and review engine**: one agent plans, another executes, and an independent reviewer decides based on recorded evidence. **Every guardrail is enforced by local scripts and a ledger, not by prompts alone.**

> AWO works completely on its own, or alongside an optional project secretary such as [MAID](https://github.com/cgy0071/MAID).

## Core workflow

![From request to independent review](docs/images/workflow-overview.svg)

You set the goal and allowed file scope. The **Planner** writes a clear task spec and dispatches it once to the **Executor**. A lightweight background script watches local receipts without burning model tokens. Once files and evidence are delivered, an independent **Reviewer** inspects them and records **accept**, **repair**, or **reject**. Independent tasks run in parallel; dependent steps wait until required reviews pass.

## Architecture and optional modules

![Where clients, authorized knowledge, status and notifications connect](docs/images/integrations-overview.svg)

Each module plugs cleanly into the core ledger so you can enable only what your project needs:

- **Role orchestration and independent review**
  Separates planning, execution, and review into distinct roles. Supports single tasks, parallel branches, and dependency DAGs while structurally preventing self-review.
- **Isolated workspaces and local merge queue**
  Runs each code assignment in its own Git worktree so parallel workers never step on each other. Accepted changes enter a local merge queue that keeps **accepted** strictly separate from **merged** and stops cleanly on conflicts or moved bases.
- **Low-cost monitoring and visual dashboard**
  Background monitoring reads local receipts instead of asking a model to reread chat history. A read-only dashboard shows active, blocked, and review-ready tasks at a glance (with an optional confirmation-gated management console).
- **Multi-client adapters**
  Decouples roles from tools: mix Codex, Claude, Pi, OMP, or optional Herdr panes across Planner, Executor, and Reviewer without forcing every role onto one client.
- **Project knowledge, topic search, and glossary constraints**
  Extracts passages read-only from local Markdown / Obsidian notes and text PDFs (preserving source offsets and page numbers without modifying files). Search authorized documents by topic without memorizing IDs, with bilingual query expansion, project glossaries (including avoid-lists), and optional `/v1/embeddings` support. **Access is checked before ranking, and revocation blocks subsequent reads immediately.**
- **External inbox and proactive alerts**
  Accepts signed external requests into an inbox for Planner inspection, and pushes authorized alerts to the Owner on permission prompts or stalls and to the Reviewer when evidence is ready.

## Five safety rules

1. **Configuration is not permission**: config files never authorize dispatch, notifications, or continuous runs without your explicit approval.
2. **Delivery is not acceptance, and acceptance is not a merge**: an idle terminal or zero exit code never counts as done, and accepted code is never auto-pushed.
3. **Each task is sent once**: uncertain sends are logged for inspection, never blindly retried, and permission prompts are never auto-approved.
4. **One controller, no self-review**: switching controllers revokes the old window immediately; an Executor can never approve its own delivery.
5. **Outside requests are data**: external messages can never authorize work on their own.

## Let your AI set this up

You do not need to type setup commands by hand. Copy this prompt into your local AI assistant (Codex, Claude Code, Antigravity CLI (agy), or Cursor) to run the 1-minute offline demo with zero model cost:

~~~text
Help me set up Agent Workflow Orchestrator (AWO) by following docs/setup.md in this repository.
First check Git, Python 3.10+, and installed AI clients. Ask me which folder to use, and install the documented release into a fresh subdirectory.
Run only demo_scheduler.py and workflow_control.py status using a fresh temporary directory for the offline demo.
Do not modify my existing projects, accounts, or keys, and do not make paid model calls or dispatch real work.
Report the demo results and how to start managing my own project, then wait for my decision.
~~~

## Further reading

- [Manual setup and full documentation index](docs/setup.md)
- [Project material search and glossaries](references/material-packets.md)
