# Git worktree code loop: execution, review, merge, materialization

English | [Chinese](worktree-code-loop.zh-CN.md)

This is the opt-in end-to-end check for a code task on the V2 path. It runs a
disposable Git repository through real Git worktree execution by the installed
Codex CLI, independent code review, the local Merge Queue, and a downstream
materialization gate. It is a compatibility and protocol check on its own
fixture, not a launcher for your project.

## What it runs

The validator creates a fresh directory, a disposable Git repository, SQLite
state, fixture credentials, and two Tasks:

| Task | Work | Result at the end |
|---|---|---|
| A | In its own worktree, fix `add()` so it sums, and extend the test. | Accepted, then landed on a local integration ref. |
| B | In a second worktree at the base materialized from A, add `mul()` and extend the test. | Accepted, not landed. |

It makes five real one-shot Codex turns: worker A, reviewer A, integration
reviewer, worker B, and reviewer B. The controller work (freeze, dispatch,
commit, merge, release) is local Python and does not call a model.

The path exercised is:

1. Task A is claimed and bound to a real worktree, branch, base commit and
   declared paths.
2. The frozen execution contract sends the worker into that worktree with
   `--sandbox workspace-write`; the harness edits actual bytes.
3. The controller commits exactly the changed paths. An independent reviewer
   reads the code, task, patch and test evidence and records accept or reject.
4. Only an accepted, closed and frozen workspace is proposed to the local Merge
   Queue. The queue builds a candidate, runs the pinned tests, and requires an
   independent integration review.
5. Landing is a local compare-and-swap ref update. The destination ref must
   equal the reviewed candidate.
6. A materialization record binds Task B to the landed base. Task B then runs
   the same worktree, review and acceptance path in its own worktree.

## Needs

- Python 3.10+ and Git on `PATH`.
- An installed and already authenticated Codex CLI. The validator uses your
  CLI default provider and model and does not read or change credentials.
- A new output directory that does not exist.
- Network and model quota for five turns. Token usage is recorded; monetary
  cost is unknown, not zero.
- `HERDR_ENV` must not be `1`; the validator refuses to start with it set.

Permissions are your client's. Codex one-shot jobs run with
`--sandbox workspace-write`, and the validator never answers an approval
prompt. If your CLI configuration asks for one, the turn waits or fails
according to that configuration.

## Run

From the repository root, with a new directory for each run. With the Codex
launcher on `PATH`:

```powershell
Remove-Item Env:HERDR_ENV -ErrorAction SilentlyContinue
python -B scripts/validate_v2_worktree_code.py --directory runtime/repro/worktree-code-1
```

To use a specific install instead of the `PATH` search, pass `--executable`:

```powershell
python -B scripts/validate_v2_worktree_code.py --directory runtime/repro/worktree-code-2 --executable C:/path/to/codex.cmd
```

CLI flags:

| Flag | Meaning |
|---|---|
| `--directory` | Required. New validation directory; an existing path is refused. |
| `--executable` | Optional Codex launcher. Without it the validator searches `PATH` for `codex.exe`, `codex.cmd` or `codex`. |
| `--timeout` | Per-turn timeout budget in seconds, default 300. It is not an overall run cap. |

On POSIX shells, clear the variable with `env -u HERDR_ENV` and use forward
slashes.

## Result

A completed validation result prints a result with
`"status": "V2_WORKTREE_CODE_LOOP_PASS"` and exits 0. Any other completed
validation result exits 2, including a real reviewer rejection, and names the
failing stage. Startup failures are different: if initialization fails before
the validation root exists, the exception is re-raised, so the process exits
with the interpreter's nonzero code (normally 1), prints a traceback and leaves
no complete result directory. Argument errors, such as an existing
`--directory` or no launcher found, exit 2 with usage text and also create no
result directory. The run directory keeps `result.json`, `trace.json`,
`trace.jsonl`, `usage.json`, `provenance.json`, `manifest.json`, the source
snapshot, native job receipts, patches, test results and merge evidence.

A pass proves, for this fixture:

- Each Assignment was bound to a real Git worktree, branch and base commit, and
  the native process ran in that worktree rather than in the delivery directory.
- The change touched only the declared paths, the tests ran against those
  bytes, and an independent reviewer made the decision.
- The Merge Queue verified a candidate and landed it by a local ref update, and
  the landed commit corresponds to the reviewed candidate.
- A downstream Task could start only from the recorded materialized base.
- Accepted means reviewed and closed; landed means the merge queue moved the
  destination ref. Task A reached both. Task B is accepted but not landed.

A pass does not prove:

- An OS sandbox. A Git worktree isolates working files, not the whole
  filesystem; out-of-scope writes are detected and refused, not prevented at
  the OS level.
- A generic arbitrary-project launcher. The fixture controls the repository,
  tasks and paths.
- Other harnesses or operating systems. The tested scope is Windows with the
  installed Codex CLI; Pi, OMP, Claude, DSH, Ubuntu, remote providers and PRs
  are not certified.
- Provider or model identity (recorded as unknown by the CLI), cost, crash
  recovery, mid-call interruption, or persistent sessions.
- Acceptance of your own work.

For the components behind this route, see [assignment
workspaces](awo-workspaces.md) and the [merge queue](awo-merge.md). For the
smaller directory-artifact check, see [the first real
run](first-real-run.md).
