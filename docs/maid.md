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

## Route a task to a project manager

Copy the [example directory](../examples/maid-directory.json) to an operator-controlled location. Register each distinct project root and manager. For a material export, enter its source project, recipient project, published document ID, exact revision SHA-256 and path to an AWO credential scoped to that document. Keep the credential outside repositories and worker workspaces. The source state must already contain that published revision.

    python scripts/maid.py --thread C:/awo/student-task --directory C:/awo/projects.json --project student --material calibration submit --spec C:/awo/student-spec.json
    python scripts/maid.py --thread C:/awo/student-task start --confirm-execute
    python scripts/maid.py --thread C:/awo/student-task status

The task spec must name the requesting repository and the same thread directory. Submit copies the authorized reference into the frozen task prompt, so the recipient can retain that snapshot after a grant changes. Omitting --material routes a task without sharing a document. Status reports the manager, the source citation when present, and the accepted evidence ID after review. A landed result is on the task's target branch in the requesting repository's disposable clone under the thread directory.

The directory selects a registered manager by project key. This local interface supports one task per conversation and explicit commands.
