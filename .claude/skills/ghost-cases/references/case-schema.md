# cases/<op_id>.json

```json
{"schema_version": 1, "operation_id": "create_todo_todos_post", "method": "POST", "path": "/todos",
 "cases": [{
   "id": "create_todo.happy.1",
   "category": "happy",
   "title": "create with valid body",
   "request": {"path_params": {}, "query": {}, "headers": {},
               "body": {"title": "buy milk"}, "auth": "valid"},
   "expect": {"status": [201]},
   "destructive": true,
   "origin": "baseline"
 }]}
```

## Fields
- `id`: unique within the file; stable so reruns and fix loops can refer to it.
- `category`: one of `happy, invalid_payload, missing_auth, bad_auth, not_found, conflict, observe_5xx`.
- `request.auth`: `none` (send no credentials), `valid` (runner injects token from the env var named in `api.toml`), `bad` (runner injects an invalid credential). Never put a real or fake token in `headers`.
- `request.path_params`: values substituted into `path` templates.
- `expect.status`: list of acceptable status ints.
- `destructive`: true if the request can change server state. Runner skips destructive cases unless `allow_writes = true`.
- `origin`: `baseline` (from `ghost gen`) or `agent` (from case-writer).

## Limits
About 12 cases per endpoint. No code, no secrets, no endpoints absent from the spec.
