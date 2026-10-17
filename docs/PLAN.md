# fastapi-ghost: plan for the Claude layer

Design rules
1. Cases are data, not code. The writer agent emits JSON case files; a deterministic Python runner executes them.
2. Scripts do the mechanical work; agents do the judgement. Agents never call the target API directly. They use `ghost` CLI verbs.
3. Always exit 0 on findings. Only a ghost bug or missing config may exit non-zero. Reports are non-blocking.
4. Auth stays out of artifacts. `api.toml` stores env var NAMES, never token values.

## CLI verbs (all take `--api <slug>`; artifacts live in `.ghost/<api>/`)

| Verb | Stage | Writes |
|---|---|---|
| `ghost init <api> --url URL [--source DIR] [--writes]` | S0 | `api.toml` |
| `ghost discover` | S1 | `spec.json`, `spec.prev.json`, `endpoints.json` |
| `ghost scan` | S2 | `source_errors.json` |
| `ghost contract` | S3 | `contract.json`, `drift.json` |
| `ghost gen` | S4 | `cases/<op_id>.json` (baseline cases) |
| `ghost validate` | S5 | `validation.json` |
| `ghost exec` | S6 | `results/results.json` |
| `ghost bundles` | S7 | `results/bundles/*.json` (evidence per failure) |
| `ghost report` | S8 | `report.json`, `report.md`, `runs/<ts>/` |
| `ghost pipeline` | all | the deterministic path S1..S8, no agents |

## Stages

| Stage | Who | Input | Output |
|---|---|---|---|
| S0 init | main session | user answers | api.toml |
| S1 discover | skill `ghost-discover` | live /openapi.json | spec.json, endpoints.json |
| S2 scan | skill `ghost-contract` | source root | source_errors.json |
| S3 contract | agent `contract-keeper` + skill `ghost-contract` | spec, source_errors, old contract | contract.json, drift.json |
| S4 write cases | agent `case-writer` + skill `ghost-cases` | contract, endpoints | cases/*.json |
| S5 validate | agent `case-validator` + skill `ghost-validate` | cases, contract | validation.json |
| S6 execute | skill `ghost-run` (script only) | validated cases, api.toml, env tokens | results.json |
| S7 explain | agent `failure-explainer` + skill `ghost-explain` | results, contract, source_errors | explanations.json |
| S8 report | agent `report-writer` + skill `ghost-report` | everything | report.md, report.json |

Hard gates: S6 never runs without S5 verdict `ok`. Writes run only with `allow_writes = true`. S7 is skipped when S6 finds nothing wrong. A failed stage writes `{"stage":..., "error":...}` and later stages degrade; final exit is 0. Validator may send a case back to the writer at most 2 times.

## Case file (`cases/<op_id>.json`)

```
{"schema_version":1,"operation_id","method","path","cases":[{
  "id","category","title",
  "request":{"path_params":{},"query":{},"headers":{},"body":null,"auth":"none|valid|bad"},
  "expect":{"status":[int]},"destructive":bool,"origin":"baseline|agent"}]}
```
Categories: happy, invalid_payload, missing_auth, bad_auth, not_found, conflict, observe_5xx.

## Contract (`contract.json`)

Keyed by `"METHOD /path"`: `request_example`, `expected` (list of `{status, source: spec|code|observed, error_body_shape, internal_code?, raise_site?}`), `unexpected`, `auth`, `last_verified`, `spec_hash`, optional `pinned: true` fields that tools must not overwrite.

## Agents (`.claude/agents/`)

- `case-writer` (sonnet): writes cases JSON from the contract; adds semantic cases the schema cannot give; sets `destructive` honestly; max ~12 cases per endpoint. Must not write test code, put secrets in cases, call the API, edit the contract, or invent endpoints.
- `case-validator` (sonnet): read-only reviewer; runs `ghost validate`, judges semantics, marks each case `ok|fix|drop`. Must not edit cases, send requests, or approve destructive cases when writes are off.
- `contract-keeper` (sonnet): builds/updates the contract and drift, reads source around raise sites for exact error bodies, classifies drift breaking/additive/cosmetic. Must not overwrite `pinned` fields, edit source, or guess unseen bodies (use `"unknown"`).
- `failure-explainer` (opus): per failure, gives cause hypothesis, confidence, file:line, the exact error body the end user sees, internal code, fix, repro curl with `$TOKEN` placeholder. Distinguishes API bug / test-case bug / environment / flaky. Must say "inconclusive" without evidence.
- `report-writer` (sonnet): runs `ghost report`, adds summary and prioritised findings. Must not change numbers, use blocking language, or include secrets.

Subagents cannot spawn subagents, so the main session orchestrates via `/ghost-api <api>`.

## Skills (`.claude/skills/<name>/`: SKILL.md, references/, scripts/)

`ghost-discover`, `ghost-contract`, `ghost-cases`, `ghost-validate`, `ghost-run`, `ghost-explain`, `ghost-report`. Scripts are thin wrappers over the `ghost` verbs; logic lives in `src/ghost` and is unit-tested.

## Risks

- Writes: default off; enforced in writer, validator AND runner (code, not prompt). `allowed_hosts`, `blocked_paths`, `max_rps`, request cap per run.
- Secrets: env var names only; redact headers and known keys; scrub literal token values from every stored string; gitignore `results/` and `runs/`.
- Timing: warm-up request, p95 needs >= 5 samples, timing findings are advisory.
- Static scan misses dynamic raises: mark `source: "unknown"`.
