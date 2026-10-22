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

All named harnesses share role and delivery contracts. Offline tests cover arbitrary labels, cross-harness notification, and failure paths. They do not prove live integration with every client version. At first use, verify identity, working, new completion, artifacts/nonce, one-time notification, and failure classification. Reuse verified adapters; recheck after relevant version or identity changes.
