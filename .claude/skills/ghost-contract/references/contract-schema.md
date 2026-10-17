# contract.json

Keyed by `"METHOD /path"` (e.g. `"GET /todos/{todo_id}"`). Per key:

| Field | Meaning |
|---|---|
| `request_example` | a request that should succeed (from spec examples or schema) |
| `expected` | list of `{status, source, error_body_shape, internal_code?, raise_site?, raise_via?, raise_confidence?, raise_exception?, raise_handler?}` |
| `unexpected` | statuses/shapes seen that are not in `expected` (e.g. 5xx) |
| `auth` | auth requirement (scheme name or none) |
| `last_verified` | timestamp of the last successful verification |
| `spec_hash` | hash of the operation's spec fragment; change means spec drift |
| `pinned` | optional `true` on a field the tools must not overwrite |

`expected[].source` values: `spec` (declared in OpenAPI), `code` (found by source scan), `observed` (seen live).
`raise_site` is `file:line` of the raise. It may be in a helper, service or dependency, not in the route itself:
`raise_via` is the call chain from the route as written in the code (e.g. `["services.get_item", "crud.get_by_id"]`, `[]` when the raise is in the route function) and
`raise_confidence` is `direct`, `via_call`, `via_dependency` or `ambiguous` (the name had several candidate definitions; every candidate is listed, so verify by reading the code).
`raise_exception` / `raise_handler` (`file:line`) are present when a custom exception class was raised and an exception handler decided the status; the body then follows the handler, so `error_body_shape` is `"unknown"` until observed or read from the handler.
Entries written before these fields existed lack them; treat missing as `direct` with an empty chain. `internal_code` is an application-level error code if the body carries one.
`error_body_shape` is the JSON shape of the error body; use the string `"unknown"` when not seen.

Example entry:
```json
"GET /todos/{todo_id}": {
  "request_example": {"path_params": {"todo_id": 1}},
  "expected": [
    {"status": 200, "source": "spec", "error_body_shape": null},
    {"status": 404, "source": "code", "error_body_shape": {"detail": "string"},
     "raise_site": "app/crud.py:17", "raise_via": ["get_todo", "crud.get_by_id"], "raise_confidence": "via_call"},
    {"status": 422, "source": "spec", "error_body_shape": "RequestValidationError"}
  ],
  "unexpected": [], "auth": "none",
  "last_verified": "2026-10-17T10:00:00Z", "spec_hash": "ab12..."
}
```
