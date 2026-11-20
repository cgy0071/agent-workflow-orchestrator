# Project entry: prepare, run, review, land, recover

English | [Chinese](project-entry.zh-CN.md)

`scripts/project_entry.py` is the user entry for one bounded change in an
existing local Git repository. It freezes a reviewable plan, then drives the
existing AWO primitives: Task and Assignment, the priority scheduler,
managed Git worktrees, the native one-shot bridge, evidence sealing,
independent review, and the local Merge Queue. It adds no scheduler,
database, adapter, or agent.

## What it is not

- It does not send anything during `prepare`. No model runs, no target ref is
  created, and nothing implies Owner approval.
- It is not a live session or a steering channel. Each worker and reviewer
  turn is one bounded one-shot process.
- It is not a remote or cross-host path, and it does not certify harnesses
  other than the installed Codex CLI. Windows with Codex is the tested scope.
- It does not replace your repository's own review rules. The independent
  reviewers look at the frozen change, diff, and acceptance output; they do
  not certify unrelated parts of the project.

## Requirements

- Python 3.10 or newer; the module uses only the standard library.
- An existing local Git repository with full history, a clean working tree at
  the frozen commit, and an absolute path. The entry clones it; the source
  repository is never written.
- The installed and authenticated Codex CLI. The entry uses it for the worker
  and the two reviewer turns. It never installs or reconfigures a provider.
- An absolute native executable for the acceptance command, for example
  `node.exe` for a Node project.

## Operator spec

Save this as `spec.json` outside your source repository:

```json
{
  "version": 1,
  "repository": "E:/work/my-project",
  "goal": "Reject a missing value for --out with exit code 2 and a clear message.",
  "allowed_paths": ["lib/build.mjs", "tests/smoke.mjs"],
  "verify_argv": ["C:/Program Files/nodejs/node.exe", "tests/smoke.mjs"],
  "worker": {"harness": "codex", "executable": "C:/Users/you/AppData/Roaming/npm/codex.cmd", "timeout_seconds": 300},
  "reviewer": {"harness": "codex", "executable": "C:/Users/you/AppData/Roaming/npm/codex.cmd", "timeout_seconds": 300},
  "authorization": {
    "source": "Owner approved this bounded change in the task request.",
    "dispatch_worker": true,
    "allow_local_land": true
  },
  "budget": {"tokens.input": 40000, "tokens.output": 8000},
  "target_ref": "refs/heads/project-entry/my-change",
  "work_root": "E:/work/awo-runs/my-change",
  "change_name": "my-change",
  "max_real_turns": 12,
  "timeout_seconds": 300
}
```

Rules the entry enforces:

- `repository`, `verify_argv[0]`, both bridge executables, and `work_root`
  are absolute. `--out` must resolve to exactly the declared `work_root`;
  both are resolved before any write, must be new, and must be disjoint from
  the source repository. A symlinked or junction ancestor is refused, and a
  refused prepare leaves no partial output directory.
- The acceptance command is a frozen argv list, executed with `shell=False`.
  The first element must be an absolute native executable, not a shell. Any
  absolute file argument is hash-pinned at preparation. `run` and
  `recover --confirm-execute` recheck the pinned executable and input hashes
  at entry, before any model or command effect, and again immediately before
  the acceptance command executes, before merging, and before each recovery
  effect. A changed or missing pinned input is refused there, so the modified
  command never runs and no worker or reviewer turn is spent on it.
- `allowed_paths` is a maximum permitted set, not a required set. The worker
  may modify a subset of those files, add new files, delete them, or rename
  within the set; the result must contain no other path. Git paths are parsed
  from `--name-status -z`, so quoted, spaced, and non-ASCII names are handled.
  Entries this entry cannot review safely (a directory, a path below a file,
  a symlink, `.git`, or a parent escape) are refused during `prepare`.
- `target_ref` is a new `refs/heads/...` ref, not `main` or `master`, not the
  source branch, and not under `refs/heads/awo/` (the managed worktree
  namespace).
- `max_real_turns` is 1 to 12 and is counted from bridge receipts.

## Commands

```text
python -B scripts/project_entry.py prepare --spec spec.json --out RUN
python -B scripts/project_entry.py run     --plan RUN/plan.json --confirm-execute
python -B scripts/project_entry.py status  --plan RUN/plan.json
python -B scripts/project_entry.py recover --plan RUN/plan.json [--confirm-execute]
```

Every command prints one JSON object. Exit code `0` means success or a
completed read-only status; `2` means refused, blocked, failed verification or
review, or an identity mismatch.

