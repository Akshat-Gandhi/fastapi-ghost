---
name: ghost-contract
description: Stages S2 and S3 of fastapi-ghost. Use to scan API source for raised errors and to build or update the contract and drift report (`contract.json`, `drift.json`, `source_errors.json`). Triggers - "build the contract", "scan source for HTTPException", "what drifted since last run", "update contract". Used by the contract-keeper agent.
---

# ghost-contract (S2 scan, S3 contract)

## Steps
1. S2: run `bash .claude/skills/ghost-contract/scripts/scan.sh <api>`. It reads the configured source root and writes `.ghost/<api>/source_errors.json`. If no source root is configured, the stage is skipped; contract entries then use `source: "spec"` or `"observed"` only.
2. S3: run `bash .claude/skills/ghost-contract/scripts/contract.sh <api>`. It writes `contract.json` and `drift.json` from `spec.json`, `spec.prev.json`, `source_errors.json` and the old contract.
3. Delegate review/enrichment to the `contract-keeper` agent: it reads source around each raise site for exact error bodies and classifies each drift item breaking / additive / cosmetic.
4. Verify: every contract key is `"METHOD /path"`; every `expected` entry has `status`, `source`, `error_body_shape`; unseen bodies are `"unknown"`; `pinned: true` fields are unchanged.

## Rules
- Never overwrite fields marked `pinned: true`.
- Never edit application source.
- Static scan misses dynamic raises: mark those `source: "unknown"` rather than guessing.

References: `references/drift-rules.md`, `references/fastapi-error-shapes.md`, `references/contract-schema.md`.
