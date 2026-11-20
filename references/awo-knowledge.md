# Local knowledge and documents

English | [Chinese](awo-knowledge.zh-CN.md)

`scripts/project_knowledge.py` is a standard-library domain module using the
existing SQLite authority. Parent-service integration and final integrated
acceptance are separate. It implements no external writes, remote transport,
provider connection, UI, or second database. It contains no Dispatch code.

## API and transaction contract

```python
create_knowledge_schema(con)
apply_command(con, project_id, principal, tool, payload)
query(con, project_id, principal, query, payload)
```

The trusted service supplies `principal = {role, principal_id, agent_id}`;
agent_id can be null for a human controller/reviewer/observer/advisor; workers
require a registered agent identity. An optional principal project_id must match.
Agent identities must exist in this project's `v2_agent`, be active, and have
the same role, except a trusted observer credential may narrow an agent's role
to read-only access. Principal ID and agent ID both participate in ownership
and independence checks. Changing a session name cannot bypass agent identity.

The parent owns authentication, project grants, envelope authorization,
deadlines, freeze/epoch fencing, command replay/idempotency, and service logs.
The module additionally checks domain roles, ownership, project isolation,
current entity revision, and independent evidence review. Never construct the
trusted principal directly from a tool payload.

Schema creation and commands require a caller transaction; use `BEGIN IMMEDIATE`
for writes. There are no begin/commit/rollback calls or `executescript` here.
On any error, roll back the entire command and its service envelope. Successful
mutations append domain audit records in the same transaction. The parent
records failed commands after rollback. Use a caller read transaction for a
consistent multi-query snapshot under concurrent writes.

Call schema creation only from an explicit parent migration. It adds the
`v2_knowledge*` tables and append-only history triggers without changing the
parent schema version or registry/service tables. Version 1 creation is
repeatable; a different stored module version is rejected. Reads do not create
or repair schemas. Both tuple and `sqlite3.Row` connections work; row_factory
is preserved.

`KnowledgeError(ValueError)` carries a stable `code` and readable message.
Codes: `invalid_payload`, `invalid_principal`, `wrong_project`, `not_found`,
`forbidden_role`, `forbidden_owner`, `revision_conflict`, `state_conflict`,
`independent_review_required`, `evidence_required`, `evidence_changed`,
`evidence_unavailable`, `unsafe_source`, `stale_knowledge`, `integrity_error`,
`transaction_required`, `schema_conflict`, `unknown_command`, `unknown_query`.
SQLite failures also propagate to the transaction owner.

## Commands and payloads

Exact names are exported by `COMMANDS`; an optional `awo_` prefix is accepted.
Workers, reviewers, and controllers may create proposals. Observers/advisors
cannot write. Creates accept optional `expected_revision: 0`. Every existing
entity mutation requires its current positive integer `expected_revision`.
Booleans and numeric strings are rejected. Source revocation checks its own
revision. Parent replay must return prior results without reapplying changes.

| Command | Payload and gate |
|---|---|
| `knowledge_propose` | Complete knowledge content; always proposed |
| `knowledge_revise` | knowledge_id, expected_revision, complete replacement content; owner or project controller |
| `knowledge_review` | ID, expected_revision, revision_sha256, evidence_sha256, verdict, comment, evidence_reviewed; independent reviewer/controller |
| `knowledge_promote` | ID, expected_revision, review_id, revision_sha256; independent reviewer/controller |
| `knowledge_retire` | ID, expected_revision, reason; owner or project controller |
| `knowledge_source_approve` | relative_path, sha256; controller only |
| `knowledge_source_revoke` | source_id, expected_revision, reason; controller only |
| `knowledge_sync_import` | proposal envelope; untrusted team proposal only |
| `doc_import` | Complete document content; always proposed |
| `doc_revise` | doc_id, expected_revision, complete replacement content; owner or project controller |
| `doc_review` | Same review fields, with doc_id; independent reviewer/controller |
| `doc_publish` | doc_id, expected_revision, review_id, revision_sha256, content_sha256; controller only |
| `doc_retire` | doc_id, expected_revision, reason; owner or project controller |

Knowledge content requires `kind`, `title`, `body`, `scope`, `source`, and
`source_refs`. Optional: `confidence` (default unknown), `expires_at`,
`verify_after_seconds`, `reviewer_principal_ids`, `rationale`, `alternatives`.
Decision requires a nonempty rationale and at least one alternative. Sources
are `human|agent|doc|review|evidence|external`.

