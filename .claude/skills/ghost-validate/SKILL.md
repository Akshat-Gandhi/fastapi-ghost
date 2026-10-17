---
name: ghost-validate
description: Stage S5 of fastapi-ghost. Use to review generated cases before execution - runs `ghost validate` and has the read-only case-validator agent mark each case ok, fix or drop in `validation.json`. Triggers - "validate cases", "review test cases", "can we run these safely".
---

# ghost-validate (S5)

## Steps
1. Run `bash .claude/skills/ghost-validate/scripts/validate.sh <api>`. It does mechanical checks (schema, categories, known operations, destructive vs `allow_writes`) and writes `.ghost/<api>/validation.json`.
2. Delegate to `case-validator` to judge semantics using `references/validation-checklist.md`. It writes `validation_review.json` (stricter-only `fix`/`drop` verdicts with reasons) and re-runs `validate.sh` so they merge into `validation.json`.
3. Only cases with verdict `ok` will run. `drop` cases are fine; `fix` cases are skipped until fixed.
4. If `fix` cases exist, return them to `case-writer` (main session orchestrates). Re-validate. Maximum 2 rounds; after that, remaining `fix` cases are dropped and noted.
5. S6 must not run unless the final verdict is `ok`.

## Rules
- The validator edits no cases and sends no requests.
- Destructive cases are never `ok` when `allow_writes` is false; mark them `drop`.

Reference: `references/validation-checklist.md`.
