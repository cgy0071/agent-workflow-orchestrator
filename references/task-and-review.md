# Editable Task and Review Contract

English | [Chinese](task-and-review.zh-CN.md)

## Default task specification

```markdown
# TASK-ID - One bounded objective
Owner authorization / parent task:
Assigned role / reviewer role:
Cwd / branch / inspected revision:
Inputs and identity:

## Objective
The question to answer and why it matters.
## Allowed / forbidden changes
Writable directories, implementation scope, read-only inputs, and stage boundaries.
## Acceptance
Reference, checks, metric definitions, and conditions for reusing existing evidence.
## Deliverables
RESULT.md / EVIDENCE.md / HANDOFF.md, changes, and raw evidence paths.
## Failure and stop
First-difference, environment, scope, preservation, and escalation conditions.
```

Adapt the template to the domain. Git, synthesis, and paper evidence are not required for every task. Specify only the applicable acceptance checks.

## Default review

Verify task/worker identity and change scope. Read the raw evidence. Distinguish executed from unexecuted work, models from measurements, and environment or observation faults from implementation faults. Record accept, repair, reject, or missing evidence, whether the baseline changes, and the next gate.

Comparators must not accept empty files, dropped records, or matching errors on both sides. Negative tests should address the actual risk. Preserve failures and reuse already verified evidence during repairs.

## Configurable continuation

workflow.transitions defines the action after each review decision: next_task, repair_task, stop, or owner_input. Before creating work, check continuous-execution authorization, scope, budget, gates, worker identity, and directory ownership.

A follow-up gets a new task ID and nonce linked to its parent. Do not overwrite uncertain submissions or historical results. A completion notification is not a new Owner instruction. Dispatch and notification may be automated; the assigned reviewer still forms the acceptance judgment.
