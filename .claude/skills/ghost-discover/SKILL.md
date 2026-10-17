---
name: ghost-discover
description: Stage S1 of fastapi-ghost. Use when the user wants to fetch or refresh the live OpenAPI spec of an API and list its endpoints (`/ghost-api`, "discover endpoints", "refresh spec", "pull openapi.json"). Runs `ghost discover`.
---

# ghost-discover (S1)

Fetches the live `/openapi.json` for `<api>` and writes the spec and endpoint list. No agent is needed; this is a script-only stage.

## Steps
1. Confirm `.ghost/<api>/api.toml` exists (S0 `ghost init`). If not, stop and tell the main session to run init.
2. Run `bash .claude/skills/ghost-discover/scripts/discover.sh <api>`.
3. Check outputs under `.ghost/<api>/`: `spec.json`, `spec.prev.json` (previous spec, if any), `endpoints.json`.
4. If the fetch failed, a `{"stage":"discover","error":...}` record is written. Report it and let later stages degrade; do not retry in a loop.
5. Summarise: number of operations, any operation without an `operationId`, and whether `spec.prev.json` differs (drift is judged later in S3).

## Rules
- Never call the API yourself (no curl); only the `ghost` verb talks to it.
- Never print token values. `api.toml` holds env var names only.
- Exit code is 0 on findings; non-zero means a ghost bug or missing config.

See `references/openapi-reading.md`.
