# Read-only visibility and audit receipts

The P2 visibility modules are reusable components behind the existing application service. They do not create another controller, dashboard server, migration authority, or action gateway. The parent integration owns authorization, page wiring, independent review, activation, and acceptance.

## Public API

```python
from runtime_views import (
    VIEW_NAMES, BUCKETS, ViewError, build_runtime_views,
    bucket_for_task, explain_state, render_html,
)
from receipt_export import (
    ReceiptError, build_receipt, export_receipt,
    verify_receipt, rebuild_receipt,
)

build_runtime_views(con, project_id, *, limit=200, now=None)
bucket_for_task(task, *, blockers=(), needs_attention=False)
explain_state(state, *, entity="task")
render_html(snapshot, *, view=None)

build_receipt(con, project_id, *, max_rows=10000)
export_receipt(receipt, destination, *, enabled=False, protected_roots)
verify_receipt(repository, *, expected_manifest_sha256=None, expected_commit=None)
rebuild_receipt(con, repository, *, expected_manifest_sha256=None, expected_commit=None)
```

Database snapshot builders require an already active caller-owned transaction and an authorized project. They use explicit columns, project-scoped SELECTs and read-only schema inspection. The legacy DAG table has no project key: its bounded scan returns only nodes whose task IDs belong to visible project tasks, and omits requests and foreign-node identities. Builders never migrate, commit, begin a transaction, poll a worker, spawn a process, or read artifacts. Tuple rows and sqlite3.Row are supported. A mode=ro SQLite connection is recommended. Project roots, credentials, nonces, raw commands, requests, private message bodies, and runtime configuration are not projected. The dashboard may show task titles and safe registry metadata; receipt export uses a stricter policy.

The caller should finish its read transaction after building a detached snapshot. External receipt Git work belongs outside the runtime transaction. No receipt function opens the production database. Errors use ViewError or ReceiptError; unexpected database access failures still propagate to the caller.

## View contract and coverage

build_runtime_views returns format_version=1, project_id, source_max_event_seq, views, buckets, sources, and limitations. Every view has title, available, rows, truncated, and explanation. sources reports availability and truncation per source table. Missing tables or incompatible column sets are unavailable. Absent project identity is an error. Rows are bounded to limit per table (1..10000); truncation is explicit and dependent views inherit it. Counts and order describe the bounded snapshot, not global totals. Malformed, duplicate-key, oversized, or non-finite recorded JSON fails closed.

| View key | Recorded sources and presentation | Explicit limit |
| --- | --- | --- |
| project | Project, service pause state, task bucket counts, pending question/thread IDs | Knowledge/docs need their separately authorized subpage |
| milestones | Persisted dag_runs graph nodes linked to visible project tasks, plus queued contract DAG run/node and dependency edges | No dedicated milestone/phase schema; unknown or out-of-project node identities stay hidden; bounded scan and no cycle inference |
| task_board | Tasks, assignment claims, dependency/agent/capability/budget observations, human attention | Not admission authority; incomplete relations can block classification |
| registry | Agent approval, last-seen observation, capability declarations | No liveness or fresh capability probe |
| assignments | Assignment generation, slots, review status and persisted send observation | Worker completion is not acceptance |
| queue | Scheduler admission_key comparator, deadlines, aging and overrides | Sorted within selected rows; final admission gates remain in scheduler |
| workspaces | Recorded lifecycle, specification hash and snapshot manifest/inventory hashes | No file reads, ownership checks, writer-quiescence proof or merge queue |
| transcript | Run/assignment IDs, recorded event observations, transcript/diff/test evidence IDs | No transcript bytes; missing verification never implies passing inspection |
| evidence | Evidence IDs, sealed/current hashes, size and recorded state | Hashes are claims until source bytes are independently verified |
| review | Review records and tasks awaiting independent review | Accepted is separate from target integration |
| budget | Run/task/workflow scopes; actual, estimated, unknown and held amounts separately | No price inference, stream reconciliation, or invented zero totals |
| resources | Held v2 assignment slots and frozen contract resource units | Legacy claims, capacity and OS/GPU isolation are not established |
| recovery | Uncertain/attempted sends, failed/blocked/historical assignments, lost/orphaned workspaces | Retained claims remain; no automatic release or resend |

These 13 component keys follow the implementation contract. The architecture's navigation combines some of them and exposes the five buckets as pages; the parent can compose these components without creating a second state authority.

The milestone view includes milestone_metadata_available=false, complete_graph=false and a concrete unavailable_reason for the missing dedicated registry. Recorded DAG declarations remain usable. Malformed runs are omitted with an explanation, missing dag_runs columns are reported, and no graph records produces an explicit empty-observation explanation. The legacy scan and projected nodes both obey the bound. This supplies actual persisted graph data without inventing milestones or granting dependency admission.

## Five buckets and human explanations

