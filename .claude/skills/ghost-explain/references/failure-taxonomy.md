# Failure taxonomy

Every explained failure gets exactly one class and a confidence (low/medium/high).

| Class | Meaning | Typical evidence |
|---|---|---|
| API bug | the API behaves wrongly for a valid or reasonably invalid request | 5xx on valid input; stack/raise site mismatched with contract; wrong status for the documented rule |
| Spec gap | API behaviour is reasonable but the spec/contract does not declare it | UNDECLARED status; HTTPException with no `responses=` |
| Test-case bug | the case is wrong | invalid request when `happy` intended; wrong `expect.status`; missing precondition (e.g. 404 for an id never created) |
| Environment | infrastructure, data or config problem | connection errors, timeouts across many endpoints, expired token, empty DB |
| Flaky | non-deterministic result | same case gave different statuses across repeats |
| Inconclusive | evidence insufficient | say what additional evidence would decide it |

## Quick heuristics
- 401/403 on `valid` auth for every endpoint: environment (token), not API bug.
- 422 on a `happy` case: first suspect the case; compare against `request_example`.
- 404 on a `happy` GET by id: test-case bug (no such resource) before API bug.
- 500 with plain body `Internal Server Error`: unhandled exception; look at source near the handler; do not guess the cause without code evidence.
- Status differs only by 400 vs 422: custom exception handler (spec gap or contract update).

## Do not
- Assert a cause that the bundle and source do not support.
- Describe a source line that was not read; give `file:line` only from `raise_site` or lines actually opened.
