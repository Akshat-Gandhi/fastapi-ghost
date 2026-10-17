# fastapi-ghost workflow (one API)

Run `/ghost-api <api>` in Claude Code. The main session orchestrates because subagents cannot spawn subagents. Scripts do mechanical work; agents do judgement; agents never call the target API directly (only `ghost` verbs do). All artifacts live in `.ghost/<api>/`.

## Stage table

| Stage | Agent | Skill | Scripts / references | Input artifacts | Output artifacts |
|---|---|---|---|---|---|
| S0 init | main session | none | `ghost init` | user answers | `api.toml` |
| S1 discover | none | ghost-discover | `discover.sh`; openapi-reading.md | live `/openapi.json`, `api.toml` | `spec.json`, `spec.prev.json`, `endpoints.json` |
| S2 scan | none | ghost-contract | `scan.sh` | source root | `source_errors.json` |
| S3 contract | contract-keeper | ghost-contract | `contract.sh`; contract-schema.md, drift-rules.md, fastapi-error-shapes.md | spec, spec.prev, source_errors, old contract | `contract.json`, `drift.json` |
| S4 write cases | case-writer | ghost-cases | `gen.sh`; case-schema.md, category-recipes.md | contract, endpoints | `cases/<op_id>.json` |
| S5 validate | case-validator | ghost-validate | `validate.sh`; validation-checklist.md | cases, contract, api.toml | `validation.json` |
| S6 execute | none (script only) | ghost-run | `exec.sh`; flags.md, safety-gates.md | validated cases, api.toml, env tokens | `results/results.json` |
| S6b bundles | none | ghost-explain | `bundles.sh` | results | `results/bundles/*.json` |
| S7 explain | failure-explainer | ghost-explain | failure-taxonomy.md, explanation-format.md | results, bundles, contract, source_errors | `explanations.json` |
| S8 report | report-writer | ghost-report | `report.sh`; report-template.md, flags.md | everything | `report.json`, `report.md`, `runs/<ts>/` |

`ghost pipeline --api <api>` runs the deterministic path S1..S8 with no agents.

## Flow

```
S0 init --> S1 discover --> S2 scan --> S3 contract --> S4 write cases
                                                            |
                                         +------------------+
                                         v
                                   S5 validate --fix (max 2)--> back to S4 (case-writer)
                                         | verdict ok
                                         v
                       [GATE: verdict ok, writes only if allow_writes]
                                         v
                                   S6 execute --nothing wrong--> S8 report
                                         | failures/flags
                                         v
                                   S7 explain --> S8 report (non-blocking)
```

## Gates and failure handling
- S6 never runs without S5 verdict `ok`.
- Writes run only with `allow_writes = true`; enforced in writer, validator and runner (code).
- Validator may send a case back to the writer at most 2 times; leftovers are dropped.
- S7 is skipped when S6 finds nothing wrong.
- A failed stage writes `{"stage":..., "error":...}`; later stages degrade; the final exit is 0. Only a ghost bug or missing config exits non-zero.
- Runner limits: `allowed_hosts`, `blocked_paths`, `max_rps`, per-run request cap. Tokens are env var names in `api.toml`; stored strings are scrubbed; `results/` and `runs/` are gitignored.

## Worked example: slug `todo`

1. `/ghost-api todo`. No `.ghost/todo/api.toml`, so the session asks: URL `http://localhost:8000`, source `../todo-app`, writes: no. It runs `ghost init todo --url http://localhost:8000 --source ../todo-app`. You export the token env var named in `api.toml`.
2. S1: `discover.sh todo` writes `spec.json` and `endpoints.json` (say 5 operations).
3. S2/S3: `scan.sh todo` finds `raise HTTPException(404)` in `GET /todos/{todo_id}`. `contract-keeper` records it as `source: "code"` with its `raise_site`, and classifies drift against `spec.prev.json` (e.g. new optional field: additive).
4. S4: `gen.sh todo` writes baseline cases; `case-writer` adds invalid_payload cases (missing `title`, wrong type) and not_found.
5. S5: `case-validator` marks `POST /todos` and `DELETE /todos/{id}` cases `drop` because writes are off; one invalid_payload case is `fix` (it was invalid in two ways). The writer fixes it; re-validation (loop 1 of 2) returns `ok`.
6. S6: `exec.sh todo` runs the remaining read-only cases; `GET /todos/999` returns 404 as expected, and one case gets `UNDECLARED` because 404 is not in the spec.
7. S7: skipped only if nothing was wrong. Here `failure-explainer` classifies the UNDECLARED as a spec gap with confidence high, citing the raise site.
8. S8: `report-writer` runs `report.sh todo` and writes `.ghost/todo/report.md` with the summary and findings. The run exits 0 regardless.
