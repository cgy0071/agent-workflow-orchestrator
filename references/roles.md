# Editable Role Templates

English | [Chinese](roles.zh-CN.md)

These defaults need no external project protocol. Users can change responsibilities, role count, permissions, and deliverables. Apply the current configuration and instructions.

| Default role | Responsibilities | Default boundary | Typical output |
|---|---|---|---|
| Planner / Engineering Lead | Design, task decomposition, experiments, evidence review, follow-up decisions | Does not replace the Executor in implementation or formal experiment operation | Plans, task specifications, reviews, decisions |
| Executor | Implement assigned work, run tests, measure, locate first differences | Does not independently change architecture, acceptance, or authorization | Changes, logs, RESULT/EVIDENCE/HANDOFF |
| Paper Lead | Research problem, literature gap, contributions, paper reasoning, experimental sufficiency | Does not equate engineering improvement with novelty or bypass planning to change implementation | Claim-evidence analysis, evidence requests, manuscripts |

## Default handoffs

Paper Lead requests evidence -> Planner writes a task -> Executor performs it -> monitor notifies Planner -> Planner reviews and follows up -> Paper Lead interprets accepted results.

Roles are independent of harnesses and models. Any role may use Codex, Pi, Claude, dsh-tui, Grok, or another tool. Preserve task identity, inputs, acceptance, and open items when changing tools. Evaluate delivered evidence rather than model branding.

## Example changes

- Software: use Planner / Implementer / Reviewer and omit Paper Lead.
- Prototyping: combine planning and execution while retaining another reviewer.
- Research: add Literature Reviewer and Experiment Auditor.
- Parallel alternatives: assign each Executor an independent writable directory and use a common reviewer.

Configure responsibilities, allowed_actions, forbidden_actions, harness, and adapter for each role. Permission changes require user authorization; a Worker cannot expand its own scope by editing configuration.
