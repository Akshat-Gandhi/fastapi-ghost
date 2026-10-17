# Category recipes

Derive values from the contract's `request_example` and the spec schema. Choose `expect.status` from the contract's `expected` list for that operation; if unknown, say so in `title` and use the observed status later rather than guessing.

## happy
Use `request_example`. Expect the declared success status. One case per distinct success shape (e.g. with/without optional query).

## invalid_payload (usually 422; may be 400 if the app customises)
Mutate a valid body/params, one fault per case:
- Remove each required field (one case per field, cap the total).
- Wrong type: string for int, object for string, null for non-nullable.
- Boundary: below `minimum`/`minLength`, above `maximum`/`maxLength`, empty string, empty list under `minItems`.
- Enum: a value not in `enum`.
- Format: malformed `email`, `uuid`, `date-time`, `uri`.
- Extra/unknown field when the schema forbids additional properties.
- Non-JSON or empty body where a JSON body is required.
- Bad path/query params: non-numeric id for an int path param, negative limit.
Expect `[422]` by default; record the real status (400 vs 422) via the run.

## missing_auth
`auth: "none"` on an endpoint with `auth` in the contract. Expect 401 or 403 (use what the contract says).

## bad_auth
`auth: "bad"`. Expect 401 or 403 as in the contract.

## not_found
Valid-shaped but nonexistent id in path params (e.g. a large int, a random uuid). Expect 404 where the contract has it.

## conflict
Only for create/update endpoints with uniqueness. Repeat a create with the same unique field (needs a prior create; mark `destructive: true`). Copy the happy POST body (it carries `{{nonce}}`, identical within a run) and set `"setup": ["<happy create case id>"]`, so the duplicate exists on every run and never collides with data from an earlier run. Expect 409 or the contract's code.

## observe_5xx
Semantic probes that might expose unhandled errors: very long strings, unicode/emoji, special characters, large numbers, deeply nested JSON, empty-but-valid collections. Expect the non-5xx statuses from the contract; a 5xx is a finding, not a case failure of the tool.

## Destructive handling
Any case that creates/changes/deletes data is `destructive: true`. Do not include cases that depend on pre-existing state unless the contract's `request_example` supplies it.

## Allowed expected statuses per category (checked by `ghost validate`)

| Category | Expected status must be |
|---|---|
| happy | 2xx |
| invalid_payload | 400 or 422 |
| missing_auth | 400, 401, 403 or 422 (a required token missing) |
| bad_auth | 400, 401, 403 or 423 |
| not_found | 404 |
| conflict | 400 or 409 |

Reason code `expected_status_does_not_match_category` means the case's `expect.status` is outside this table: change the category, not the status.
