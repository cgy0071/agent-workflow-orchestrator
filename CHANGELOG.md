# Changelog

English | [Chinese](CHANGELOG.zh-CN.md)

The project has no numbered releases yet. Entries are grouped by the date changes reached `main`; pull request numbers link to the details. Every change keeps the safety model: each task is sent once, approval prompts are never answered automatically, done means an independent recorded review, and configuration is not permission.

## Unreleased

### Added

- **Opt-in V2 service boundary.** `scripts/awo_service.py` adds additive `v2_*` tables, a read-only dry-run and controller-applied conservative migration, a command envelope with durable idempotency, and freeze/epoch guards for legacy register/dispatch and the final send of continuations. V2 stays disabled by default; the later V2 components listed below are separate opt-in modules. Migration reuses the existing acceptance gate and never upgrades missing or changed evidence. This work is uncommitted and not on `main` or GitHub. See [references/awo-service.md](references/awo-service.md).
- **Minimal Agent registry.** Controller-issued single-use enrollment grants, pending self-registration, capability evidence levels, authenticated heartbeat, and revocation. Secrets are written only to exclusive local files; capability support requires matching context plus a rechecked local evidence hash. No scheduler/MCP/messaging authority is added. See [references/awo-registry.md](references/awo-registry.md).
- **Budget ledger.** Run/task/workflow scopes derived from the Assignment/Task/project, atomic scoped reservations, delta/cumulative samples, per-run exposure, authoritative close reconciliation, dimension-specific unknown handling, and controller limit/action commands. No scheduler activation or provider hard-cap claim is added. See [references/awo-budget.md](references/awo-budget.md).
- **Additional V2 components (unreleased).** Opt-in modules extend the same ledger without adding another authority: the priority scheduler (queue order, collaboration slots, admission gates), assignment workspaces (isolated roots, Git worktrees, frozen source), durable agent messaging, local knowledge, the application gateway, the collaboration runtime, the merge queue, local supervision with paired external drafts, and read-only runtime views with isolated audit receipts. Each write-capable module stays gated by the service enable switch and has its own guide under `references/awo-*.md`; the read-only runtime views serve an authenticated snapshot whenever the V2 schema is present. Independent acceptance of these components is tracked separately and is not claimed by this entry.
- **Git worktree code loop (unreleased).** `scripts/validate_v2_worktree_code.py` exercises the opt-in V2 path from real Git worktree execution through independent review and the local Merge Queue to downstream materialization. It makes five real authenticated Codex turns on a disposable fixture, creates Task A and dependent Task B, lands Task A, and leaves Task B accepted but not landed. Monetary cost remains unknown; Windows with the Codex CLI is the tested scope. It is not a launcher for an arbitrary project. This work is uncommitted and not on `main` or GitHub. See [references/worktree-code-loop.md](references/worktree-code-loop.md).
- **Project entry for an existing repository (unreleased).** `scripts/project_entry.py` prepares a hash-sealed plan for an arbitrary local Git repository, then on explicit execution sends one bounded Codex worker turn into a managed worktree, enforces frozen allowed paths and acceptance argv, seals worker evidence, obtains independent code and integration reviews, lands through the local Merge Queue, and supports read-only status and recovery without resending an attempted Assignment. It reuses the existing ledger, scheduler, native bridges, workspaces, review sealing, and merge queue; no new state authority is added. Accepted stays distinct from landed, and preparation never launches a model or moves the target ref. Tested on Windows with Codex; monetary cost is unknown. See [references/project-entry.md](references/project-entry.md).

### Fixed

- **Background progress stays one incident.** A `working` or `settling` observation, or a fresh completion sequence without a valid delivery, no longer ends a monitor stall episode, so a worker that keeps going idle while an owned background process continues reports one alert instead of a repeating sequence. The episode now closes only on a valid task-bound delivery or a continuation journaled for that task, generation and episode; a stall after that continuation is a new episode with its own event and notification. Output text, process IDs, heartbeats and files that look like liveness never close an episode, and an expired, mismatched or forged liveness record cannot suppress a real stall. See [references/monitoring.md](references/monitoring.md) and [references/scheduling.md](references/scheduling.md).
- **Crash test effect publication (tests only).** The collaboration runtime crash test publishes its effect PID through a same-directory temporary file and atomic replace, with bounded cleanup of the exact owned process. A reader can no longer observe an empty final file, and the test proves this with an injected writer pause. No product behavior changes.

## 2026-11-06

### Changed

- **Dashboard: one place per agent.** **Check agents now** shows each agent's live status on its role card, next to its open tasks, instead of in a separate panel far down the page that repeated the role list. A working agent with no ledger task is labelled as busy outside the workflow, and an idle agent with an open task points to the task stage.

### Added

