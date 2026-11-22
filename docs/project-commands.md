# Windows project commands

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
