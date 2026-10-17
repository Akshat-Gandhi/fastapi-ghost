# FastAPI error body shapes

## HTTPException (default handler)
`raise HTTPException(status_code=404, detail="Todo not found")` returns status 404 and:
```json
{"detail": "Todo not found"}
```
`detail` may be any JSON-serialisable value (dict/list), then the body is `{"detail": <that value>}`.
Custom `headers=` are sent as response headers (e.g. `WWW-Authenticate: Bearer` on 401).
HTTPException statuses are not added to OpenAPI automatically.

## 422 RequestValidationError (default handler)
Raised when path/query/header/body fails validation. Status 422, body:
```json
{"detail": [
  {"type": "missing", "loc": ["body", "title"], "msg": "Field required", "input": {}},
  {"type": "int_parsing", "loc": ["query", "limit"],
   "msg": "Input should be a valid integer, unable to parse string as an integer", "input": "abc"}
]}
```
`loc[0]` is `body|query|path|header|cookie`; the rest is the field path. Exact `type`/`msg` text depends on the installed pydantic version; treat it as observed, not fixed. Older FastAPI/pydantic v1 bodies omit `input`.

## Custom handlers
Apps can register `@app.exception_handler(...)` to change status (e.g. 400 instead of 422) or body shape (e.g. `{"error": {"code": ..., "message": ...}}`). If a custom shape is seen, record it in `error_body_shape` and any application code in `internal_code`. No authoritative 400-vs-422 policy exists; determine it by observation.

## Unhandled exceptions
An uncaught exception gives a 500 with a plain-text body `Internal Server Error` (unless a handler or middleware changes it). Record it under `unexpected`.

## Auth
Missing/bad credentials usually yield 401 (or 403 with some security dependencies). Which one is app-specific: record what is observed, do not assume.
