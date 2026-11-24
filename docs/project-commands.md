# Windows project commands

**[Download the Windows project commands 0.1.0](https://github.com/cgy0071/agent-workflow-orchestrator/releases/tag/project-cli-v0.1.0)** - Under Assets, download `AWO-Windows-Project-CLI-0.1.0.zip`, extract it, and run `python -B try_project.py` there with Python 3.10+. Both retrieval backends should print `201 HV`, followed by `WINDOWS PROJECT COMMANDS PASS`. The demo needs no existing project or token.

From a source checkout, run `python scripts/awo_project.py --help`. For the published host preview ZIP, use the demo commands on its release page instead.

| Operation | Command |
|---|---|
| Grant, revoke or check a registered agent's project role | `python scripts/awo_project.py role --help` |
| Search published project documents and get revision-bound citations | `python scripts/awo_project.py retrieve --help` |
| Read the knowledge inventory for an authorized task | `python scripts/awo_project.py inventory --help` |
| Prepare, execute, inspect or recover a task | `python scripts/awo_project.py task --help`; see the [project task guide](../references/project-entry.md) |
| Check a local harness | `python scripts/awo_project.py probe codex` for a local protocol handshake; `probe claude` for a single billed marker on a configured DeepSeek Flash route |

For an existing project with a published document, set these PowerShell variables to its state directory, project root, reader token file and document ID:

```powershell
python scripts/awo_project.py retrieve --state "$state" --project-root "$project" --token-file "$readerToken" --doc "$doc" --query "Hardness" --backend sqlite --skill examples/project-domains/materials.json
```

The result includes the document ID, revision and content digest. Switch to `--backend lexical` for the same request and authorization. The declarative materials skill reads the Assay and Hardness fields; omit `--skill` for a text search. Document retrieval and the task knowledge inventory use separate authorization paths.

Use an absolute, existing directory for `--project-root`; `--state` must point to an initialized AWO state directory. Token files may use absolute or relative paths and UTF-8 with or without a BOM. The same encoding works for skill JSON. Run `role grant --help` or `role revoke --help` for required write arguments: supply the controller epoch and the revision returned by `role check`. Task inventory also requires the optional knowledge plugin.

## Look up a project across source revisions

From a source checkout, use a project-wide service credential and the content root that its controller has already bound with `project.bind_root`:

```powershell
python scripts/awo_project.py domain --state "$state" --project-root "$project" --token-file "$readerToken"
```

The command returns the ledger project ID, the canonical root path digest and the root binding revision. A commit or branch switch keeps this identity. On reopening a conversation, pass the saved ID with `--expected-project-id`. A changed ledger identity, copied state directory, mismatched root or expired credential causes the lookup to fail. A digest of a path identifies the location and does not verify the files in it.

For one secretary working with two projects, configure a separate state, bound root, credential and expected ID for each. The existing project-entry command prepares its own state per run. Those run IDs alone cannot group tasks from a long-lived project. This command reads an existing project domain and does not attach an existing task or authorize access to another project's materials.

Further integration has three steps. First, submit tasks through their selected project's controller and coordinate any shared path and harness claims before dispatch. Next, reserve and reconcile each task's budget with that controller. Finally, on restart, resolve the saved project ID and read the original assignment, review and evidence before attempting further dispatch.
