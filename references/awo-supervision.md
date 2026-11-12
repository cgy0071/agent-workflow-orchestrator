# Local supervision and paired external drafts

English | [Chinese](awo-supervision.zh-CN.md)

The standard-library module [supervision.py](../scripts/supervision.py) implements
P2-3 read-only advice and a bounded controller entry, plus P2-5 local paired
draft intake. Its only durable authority is the existing workflow SQLite ledger.
It does not run models, send external messages, dispatch workers, perform merges,
install providers, or configure a client. Core MCP discovery stays at 13 tools
(16 only with explicit messaging); supervision is a separate parent extension.

## Integration and transaction ownership

The parent explicitly calls create_supervision_schema(con) inside its schema
transaction. The parent service schema must already exist. Six module tables
use the v2_supervision_ prefix: meta, owner_identity, entry, approval, pair and
draft. No Owner identity is installed by schema creation. No domain function
opens a connection, commits, rolls back, or performs an implicit migration.
Use sqlite3.Row and BEGIN IMMEDIATE for writes; reads support BEGIN on a
read-only connection. The caller must roll back the entire command on failure.

Parent authentication supplies the complete service_access.authenticate result
for agent calls. The module checks the token record, active agent, project and
resolved root, scope, current binding and epoch again. It does not accept role
or identity overrides from payloads. Preserve task/assignment/generation scope.
Knowledge-scoped credentials cannot access any agent supervision entry point.
The check rejects a non-null knowledge_id in either the authenticated principal
or the stored access-token row, including cached principals that omit it. Older
schema baselines may omit this column; an absent or null scope remains compatible.

For ordinary apply_command operations the parent owns command_id, expected_epoch,
durable replay, error mapping and rejection journaling. Successful domain
mutations add events to the same journal. Queries never write audit records.
controller_entry and convert_draft instead execute their embedded existing
service command through service_api authorization/mutation and v2_command_log
on the same connection. They preserve its command_id and arguments, so direct
gateway, subprocess and Overseer replays share the same command result. Do not
reuse that ID for a differently named outer command-log record. An extension
may use a distinct outer ID, with all live gates rechecked before outer replay.

## Owner authentication is a parent boundary

The baseline agent token service has no Owner token role. The parent must provide
a separate authenticated local Owner setup/decision interface. The domain
module provides no enrollment method or CLI for humans, no credential issuance
and no boolean owner_approved override. Test fixtures provision isolated Owner
rows to exercise this contract; they do not establish production Owner setup.

The parent provisions v2_supervision_owner_identity with these exact fields:

| Field | Type and meaning |
| --- | --- |
| project_id | TEXT, references the existing v2_project |
| principal_id | TEXT, owner: followed by 1..100 safe identifier characters |
| auth_generation | INTEGER greater than zero |
| linked_agent_id | Nullable TEXT; an agent that is not independent of this Owner |
| expires_at | INTEGER UTC Unix seconds |
| revoked_at | Nullable INTEGER UTC Unix seconds |

Primary key: (project_id, principal_id). Parent stores Owner secret hashes in its
own credential table and authenticates before constructing this Python mapping:

    {'role':'owner','principal_id':'owner:local-human','project_id':'...',
     'auth_generation':1,'agent_id':None}

The identity must be active, unexpired and at that authentication generation.
Owner credentials must never be synthesized from an agent or external payload.
Incrementing auth_generation, revoking or expiring the identity invalidates its
issued bindings, approvals and pairs. Parent separately checks each Owner
credential's revocation/expiry. A linked Owner cannot bind or approve its own
controller agent. An independent human Owner may have linked_agent_id NULL.

## Advisor and controller entry

overseer(..., mode='advisor') is the default. It allows only awo_task_get,
awo_task_list and awo_assignment_get, subject to the existing service scopes.
Every write is rejected even if the underlying credential is a controller.

query(con, project_id, principal, operation, payload) supports:

