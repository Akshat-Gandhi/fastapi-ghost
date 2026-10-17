# Validation checklist

Output: `.ghost/<api>/validation.json`: `cases.<id>.verdict` (`ok|fix|drop`) with `problems`/`warnings`, plus a `summary` count. Semantic judgements go in `validation_review.json` (stricter only) and are merged by re-running `ghost validate`.

Mechanical problem codes: `unknown_category`, `bad_expected_status`, `expected_status_does_not_match_category`, `destructive_mislabelled`, `writes_disabled`, `host_not_allowed_for_writes`, `blocked_path`, `missing_path_params:*`, `unknown_path_params:*`, `credentials_in_case`, `happy_body_violates_schema`, `invalid_case_body_is_valid`, `duplicate`. Stateful fields: `bad_setup`, `setup_unknown:<id>`, `setup_self`, `setup_cycle`, `setup_not_runnable:<id>` (a setup case is not `ok`), `bad_captures`, `capture_unknown:<name>` (placeholder with no `captures` in the setup chain), `bad_teardown`, `teardown_unknown_endpoint`, `teardown_destructive_mislabelled` (`destructive` must be true exactly for non-GET methods), `teardown_path_params_mismatch:*`, `teardown_needs_capture:<name>`, `teardown_not_allowed` (writes off, host not allowed or blocked path: `drop`). A body containing `{{nonce}}` is schema-checked with a same-length stand-in; bodies using captured values are not schema-checked (warning).

`writes_disabled`, `host_not_allowed_for_writes`, `blocked_path`, `teardown_not_allowed` and `duplicate` mean `drop`; the rest mean `fix`.

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
