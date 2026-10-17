# Safety gates (S6)

Enforced in code by the runner, not by prompts. Agents and the main session must also respect them.

| Gate | Where configured | Behaviour |
|---|---|---|
| Validation verdict | `validation.json` | each case runs only with verdict `ok`; `fix` and `drop` cases are skipped |
| Writes | `allow_writes` in `api.toml` (set at init with `--writes`) | default off; destructive cases are not sent when off |
| Host | `allowed_hosts` | requests to other hosts refused |
| Paths | `blocked_paths` | matching paths never requested |
| Rate | `max_rps` | requests throttled |
| Volume | per-run request cap | run stops at the cap |

## Secrets
- `api.toml` stores env var NAMES only. Tokens come from the environment at run time.
- Headers and known keys are redacted; literal token values are scrubbed from every stored string.
- `results/` and `runs/` are gitignored.

## Timing
- A warm-up request precedes timing. p95 needs at least 5 samples. Timing findings are advisory.

## Failure handling
A failed stage writes `{"stage": ..., "error": ...}`; later stages degrade. Final exit is 0. Non-zero is reserved for a ghost bug or missing config; report it and stop instead of working around it.
