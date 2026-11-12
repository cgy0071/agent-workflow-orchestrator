# Merge metadata, queue, and accepted bases

English | [Chinese](awo-merge.zh-CN.md)

This is the stdlib-only domain adapter in scripts/merge_queue.py. SQLite remains
the single Task/Assignment authority. Code acceptance, Git landing, and noncode
artifact finalization are separate outcomes. The parent service must register
the routes and schema explicitly; importing this module performs no migration.

## Integration contract

Public functions:

| Function | Contract |
| --- | --- |
| create_merge_schema(con) | Add metadata tables inside the caller's transaction. No commit or shared schema version bump. |
| apply_command(con, project_id, principal, tool, payload) | Validate and write metadata in the caller's transaction. No subprocesses. |
| execute_intent(database_path, intent_id, operation_token) | Consume one committed ticket, perform external work, persist a receipt in the same database. |
| recover_intent(database_path, intent_id, operation_token) | Inspect a started operation after controller-recorded quiescence. Never rerun its effects. |
| read_queue(con, project_id, principal, filters=None) | Pure request/candidate projection; filters: task_id, merge_request_id, state, limit (1..100). |
| read_intents(con, project_id, principal, task_id) | Pure recovery projection including stack/archive intents, revisions and executor PID; no bearer tokens. |
| materialization_gate(con, project_id, task_id, base_commit) | Pure proof lookup for the downstream Task and exact base; rehash all accepted parent evidence/reviews. |

The service derives principal as a plain object containing agent_id, role,
principal_id, and project_id. Never accept it from a worker payload. The domain
checks active registry membership, project, role, Task/Assignment ownership,
generation and revisions. Controller-only writes are the default; merge.review
also permits a reviewer. Every reviewer must be independent of every Assignment
of the Task, including competing slots. Workers can read only their requests;
reviewers, observers and controllers can inspect the project.

A controller Agent may use a credential narrowed to reviewer without changing
agent_id. Such a principal must include binding_id and integer binding_version.
The domain checks the service's binding-to-Agent mapping and current review
permission. Integration review_json preserves binding_id, binding_version and
binding_sha256; landing rechecks the active binding and all competing workers.
Missing, stale, wrong-Agent or worker-only bindings fail closed.

The parent owns authentication, command IDs/replay, controller epoch admission,
freeze policy, and outer events. Domain events call awo_service._event lazily.
Commands require an open transaction; the caller must use BEGIN IMMEDIATE and
roll back on any error. The domain never commits it. SQLite cannot expose the
transaction's BEGIN mode through the Python connection, so IMMEDIATE is a caller
contract. Executor transactions cover only ticket consumption and report storage;
Git, verification and provider processes run with no SQLite write lock held.

Ticket creation snapshots the enabled, unfrozen service's controller epoch.
Execution rechecks it before doing work and before landing. A new controller can
finalize an already recorded effect after rehashing its bindings. No Task state is
changed by the executor, even if Git CAS has already succeeded.

## Commands and payloads

All payloads reject unknown fields.

| Tool | Payload |
| --- | --- |
| merge.policy | repository_id, repo_root, operation_root, archive_root, verify_argv, pins; optional policy_id, timeout, max_attempts, provider_argv, receipt_key_path |
| merge.propose | task_id, assignment_id, generation, workspace_id, policy_id, target_ref, expected_revision (Task); optional parents, pr_id |
| merge.enqueue | merge_request_id, expected_revision (request) |
| merge.begin | merge_request_id, expected_revision (request), optional kind: build (default), land, poll |
| merge.review | merge_request_id, expected_revision (request), note_path, note_sha256 |
| merge.finish | intent_id, expected_revision (intent, from executor result or read_intents) |
| merge.recover | intent_id, expected_revision (intent), evidence_path, evidence_sha256 |
| merge.cancel | merge_request_id, expected_revision (request) |
| merge.materialize | downstream task_id, expected_revision (Task), policy_id, target_ref, parents |
| artifact.begin | task_id, assignment_id, generation, policy_id, expected_revision (Task) |

Parents are an ordered list of objects with task_id, assignment_id and generation.
There can be at most eight; self/duplicate parents are rejected.
All Git object IDs are full hashes; target_ref is a safe refs/heads/... name.
Source and target must differ. The repository ID is a configured stable identity,
not a filesystem-path hash.