| Bucket | Meaning |
| --- | --- |
| Coming In | Ready/queued work without a recorded blocker; still awaiting fresh admission |
| Going Out | Active work without a recorded blocker or human gate |
| Needs You | Draft approval, pending questions, budget approval, review_ready, repair or rejection decisions |
| Blocked | Known prerequisite/environment/capability/budget failures, uncertainty, incomplete admission observations or unrecognized states |
| Completed | Only Task accepted, landed, finalized; each has a distinct label |

review_ready never enters Completed. Assignment completed means delivered pending independent review. Accepted is labeled "Accepted; not yet landed"; landed and non-code finalized remain distinct. Human attention takes precedence over ordinary blockers for unfinished tasks. Cancelled work is not successful completion. Unknown cost is null, not zero. known_actual and known_estimated include only recorded numeric metrics; total_actual is null whenever any member lacks actual usage, has an estimate, or source coverage is incomplete. held_reservation is a separate exposure, never spending. A recorded actual zero remains zero.

explain_state returns label, explanation and suggested_action. For example, no_monitor becomes "No recent monitoring observation" with an inspection suggestion; raw transport labels are not user-facing diagnoses. Suggestions are plain text.

render_html returns an escaped HTML fragment. Every dynamic value and key is escaped; navigation uses fixed local anchors. There are no scripts, forms, write buttons, inline event handlers, external links, embedded runtime JSON, or mutation URLs. The parent may embed the fragment in its existing authorized page. Add any later write action only through the existing authenticated gateway and its normal command checks.

## Receipt privacy and hash semantics

build_receipt reads the same bounded project sources, rejects truncation, and produces a detached allowlisted audit projection. It requires project and event tables; unavailable optional tables remain explicit. No evidence or transcript bytes are opened. It exports task/assignment/review/evidence metadata, safe lifecycle values, typed numeric usage, and ordered project events. IDs, relationship keys, event-type identities, capability keys and usage dimensions are stable SHA-256 pseudonyms. They preserve equality joins but are not anonymization: low-entropy originals may be guessed.

Credentials and credential digests, nonce/token fields, paths, prompts, messages, command bodies, free text, configuration and raw event JSON are excluded. No arbitrary payload field is copied. Invalid enums, timestamps, hashes or numeric values become null. Allowlisted SHA-256 claims (including evidence, request, content, specification, inventory, manifest and revision hashes) retain their recorded spelling and value. Nested claims use a pseudonym for the source location. Sensitive subtrees, including credentials, config, commands, prompts and message bodies, are not traversed for hash claims. Unknown fields are omitted; the archive does not claim a lossless copy of them.

Each event carries three different checks:

1. source_payload_utf8_sha256 hashes the exact original payload_json UTF-8 bytes, including omitted fields. It is a commitment, not a copy of those bytes.
2. exported_fields_sha256 and exported_payload_sha256 hash the sanitized fields and sanitized payload respectively.
3. previous_sha256 links the canonical exported event envelope to the prior exported project event. The first predecessor is 64 zeros. Source global_seq must increase; gaps are valid because other projects may interleave.

Evidence sealed/current hashes, review note hashes and workspace snapshot hashes remain claims about source objects. Exporting a claim does not verify its omitted source bytes or accept the evidence. The manifest and every receipt retain the reconstructability limits.

## Isolated local Git export

Export is off by default. enabled must be Boolean True; protected_roots must be a nonempty list/tuple supplied by the trusted controller, covering the source checkout, runtime state, delivery/evidence directories and other protected locations. destination must be a new absolute local path in an existing private parent. Existing destinations, overlap in either direction, nested Git/bare repositories, traversal, UNC/ambiguous Windows paths, symlinks and junction/reparse ancestors are refused. There is no adoption, append, overwrite, delete, remote, fetch, push or automatic repair operation.

The caller must keep the parent private and prevent concurrent hostile filesystem replacement. Portable path inspection is not an OS sandbox or a race-free security boundary against another process with write access. Windows file modes are not ACL provisioning.

The new repo contains manifest.json and objects/<sha256>.json, followed by one root Git commit. Canonical JSON is ASCII-safe UTF-8, sorted keys, compact separators, finite values and one trailing LF. Each object is addressed by SHA-256. The manifest lists exact object sizes, addresses, record/event order, coverage and chain tip via its referenced snapshot. Identical snapshots produce identical content and fixed-metadata commits. APIs never modify an existing receipt. Immutability is enforced by content addressing and verification; the filesystem itself is not write-once storage.

Git uses bounded argv without a shell, a scrubbed environment, disabled system/global config, empty templates, disabled hooks/signing/auto-maintenance, no optional locks and no network protocol. Local config is restricted to Git init's basic core settings. Object alternates, grafts, shallow history, external Git directories, special files and hardlinked files are rejected. Default operations time out after 20 seconds and cap combined output at 8 MiB. Input JSON is capped at 1 MiB, receipt objects/manifests at 4 MiB, archives at 64 MiB and detached objects at 100000. Any bound can reject export; partial destinations from external failure are preserved, never retried by adoption or deleted.

