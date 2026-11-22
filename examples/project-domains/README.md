# Read a published project document

Use a credential issued for the bound project with permission to read the selected document. A document-scoped credential works for that exact document. Task credentials are not project reader credentials. Have the document ID available from its publication receipt.

From the repository root in PowerShell:

```powershell
python scripts/project_retrieve.py --state "$state" --project-root "$project" --token-file "$tokenFile" --doc "$docId" --query alloy --backend lexical --skill examples/project-domains/materials.json
```

Set the four variables to the service state directory, bound project directory, credential file and published document ID respectively. Change only `--backend lexical` to `--backend sqlite` to use an in-memory SQLite index for this invocation. Both rank by the sum of exact case-folded word counts, breaking ties by document ID. Repeat `--doc` to search up to 16 explicit documents; the whole operation fails if any is inaccessible. The winning document is fetched again before returning its project ID, exact revision and content digest. Sources must be currently usable according to the service.

To add a domain, copy a skill JSON into your project, choose a versioned skill name, and list the exact case-sensitive field names used in your documents. Pass that path via `--skill`. No installation into Python or code execution is required. For example, `eda.json` extracts the lines beginning with `Timing:` and `Worst slack:`. Repeated or missing fields fail the operation; omit the skill to return the authorized text and citation only.

This command searches project documents that have been imported, reviewed and published in the service. It does not scan folders, create grants, or publish documents.
