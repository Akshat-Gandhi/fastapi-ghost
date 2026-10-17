# Validation checklist

Output: `.ghost/<api>/validation.json` with a verdict per case (`ok|fix|drop`), a reason for non-ok, and an overall verdict (`ok` when no case needs `fix`).

## Structure (mechanical)
- [ ] Valid JSON, `schema_version` is 1.
- [ ] `operation_id`, `method`, `path` match an operation in `endpoints.json`.
- [ ] Case ids unique; category in `happy, invalid_payload, missing_auth, bad_auth, not_found, conflict, observe_5xx`.
- [ ] `expect.status` non-empty list of ints; `origin` is `baseline` or `agent`.
- [ ] At most about 12 cases per endpoint.

## Safety
- [ ] No secrets or token-like strings in headers, body, query. `auth` uses only `none|valid|bad`.
- [ ] `destructive` is true for every state-changing request (POST/PUT/PATCH/DELETE, or GET with side effects noted in the spec).
- [ ] If `allow_writes` is false in `api.toml`: every destructive case is `drop`.
- [ ] Path is not in `blocked_paths`.

## Semantics (judgement)
- [ ] Request matches the category: an `invalid_payload` case really is invalid, and invalid in one way only.
- [ ] A `happy` case is actually valid per the schema and required params are filled.
- [ ] `expect.status` is consistent with the contract's `expected` for that operation and category.
- [ ] `not_found` uses an id that cannot plausibly exist.
- [ ] `conflict` has a realistic precondition; otherwise `drop`.
- [ ] Not a duplicate of another case.
- [ ] Auth category matches the contract's `auth` (no `missing_auth` for an unauthenticated endpoint).

## Verdict rules
- `fix`: the case is useful but wrong; give a concrete instruction ("body missing required field `title`").
- `drop`: unsafe, redundant, or not worth fixing.
- `ok`: everything above passes.