The seven kinds are `preference|convention|constraint|hazard|decision|fact|reference`.
Personal convention is rejected. Fact requires references and a known confidence
(`high|medium|low`); high confidence grants no review or promotion authority.
Reference remains a pointer, never automatically a fact. Every fact, whatever
its declared `source`, needs at least one replayable local `file`, registered
`evidence`, or published `doc` reference. Inline text and external hash claims
are assertions, not local proof; a proposed fact citing only them is rejected
with `evidence_required` before any review. `source` is declared provenance, not
authenticated authorship: labels such as `human`, `agent`, `review`, or
`evidence` are the submitter's assertion, and independent review remains the gate
that must reject claims it cannot verify. Record an inference that lacks
replayable bytes as a decision, a reference, or a draft proposal instead.

Document content requires title, scope, and exactly one of: bounded inline
`content` (optional content_sha256), or approved `source_id` with mandatory
content_sha256. The same expiry, verification interval, and personal reviewer
fields apply. Imported bytes are retained in SQLite. A revised published doc
returns to proposed; its old published hash remains historical, not current.

Results include entity_id, knowledge_id or doc_id, project_id, revision,
content_revision, state, review_id, revision_sha256, and content_sha256. Source
results contain source_id, source revision and state. Entity revision increments
on every state mutation; content_revision only on content revisions.

## Hashes, independent review, and publication

Content SHA-256 covers exact UTF-8 body bytes, without newline normalization.
Revision SHA-256 binds the complete normalized manifest and provenance using
sorted compact ASCII-escaped JSON. Evidence SHA-256 binds body hash and ordered
source references. Revisions, reviews, imported envelopes, and audit history
are append-only in SQLite. Unknown payload keys and author/state overrides fail.

All kinds follow proposed -> reviewed -> active. Documents follow proposed ->
reviewed -> published. Rejection makes the item rejected and unusable. Review
never promotes or publishes. Every revision clears review and verification time,
including metadata-only revisions with unchanged body bytes. All previous
revision authors are excluded from review and knowledge promotion.

Accept requires `evidence_reviewed: true`, a nonempty comment, and exact current
revision/evidence hashes returned by get. Every local reference is checked again
at review and promotion/publication. Fact needs at least one locally verifiable
reference; an external URL plus claimed hash is insufficient. The reviewer must
assess whether the evidence supports the claim: matching bytes alone is not truth.

Registered evidence producers and referenced document authors cannot review
facts based on their own evidence. A controller may publish their own document
after independent review, but cannot self-review. Knowledge promotion itself
must be independent of every knowledge revision author. Promotion checks that
an agent reviewer is still active with the recorded role.

Publish records the reviewed hash in SQLite only. It never writes a Word file,
repository file, remote document, website, or other external publication.

## Submission provenance and declared sources

Every new revision stores server-derived provenance beside its manifest:
`submitted_by` (principal_id, agent_id, role, binding_id, binding_version),
`source_assertion` (`declared` label plus `authenticated: false`), and the
existing type/origin fields. These values come from the authenticated service
principal mapping, never from payload fields; unknown author/role/binding keys
are rejected as invalid payload. A revision refreshes `submitted_by` for the
principal that actually submitted it and keeps prior structural fields such as
sync origin and `supersedes_sha256`.

Projections add `source_authenticated` (always false), `source_assertion`,
`submitted_by`, and `provenance_class` (`authenticated-submission` when a
submitter is recorded; `legacy-unknown` for rows written before this metadata
existed). A declared human source cannot be authenticated: no knowledge command
accepts an Owner or human credential, and Owner credentials are interactive
supervision-only. `source` stays an assertion even after independent review.
Legitimate human-source claims are recorded with replayable local evidence, but
the label is never itself proof.

Rows written before this metadata keep their exact bytes, hashes and current
decay behavior and are never silently relabeled. An active inline-only legacy
fact keeps its existing usability; while it still relies only on inline or
external claims it cannot be re-reviewed, promoted, or revised, because those
gates apply the replayable-evidence rule. Legacy revisions may have no `submitted_by`; that is recorded explicitly
rather than treated as authentication.

Replayed commands return the recorded result for the same principal, command_id
and payload hash; reusing the id with different bytes is an idempotency conflict,
so replay cannot attach new provenance. Revoked or expired credentials fail
authentication before replay is consulted.

