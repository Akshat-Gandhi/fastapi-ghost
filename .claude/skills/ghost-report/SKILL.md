---
name: ghost-report
description: Stage S8 of fastapi-ghost. Use to build the final non-blocking report - runs `ghost report` and has the report-writer agent add a summary and prioritised findings to `report.md` and `report.json`. Triggers - "write the report", "summarise the run", "what did ghost find".
---

# ghost-report (S8)

## Steps
1. Run `bash .claude/skills/ghost-report/scripts/report.sh <api>`. It writes `.ghost/<api>/report.json`, `report.md` and a snapshot in `runs/<ts>/`.
2. Delegate to `report-writer`: read `report.json`, `explanations.json` (if S7 ran), `drift.json`, `validation.json`; add an executive summary and prioritised findings to `report.md` using `references/report-template.md`.
3. Numbers (counts, percentiles, statuses) come from `report.json` only; never recompute or alter.
4. If earlier stages wrote `{"stage":...,"error":...}`, list them under "Degraded stages".

## Rules
- Non-blocking language only (findings, recommendations); the run never fails a build.
- No secrets or token values.

References: `references/report-template.md`, `references/flags.md`.
