# Harness-Independent Contract

English | [Chinese](harness-adapters.zh-CN.md)

## Record four separate identities

```text
role: planner | executor | paper-lead | another configured role
harness: codex | pi | omp | claude | dsh-tui | grok | another actual client
model: actual model, or unknown
transport: herdr | command | receipt
```

Grok and DeepSeek may be models inside Pi or another client rather than separate harnesses. Record the actual runtime. A nickname such as executor-grok establishes neither role permissions nor CLI capabilities. Any role may use any harness.

## Shared contract

Keep the task specification, frozen inputs, nonce, delivery, comparisons, and review requirements independent of provider formats. Adapters translate exit codes, stdout, and lifecycle events into the shared contract.

Choose an interface based on verified capabilities:

1. **Native Herdr:** discover pane, terminal, agent kind, and cwd; normalize get responses. Read bounded output only on completion or exception. Verify the actual agent kind rather than hardcoding codex.
2. **Official CLI/API adapter:** inspect current-version help and read-only identity queries. Supply status/identity commands that emit canonical JSON and a prompt/resume command targeting a verified session. Execute argument arrays rather than shell strings. The bundled command adapter does not guess provider commands.
3. **One-shot CLI job:** `run_job.py` starts a verified noninteractive command and emits task-bound status, the real exit code, stdout, and separate stderr. The receipt adapter reads them. See [OMP one-shot bridge](omp.md) for the optional OMP integration. A persistent interactive TUI is not a one-shot job: process existence alone proves neither working nor completion.

A TUI without official lifecycle events needs a verified plugin or controlled bridge. Until then, report ADAPTER_NOT_VERIFIED. Do not fill that gap with blind key injection, approval-dialog answers, or configuration changes.

## Native bridge crash residue

The optional `native_worker.py` command bridge records the runner PID in `active.json` and `run_job.py` records its own PID in the first receipt. If the runner exits before writing `outcome.json`, status reports `exited` with `exit_reason: runner_gone` and the job directory. An older active record without a runner PID remains `working` until inspected. Windows liveness checks use `OpenProcess` and `GetExitCodeProcess`, never `os.kill(pid, 0)`; PID reuse means only a confirmed negative result proves the recorded PID is gone.

A crashed sender can leave `send.lock`. Inspect the owner PID, then explicitly clear only a confirmed-dead lock:

```sh
python scripts/native_worker.py --worker /absolute/native-worker release-lock --reason "verified sender exited"
```

After inspecting the job, release the bridge slot only if an `outcome.json` exists or the runner PID is confirmed gone:

```sh
python scripts/native_worker.py --worker /absolute/native-worker release --reason "inspected failed job"
```

The second command archives the old active record as the job's `released.json`. Neither command stops a process or resends a task. **Releasing the bridge does not close the ledger task**: the controller must separately use the existing `reconcile --outcome terminated` evidence flow or record an independent review closure.

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

All named harnesses share role and delivery contracts. Live integration is not established for every client version. At first use, verify identity, working, new completion, artifacts/nonce, one-time notification, and failure classification. Reuse verified adapters; recheck after relevant version or identity changes.
