---
name: ghost-cases
description: Stage S4 of fastapi-ghost. Use to generate baseline test cases with `ghost gen` and to have the case-writer agent add semantic cases to `.ghost/<api>/cases/<op_id>.json`. Triggers - "write cases", "generate test cases", "add invalid payload cases", "fix cases the validator rejected".
---

# ghost-cases (S4)

## Steps
1. Run `bash .claude/skills/ghost-cases/scripts/gen.sh <api>`. It writes baseline cases (`origin: "baseline"`) to `.ghost/<api>/cases/<op_id>.json` from `contract.json` and `endpoints.json`.
2. Delegate to the `case-writer` agent to add `origin: "agent"` cases the schema cannot provide (see recipes). Max about 12 cases per endpoint.
3. Check each file against `references/case-schema.md`: valid JSON, `schema_version: 1`, unique case ids, category from the allowed list, `expect.status` list of ints.
4. Keep reruns clean: use `{{nonce}}` for unique fields, `setup`/`captures` to reach ids Ghost created, and never target seed data with a write (see "Stateful fields" in `references/case-schema.md`).
5. Set `destructive` honestly: true for any POST/PUT/PATCH/DELETE case that changes state.
6. When the validator sends cases back (`fix`), revise only those case ids. At most 2 rounds.

## Rules
- Cases are data. Never write test code.
- Never put secrets in cases; use `"auth": "none|valid|bad"` only.
- Never call the API, edit the contract, or invent endpoints.

References: `references/case-schema.md`, `references/category-recipes.md`.
