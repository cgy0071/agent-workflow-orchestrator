# Search authorized research materials

The operator can search named published documents and verified SRC excerpts together.
This local CLI displays material to the credential holder. Its output is not a grant
to disclose it to another project or to inject it into a worker. Re-query at use time.
It cannot retract previously printed text. MAID needs no change for its existing
named-document requests.

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
task-specific verification. Each reader obtains only explicitly selected material;
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

Version 1 returns up to five excerpts with text hashes and source citations.
Document offsets count Unicode characters; plugin locators come from the registered
source. Neither a ranking nor a matching hash verifies a scientific conclusion.
Limits: 16 explicit inputs, 256 passages, 8192 vocabulary terms; narrow the selection
on overflow. Zotero and persistent indexes are outside this increment.
