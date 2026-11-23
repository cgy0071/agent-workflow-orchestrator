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

An operator maintains a local JSON directory. Each project has its own key, manager name and repository. An export names one recipient and one published document revision. The credential file belongs outside the repositories and must hold an AWO token scoped to that document. Replace the sample paths, document ID and revision with your own values.

```json
{
  "version": 1,
  "projects": {
    "student": {"manager": "student-lead", "root": "C:/work/student"},
    "materials": {"manager": "materials-lead", "root": "C:/work/materials", "state": "C:/awo/materials-state"}
  },
  "exports": {
    "calibration": {"source": "materials", "recipient": "student", "doc_id": "<published document ID>", "revision_sha256": "<published revision digest>", "token_file": "C:/awo/private/calibration.token"}
  }
}
```

    python scripts/maid.py --thread C:/work/awo-demo --directory C:/awo/directory.json --project student --material calibration submit --spec C:/work/spec.json

The spec must point to the student's repository. At submission MAID checks the source grant and copies the selected revision into the prepared worker prompt. A later revocation applies to future exports. Keep the conversation directory private because it holds the exported copy. Omit `--material` for a task that needs only the requesting project.

Use the same start and status commands as above. The result lives on the task clone's target ref. Status includes the project, manager, source citation and reviewed evidence identifier. A manager name identifies the selected route. This version supports one bounded task per local conversation.
