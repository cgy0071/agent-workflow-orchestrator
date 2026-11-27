# Search authorized research materials

The operator can search authorized published documents and verified SRC excerpts together.
This local CLI displays material to the credential holder. Its output is not a grant
to disclose it to another project or to inject it into a worker. Re-query at use time.
It cannot retract previously printed text. MAID needs no change for its existing
named-document requests.

    python -B scripts/awo_project.py materials --state STATE --project-root PROJECT --doc-token DOC_TOKEN_FILE --kb-token TASK_TOKEN_FILE --task TASK_ID --query "inference throughput" --top-k 3

    python -B scripts/material_candidates.py sample.md
    python -B scripts/material_candidates.py sample.pdf
    python -B scripts/awo_project.py materials --state STATE --project-root PROJECT --doc-token DOC_TOKEN_FILE --doc DOC_ID --kb-token TASK_TOKEN_FILE --task TASK_ID --note SRC_ID --pdf PDF_SRC_ID --query "measured latency" --provider lsa

Extraction is an operator action on one chosen file. It never follows Obsidian links,
scans a vault or writes a source. Markdown retains UTF-8 byte ranges including CRLF
offsets; PDF candidates refer to physical pages starting at one. An image-only page
fails the whole extraction. Review the candidates, supply the correct source author,
project and version, and use existing approval and exact-source grant procedures.
Candidates confer no authority. Repeated PDF quotes may fail the plugin's uniqueness
check: refine the excerpt before approval. PDF parsing assumes trusted local documents.

The document service owns its published revisions. The plugin owns SRCs, policy and
task-specific verification. With no source IDs, discovery pages through visible, usable published documents and
checks task-granted SRCs in the current project. Either credential can be used alone;
supply --task with the KB credential. A single-document token cannot list its peers;
both credentials for a mixed packet must identify the same project and recipient.
All candidates are read again before emitting a result, because even a non-winning
excerpt can influence the ranking. Reads are sequential, not an atomic transaction
across stores. There is no durable text/vector cache or synchronization write.

The default scorer is offline latent semantic analysis (NumPy): fit term weights and
a truncated singular-value decomposition to this authorized selection only. No
downloaded model, internet or paid service is needed. With little co-occurrence
evidence it cannot infer arbitrary synonyms. Use --provider lexical for the
standard-library TF-IDF cosine baseline. Install NumPy in the chosen environment for
lsa; PDF extraction additionally needs pypdf, and plugin reads need a compatible,
enabled awo-kb deployment. Missing providers do not silently change rankings.
A trusted local scorer can implement (passage_texts, query, provider_id) -> scores;
never hand authorized material to a remote service without separate permission.

By default the CLI returns up to five excerpts with relevance scores, text hashes and
source citations. Use --top-k 1 through 32. No positive match yields an empty list.
The LSA provider blends lexical and latent cosine similarity. Chinese characters and
bigrams and English word trigrams permit partial matches; scores are not probabilities.
Document offsets count Unicode characters; plugin locators come from the registered
source. Neither a ranking nor a matching hash verifies a scientific conclusion.
Limits: 16 explicit inputs or 256 discovered sources, 256 passages, 8192 vocabulary terms; exceeding a limit fails the query without partial results. Narrow the selection
on overflow. Zotero and persistent indexes are outside this increment.

## Searching English papers from a Chinese query

Without any service, the operator or secretary can supply optional translation
terms, e.g. append `--provider lexical --expand "inference throughput" --expand "frames per second"`
to the materials command above while keeping the original Chinese query. Expansion
only affects ranking, never the corpus or citations. Terms are caller-supplied,
not automatically translated. Up to 12 terms, 96 characters each; combined query
and terms must fit the existing 512-character limit. Default LSA also accepts them.

For multilingual embeddings, explicitly append these options (example for a
locally configured Ollama endpoint with bge-m3 already available):

    --provider http --embedding-url http://127.0.0.1:11434/v1/embeddings --embedding-model bge-m3

For LM Studio use the endpoint and loaded model ID from your server. For an online
service, specify its complete HTTPS embeddings URL and model ID, and explicitly
add `--allow-remote-embedding` only if disclosure of the query and **all eligible
passages**, not just the winners, to that destination is authorized. Set
`AWO_EMBEDDING_API_KEY` in the process environment if the selected endpoint needs
a bearer key; unset it when changing endpoints. The adapter sends no project tokens,
citations or paths as metadata. It uses one POST, no redirects, proxies or automatic
retries. Unavailable services and invalid vectors fail the query; rerun explicitly
with lexical plus expansion for offline use. Ranking uses cosine similarity.
Cross-language quality depends on the selected model.

