# Assignment workspace core

English | [Chinese](awo-workspaces.zh-CN.md)

`scripts/workspace_manager.py` implements P0-4 Git worktree and noncode directory
workspaces behind the application service. It uses the existing SQLite database
and existing `v2_assignment`; it does not create Tasks, dispatch workers, accept
evidence, or introduce a second controller. The application service owns principal,
project, controller epoch, freeze, revision, command replay and event authorization.
Service/CLI integration is provided by `workspace_runtime.py` behind the
authenticated controller boundary; scheduler dependency and materialization
proof remains the scheduler's own gate. Activation
and independent acceptance follow P0-3, then P0-4.

## Transaction and external-operation contract

All `con` write functions require an already open caller transaction. Use
`BEGIN IMMEDIATE`, commit only after the complete service command succeeds, and
roll back on exceptions. These functions do not commit or migrate implicitly.

1. In the service migration transaction, call `create_workspace_schema(con)`.
   This adds `v2_workspace`, `v2_workspace_journal`, and `v2_workspace_meta` with
   local schema version 1. An immutable-column trigger protects the reservation.
2. Outside any transaction, call `prepare_workspace(...)`. It performs read-only
   filesystem and bounded Git preflight and returns a normalized specification.
3. In the service reservation transaction, call `reserve_workspace(con, spec)`
   and `begin_provision(con, workspace_id)`. Save the returned operation token,
   then commit. Reservation binds `v2_assignment.workspace_id` and advances that
   Assignment's revision. It never changes Assignment state or `holds_slot`.
4. Call `provision_workspace(database_path, workspace_id, operation_token)`.
   A short, separate SQLite transaction consumes the committed ticket once, before
   external execution. Uncommitted or consumed tickets cannot create a workspace.
5. In a new service transaction, call `finalize_workspace(con, workspace_id, report)`
   with the actual returned provisioning report. It rechecks the ownership and
   materialization fingerprint without a Git subprocess. Only inspected clean
   creation at the frozen base becomes `ready`; other outcomes are quarantined.

The external operation's small ticket-consumption transaction is not another
authorization boundary. Call it only after the service has authorized the command.
Internal dictionaries and controller attestations are trusted service inputs, not
untrusted worker JSON. Unknown external outcomes must be reconciled; never replay
the external call just because a command response was lost.

```python
# Within an authorized application-service handler; paths/identities are validated
# controller inputs. The root and immediate destination parent already exist.
spec = prepare_workspace(
    task_id=task_id, assignment_id=assignment_id, host=local_host,
    kind="git_worktree", root=workspace_root, path=workspace_path,
    repository_id=repository_key, repo_root=source_checkout,
    base_commit=full_commit_oid, allowed_paths=["scripts/**", "references/**"],
    artifact_paths=[delivery_directory], harness=harness_version,
    resource_claims=resource_claim_ids,
)
con.execute("BEGIN IMMEDIATE")
try:
    reservation = reserve_workspace(con, spec)
    ticket = begin_provision(con, reservation["workspace_id"])
    con.commit()
except Exception:
    con.rollback()
    raise

report = provision_workspace(database_path, ticket["workspace_id"], ticket["operation_token"])
con.execute("BEGIN IMMEDIATE")
try:
    workspace = finalize_workspace(con, ticket["workspace_id"], report)
    con.commit()
except Exception:
    con.rollback()
    raise
```

`prepare_workspace` keyword arguments are `task_id`, `assignment_id`, `host`,
`kind`, `root`, `path`, `allowed_paths`; optional arguments are `repository_id`,
`repo_root`, `base_commit`, `branch`, `artifact_paths=()`, `harness=""`, and
`resource_claims=()`. Git requires a configured repository ID and full 40/64-hex
commit OID. Default branch: `awo/<task>/<assignment>`. Directory mode rejects Git
identity fields. Workspace ID and ownership token are generated once in preflight.

## Scheduler gate and claims

`check_workspace_ready(con, workspace_id, *, assignment_id=None, host=None, allow_active=False)` is
read-only and is intended to run inside the scheduler's final reservation
transaction. It returns `{ready, reasons, workspace_id}` and, when ready, also
`path`, `assignment_id`, `host`, `revision`, and `base_commit`.

By default the gate requires the ready state. It checks current Assignment binding/state, physical claims,
canonical path and ancestor identity, ownership marker, complete bounded file-tree
fingerprint, and Git HEAD/index/ref/config/backlinks. A deleted tree, late writer,
switched branch, changed ref or replaced marker closes the gate. There is no Git
subprocess, commit, migration or mutation in this check. Full content hashing can
lengthen the scheduler transaction; the current implementation favors correctness
and explicit limits over cached readiness.