`prepare` writes the byte copy of the spec, the hash-sealed `plan.json`,
`plan.sha256`, a human-readable `PLAN.md`, the disposable clone at the frozen
commit, an empty ledger under `state/`, and the bridge roots. Review
`PLAN.md` before executing.`run` requires `--confirm-execute`. It rechecks the source commit, clone,
tokens, and target, then enqueues, claims, provisions a worktree, dispatches
one worker turn, checks the changed paths and runs the frozen acceptance
argv, commits only the allowed paths, seals evidence, sends one independent
code reviewer turn, closes the Assignment as accepted, and when
`allow_local_land` is true builds and reviews a candidate in the Merge Queue
and lands it with a compare-and-swap ref update. Accepted means only that the
controller and an independent reviewer approved; landed means the target ref
tip equals the reviewed candidate commit. When `allow_local_land` is false the
run ends at `ACCEPTED_NOT_LANDED` and releases its workspace.

`status` is read-only. It reports the journal phase, ledger bindings, target
tip, and the next safe action.

`recover` diagnoses first. It never resends an attempted Assignment and never
repeats a reviewer send: each reviewer attempt is identified by a durable
record written before the send and reconciled against the bridge job and its
raw output. With `--confirm-execute` it revalidates the plan, source, tokens,
bindings, committed worker receipt, acceptance evidence and any stored merge
candidate before a single recovery effect, then resumes only controller-side
steps that are safe to repeat. A merge ticket that was reserved but never
started runs once on confirmation; a ticket that had started is recovered only
after its recorded executor is confirmed dead, with bound, reused quiescence
evidence. A completed landing is idempotent: repeated recovery preserves the
target ref, evidence and turn count. A completed accepted-without-landing run
is terminal too: recovery recognizes it from the ledger records, revalidates
the retained review and acceptance evidence without touching the released
worktree, releases a frozen workspace if the crash fell before release, and
reports `ACCEPTED_NOT_LANDED` again without running the acceptance command,
merging, or sending any turn. A merged run holds an execution lock, so
two `run`/`recover` processes cannot drive one plan. If an outcome is
uncertain, it reports the persisted state and asks you to inspect instead of
guessing.

Review authority comes from the ledger, never from `run.json`. A sealed
`reject` or `repair` verdict is terminal for that Assignment: no restart,
cache edit, or recovery promotes it, and a fresh prepared attempt is needed.
The acceptance note, reviewer identity, task/nonce, assignment generation and
reviewed commit must all match the sealed ledger record before landing.

The controller commits against a durable intent. Before committing it stages
the whole worktree, requires the staged path set to equal the verified change
set, and records the exact staged tree hash with the acceptance hashes. After
committing it requires the new commit's tree to equal that hash. If the
process dies between the commit and the journal write, recovery adopts a
commit only when its tree, parent, message, paths and retained acceptance
evidence all match the intent; the missing changed paths are reconstructed
from that validated commit. A rewritten or tampered commit is refused rather
than adopted, and recovery is idempotent.

## Authorization boundary

The entry bootstraps a fresh local ledger with machine identities through the
documented local route, then issues scoped tokens: controller, merge,
worker, reviewer, and merge-reviewer. The worker and reviewer credentials are
issued separately and the reviewer token is never handed to the worker.

This proves operator-authorized bounded execution. It does not fabricate a
human Owner credential. The human authorization is the frozen
`authorization.source` text, the act of preparing the plan, and the separate
`--confirm-execute` act. Where the surrounding installation requires an
authoritative human controller, that controller must authorize through the
existing configured authority before you execute the plan.

## Failure behavior

| Situation | Behavior |
|---|---|
| Source moved, wrong base, tampered plan | Refuse before any model turn |
| Changed or missing pinned verification input or executable | Refuse before dispatch, before execution, and before recovery effects; the command does not run |
| Missing token or changed reviewer identity | Refuse before the worker turn |
| Out-of-scope change, failed acceptance argv | Stop before commit, review, and landing; keep evidence |
| Crash after the commit, before the journal update | Adopt only the exact recorded staged tree and reconstructed paths; altered content is refused |
| Accepted without landing (`allow_local_land` false) | Terminal; recovery revalidates retained evidence and never executes, lands, or resends |
| Reviewer rejects or asks for repair | Stop and close with the sealed negative verdict; it stays terminal across restarts; no repair loop is claimed |
| Tampered or missing plan, receipt, review note, or cached verdict | Refuse; never manufacture acceptance from the mutable cache |
| Dispatch outcome uncertain | Stop and report; `recover` never resends |
| Interrupted merge intent | Diagnose; with confirmation validate evidence, require a dead executor, reuse bound quiescence evidence, use the existing Merge Queue recovery |

Dirty user files in the source repository are never stashed, reset, or
deleted. `prepare` records whether the source was dirty and clones only the
committed history at the frozen commit, so uncommitted work is not part of the
change. Commit the work you want reviewed before preparing.

## Limits

- The tested path is Windows with the installed Codex CLI, one-shot turns.
  Herdr, other harnesses, trusted background child liveness, cross-host
  operation, and production migration are later stages.
- Token counts come from the native `turn.completed` events. Monetary cost is
  reported as unknown; provider pricing is not applied.
- This entry composes existing components; their own documented limits still
  apply.
