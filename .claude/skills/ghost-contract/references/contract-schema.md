# contract.json

Keyed by `"METHOD /path"` (e.g. `"GET /todos/{todo_id}"`). Per key:

| Field | Meaning |
|---|---|
| `request_example` | a request that should succeed (from spec examples or schema) |
| `expected` | list of `{status, source, error_body_shape, internal_code?, raise_site?}` |
| `unexpected` | statuses/shapes seen that are not in `expected` (e.g. 5xx) |
| `auth` | auth requirement (scheme name or none) |
| `last_verified` | timestamp of the last successful verification |
| `spec_hash` | hash of the operation's spec fragment; change means spec drift |
| `pinned` | optional `true` on a field the tools must not overwrite |

`expected[].source` values: `spec` (declared in OpenAPI), `code` (found by source scan), `observed` (seen live).
`raise_site` is `file:line` of the raise. `internal_code` is an application-level error code if the body carries one.
`error_body_shape` is the JSON shape of the error body; use the string `"unknown"` when not seen.

Example entry:
```json
"GET /todos/{todo_id}": {
  "request_example": {"path_params": {"todo_id": 1}},
  "expected": [
    {"status": 200, "source": "spec", "error_body_shape": null},
    {"status": 404, "source": "code", "error_body_shape": {"detail": "string"},
     "raise_site": "app/routes/todos.py:42"},
    {"status": 422, "source": "spec", "error_body_shape": "RequestValidationError"}
  ],
  "unexpected": [], "auth": "none",
  "last_verified": "2026-10-17T10:00:00Z", "spec_hash": "ab12..."
}
```