- advisor_snapshot: bounded scoped tasks, current epoch and read-only status.
- advisor_suggest: {goal}; deterministic state-based suggestions from at most
  200 visible tasks, labelled unapproved_draft and persisted=false. No model or
  hidden decision maker is claimed.
- draft_get: {draft_id}; returns explicitly untrusted proposal content.
- draft_list and pair_list: {limit?,after?}; project-wide controller/observer
  credentials only, at most 200 records.

Controller entry requires an explicit Owner-created binding to the active
controller agent, binding ID/version, epoch and expiry. It is restricted to
main context and unscoped controller credentials. It can delegate only these
already implemented service operations: awo_task_create, awo_assignment_claim,
awo_resource_claim and awo_resource_release. Each invocation needs an exact
independent Owner approval, including the entry revision, service tool, full
arguments, original command_id, expected_epoch and any expected_revision.
The existing service continues to enforce admission, ownership, capacity and
closure gates. No queue authorization is manufactured here.
An assignment claim may durably return WAITING without a reservation. Replaying
that command preserves WAITING even after admission changes; a new attempt needs
a new command_id, current revision and new exact Owner approval.

Review submission, self-review, accept/land/merge, pause/cancel and send_as_you
are not exposed. Unknown operations fail closed. Rebinding changes the entry
revision and cannot revive an old approval. Revocation and expiry are checked
before replay; a previous successful result is not fresh authorization.

Owner-only apply_command operations and strict payloads:

| Operation | Payload |
| --- | --- |
| supervision_bind | agent_id, binding_id, binding_version, expected_epoch, expires_at; expected_revision required for replacement |
| supervision_revoke_binding | agent_id, expected_revision |
| supervision_approve | approval_id, intent, expires_at |
| supervision_revoke_approval | approval_id, expected_revision |
| supervision_pair | pair_id, local_id, peer_id, key_sha256, expires_at |
| supervision_revoke_pair | pair_id, expected_revision |

command_intent builds a read-only exact command description for presentation to
the Owner. Approval is a separate independently authenticated operation; merely
building or showing an intent grants nothing. Unknown fields are rejected.

## Bounded paired draft protocol

The explicit protocol label is awo.paired-draft.v1, a local AWO adaptation.
It is not advertised as a standard A2A implementation or a tested installed
client. There is no network listener, discovery, key exchange, external send,
streaming, remote execution or live peer compatibility claim.

Owner pairing pins project_id, pair_id, distinct local_id/peer_id, a SHA-256 key
fingerprint and expiry. Use a fresh secret with at least 32 random bytes through
the parent's trusted secret-file boundary. The module accepts 32..128 key bytes
separately from logged payloads and stores only the key fingerprint. A revoked
pair ID cannot be reused; create a fresh pairing for rotation. HMAC is shared-key
authentication, not an asymmetric identity proof or non-repudiation mechanism.

An envelope has exactly protocol, project_id, pair_id, sender, recipient,
message_id, issued_at, expires_at, payload and signature. payload contains only
title and body. Times are UTC Unix integer seconds. Signature is lowercase
hex HMAC-SHA256 over protocol + LF + canonical JSON of the envelope without
signature. Canonical JSON uses Python json.dumps with sort_keys=True,
separators=(',', ':'), ensure_ascii=True and allow_nan=False. No floats occur.
make_envelope constructs this value locally; decode_envelope rejects duplicate
keys, invalid UTF-8, nonfinite numbers, excess bytes/depth and unsupported types.

receive_envelope requires a current authenticated local controller. It checks
pair and Owner revocation, project, both identities, key fingerprint, signature
and time bounds before any write. Same pair/message ID plus identical canonical
envelope returns the existing draft. A changed envelope conflicts. Intake
creates only a v2_supervision_draft row with untrusted=true; it creates no Task,
Assignment, dispatch intent or external message. Content is never evaluated or
interpreted as instructions and URLs/paths are never fetched.

