# Agent registry and capability evidence

The optional V2 service includes a minimal local Agent registry. It is additive
and disabled from execution; it does not dispatch work, start Harnesses, or make
a capability claim real by declaration.

## Agent records

An Agent has a stable project-bound `agent_id`, a lowercase `handle` unique in
the project, a role (`worker`, `observer`, `reviewer`, or `controller`), declared
Harness/version/host/OS, lifecycle (`pending`, `active`, or `revoked`), and a
`last_seen_at` heartbeat timestamp. Existing active local bindings are imported
as provenance-bearing identities: their roles are preserved, their Harness
version is `unknown` unless a real measured version was stored, and every
imported operation remains unknown. A binding revision is never treated as a
Harness version.

Historical assignments use the task's frozen `worker_binding.instance_id`, not
the role's current binding. A retired worker is imported revoked and cannot be
mistaken for its replacement. Revoking an Agent does not change any Task,
Assignment, claim, or slot. Agents have no expiry in this foundation; only
enrollment grants expire.

## Enrollment

A controller issues a single-use, project-bound, expiring enrollment grant for
the `worker` or `observer` role. The grant lists the allowed capability keys; a
client cannot ask for `cancel` or `steer` merely because the list has the same
length. The grant secret is written only to an explicitly chosen exclusive local
file with an exclusive create and POSIX mode 0600 where the platform supports
it. On Windows, confidentiality depends on the chosen directory's ACL; 0600 is
not an enforceable Windows guarantee. The database, events, command log, replay
response, and CLI
arguments contain only its hash or metadata, never the raw secret.

An enrollment grant creates an active Agent of the granted role and issues its
Agent token to another exclusive local file. Replaying the same command returns
the original metadata; a different payload conflicts. A reused, expired, or
revoked grant is rejected. No controller token is ever issued to a worker.

Unapproved self-registration creates a `pending` worker with empty/unknown
capabilities and no token. It cannot approve itself, become a reviewer or
controller, or claim execution authority. A controller approval issues the token
and keeps the role `worker`.

## Capabilities

Capability rows store the operation key, level (`supported`, `unsupported`, or
`unknown`), Harness version, OS, host, check time, source, and evidence hash/path.
A registration declaration is stored as `unknown` with `self_declared` source; it
never becomes verified by itself.

A controller can record `supported` only with an explicit measured
`harness_version` (not `unknown`), matching Agent OS/host, an ISO `checked_at`,
and an existing local evidence file whose SHA-256 matches `evidence_sha256`. The
evidence path is stored. Matching rechecks the context and the evidence hash;
missing, changed, or mismatched evidence degrades the capability to missing, and
required `unknown` capabilities fail matching. A declaration or imported binding
cannot verify support. Native session resume/send/steer must be declared and
evidenced separately from one-shot CLI execution.

## Heartbeat and revocation

A heartbeat is authenticated by the Agent token and updates only `last_seen_at`
and the Agent revision. It never changes Task/Assignment status, renews evidence,
or releases a slot. A missing heartbeat means stale/unknown, never proof of
death. Replaying the same heartbeat command is deduplicated.

Revocation blocks future authenticated use and retains the Agent record,
capability history, and every active claim. The controller can also revoke an
unused enrollment grant. Revocation and heartbeat remain usable while the service
is frozen; new enrollment, registration, approval, and capability admission are
blocked by freeze.

## CLI

Controller commands use the same command envelope (`command_id`, `tool`,
`expected_epoch`, `payload`). Authentication differs by entry point:

- controller admission/administration (`agent-enroll`, `agent-approve`,
  `agent-revoke`, `grant-revoke`, `agent-capability`, `agent-list`, `agent-get`,
  `agent-match`) requires the active controller binding and its dispatch
  permission;
- `agent-register` authenticates with the one-time grant secret file, not a
  controller binding;
- `agent-self-register` is an unapproved local request and creates only a
  pending worker with no token;
- `agent-heartbeat` and `agent-status` authenticate with the Agent token file.

```sh
python -B scripts/awo_service.py --state STATE agent-enroll \
  --envelope enroll.json --secret-out /local/private/grant.txt \
  --actor INSTANCE --version 1 --context main

python -B scripts/awo_service.py --state STATE agent-register \
  --envelope register.json --grant-file /local/private/grant.txt \
  --token-out /local/private/agent-token.txt
```

```json
{
  "command_id": "cmd-enroll-0001",
  "tool": "agent.enroll",
  "expected_epoch": 0,
  "payload": {
    "role": "worker",
    "allowed_capabilities": ["status", "collect"],
    "expires_minutes": 10
  }
}
```

```json
{
  "command_id": "cmd-register-0001",
  "tool": "agent.register",
  "expected_epoch": 0,
  "payload": {
    "handle": "builder-1",
    "harness": "claude",
    "harness_version": "1.2.3",
    "host": "local",
    "os": "win32",
    "capabilities": ["status", "collect"]
  }
}
```

Other operations:

```sh
python -B scripts/awo_service.py --state STATE agent-self-register --envelope self.json
python -B scripts/awo_service.py --state STATE agent-approve \
  --envelope approve.json --token-out /local/private/approved-token.txt \
  --actor INSTANCE --version 1 --context main
python -B scripts/awo_service.py --state STATE agent-capability \
  --envelope capability.json --actor INSTANCE --version 1 --context main
python -B scripts/awo_service.py --state STATE agent-heartbeat \
  --envelope heartbeat.json --token-file /local/private/agent-token.txt
python -B scripts/awo_service.py --state STATE agent-status \
  --token-file /local/private/agent-token.txt
python -B scripts/awo_service.py --state STATE agent-list \
  --actor INSTANCE --version 1 --context main
python -B scripts/awo_service.py --state STATE agent-match --agent AGENT_ID \
  --required status,collect --actor INSTANCE --version 1 --context main
python -B scripts/awo_service.py --state STATE agent-revoke \
  --envelope revoke.json --actor INSTANCE --version 1 --context main
python -B scripts/awo_service.py --state STATE grant-revoke \
  --envelope grant-revoke.json --actor INSTANCE --version 1 --context main
```

Secrets are read from files (`--grant-file`, `--token-file`), never from the
envelope or argv. `--secret-out` and `--token-out` must be absolute local paths;
an existing file is never overwritten.

## Limits

There is no MCP server, messaging, scheduler activation, budget, fanout, or
worktree operation in this registry. A registry declaration or local SQLite test
is not live Harness certification. The legacy workflow remains the default and
continues to work when the V2 service is not used. New enrollment, registration,
approval, and capability admission are blocked while the service is frozen;
heartbeat, revocation, reads, and recovery remain available.