Call `mark_workspace_active(con, workspace_id)` after the check and **before**
changing Assignment.state from `reserved`/`blocked`. It repeats readiness, sets
`active`, clears prior quiescence, and journals writer uncertainty. Perform this
in the same transaction as scheduler slot, budget, resource and outbox reservation.
Rollback restores readiness. The scheduler must retain its own dependency-evidence
and accepted-but-unlanded materialization checks; a workspace fingerprint does not
prove those dependencies were applied. This core does not implement a merge queue.

For the second bounded-send transaction, the parent must first verify its persisted
`send_state="attempted"` intent, then call the gate with `allow_active=True`.
Only this explicit Boolean opt-in additionally permits workspace status `active`;
the bound Assignment must still be `reserved` or `blocked`. Every other workspace
or Assignment state is rejected. Non-Boolean flag values fail closed. The same
ownership, identity, host, claim and full fingerprint checks run in either allowed
workspace state, without changing status, revision, claims, intent or quiescence.
The core does not inspect or authorize send intents; the parent owns that guard
and must preserve attempted intent on process death. This flag grants no retry,
workspace reuse or writer-quiescence permission. `mark_workspace_active` always
uses the default gate and rejects an already active workspace.

`workspace_claims(con, host=None)` returns host-qualified physical claims with
workspace/Assignment/Task IDs, status, `workspaces` (launch path and declared
artifact paths), and resource association IDs. Every state except `released`
retains its claims. The caller must also check its existing scheduler/resource
claims in the same transaction. The core checks legacy active dispatch/task paths
and retained evidence/review paths. No expiry or disconnected-worker heuristic
releases a claim. Different worktrees may edit identical relative files: those are
later merge risks, not physical-directory conflicts.

## Ownership and recovery

One immutable workspace belongs to one Assignment. Task, Assignment, host,
repository identity, checkout identity, base, branch, root/path, allowed paths,
artifacts, resource associations and token are frozen in the specification.
Retries requiring a new workspace require a new Assignment. A released row remains
history; the core does not transfer or recycle ownership.

The destination must be absent and strictly inside an existing root, outside the
source checkout and Git metadata. The provisioner exclusively creates the directory
before Git and verifies its filesystem identity afterward. Git creation uses argv,
`worktree add --no-track -b`, and the frozen commit. No shell-built commands, force,
prune, network operation or automatic retry is used. Hooks are disabled, global Git
configuration is excluded, and local checkout-filter configuration is rejected.

For Git worktrees, `git rev-parse --git-path awo-workspace.json` must resolve into
the registered worktree-private Git directory. The marker includes the immutable
specification hash, ownership token and directory identity. `.git` is inspected as
a pointer file, with private/common-directory and backlink checks. Directory mode
uses `.awo-workspace.json`. Preexisting, changed or missing markers never authorize
adoption. A crash between Git creation and marker publication leaves quarantine.

`inspect_workspace(workspace)` is read-only and returns an external inspection
report. `recover_workspace(con, workspace_id, report, quiescence=None)` applies a
fresh report in the caller transaction. Missing paths become `workspace_lost`;
unowned resources remain quarantined. Owned paths without quiescence stay active
or become orphaned. The service must translate a lost workspace into its own blocked
Assignment/dispatch projection; this core does not rewrite those lifecycle states.

To recover readiness or reserve cleanup, the service must supply an attestation:

```python
quiescence = {
    "workspace_id": ws["workspace_id"], "assignment_id": ws["assignment_id"],
    "host": ws["host"], "revision": ws["revision"], "verified": True,
    "controller": controller_id, "reason": inspected_process_or_host_fencing_reason,
    "evidence_path": preserved_observation_path, "evidence_sha256": observation_sha256,
}
```

The evidence must be outside the workspace and match its SHA-256. The revision
binding prevents stale reuse. The service authenticates the controller and verifies
the actual process/host fencing evidence. A Boolean, PID disappearance, matching
branch/base or matching marker is insufficient. Recovery only restores ready for a
reservable Assignment with clean, known contents at the original base; other owned
quiescent work is frozen. A generic inspection cannot substitute for a successful
creation report in `finalize_workspace`.

## Inventory, scope and retained evidence

`collect_inventory(workspace)` reads all filesystem entries, including Git-ignored
and untracked files, plus base/index modes/object IDs, tracked modifications,
deletions, staged/unstaged status, detected renames and committed changes from the
frozen base. Both rename endpoints count for scope. Current regular files have
SHA-256, byte count, mode, binary indication and inferred MIME. Deleted paths retain
their base/index object IDs; they are not reported as empty content hashes.

Globs are relative and segment-aware: `*` matches one segment and `**` crosses
directories. Scope review is blocked by undeclared changed paths or unknown content.
Links/junctions are inventoried without following their content and block sealing;
escapes are explicit. Hardlinks, special files, Git-encoded symlinks, submodules
without a separate inventory and unmerged indexes block sealing. This conservative
policy avoids presenting incomplete artifacts as complete evidence.