Policies are immutable controller-approved local configuration. All three roots
must exist; operation/archive roots must be outside the repository and disjoint.
verify_argv is an argv list, never a worker-supplied shell string. The executable
and absolute input files must be pinned in pins (absolute path to SHA-256).
Git's installed executable is pinned automatically. Installed executable hardlinks
are permitted with stable-content checks; worker evidence hardlinks are rejected.
Use resolved executable paths. Commands run with shell=False, a 1..300 second
timeout (default 30), and bounded output. max_attempts is 1..10 (default 3).
Do not approve arbitrary worker-provided scripts or interpreters on their behalf.

## Original acceptance contract

The original Task must be accepted; the exact Assignment must be completed,
accepted, slot-released, and not superseded in its slot. Its owned workspace must
be frozen with controller-verified, hashed quiescence evidence. Ownership markers
are checked again externally. Per-assignment accepted evidence and a sealed
independent accept review must exist in the parent's v2 tables.

### Native scheduler acceptance

An Assignment with v2_scheduler_intent uses the existing native acceptance
authority. accepted_evidence_id may name RESULT.md or HANDOFF.md. The original
REVIEW.md stays Markdown; no new JSON review or worker manifest is required.
The domain calls priority_scheduler._verified_delivery with the original
legacy_task_id and review event, checks the closed accepted intent, and rehashes
the exact per-Assignment delivery set. It resolves the review actor's instance_id
through service._agent_id_for_binding, validates its active binding/version, and
checks identity independence across every competing Assignment. The original
review path, bytes, hash and actor are retained, not replaced by an import review.

The parent must freeze the accepted workspace using workspace.freeze. A frozen
label alone is insufficient: its hashed proof must contain the original accepted
closure and an observed idle worker with matching identity/completion sequence.
The v2_workspace_review inventory must be independently accepted, hash-valid,
in scope and still match the owned workspace. Inside caller transactions,
validation uses workspace._fingerprint (content, ownership and Git metadata;
no subprocess). Outside a transaction the executor also calls the canonical
workspace_runtime.verify_reviewed_workspace and compares the full inventory.
This is repeated before external landing and during receipt finalization.

The internal derived manifest has these exact keys:

| Kind | Keys |
| --- | --- |
| Both | task_id, assignment_id, generation, workspace_id, inventory_sha256, review_event_id, review_path, review_sha256 |
| Git | kind="git", repository_id, source_ref, source_commit, base_commit |
| Artifact | kind="artifact", files (each entry: path, sha256, bytes) |

Git source_commit is the sealed inventory head; source_ref/base/repository come
from the owned workspace. Code must be committed and clean. Artifact files are
the sealed inventory's regular, known outputs/ files; an empty set is rejected.
The derived manifest is persisted inside the intent/request seal, not written
into the worker workspace. A native validation failure never falls back to an
import. Parent scheduler materialization wiring remains separate integration work.

### Approved imports

For service-approved accepted Assignments without a native scheduler intent,
the accepted Git evidence file is JSON:

~~~json
{
  "kind": "git",
  "task_id": "T",
  "assignment_id": "A",
  "generation": 1,
  "repository_id": "configured/repo",
  "source_ref": "refs/heads/awo/T/A",
  "source_commit": "FULL_COMMIT_ID",
  "base_commit": "FULL_BASE_ID"
}
~~~

The sealed original review JSON binds task_id, assignment_id, generation,
evidence_id, evidence_sha256, reviewer_agent_id and verdict ("accept").
The v2_review actor_json must contain the service-resolved reviewer agent_id.
Evidence byte count, hashes, review contents and workspace binding are rechecked
when proposing, reserving, executing and finalizing success.

## Local queue flow

1. Propose an accepted source, then enqueue. Task remains accepted.
2. Reserve a build intent with merge.begin. Commit the caller transaction before
   passing intent_id and the returned operation_token to execute_intent.
3. The executor freezes the current target tip, creates a new detached operation
   worktree, composes accepted parents using merge-tree preflight and real merges,
   rebases source commits (preserving merges), and runs the approved verifier.
   A conflict aborts any in-progress rebase/merge; original workspaces are retained.
