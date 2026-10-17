---
description: Run the full fastapi-ghost workflow (S0..S8) for one API
argument-hint: <api-slug>
---

Run the fastapi-ghost workflow for API `$ARGUMENTS` from this session. Subagents cannot spawn subagents, so you orchestrate every stage. Artifacts live in `.ghost/$ARGUMENTS/`. Never call the API yourself; only `ghost` verbs and the skill scripts talk to it. Exit 0 on findings is normal. The final result is a non-blocking report.

If `$ARGUMENTS` is empty, ask for the slug and stop.

## Stages
- S0 init (you): if `.ghost/$ARGUMENTS/api.toml` is missing, ask the user for the base URL, optional source dir, and whether writes are allowed (default no). Run `ghost init $ARGUMENTS --url URL [--source DIR] [--writes]`. Remind them tokens go in env vars named in api.toml.
- S1 discover: skill `ghost-discover`. Gate: spec.json and endpoints.json exist; else record the degraded stage and continue where possible.
- S2 scan: skill `ghost-contract` (scan.sh). Skip if no source root; contract then relies on spec and observation.
- S3 contract: agent `contract-keeper` + skill `ghost-contract`. Gate: contract.json and drift.json exist.
- S4 write cases: run gen.sh (skill `ghost-cases`), then agent `case-writer`.
- S5 validate: agent `case-validator` + skill `ghost-validate`. Gate: verdict `ok`. If any case is `fix`, send those ids back to `case-writer` with the reasons, then re-validate. Max 2 fix loops; afterwards drop remaining `fix` cases and note it. Destructive cases are dropped when `allow_writes` is false.
- S6 execute: skill `ghost-run` (script only). HARD GATE: never run unless validation verdict is `ok`. Writes only with `allow_writes = true`.
- S7 explain: agent `failure-explainer` + skill `ghost-explain`. Skip if S6 found nothing wrong.
- S8 report: agent `report-writer` + skill `ghost-report`.

## Failure handling
If a stage fails it writes `{"stage":..., "error":...}`; report it, let later stages degrade, and keep going. A non-zero exit from `ghost` means a ghost bug or missing config: stop and tell the user. Shortcut for the deterministic path without agents: `ghost pipeline --api $ARGUMENTS`.

## Finish
Show the path to `.ghost/$ARGUMENTS/report.md` and a 5-line summary (endpoints, cases run, flags by type, drift counts, degraded stages). Use non-blocking language. Never print token values.

## Practical notes (from running this on real projects)
- Paths like `.claude/skills/...` are relative to the repository that holds `.claude`; `.ghost/` lives under `GHOST_HOME` (default: the current directory). Run the scripts by absolute path and set `GHOST_HOME` and `GHOST_BIN` (the `ghost` executable to use) when the two are in different places.
- A "fix loop" is one round of: writer edits the listed `fix` cases, then the validator re-validates. At most 2 rounds; edits you make before the first `ghost validate` do not count.
- Multipart/file-upload bodies are not supported yet; such endpoints only get null-body cases.
- If the API's real code lives in a library outside `source_root`, `scan` finds no raise sites; read the library source and fill `contract.json` by hand (the contract-keeper does this).
- `ghost scan` prints a warning (and writes `unparsed_files`) when it could not read some source files, e.g. newer Python syntax than the interpreter running Ghost.