- **Remote command line from a phone** (optional, Windows; [references/remote-access.md](references/remote-access.md)). `contrib/windows-remote/setup-ssh-tailscale.ps1` sets up Windows OpenSSH reachable only through Tailscale: key login only, listening on the Tailscale address, a firewall rule limited to `100.64.0.0/10`, cmd or PowerShell as the default shell, and a startup task that waits for Tailscale. `hd.cmd`/`hd.ps1` list, read and type into Herdr panes over SSH, where Herdr's full-screen UI does not render well.
- **Tailscale identity check for the dashboard.** With `--public-origin` behind `tailscale serve`, `--tailscale-user LOGIN` admits only requests whose `Tailscale-User-Login` (set by Serve, which strips client copies) is a listed login. The token is still required; loopback requests are unaffected. Responses now carry `Referrer-Policy: no-referrer`. The monitoring guide has a phone-over-Tailscale section with privacy notes.

## 2026-11-05

### Added

- **Owner request inbox** (`scripts/owner_intake.py`, #19). An optional `owner_intake` section lets an authenticated external channel, such as a chat bot, drop HMAC-signed requests into a project inbox. `forward` gives an idle, identity-checked Herdr Planner a one-line request ID, never the text. `claim` returns the text as untrusted data, and dispatch still needs the Owner's direct approval. `list` is read-only; `forward --once` and `--interval` share one lock. Channel contract: [references/owner-intake.md](references/owner-intake.md).
- **Owner notification outbox** (`scripts/owner_notify.py`, #14). Optional push alerts for permission prompts, worker questions, failures, unconfirmed starts, long review waits and over-concurrency, through a file, webhook (generic, Feishu, DingTalk, WeCom) or command channel. It has its own outbox with at-least-once delivery and a bounded retry schedule. It reads the ledger and never changes task state.

### Changed

- **Independent review is enforced** (#13). The same agent cannot be both a task's worker and its reviewer, including in older ledgers. Ledger invariants are enforced.
- **Field rules** (#15). New task IDs and nonces accept only letters, digits and `._-`.
- **Native bridge residue** (#15). A bridge runner that dies before recording an outcome is reported as `exited` instead of leaving a task waiting forever. `native_worker.py release-lock` and `release` clear it explicitly.

### Documentation

- The README is now a short introduction and quick start; the manual walkthrough, concepts, configuration files, full safety model and repository layout moved to [references/manual-workflow.md](references/manual-workflow.md).
- README covers self-review refusal, field rules and bridge residue (#18). Intake docs clarify the `inbox_only` hint and unknown project names (#20).

## 2026-11-04

### Added

- **Follow-up and settle** (#11). `settle` records a review and releases or closes the attempt in one step. `follow-up` prepares the next task's request from the sealed review and current bindings. `confirm-start` and `observe-start` confirm that a worker really started, and fast polling is available for short tasks.
- **Worker result blocks** (#12). A worker can end with a fenced `awo-result` block (`done`, `partial` or `blocked`). The monitor and dashboard show it as the worker's own report, not as acceptance.
- **Guided continuation and history** (#12). `continue` can carry a short note and opens with the detected stall cause. A repair-chain limit stops endless repair loops. `workflow_control.py history` shows one task's journal.

## 2026-10-31

### Added

- **Dashboard management console** (#9, `--allow-manage`). Reconcile, start or stop monitoring, continue, close, release, tick, settings, pane labels and agent assignment. Each action is previewed, then confirmed once, and every check runs again at confirm time.
- **Workflow statistics** (#9). Stage durations and daily counts from the ledger journal.

### Changed

- **Stall diagnosis** (#10). The monitor explains why a worker stopped (provider error, quota, network, approval prompt) with guidance. It flags tasks whose start was never confirmed and reviews left waiting.

## 2026-10-29

### Added

- Dashboard: hide old finished tasks behind a range, show what needs attention first and fold handled history, readable task chains, and an optional graph view for follow-up chains (#3, #4, #8).

### Documentation

- Diagrams show stall handling; README picture refreshed (#5, #6, #7).

## 2026-10-26

### Added

- Stalled-worker recovery with one bounded continuation of the same task, and Herdr pane names kept in sync with roles (#2).
- Optional OMP one-shot jobs.

## 2026-10-22 to 2026-10-25

First public versions:

- Configurable multi-agent workflow Skill (`SKILL.md`), SQLite ledger, controller bindings, verified handoffs and durable review gates.
- Single-attempt dispatch, reviewed DAG scheduling, resource claims, receipts and an offline demo.
- Herdr, command and receipt adapters; native one-shot and session bridges for Codex and Pi.
- Local dashboard: status, guarded dispatch, review gate, result viewer, controller chat, English/Chinese UI and private LAN HTTPS.
- English entry points with maintained Chinese translations, and an encoding checker.
