# Configure your MAID secretary

Run these examples from a checkout containing `apps/maid/`. The student repository produces a result using one published calibration document from the materials project. Substitute local paths and keep the thread outside both repositories.

## Secretary and terminal

The default secretary is agy. Pass `--secretary examples/maid-secretary-claude.json` to chat, request or direct to select Claude. A trusted local JSON profile supplies a turn command array (prompt on stdin, reply on stdout) and an optional direct command array for your terminal. Commands may use {thread} and {model}. For persistent conversations, the adapter must save and resume its session by thread. The Claude example starts fresh sessions.

Sign in to agy, list the available models and pass one model ID to chat. Send a message and type `exit` before using direct in the terminal of your choice to reopen the native Gemini session.

```powershell
agy models
python apps/maid/maid.py --thread D:/MAID/threads/student-01 chat --model gemini-3.8-flash-low
python apps/maid/maid.py direct --thread D:/MAID/threads/student-01
```

Existing interactive agents can be reached in their own terminals. Use status for one-shot automated workers.

## Projects, personnel and sharing

Copy the [directory example](../examples/maid-directory.json) to directory.json. For each project set root to its Git root and manager to the name recorded on the route. The name alone grants no access. Use the [full task spec](../references/project-entry.md) to set repository, a new work_root matching the thread, worker and reviewer executables, allowed_paths and verify_argv. Both automated roles currently use authenticated Codex processes.

In exports.calibration replace the document ID, published revision SHA-256 and path to the source-issued document credential. Keep the credential outside the recipient repository. The grant allows the named recipient to read that revision. Reverse sharing requires a separate grant.

```powershell
python apps/maid/maid.py --thread D:/MAID/threads/student-01 --directory directory.json --project student --material calibration submit --spec spec.json
python apps/maid/maid.py --thread D:/MAID/threads/student-01 start --confirm-execute
python apps/maid/maid.py --thread D:/MAID/threads/student-01 status
```

Check the route before starting. After reopening, inspect review and accepted_evidence_id. Use routed submit for cross-project materials; the natural-language request command currently takes a local project profile.

## Knowledge location

The project root identifies the project. State points to its initialized AWO state directory. For the enabled host plugin, configure projects.<project ID>.vault and source_root in the state's kb-host.json to point to the record store and evidence directory respectively. The host also needs a pinned task revision, permitted read operations and source grants. Replace the task and credential below with authorized values.

```powershell
python scripts/awo_project.py inventory --state D:/MAID/state/materials --project-root D:/Research/materials --task T-A --token-file D:/MAID/credentials/task-reader.token
```

See [project commands](project-commands.md) for document retrieval using a document-scoped credential and for project role grants.
