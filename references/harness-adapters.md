# Harness-Independent Contract

English | [Chinese](harness-adapters.zh-CN.md)

## Record four separate identities

```text
role: planner | executor | paper-lead | another configured role
harness: codex | pi | claude | dsh-tui | grok | another actual client
model: actual model, or unknown
transport: herdr | command | receipt
```

Grok and DeepSeek may be models inside Pi or another client rather than separate harnesses. Record the actual runtime. A nickname such as executor-grok establishes neither role permissions nor CLI capabilities. Any role may use any harness.

## Shared contract

Keep the task specification, frozen inputs, nonce, delivery, comparisons, and review requirements independent of provider formats. Adapters translate exit codes, stdout, and lifecycle events into the shared contract.

Choose an interface based on verified capabilities:

1. **Native Herdr:** discover pane, terminal, agent kind, and cwd; normalize get responses. Read bounded output only on completion or exception. Verify the actual agent kind rather than hardcoding codex.
2. **Official CLI/API adapter:** inspect current-version help and read-only identity queries. Supply status/identity commands that emit canonical JSON and a prompt/resume command targeting a verified session. Execute argument arrays rather than shell strings. The bundled command adapter does not guess provider commands.
3. **One-shot CLI job:** `run_job.py` starts a verified noninteractive command and emits task-bound status, the real exit code, and stdout. The receipt adapter reads them. A persistent interactive TUI is not a one-shot job: process existence alone proves neither working nor completion.

A TUI without official lifecycle events needs a verified plugin or controlled bridge. Until then, report ADAPTER_NOT_VERIFIED. Do not fill that gap with blind key injection, approval-dialog answers, or configuration changes.

## Command adapter response

The Worker's status_argv returns:

```json
{
  "identity": {"kind": "pi", "terminal": "verified-terminal", "launch_cwd": "/workspace"},
  "status": "working",
  "completion_seq": 17
}
```

Canonical states: working, idle, done, blocked, exited, failed. `output_argv` returns bounded completion output only after a fresh completion and the artifact contract match. Reviewer `identity_argv` returns a JSON object containing identity. Its `prompt_argv` must have one standalone `{message}` argument. Verify the target before dispatch; do not use whichever window is focused.

Freeze paths, identity values, commands, and session provenance in the task record. Keep credentials out of these records. The adapter does not authorize provider, model, or authentication changes.

## Compatibility checks

Use [versioned capability profiles](capabilities.md) to distinguish spawn, attach/resume, send, status, cancel, collect and heartbeat. [The scheduler](scheduling.md) can submit a verified command/Herdr worker once; this adds no implicit client lifecycle capabilities.

Window selection is separate from the harness label. Follow [setup-and-switching.md](setup-and-switching.md) for live binding and handoffs. Only copy conversation_id/branch_id if the client actually exposes them. A terminal ID is not a conversation-branch identity; use inbox_only when metadata is unavailable, or explicitly reserve a dedicated main window. Never synthesize these fields.

All named harnesses share role and delivery contracts. Offline tests cover arbitrary labels, cross-harness notification, and failure paths. They do not prove live integration with every client version. At first use, verify identity, working, new completion, artifacts/nonce, one-time notification, and failure classification. Reuse verified adapters; recheck after relevant version or identity changes.

## Versioned field observations

These are external evaluator reports, provided as issue-0 through issue-3 and reply-0 through reply-3, not a supported-version guarantee or an independent maintainer reproduction. The original report names skill commit 84371b3, Windows build 10.0.26200, Herdr 0.9.3 (protocol 22), and Python 3.12.4. The replies report a successful full loop against 6556c34 on the same machine. Harness client versions were not supplied; original logs and response files are not bundled here.

| Herdr | Harness | agent_session | agent prompt | agent start | Sequence field |
|---|---|---|---|---|---|
| 0.9.3 | pi; client version unreported | Reported present | Reported delivered; review gate passed | agent_pane_busy reported in idle cmd/PowerShell shells; pane run used instead | Pending raw-response confirmation |
| 0.9.3 | dsh-tui; client version unreported | Reported absent | Reported agent_not_ready; use inbox_only in this environment | Not tested in the supplied report | Pending raw-response confirmation |

The original report identifies state_change_seq as the raw Herdr field, while the follow-up quotes a receipt with sequence_source=completion_seq. Without the corresponding raw agent get response and adapter configuration, the source cannot be reconciled; a normalized completion_seq value alone does not identify the raw field. Keep the sequence column pending until those artifacts are available. There are no live findings here for Codex, Claude, Grok, or other version pairs.

### Subsequent DAG feedback

The evaluator's next report used a 2026-10-22 main archive without a commit SHA,
on Windows 10.0.26200 / Herdr 0.9.3 / Python 3.12.4. The Pi client version was
not recorded; its model label is not a client version. The evaluator reports a
turn ending in persistent `done`, with later prompts still accepted and the
sequence advancing. The idle-only scheduler check is independently reproducible
in source and offline fixtures. Requests can now explicitly map that verified
between-turn label with `"status_map": {"done": "idle"}`; see [scheduling.md](scheduling.md).
Raw busy/failed states, identity checks, completion baselines and ledger claims
remain enforced. Local subprocess and mocked Herdr-response tests cover this
path; they do not independently establish live Pi compatibility.

The report also identified the public dag_view signature inconsistency and
missing markers in the DAG template. Both have regression coverage. Its claim
that the previous demo reset workers between passes does not match the source:
the binding and sequence persist, while the old fixture always reported idle.
Its archive line counts and encoding-check count do not identify the tested
commit. Retain the raw archive/version evidence before attributing results to a
specific revision or upgrading any capability profile to verified.

### Closing report for a000816

The evaluator's reply-4 identifies a000816 and closes the reported issues. It
reports two dependent DAG nodes accepted on the same Pi instance, with persistent
done status and sequences 1574 then 1576, using the explicit status mapping.
During a separate provider timeout, the worker reportedly remained working for
about eight minutes and the scheduler refused further dispatch. The report also
lists 89 passing tests, 40 clean encoding checks and OFFLINE_DEMO_PASS.

Its first DAG attempt reused delivery filenames in one cwd; later delivery
overwrote earlier accepted bytes and the evidence hashes correctly stopped
counting as valid. Per-task delivery directories resolved that report. See the
[evidence-retention guidance](scheduling.md#isolation-and-resource-quotas) for
serial workers sharing an execution directory.

These are evaluator observations, not a new maintainer live reproduction. The
Pi client version and complete raw run artifacts remain unreported; capability
profiles are unchanged. One historical statement in the closing summary is
incorrect: no sys.path insertion was added. The earlier cwd claim was not
reproduced; test_cli_runs_from_unrelated_directory verifies normal script launch.

For future entries, retain the OS, Herdr and harness versions, skill commit, adapter configuration, raw identity/status responses, actual launch command, and delivery/review evidence. Exclude credentials. Use this table as a troubleshooting starting point, then recheck the installed version and selected session. Herdr panes and commands apply only to the Herdr adapter.
