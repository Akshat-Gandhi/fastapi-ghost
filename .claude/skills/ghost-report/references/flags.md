# Result flags

Flags are attached to results/report rows. The runner sets them; agents must not change them. Exact numeric thresholds live in the runner code; do not state numbers not shown in the output.

| Flag | Meaning | Severity guide |
|---|---|---|
| CASE_FAILED | the response status was not in the case's `expect.status` | investigate: API bug, case bug, or environment |
| 5XX | a 5xx response was returned | high; always an API-side problem to explain |
| UNDECLARED | a status or body shape was observed that the spec/contract does not declare | medium; usually a spec gap (e.g. undeclared HTTPException) |
| DRIFT | spec or contract changed since the previous run (from `drift.json`) | by drift class: breaking high, additive low, cosmetic info |
| SLOW | timing above the runner's advisory threshold for the endpoint | advisory only; needs >= 5 samples after warm-up |
| FLAKY | the same case gave different results across repeats | medium; do not blame the API before ruling out environment |

Notes
- Expected rejections (e.g. 401 on `missing_auth`) are passes, not failures.
- A case can carry several flags.
- Flags never make the run exit non-zero.