## Verification and audit rebuild

verify_receipt reads the exact file inventory, canonical objects, object sizes/hashes, source coverage/counts, redaction schema, ordered event chain, Git root commit and committed blob identities. It does not refresh the index, checkout, migrate, repair or mutate files. Stated source hashes cannot be verified without omitted source bytes. An optional expected_manifest_sha256 or expected_commit supplied outside the repository pins provenance to the caller's trusted value. With no external pin the result explicitly says "internal consistency only". Git history is not an authentication mechanism by itself.

rebuild_receipt verifies first, then requires an empty SQLite destination in the caller's transaction. It creates only:

| Audit table | Columns |
| --- | --- |
| receipt_meta | key PRIMARY KEY, value_json |
| receipt_record | ordinal PRIMARY KEY, source_table, object_sha256, data_json |
| receipt_event | global_seq PRIMARY KEY, event_sha256, data_json |

The full sanitized records/events are reconstructed exactly, with hashes and order. A savepoint removes partial audit tables on failure; the outer caller still owns commit/rollback. Existing main or temporary schema objects are refused. There is no restoration of v2_* runtime tables, credentials, executable requests, artifact bytes, permissions, unknown omitted fields, or original IDs. This is an audit rebuild, not production recovery or event replay.

## Application CLI and dashboard route

The application service exposes the same read-only projection behind an issued
access token. It adds no listener, write route, scheduler action or review gate.

```powershell
python -B scripts/awo_service.py --state STATE views --project-root ROOT --token-file TOKEN [--limit N]
python -B scripts/awo_service.py --state STATE views-html --project-root ROOT --token-file TOKEN [--view VIEW] [--limit N]
```

The token must be a project `controller` or `observer` credential without task,
assignment or knowledge scope. `views` returns the JSON snapshot with
`can_write=false`; `views-html` adds the escaped fragment for one of the 13 view
keys (`project`, `milestones`, `task_board`, `registry`, `assignments`, `queue`,
`workspaces`, `transcript`, `evidence`, `review`, `budget`, `resources`,
`recovery`). `limit` is 1..1000 per table. A ledger that has the access tables
but not the V2 service projection returns
`{"available": false, "reason": "v2_ledger_unavailable", "can_write": false}`
after the credential is checked, instead of an empty guess; a ledger without the
access schema is refused as unauthenticated rather than served empty.

Receipt operations use the same credential and stay opt-in:

```powershell
python -B scripts/awo_service.py --state STATE receipt-export --project-root ROOT --token-file TOKEN --destination NEW_REPO --enable-export --protected-root ROOT --protected-root STATE
python -B scripts/awo_service.py --state STATE receipt-verify --project-root ROOT --token-file TOKEN --repository REPO [--manifest-sha256 H] [--commit C]
python -B scripts/awo_service.py --state STATE receipt-rebuild --project-root ROOT --token-file TOKEN --repository REPO --audit-db EMPTY_DB [--manifest-sha256 H] [--commit C]
```

Without `--enable-export` the export is refused. `--destination` must be a new
absolute local path outside every `--protected-root`; at least one protected root
is required, so list the source checkout and the runtime state directory. verify
and rebuild never mutate the receipt repository; rebuild writes only into a new
SQLite file and refuses an existing one.

The existing dashboard serves the same projection through
`GET /api/v2/views?limit=N` under its normal page token, Host, Origin and
`Cache-Control: no-store` checks. It opens the ledger read-only and returns
`{"available": false, "reason": "v2_ledger_not_initialized", "can_write": false}`
when the V2 ledger is absent. The page marks missing transcripts, diffs or
milestones as unavailable or partial in plain language. POST and other write
methods are not routed; the read model has no dispatch, review or closure
authority.

## Validation and rollout

Run the focused tests from scripts with Python 3.10+ and the local Git CLI:

```text
python -B -m unittest test_runtime_views test_receipt_export -v
```

Tests use actual SQLite, a read-only database URI, parsed HTML, disposable Git commits containing only synthetic fixture data, manifest/content/chain tampering, trust pins, empty-database rebuild/rollback, environment isolation, protected paths, hardlinks and Windows junctions. Directory symlink tests skip when platform privileges are unavailable. Set AWO_VISIBILITY_TEST_ROOT to an external evidence directory to retain all temporary work outside the checkout. No live model, Ubuntu host, GPU or remote repository operation is exercised.

The parent owns service/page wiring, authenticated action controls, independent review and activation. Implementation and fixture validation are not acceptance. Disable the UI or opt-in exporter to stop exposure; these modules make no production schema changes.
