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

For the optional [OMP one-shot bridge](omp.md), the installed 18.12.6 CLI completed a real two-task DAG with independent reviews through `command` and `receipt`; a separate real OMP probe used the `write` tool. The failed JSONL/exit/receipt path was also observed with an expired provider credential. `status` and `collect` refer to the orchestrator-managed job, not an interactive OMP session. Conversation resume, live message send, Swarm, and explicit cancel are not supported by this bridge.
Binding an identity does not resume a harness conversation. A status observation
timestamp is not a native worker heartbeat. Unsupported operations raise errors;
the scheduler does not guess provider commands or provision sessions.

The local Python-child demo exercises command transport and review. For a
real client, create a profile for the installed version after checking its actual
identity, command responses, and task delivery. No client is preconfigured.

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
