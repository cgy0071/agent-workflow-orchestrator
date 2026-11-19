# Native framework: evidence and remaining work (2026-11-19)

The initial architecture delivery includes the 13 requested documents, two additional module designs and nine numbered ADRs plus their index. All 29 design manifest hashes matched. Evidence below is from separate Windows offline candidates.

| Area | Verified | Remaining |
|---|---|---|
| Core and models | Project-specific disable; trusted local provider install and activation with unchanged runtime/profile | Untrusted plugin isolation, live provider interoperability, routing and billing |
| Runtime, registry and skills | Reviewed child result consumed by its own resumed parent; approved enrollment; resumed session retains original skill | Multi-host recovery, production identity and upgrades at arbitrary turns |
| Workflow and harness | Fixed-capacity wait/recovery without duplicate submission; simulated Grok Build timeout cleanup | Dynamic capacity, live wake and actual Grok Build sessions |
| Observability | Bounded events, project queries and redacted exports | Production telemetry |
| Knowledge | Backend swap with valid citations; scoped projection reads and refusal after revocation | Live sources and clean Windows first use |

CLI SHA-256: 9a0caf4e84a9c332c87af18e7caedcc5f308d0a7126cdfec7264088543476656. Engineering tested concurrent release ordering; independent validation repeated sequential revocation, credential separation, legacy refusals and exact returned bytes. Previously delivered bytes cannot be recalled.

Next: pin one combined Windows candidate, replay the cross-module checks and test the README on a clean Windows host. The native CLI candidate is not distributed with this source preview.

## Windows stable release gates

1. Declare supported Python and Windows versions, trusted plugins, and the provider/harness combinations that actually ship. Keep experimental adapters optional.
2. Build one versioned distribution from a clean tag. Pin its dependencies and remove local paths, private receipts and credentials. Test the exact artifact by hash.
3. On that artifact, run one complete task: authorize, retrieve, cite, execute, independently review and write back. Then revoke access, restart after interruption, and prove there is no duplicate send or write.
4. Exercise a supported real model and harness on a public fixture with an explicit budget before advertising live support. Run clean Windows install, upgrade, backup/restore and rollback from the public instructions.
5. Publish the support matrix, known issues, version and checksum after all mandatory gates pass. A historical test of a different archive cannot close a gate.
