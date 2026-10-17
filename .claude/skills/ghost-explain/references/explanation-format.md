# explanations.json

A list with one entry per failing case; `case_id` links it to the report:

```json
{"schema_version": 1, "explanations": [{
  "case_id": "get_todo.happy.1",
  "class": "API bug",
  "hypothesis": "handler dereferences None when the todo has no owner",
  "confidence": "medium",
  "location": "app/routes/todos.py:57",
  "user_sees": {"status": 500, "body": "Internal Server Error"},
  "internal_code": null,
  "fix": "guard for missing owner before attribute access",
  "repro": "curl -s -H \"Authorization: Bearer $TOKEN\" http://localhost:8000/todos/1"
}]}
```

## Field rules
- `class`: one of the taxonomy classes (including `Inconclusive`).
- `confidence`: `low|medium|high`. Use `low` or class `Inconclusive` when evidence is thin.
- `location`: `file:line` or `null`. Never invented.
- `user_sees`: the exact status and body the end user receives, copied from the bundle. For a 422, the real `detail` list.
- `internal_code`: application error code from the body or contract, else `null`.
- `fix`: a concrete change, or what to check when inconclusive.
- `repro`: a curl line with `$TOKEN` placeholder; method, path and body from the bundle.
- Never include secrets or literal tokens.
