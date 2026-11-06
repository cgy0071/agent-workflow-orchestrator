# Changelog

English | [Chinese](CHANGELOG.zh-CN.md)

The project has no numbered releases yet. Entries are grouped by the date changes reached `main`; pull request numbers link to the details. Every change keeps the safety model: each task is sent once, approval prompts are never answered automatically, done means an independent recorded review, and configuration is not permission.

## 2026-11-06

### Added

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