## Evidence and approved files

source_refs contains 1..16 distinct ordered objects, each with lowercase sha256:

| kind | Other required fields | Verification |
|---|---|---|
| inline | label, content | Hash retained UTF-8 bytes |
| file | source_id | Re-read approved bounded path and check hash |
| evidence | evidence_id, source_id | Real bytes plus project/task/assignment ownership, sealed/current hashes, state, registered path |
| doc | doc_id, content_revision | Current fresh published document with matching body hash |
| external | uri | Untrusted pointer; never fetched or counted as local proof |

External URI forms: http://, https://, urn:, awo-local:. Changed, missing, or
revoked sources close write gates and make read projections stale.

Only a controller can approve an exact project-relative path and hash. The sole
root is service-managed `v2_project.root_path`; payloads cannot override it. The
parent must populate the intended local root. If a deployment's root points to
a state directory, use inline text or files within that directory until the
parent explicitly configures another root. Approval makes those bytes citable
by project members; do not approve private files as shared project sources.

The module rejects absolute/traversal/UNC/device/alternate-stream paths,
backslashes, ambiguous Windows names, Git internals, symlinks, junctions/reparse
points, hardlinks, nonregular and oversized files. Reads compare identity, size,
mtime, and within-API ctime before/after reading, then compare bytes to the hash.
Windows stat/fstat ctime semantics need not match each other. Filesystems and
SQLite are not one transaction: later file changes become stale at the next
query/gate. These checks are not an OS sandbox against an adversarial local
administrator racing path replacements.

## Scopes, queries, decay, and retirement

Scopes are personal|project|team, always within one project. Team is a local
visibility/export scope, not remote membership. Personal items are visible only
to their owner and explicitly named reviewer/controller principal IDs. A
controller has no blanket private access. Named reviewers can review/promote,
but cannot edit another owner's personal item. Scope is immutable; changing it
requires a separately reviewed proposal.

Exact names are exported by `QUERIES`:

- knowledge_get / doc_get: relevant ID, optional content_revision for a retained
  historical snapshot. Historical results are unusable, not reactivated.
- knowledge_list / doc_list: optional limit, after_id, scope, and knowledge kind.
  Returns metadata plus next_after_id.
- knowledge_history / doc_history: relevant ID, optional limit/after_seq.
  Returns audit events, associated reviews, and next_after_seq.
- knowledge_sync_export: knowledge_id of a team entry; returns an auditable
  proposal envelope, not a command or active remote record.

Queries write nothing: no recall counters, expiry updates, or schema changes.
Page size defaults to 50, range 1..100. Visibility precedes pagination so private
IDs do not leak in cursors. Kind/scope filtering uses a bounded visible page;
sparse/empty pages can still have a continuation cursor.

Verification interval defaults to 30 days, allowed range 1 second..365 days.
Expiry requires a timezone and is normalized to UTC. Only accepting review sets
last_verified_at. Projections return decay=fresh|stale|expired, stale_reasons,
effective_state, and usable. Stale active/published records retain stored state
but have effective_state=stale and usable=false. Consumers must use this
projection, not the active flag alone.

Fresh accepted entries have trust=confirmed; otherwise origin is human or agent.
External references (including mixed-source entries) and sync provenance stay
source_trust=untrusted even after local review.
Confidence is separate, with confidence_is_proof=false. Expired content needs a
new revision; aged verification can be renewed by independent review and explicit
promotion/publication. Retirement retains all history and is terminal.

Projections also expose `source`, `source_authenticated`, `source_assertion`,
`submitted_by`, `provenance_class`, `expires_at`, `verify_after_seconds`,
`author_principal_id`, `author_agent_id`, and the current `review` record
(reviewer principal/agent/role, verdict, reviewed_at), which may be a rejection.
Every revision row retains its own author and manifest; `knowledge_get` with an
explicit `content_revision` returns that revision's source, author, `expires_at`,
`verify_after_seconds` and `evidence_sha256`, while its `review` field is `None`
because the review pointer belongs to the current revision. `knowledge_history`
returns audit events, attaching the review record only where the action names one.

## Proposal exchange boundary

Format `awo.knowledge.proposal.v1` contains project_id, origin, entry, sha256.
The envelope digest covers all fields except its own sha256. Origin retains
entity ID, content revision, revision hash, exporter, and export timestamp.
Local file/evidence/doc pointers become untrusted awo-local references; bounded
inline evidence is retained. Export is a pure read: its self-contained envelope
is the audit artifact. Transport/access logging belongs to the parent.