Limits: 10,000 filesystem entries, depth 128, 64 MiB per regular file, 256 MiB
total content per scan, 32 MiB per subprocess output stream, and a 30-second default
command timeout. Large files receive explicit unknown policy entries; incomplete
scans fail closed. No silent hash omission or truncated patch is a valid snapshot.

`snapshot_workspace(workspace, evidence_root)` writes current file bytes (including
untracked/ignored/binary artifacts), four full-index binary patches (base-to-tree,
staged, unstaged, committed), and a content-addressed manifest. The evidence root
must be outside the repository and workspace. Files are fsynced and published with
atomic hard links; directories are fsynced on POSIX. Windows directory-fsync
durability is not claimed. The store filesystem must support hard links. A second
inventory detects changes during archival; partial CAS blobs may remain, but no
valid final manifest is returned on failure.

`verify_snapshot(snapshot)` verifies the manifest and every content/patch hash
without touching the workspace or database. Snapshots remain readable after a clean
workspace removal. Snapshot creation does not mean reviewer acceptance; even a
retained snapshot with `scope_ok=False` must pass the independent scope/review gate.

## Explicit release and noncode work

Use `begin_release(con, workspace_id, inspection, snapshot, quiescence=...)`, commit,
then `release_workspace(database_path, workspace_id, operation_token)`, then
`finalize_release(con, workspace_id, report)` in a new caller transaction. Release
rechecks snapshot, ownership, content and quiescence evidence before the external
operation. Dirty, ignored/untracked, foreign or changed work is retained. Git removal
never uses force; the branch is retained. Noncode directories can only be removed
when empty except for their owned marker. Failed/ambiguous removal is quarantined
with claims retained. CAS evidence is never deleted by workspace cleanup.

For research/noncode work, use `kind="directory"`, an explicit output glob such as
`["outputs/**"]`, and no Git fields. Preserve artifact snapshots for review and
service-controlled Task finalization. No commit, MergeRequest or `landed` transition
is required. `resource_claims` associates existing claims; it does not reserve GPU
capacity. Dataset read-only mounts, GPU process isolation and permissions are not
implemented by directory mode.

Worktrees and allowed paths are review boundaries, not an OS security sandbox.
Claims are scheduling records, not filesystem/process locks or GPU isolation.
Use one local-host SQLite ledger; shared cross-host SQLite and UNC/device/alternate
stream roots are unsupported. Canonical Windows case/short paths and symlink/junction
ancestors are checked. Same-user hostile concurrent filesystem replacement still
requires OS permission isolation/fencing; these checks do not create that boundary.

## Application CLI

`scripts/awo_service.py` wraps the core behind the authenticated controller
boundary. The core functions above still own their transaction and
external-operation contracts; the wrappers add controller binding, epoch,
revision and command replay.

```powershell
python -B scripts/awo_service.py --state STATE workspace-create --actor ACTOR --version VERSION --envelope CREATE.json
python -B scripts/awo_service.py --state STATE workspace-recover --actor ACTOR --version VERSION --envelope RECOVER.json
python -B scripts/awo_service.py --state STATE workspace-release --actor ACTOR --version VERSION --envelope RELEASE.json
python -B scripts/awo_service.py --state STATE workspace-freeze --actor ACTOR --version VERSION --envelope FREEZE.json
python -B scripts/awo_service.py --state STATE workspace-inspect --actor ACTOR --version VERSION --workspace WORKSPACE_ID
```

Every envelope is a command envelope with `command_id`, `tool`
(`workspace.create`, `workspace.recover`, `workspace.release` or
`workspace.freeze`), `expected_epoch` and `payload`. Repeating a command returns
the recorded result. Create accepts only `assignment_id`, `expected_revision`,
`kind`, `root`, `path`, `allowed_paths` and the optional Git fields
`repository_id`, `repo_root`, `base_commit` and `branch`. Recover takes
`workspace_id` and `expected_revision`; release and freeze also take
`evidence_root`. A Git task frozen by the scheduler must use the exact frozen
launch directory and materialized base commit; a missing source workspace blocks
the send instead of falling back to the delivery directory. For a Git worktree
Task the frozen message must also carry an absolute `execution_cwd` equal to that
workspace path and `sandbox` set to `workspace-write`; the delivery `cwd` must
stay outside the source workspace, and the native receipt records both
directories. `workspace-inspect` is read-only and never prints operation or
ownership tokens.

## Validation

Run `python -B -m unittest discover -s scripts -p test_workspace_manager.py -v`.
Set `AWO_WORKSPACE_TEST_ROOT` to an external evidence directory to contain disposable
Git/SQLite/process fixtures. Fixture commits contain synthetic content only.
The suite exercises worktrees, full inventories, scope, CAS, claims, rollback,
two-process races, real process death, dirty/foreign retention and Windows junctions.
Symlink creation is skipped explicitly when the OS denies the privilege. These
local tests do not establish live Ubuntu, GPU or Harness/provider compatibility.