4. The executor persists a report. merge.finish records the exact candidate,
   verifier log/hash and materialization proof. Task remains accepted.
5. An independent reviewer seals the candidate returned by read_queue. The note
   binds candidate_sha256, task_id, assignment_id, generation, reviewer_agent_id
   and verdict ("accept"). candidate_sha256 uses canonical JSON: sorted keys,
   separators comma/colon, ensure_ascii=True, allow_nan=False, then SHA-256.
6. Reserve kind=land, commit, execute, then finish. The executor rehashes both
   reviews, verifies source and candidate cleanliness, and executes the real
   expected-old update-ref CAS. Only finish changes the Task to landed.

A build report has outcome=verified and a candidate. Executor results include
intent_id and intent_revision. A begin result includes intent_id, operation_token,
kind, revision (intent), merge_request_id, state and merge_revision. A busy begin
returns state=waiting_blockers and merge_revision, with no intent or token; retry
admission after the blocker completes. Queue projections never return tokens.

One unresolved operation holds the target reservation. FIFO orders waiting builds
by creation time and ID. CAS mismatch requeues with a consumed failure attempt;
exhaustion requires repair. Conflicts, failed verification or changed review
evidence fail the request and set Task repair_required. Dirty source/repository,
checked-out target or environment errors block without consuming an attempt.
Blocked requests can be enqueued again. Cancellation is metadata-only and cannot
cancel an active operation or landed request. No worktree is force-reset/deleted.

The implementation refuses any observed checked-out target, even clean, custom
checkout filters/merge drivers, shallow history, and source symlinks/submodules.
The candidate must remain clean after verification. Keep verifier outputs in
captured stdout/stderr, not unreviewed files in the candidate.

## Stack bases before dispatch

merge.materialize accepts a draft/ready/queued/blocked downstream Task without a
live Assignment slot. It reserves a standalone stack intent. The executor builds
and verifies a base containing the specified accepted parents, with no target-ref
update. Finish stores the proof and leaves Task/Assignment state unchanged.
Its result has outcome=materialized and candidate.stack_base.

Before creating the downstream workspace, the parent scheduler calls
materialization_gate for that Task and exact stack_base. A successful result
contains materialized_from, base_commit, materialization_id and base_artifact_sha.
The parent must compare materialized_from against the full required code dependency
set and provision the workspace at that exact commit. Missing proof or changed
parent evidence returns ready=false, reason=unmaterialized_dependency. This module
does not dispatch workers or silently bypass the scheduler's existing gate.

## Recovery and replay boundary

Intent states are ready -> started -> reported -> finished. Duplicate execution
of reported/finished tickets returns the stored receipt without external work.
An uncommitted or wrong-token ticket cannot execute. Started without a report is
uncertain: the target lock is retained, even on timeout, output overflow, or PID
disappearance. There is no lease-expiry retry.

The controller must establish executor/descendant quiescence externally and seal
a JSON observation containing intent_id, executor_pid, quiescent=true, and reason.
merge.recover records its path/hash. recover_intent then inspects:

- Target equals reviewed candidate: record recovered CAS success, rehash bindings.
- Target equals expected old tip: record no-effect/rebase-required; no replay.
- Any third tip: retain uncertainty and lock for manual reconciliation.
- Interrupted build/poll: retain operation directory and report a known stopped
  environment block, allowing a fresh intent after finalization.
- Complete archive manifest: validate all archived blobs and recover its receipt.
  A missing/partial archive is retained and requires explicit reconciliation.

Recovery never accepts an arbitrary caller-supplied success report. Finish reads
the internally persisted report/hash. Its effects roll back with the caller's
transaction; an already executed Git CAS is not rolled back or executed twice.

## Noncode artifacts

An accepted directory-workspace manifest has kind=artifact, the same identity
fields, and files: an ordered list of path, sha256 and bytes. Each unique path
must be below outputs/. The executor rehashes source bytes and copies them into
archive_root/EVIDENCE_SHA256/FILE_SHA256, then publishes a manifest. The workspace
inventory must satisfy its allowed scope. File/inventory bounds come from the
workspace adapter (64 MiB/file, 256 MiB/inventory); this is not a bulk-data store.

