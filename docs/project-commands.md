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

### Schedule tasks in the same project

For shared scheduling, point both tasks at the same enabled controller ledger, bound project root and project-wide controller credential. Register the workers, reviewer, capabilities and budget policy in that ledger first. The secretary calls the following command once per envelope (task.create, scheduler.enqueue, then scheduler.claim). Each envelope uses the existing service format with a stable command_id, expected_epoch, tool and payload. See the [scheduler contract](../references/awo-scheduler.md) for request fields.

```powershell
$domain = @("--state", $state, "--project-root", $project, "--token-file", $controllerToken, "--expected-project-id", $projectId)
python scripts/awo_project.py schedule @domain command --file create.json
python scripts/awo_project.py schedule @domain command --file enqueue.json
python scripts/awo_project.py schedule @domain command --file claim.json
python scripts/awo_project.py schedule @domain dispatch --assignment $assignment --epoch $epoch
python scripts/awo_project.py schedule @domain status --task $task
python scripts/awo_project.py schedule @domain reconcile --assignment $assignment --epoch $epoch
```

Queue requests can declare literal project-relative `write_paths`, for example `["src/report.md"]`. The scheduler checks these claims, working directories, worker capacity, requested resources and shared budget before reservation. The participating workers must declare the paths they intend to modify. Separate ledger files govern independent scheduling pools.

Reuse the original command ID after an interrupted response. The ledger returns the original result. A WAITING result leaves the task queued. Use a fresh claim command ID after capacity changes. On restart, read the original task and assignment, then reconcile any attempted send. Review and close the assignment through the existing AWO review flow. Closure releases its claims. A delivered result alone retains them.
