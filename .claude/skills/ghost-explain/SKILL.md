---
name: ghost-explain
description: Stage S7 of fastapi-ghost. Use to turn failures into evidence bundles (`ghost bundles`) and have the failure-explainer agent write per-failure diagnoses to `explanations.json`. Triggers - "why did this fail", "explain failures", "root cause", "repro for this 500". Skipped when S6 found nothing wrong.
---

# ghost-explain (S7)

## Steps
1. If `results/results.json` has no failing or flagged results, skip this stage.
2. Run `bash .claude/skills/ghost-explain/scripts/bundles.sh <api>`. It writes one evidence file per failure to `.ghost/<api>/results/bundles/*.json`.
3. Delegate to `failure-explainer`. For each bundle it reads the bundle, `contract.json`, `source_errors.json`, and the source around `raise_site` if present, then writes `.ghost/<api>/explanations.json` following `references/explanation-format.md`.
4. Classify each failure using `references/failure-taxonomy.md`. Without evidence, say "inconclusive".

## Rules
- No requests to the API. Evidence comes from bundles and source only.
- Repro curl uses a `$TOKEN` placeholder, never a value.

References: `references/failure-taxonomy.md`, `references/explanation-format.md`.
