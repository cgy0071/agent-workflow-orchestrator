# MAID: local project secretary

Use the same local conversation directory to submit a project-entry spec, start its authorized work, and ask for the result after restarting. Start with the [project-entry guide](../references/project-entry.md) to prepare a spec with the repository, allowed paths, reviewer and verification command. The conversation directory must match its work_root.

    python scripts/maid.py --thread C:/work/awo-demo chat

In the console, enter these lines separately (use the actual spec path):

    submit C:/work/spec.json
    status
    start --confirm-execute
    status
    exit

Start waits for the run. Another terminal can read status while it runs. Reopen the same directory to ask again. The reply includes the AWO task and assignment states, review verdict, evidence identifier when accepted, and next action. Closing the console between commands does not lose the task. Repeating start cannot resubmit a journaled run. For interrupted execution use the existing project-entry recovery procedure.

This first slice is local, uses explicit commands and holds one task per conversation directory. It is not an inbound network endpoint. The next milestones are a project manager directory with separate cross-project grants, verified model and harness routing, then a QQ adapter with authenticated sender mapping, deduplication and replies tied to the original message.
