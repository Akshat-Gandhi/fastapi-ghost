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

## Stateful fields (all optional)
Use them so repeat runs against the same server stay quiet. `ghost gen` already writes them for create/item-path pairs; copy the pattern, do not invent new ones.

```json
{"id": "post_todos.happy.1", "category": "happy", "destructive": true,
 "request": {"body": {"title": "ghost-{{nonce}}"}, "auth": "valid", "path_params": {}, "query": {}, "headers": {}},
 "captures": {"todo_id": "id"},
 "teardown": {"method": "DELETE", "path": "/todos/{todo_id}", "path_params": {"todo_id": "{{todo_id}}"},
              "destructive": true, "auth": "valid"}}
{"id": "get_todos_todo_id.happy.1", "category": "happy", "setup": ["post_todos.happy.1"],
 "request": {"path_params": {"todo_id": "{{todo_id}}"}, "auth": "valid", "query": {}, "headers": {}}}
```
- Placeholders `{{name}}` work in any string of `request` (path_params, query, headers, body). A string that is exactly one placeholder keeps the captured value's type (an int id stays an int).
- `{{nonce}}` is a 6 character random string, the same for the whole run; `{{nonce:4}}` is its first 4 characters. Use it in unique-valued fields of happy and conflict bodies. The resulting value must still satisfy the schema (length, format, enum). Do not use it for invalid_payload cases.
- `setup`: ids of cases that run first (once per run). They must exist in some case file, have verdict `ok`, and not form a cycle. A placeholder `{{name}}` in a case needs a `captures` of that name in its `setup` chain.
- `captures`: `{name: "dotted.path"}` into the JSON response of a 2xx reply (`id`, `data.id`, `items.0.id`). Only strings and ints are captured. If nothing was captured the dependent case is reported as `skipped`, not failed.
- `teardown`: one request that deletes what the case created (`method`, `path` from the spec, `path_params` using this case's own captures, `auth`, `destructive`). `destructive` must be honest: true for any non-GET method. Runs after all cases, newest first; failures are reported but never fail the run.
- A conflict case (409) should repeat the happy POST body (same `{{nonce}}` values) and list the happy case in `setup`; the nonce is identical within a run, so the duplicate really is a duplicate.
- Never write a DELETE happy case against an id Ghost did not create (seed data). Use a chain, or leave it out.

## Limits
About 12 cases per endpoint. No code, no secrets, no endpoints absent from the spec.