artifact.begin -> execute_intent -> merge.finish changes accepted to finalized
only after the archive manifest and every blob revalidate. It creates no
MergeRequest, changes no Git ref and never writes landed.

## PR status adapter: local protocol, external compatibility unverified

P2-6 includes the local multi-parent engine and an approved argv status adapter.
It implements no remote write or implicit fetch. With provider_argv and a
receipt_key_path (at least 32 bytes), propose may bind pr_id. After candidate
review, use kind=poll. The adapter receives its approved argv followed by
--request ABSOLUTE_JSON_PATH --receipt ABSOLUTE_JSON_PATH.

Request fields: format=1, intent_id, project_id, repository_id, pr_id, target_ref,
candidate_commit, expected_old_tip, candidate_sha256. The receipt is an object
with body and hmac_sha256. Body must repeat every request field and add state
(pending/merged/closed), ci, and merge_commit for merged status. The signature
is HMAC-SHA256 over canonical ASCII JSON using the approved local key. A receipt
is specific to one operation, PR, candidate and base; it cannot be replayed for
another ticket. The key belongs outside worker workspaces with controller-managed
permissions. Local HMAC authenticates the approved adapter, not the remote host.

Pending PR/CI becomes waiting_remote and releases the target lock when finished.
Merged requires ci=passed plus locally available Git objects whose tree matches
the reviewed candidate and whose ancestry contains the expected old tip. Only
then may finish record landed via authenticated_pr_receipt. The adapter is the
trust boundary for the remote branch assertion; deployment must supply and review
a real read-only provider implementation and authorization outside this module.

Tests explicitly named simulated_provider use local Python programs and no
network. They validate signatures, bindings and queue behavior, not GitHub,
GitLab or other live provider compatibility. No live remote compatibility or
production PR landing is claimed.

## Application CLI

`scripts/awo_service.py` runs a committed merge intent without exposing its
operation token:

```powershell
python -B scripts/awo_service.py --state STATE merge-execute --project-root ROOT --token-file TOKEN --intent INTENT_ID --expected-epoch EPOCH
python -B scripts/awo_service.py --state STATE merge-recover --project-root ROOT --token-file TOKEN --intent INTENT_ID --expected-epoch EPOCH
```

The token must be a project-wide controller credential; task, assignment,
generation and knowledge scopes are rejected. The service authenticates, checks
the current controller epoch and freeze state, reads the committed ticket in a
short transaction, and rolls back before the external runner starts.
`execute_intent` consumes the durable ticket before launching Git or a provider;
`merge-recover` resumes only an intent that already recorded an external attempt.
A lost command response is reconciled with the original command_id and the
external operation is never blindly repeated.

## Operational limits and validation

Worktrees and SQLite reservations provide cooperative process discipline, not
OS isolation. Same-user writers can still change files or check out a target
between inspection and CAS; deploy host fencing for writers outside the service.
The expected-old CAS still prevents overwriting an independently advanced ref.
Verifier/provider executables are trusted controller configuration. Worktrees,
receipts and uncertain operations are retained for review; no pruning policy or
background queue daemon is installed by this module.

Run the offline fixture suite:

~~~powershell
python -B -m unittest discover -s scripts -p "test_merge_queue.py" -v
python -B scripts/check_encoding.py
~~~

Fixtures create real disposable Git repositories, SQLite databases, subprocess
verification, and a child process that dies after CAS before report persistence.
NativeMergeTests additionally uses the actual parent scheduler and
workspace_runtime fixtures for Markdown acceptance, observed freeze, narrowed
reviewer credentials, independent identity checks, a restarted executor, Git CAS,
artifact finalization and mutation rejection. This class explicitly skips on an
older baseline without workspace_runtime; a skip is not native validation.
The worker evidence records a separate run loading current parent dependencies
and these exact owned source files, including dependency hashes and raw output.
The parent service now registers `merge-execute` and `merge-recover` through
`application_runtime.py`, installs the merge schema in `create_schema`, and the
scheduler requires `merge_queue.materialization_gate` proof for code
dependencies. That wiring is covered by the parent integration tests; it does not
extend the worker's fixture results to live providers.
