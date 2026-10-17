# report.md template

`ghost report` produces the numeric body. The report-writer adds the summary and findings sections. Keep numbers identical to `report.json`.

```
# <api> report (<timestamp>)

## Summary
3-6 sentences: scope (endpoints, cases run, skipped), headline result, top risks.
Stay non-blocking: "findings" and "recommendations", never "build failed".

## Prioritised findings
1. [FLAG] METHOD /path - one-line finding
   - Evidence: status/body or drift item (from report.json)
   - Cause: from explanations.json (class, confidence) or "not explained"
   - Recommendation: concrete next step
(order: 5XX, breaking DRIFT, CASE_FAILED, UNDECLARED, FLAKY, additive DRIFT, SLOW)

## Drift
Counts by breaking / additive / cosmetic, and the breaking items.

## Coverage and skips
Cases run, dropped by validator, destructive cases skipped (allow_writes off).

## Degraded stages
Any `{"stage","error"}` records; omit the section if none.

## Notes
Timing is advisory (warm-up discarded, p95 needs >= 5 samples).
```

## Style
- Plain language; one finding per bullet; reference flags by name (see flags.md).
- No secrets, no literal tokens; repro curls keep `$TOKEN`.
- Do not change counts or reclassify flags.