Import validates project/schema/size/hash/types, stores the entire original
envelope and importer, and creates a new proposal owned by the local importer.
Only team/external entries are accepted. Remote local-source IDs, reviewer
assignments, author overrides, active status, review IDs, and trust overrides
are rejected. Identical envelope digests cannot be imported twice. A different
envelope for the same origin entity and content revision is accepted as a
separate untrusted proposal and flagged with `sync.origin_conflict: true`; the
module performs no conflict resolution, and neither copy can activate without
local review. Fact entries are refused at import with `evidence_required`:
export reduces local file/evidence/doc references to external pointers, and
inline or external references are not local proof. A team fact must be
re-proposed locally with replayable evidence and pass local independent review;
the authenticated importer is a local submitter, not the original author.

Hashes detect changed envelopes, not sender authenticity. Changed content with
a recomputed digest is another untrusted proposal and still requires local
independent evidence review and explicit promotion. Exported local-only evidence
must be supplemented with local verifiable evidence before fact acceptance.
There is no transport, credential handling, peer discovery, conflict resolution,
signature verification, or federation.

## Authenticated application entry

Keep the workflow state directory separate from project content. After the
explicit schema upgrade, the Controller binds the content root once with the
ordinary command envelope:

~~~json
{"command_id":"bind-content-root-001","tool":"project.bind_root","expected_epoch":1,"payload":{"project_root":"E:/projects/example","expected_revision":0}}
~~~

Use the actual current epoch. Save the envelope to a file and invoke
awo_service.py --state STATE command --actor ACTOR --version VERSION
--envelope FILE. Binding is immutable; replay the same command to recover its
result. It does not move files or change the legacy state directory.

Use access-issue to obtain a project-root credential. A reviewer credential
names knowledge_id (a knowledge or document ID), plus its current review-capable
binding. The service keeps the real Agent identity, so a narrowed credential
cannot review that Agent's own work. Task/run credentials do not grant knowledge
access. Local source approval reads only the explicitly bound content root.

~~~powershell
python -B scripts/awo_service.py --state STATE extension --project-root ROOT --token-file TOKEN --tool doc_import --arguments ARGS.json
python -B scripts/service_http.py --state STATE --project-root ROOT --extensions-enabled
~~~

Extension arguments contain payload; writes also require command_id and
expected_epoch. The command payloads in this guide remain unchanged. CLI and
opt-in HTTP/WS share authorization and durable replay. The MCP core catalog
remains 13 tools, or 16 with messaging; project extensions are separate.
Publishing records the reviewed document in SQLite and performs no network
upload. Run test_service_extensions.py for the actual application and wire
integration tests in addition to the domain tests below.

## Bounds and tests

Canonical JSON payload: 1 MiB. Doc/file bytes: 256 KiB. Knowledge body and each
inline evidence body: 64 KiB. Maximum 16 references, eight personal reviewers,
16 alternatives. Content limits count UTF-8 bytes; payload limits count canonical
ASCII-escaped JSON bytes. Strings, timestamps, integer revisions, unknown fields,
and finite JSON values are validated.

```powershell
python -X utf8 -B -m unittest discover -s scripts -p "test_project_knowledge.py" -v
python -B scripts/check_encoding.py
```

Tests use real SQLite with accepted parent DDL and isolated local files.
P1KnowledgeTests covers P1-5 minimum knowledge, proof/review/promotion, changed
evidence, revisions, decay, retirement. DomainBoundaryTests covers transaction,
schema, principals, projects, ownership, concurrency, append-only history.
P2DocumentTests covers P2-1 document workflow and file safety. P2MemoryTests
covers P2-2 kinds/scopes/privacy/proposal exchange, the fact evidence gate,
projection provenance, and sync conflict/expiry/retirement. ProvenanceTests
covers every declared source label, server-derived submission metadata,
revision relabeling, legacy-unknown rows, and sync importer provenance. The
application suite (test_service_extensions.py) adds real CLI/gateway/SQLite
rejection of malicious source labels, replay idempotency, revoked-credential
ordering, and scoped-entity denial. These tests do not establish production
migration or federation acceptance. A host
without symlink privilege explicitly skips that real filesystem test; hardlink
and file mutation checks still execute.
