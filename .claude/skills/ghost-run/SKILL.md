---
name: ghost-run
description: Stage S6 of fastapi-ghost (script only, no agent). Use to execute validated cases against the live API with `ghost exec` and produce `results/results.json`. Triggers - "run the cases", "execute tests", "probe the API". Refuses to run without validation verdict ok.
---

# ghost-run (S6)

## Steps
1. Gate: read `.ghost/<api>/validation.json`. If the verdict is not `ok`, stop and report; do not run.
2. Gate: check `api.toml`. Env var names for tokens must be set in the environment (do not print values). Writes run only if `allow_writes = true`.
3. Run `bash .claude/skills/ghost-run/scripts/exec.sh <api>`.
4. Read `.ghost/<api>/results/results.json` and give a count by status and by flag (see `references/flags.md`).
5. If nothing is wrong, S7 is skipped; go to S8.

## Rules
- The runner (code, not prompt) enforces `allowed_hosts`, `blocked_paths`, `max_rps`, a per-run request cap, and write gating.
- Never call the API with curl or other tools.
- Exit code is 0 on findings.
- `results/` and `runs/` are gitignored; tokens are scrubbed from stored strings.

References: `references/flags.md`, `references/safety-gates.md`.
