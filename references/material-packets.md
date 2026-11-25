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
