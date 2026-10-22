# Versioned harness capability evidence

English | [Chinese](capabilities.zh-CN.md)

Record `spawn`, `attach_resume`, `send`, `status`, `cancel`, `collect` and
`heartbeat` separately. `examples/capability-profile.json` is a complete profile
template. Every operation is `unverified`, `unsupported`, or `verified`.

| Transport implementation | spawn | attach/resume | send | status | cancel | collect | heartbeat |
|---|---|---|---|---|---|---|---|
| herdr | absent | explicit binding | conditional prompt | agent get | absent | agent read | absent |
| command | absent | explicit binding | configured argv | configured argv | absent | configured argv | absent |
| receipt | run_job one-shot | absent | absent | status file | absent | output file | absent |

This table describes this package's interfaces, not verified client versions.
Binding an identity does not resume a harness conversation. A status observation
timestamp is not a native worker heartbeat. Unsupported operations raise errors;
the scheduler does not guess provider commands or provision sessions.

| Harness | spawn | attach/resume | send | status | cancel | collect | heartbeat |
|---|---|---|---|---|---|---|---|
| Codex | unverified | unverified | unverified | unverified | unverified | unverified | unverified |
| Pi | unverified | unverified | unverified | unverified | unverified | unverified | unverified |
| Claude | unverified | unverified | unverified | unverified | unverified | unverified | unverified |
| dsh-tui | unverified | unverified | unverified | unverified | unverified | unverified | unverified |
| Grok | unverified | unverified | unverified | unverified | unverified | unverified | unverified |

The older [field reports](harness-adapters.md#versioned-field-observations) remain
external reports with missing harness versions/raw responses. They do not upgrade
these entries to independently verified claims. The Python-child demo verifies
local command transport behavior and fixture review, not these clients.

Before marking an operation verified, record OS, harness and transport versions,
source commit, exact reproduction argv arrays and nonempty raw evidence files
with SHA-256 hashes. Keep raw identity/status responses, task/nonce, exit codes,
delivery and review evidence as applicable. Store private logs outside the public
repository. Relative evidence paths must stay inside the profile directory.

```sh
python scripts/workflow_scheduler.py capabilities --file /absolute/profile.json
```

The validator checks profile completeness, versions, command shape and evidence
hashes. It does not execute those commands, independently validate their content,
or authorize operations. First-use client verification still requires an actual
authorized integration check. Version changes need a new evidence profile.