Before sending, AWO checks the source snapshots again and aborts on change.
Sequential authority checks and HTTP delivery are not an atomic transaction:
revocation stops subsequent queries but cannot recall text already sent.

## Project terminology and secretary suggestions

The operator may create a UTF-8 JSON file at
`STATE/glossaries/PROJECT_ID.json`. Use the authenticated project's ID (also
returned in document citations), not its directory name. Example:

```json
{"\u63a8\u7406\u541e\u5410\u91cf": {"terms": ["inference throughput"], "avoid": ["bandwidth"]}}
```

For a ready-to-copy example, use the [public machine learning glossary](../examples/glossary-ml-sample.json). Copy it to the project-specific JSON path above.

Keys are local aliases; use separate entries for alternative names. Matching uses
normalized case and whitespace, with longer overlapping aliases taking precedence.
All designated terms are included. For matched entries, avoid phrases are removed
from the query and rejected in suggestions; conflicting designated terms fail the query.
Avoid is a translation rule, not a ban on passages containing that word.

For uncovered text the secretary has a small built-in offline bilingual vocabulary.
It does not contact a model or guess unknown translations. A calling secretary may
supply additional translations with `--expand-for SOURCE TERM`; SOURCE must occur
in uncovered query text. Legacy unscoped `--expand` suggestions are used only if
no glossary alias matched. At most 12 secretary terms are applied.
Use `--strict-glossary` to ignore all secretary suggestions, or `--no-auto-expand`
to turn off only the built-in vocabulary. With no glossary matches strict mode
searches the original query. Successful packets include the applied terms in
`expansion.glossary` and `expansion.secretary`. These lists describe how the query
was formed, not what the paper claims. Excerpts and citations are unchanged.

## Preserve quotations and original titles

During terminology checks, polishing, or translation review, preserve direct quotations, titles in References or Bibliography, and original titles of external papers wherever they appear. Keep the source spelling, wording, capitalization, hyphens, and punctuation verbatim. Exclude these spans before applying glossary rules or bulk replacements, even if the source appears to contain an error.

For example, keep `back-propagation` in a cited title even if the surrounding prose uses `backpropagation`. Place any labeled translation or editorial note outside the quoted text or original title.

## Read and freeze task evidence (Python API)

Use `task_evidence.KnowledgeReadFacade(api, credentials, task=None)` with the
authenticated service backend. Without a task, supply a document credential
for learning. For task reads, supply the target task and the recipient's own
credentials by source kind (doc, note, pdf). The facade performs reads only.

1. Call `search(query)` and show the selected hits and their citations.
   Record `checksum(facade.selection(selected_hits))` with the draft.
2. At confirmation, call `pin(selected_hits, draft_digest, expected_selection_digest)`.
   The task facade re-reads the selection. Keep the returned
   `awo.task-evidence.v1` manifest and its digest with the confirmed task.
3. Call `resolve(manifest, expected_packet_digest)` with the digest from
   the trusted task record and the current recipient's facade. A failed
   authorization or missing exact revision stops the read. The packet digest
   provides integrity only when its expected value comes from the host.

Document locations are half-open Unicode character offsets. Plugin references
identify an immutable KB history revision and SRC record, the source file
digest, and the verified excerpt digest. PDF references also identify the
physical page. Tokens, source paths, and excerpt bodies are excluded from the
manifest. The materials CLI continues to produce its v1 search response.

### Freeze a reviewed task draft

For an evidence-backed project entry, add evidence_route to the local execution
spec before prepare. Set state, root, project_id and task to the selected
knowledge domain and task. Add separate worker and reviewer objects, each with
its authorized agent_id and absolute task_token_file; include doc_token_file
for published documents or kb: true for task-scoped plugin sources. These are
operator-owned file references, not bearer tokens. Use the same goal as the
reviewed draft. The route explicitly binds the knowledge task to this run.
Do not put personal configuration or credentials in Git.

Use the public start operation with --confirm-execute, --draft and
--draft-digest. The confirmation and pointer-only manifest are recorded
atomically in task-evidence.sqlite3 under the knowledge domain state. The
scheduler still owns dispatch. Each native recipient resolves the references
again under its own grants; successful jobs record task-evidence-receipt.json
beside job.json. Review and integration review read independently. Revocation
blocks the next read; it cannot recall data previously delivered.