conversion_intent accepts draft_id, expected_revision=1, command_id,
expected_epoch and optional task_id. It binds the original envelope hash and
signed title/body into an exact single/P3 draft Task command. Owner approves
that intent separately. convert_draft adds approval_id, rechecks all gates and
calls the existing service on the same connection. Task creation, command
receipt, approval use and draft conversion commit or roll back together.
Distinct concurrent commands can convert a draft only once. The resulting Task
remains draft: conversion is never dispatch authorization. Pair revocation or
expiry blocks outstanding proposals. Conversion must also occur before the
original envelope expiry; intake does not extend it. Historical read-only
projection remains.

## Limits and local CLI

| Bound | Value |
| --- | --- |
| Canonical protocol/CLI JSON | 64 KiB |
| JSON nesting | 12 levels |
| Proposal title/body | 512 / 32000 UTF-8 bytes |
| Envelope lifetime / future clock allowance | 900 / 30 seconds |
| Pair / controller entry / approval lifetime | 24 hours / 4 hours / 1 hour maximum |
| Read list / state-rule scan | 200 records |
| CLI token file | 16 KiB |

The CLI defaults to advisor, with an existing issued access-token file and a
JSON request file containing {tool,arguments}:

    python -B scripts/supervision.py --state /path/state --project-root /path/project --token-file /path/access.txt --request-file /path/read.json

For a read, set tool to awo_task_list and arguments to an empty JSON object.
For an approved write, add --mode controller_entry --approval-id APPROVAL_ID and
use the exact approved service arguments. CLI owns its connection/transaction
as a caller; it does not install tables or Owner identities. It exposes neither
Owner approval nor key-bearing pairing/import routes. Tokens never appear in
command-line arguments or diagnostics. Failures return stable machine codes.
No command is automatically retried; reconcile lost responses with the original
command_id.

## Owner credential bootstrap

Owner credentials are separate `awo_owner_` bearer tokens bound to the project
content root and an authentication generation. Bootstrap is a local
administrator operation on an interactive terminal; it is never an Agent, MCP,
HTTP or draft route:

```powershell
python -B scripts/owner_access.py --state STATE --project-root ROOT setup --actor CONTROLLER_ID --version VERSION --owner-id OWNER_ID --token-out OWNER_TOKEN.txt [--expected-generation N] [--minutes M] [--linked-agent AGENT_ID]
python -B scripts/owner_access.py --state STATE --project-root ROOT command --token-file OWNER_TOKEN.txt --envelope ENVELOPE.json
```

Setup prints the exact phrase `AUTHORIZE OWNER <owner_id>` and requires it to be
typed; without a terminal it refuses. The project content root must already be
bound, and a linked Agent must belong to the same project. Rotation passes the
current generation as `--expected-generation` and revokes earlier credentials.
Command envelopes carry `command_id`, `tool`, `expected_epoch` and `payload`; the
tool is one of the supervision Owner commands or `owner.revoke`. Authentication,
expiry, revocation, root binding and generation are checked before replay, and a
previous result is not fresh authorization. `owner.revoke` must name the current
`auth_generation`. The credential owns no task, review or dispatch authority; it
only authorizes the supervision decisions listed above.

## Verification and parent integration

    python -B -m unittest discover -s scripts -p test_supervision.py -v
    python -B scripts/check_encoding.py

Tests compose ServiceAccessTests, actual service APIs, SQLite and real local
subprocesses, including the production gateway backend. They cover read-only
advice, exact approval, role/identity/scope rejection, expiry/revocation, replay,
transaction rollback, signature/JSON rejection and competing processes.
The Owner fixture is explicitly the parent authentication seam. The parent now
installs the supervision and Owner schema in `create_schema` and integrates the
real Owner credential through `owner_access.py`, including setup, rotation,
revocation, expiry, generation and permission-before-replay checks on disposable
local SQLite fixtures. These are local fixture results, not a production Owner
installation or an A2A certification.
