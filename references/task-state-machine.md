# Task state and event contract

English | [Chinese](task-state-machine.zh-CN.md)

The controller keeps execution, dispatch, delivery, notification, review and
continuation distinct. [Scheduling](scheduling.md) describes the commands;
`task-event.schema.json` specifies the machine-readable journal envelope.

| Dimension | States | Transition authority/evidence |
|---|---|---|
| Dispatch | reserved -> submitted / uncertain -> running / not_sent / terminated | Current controller; durable request, nonce and generation; inspected reconciliation note |
| Execution claim | running -> finished -> resolved | Registered monitor delivery/exit; valid accepted evidence or inspected termination |
| Delivery | working, settling, review_ready, incomplete, failed, exited, adapter_error, monitor_timeout, monitoring_stopped | Frozen request, pinned identity, completion sequence, artifacts and hashes |
| Notification | inbox-only, reserved, submitted, delivery_uncertain | Explicit authorization, verified target and single persisted send attempt |
| Review | pending -> acknowledged -> reviewed | Bound main reviewer, configured verdict and hash-bound REVIEW.md |
| DAG node | waiting -> ready -> reserved/submitted/running -> under_review -> accepted | All dependencies accepted and revalidated; separate worker capacity and workspace/resource claims |
| DAG block | blocked, evidence_changed, blocked_dependency | Rejection, failure, invalid evidence, termination or blocked upstream |

`tasks.status=running` denotes an occupied or uncertain execution slot, including
an unconfirmed dispatch. `dispatches.status=running` denotes independently
confirmed execution. Neither is a claim that a CPU process is currently active.
`finished` releases the legacy role slot after delivery/failure; scheduler
workspace/resource claims remain until acceptance or inspected termination.
`resolved` releases those claims. A valid acceptance remains available to a DAG
after release, provided its evidence bytes remain unchanged.

Every scheduler reservation, submission, uncertainty, reconciliation, delivery
event, review and release has a journal record with event ID, task, generation,
actor, UTC time, kind and evidence data. Controller versions are stored in the
reservation or audit records. Monitor events identify the frozen request hash
as their producer; this is a local provenance convention, not authentication.

Task IDs are immutable attempt identities. Reusing an ID with different inputs
fails. Repeating an identical unconfirmed reservation cannot send a second time.
Monitor event IDs derive from the request hash and report status, so repeated
completion reports do not duplicate review events. Late anomaly events cannot
grant acceptance: accept requires review_ready, and DAG readiness additionally
requires task/nonce-bound receipt and unchanged review/delivery files.

An uncertain send cannot transition to a fresh dispatch of the same ID. An
already confirmed execution cannot be reconciled as not_sent. Closed attempts
cannot be reopened. Review rejection/repair cannot satisfy a DAG dependency.
An accepted review whose bytes change cannot admit a dependent node. A stale,
side or unknown controller cannot dispatch, reconcile, release or review.

Rework is a new scoped task with a new ID and nonce; record the next generation
and the sealed parent review as appropriate. The scheduler does not replay
failed work or infer cancellation from missing updates. Standard-library
regression tests exercise these transitions, crash windows, conflicting claims,
duplicate events, controller changes, and invalid dependency admission.
