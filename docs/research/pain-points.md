# Pain points in testing FastAPI / REST APIs

Research date: 2026-10-17. Method: web searches only; claims are limited to what the cited
pages said. Items marked (unverified) came from a single secondary source or are inference.

## Summary

- FastAPI test setup is fiddly: sync `TestClient` vs async `httpx.AsyncClient`, lifespan
  events not firing under `AsyncClient`, `dependency_overrides` cleanup, and DB rollback fixtures.
- FastAPI does not infer error statuses raised via `HTTPException`; they must be declared by
  hand, so specs routinely under-document errors. The auto-added 422 entry only appears under
  certain conditions.
- Schema-driven tools (Schemathesis, Dredd) depend on a truthful spec; when spec and code
  drift, every case fails "for the wrong reason". Auth-related false positives recur.
- Probing a live server is risky: fuzzers can modify or delete data, and none of the sources
  gave a settled recipe for safely exercising non-idempotent endpoints.
- Opportunity for fastapi-ghost: a safe-by-default, read-the-live-spec tool that reports
  spec-vs-reality drift and latency percentiles per endpoint, with explicit side-effect controls.

## Problems table

| Problem | Who hits it | Evidence / source | What fastapi-ghost could do |
|---|---|---|---|
| `AsyncClient` does not trigger lifespan (startup/shutdown) events; needs `LifespanManager` | Devs writing async tests | https://fastapi.tiangolo.com/advanced/async-tests ; https://safir.lsst.io/user-guide/http-client.html | Probe a real running server over HTTP, so lifespan is already executed; no in-process harness needed |
| `TestClient` is sync; async tests need `AsyncClient` + `ASGITransport` | Teams with async DB/code | https://fastapi.tiangolo.com/advanced/async-tests | Black-box HTTP client avoids the sync/async split entirely |
| `dependency_overrides` must be cleared after tests, and key must be the exact callable used (second part is the researcher's note, not a source claim) | Anyone mocking DB/auth | https://app-generator.dev/docs/technologies/fastapi/testing.html (override + cleanup pattern) | Do not require overrides; accept a pre-provisioned token/header instead |
| DB isolation: rollback fixtures break when code under test commits; SAVEPOINT workarounds needed | Devs using SQLAlchemy/SQLModel | https://github.com/tiangolo/sqlmodel/discussions/940 ; https://www.oddbird.net/2024/02/09/testing-fastapi/ | Target a disposable/staging DB; record created resources and optionally clean up via declared DELETE endpoints |
| Background tasks hard to test (no source found beyond a warning against `time.sleep()` waits) (weakly verified) | Devs with post-response work | https://playbooks.com/skills/eyadsibai/ltk/fastapi-testing | Out of scope for black-box; flag as "not observable via HTTP" in report |
| `HTTPException` statuses are not inferred into OpenAPI; must use `responses=` manually | API authors, client generators | https://git.tdem.in/tiangolo/fastapi/discussions/9124 ; https://github.com/Saran33/fastapi-docx ; https://pypi.org/project/fastapi-responses/ | Probe for statuses actually observed (404, 401, 409...) and list "observed but undeclared" per operation |
| 422 documented automatically only when route has params/body and no own 422/4XX/default declared (from a discussion snippet; verify against source) | Spec consumers | https://github.com/tiangolo/fastapi/discussions/6695 | Verify the 422 body shape live; flag routes where 422 is declared but never reachable, or reachable but undeclared |
| 422 vs 400 confusion: validation failures raise `RequestValidationError` -> 422; custom handlers can change this. No source found explaining 400 vs 422 policy | Clients expecting 400 | tutorials only, e.g. https://dev.to/ayush_kumar_085a0f2c54e3f/mastering-fastapi-error-handling-best-practices-a-hands-on-guide-10p6 (weak) | Send deliberately invalid payloads, record actual status (400 vs 422), and report mismatch with the spec |
| Missing / loose response models; response shape drifts from schema | Frontend/client devs | Inferred; Tessl notes spec/implementation mismatch makes cases fail for wrong reasons: https://tessl.io/registry/testland/schemathesis-fuzzing | Validate live responses against declared response schema; flag undeclared fields and missing `response_model` (empty schema) |
| Spec drift: stale docs degrade over time | All API teams | https://philsturgeon.com/dredd-api-testing-documentation/ | Re-run on each deploy; diff the generated contract against the previous one |
| Auth-related false positives in Schemathesis (`ignored_auth` when API returns 403 not 401; `unsupported_method` when 401/403 precedes routing) | Schemathesis users | https://newreleases.io/project/pypi/schemathesis/release/4.26.0 | Report 401/403 as separate classes instead of failures; make expected auth codes configurable |
| Schemathesis can generate schema-valid but semantically invalid inputs, causing false positives | Schemathesis users | https://aicoolies.com/reviews/schemathesis-review | Mark such results "needs review" rather than "fail" |
| Multi-step flows (login then use, create then fetch) not modeled by default; stateful mode via links is opt-in (sources disagree) | Schemathesis users | https://tessl.io/registry/testland/schemathesis-fuzzing ; https://schemathesis.readthedocs.io/en/latest/faq/ | Offer lightweight create->read->delete chaining using path-parameter matching |
| Fuzzing production can modify/delete data | Anyone pointing a fuzzer at a live host | https://winf.dhsh.de/gitlab/help/user/application_security/api_fuzzing/index.md (GitLab docs mirror) | Default to safe methods (GET/HEAD/OPTIONS); require explicit flag for mutating methods; refuse non-local hosts without confirmation |
| Rate limits and idempotency are things to test deliberately in a controlled environment | SRE / backend teams | https://www.gravitee.io/testing-guide.html | Throttle requests, honor 429 / Retry-After, report rate-limit behavior |
| No settled recipe for safely fuzzing non-idempotent POST/DELETE near production (search gap) | Everyone | (gap noted in search results; no source) | Allowlist/denylist by path+method, dry-run mode that prints planned requests |
| Latency: mean misleads; need p95/p99; load generators can suffer coordinated omission; k6 default summary omits p99 | Perf testers | https://arxiv.org/pdf/2605.00428 ; https://tessl.io/registry/testland/k6-load-testing/files/references/latency-percentiles.md | Report median/p95/p99 over N samples per endpoint; state clearly it is a probe, not a load test |
| Provider states (seed data) are where Pact setups break down | Pact users | https://docs.pact.io/faq ; https://pactflow.io/blog/contract-testing-using-json-schemas-and-open-api-part-1/ | Support optional seed hooks or user-supplied example values per parameter |

Note on the 422/400 row: the only sources actually fetched for 422 were generic tutorials; the
researcher found no authoritative explanation of 400-vs-422 policy. Treat that row as an
open question to be answered empirically by probing.

## Gaps existing tools leave

- **Schemathesis** (https://schemathesis.readthedocs.io/en/latest/faq/): strong property/fuzz
  testing, but it trusts the spec, has recurring auth-check false positives
  (https://newreleases.io/project/pypi/schemathesis/release/4.26.0), and produces failures,
  not a human-friendly per-API report or a generated contract artifact.
- **Dredd**: OpenAPI 3 support labeled experimental in its docs
  (https://dredd.readthedocs.io/); an issue says early support validated bodies against
  examples, not schemas (https://github.com/apiaryio/api-elements.js/issues/71, repo archived);
  needs hooks for auth and dynamic data
  (https://www.commencis.com/?p=112413). A self-published 30-spec comparison reported
  Dredd 78% / Schemathesis 91% contract coverage (non-peer-reviewed, treat cautiously):
  https://dev.to/rishi_gaurav/we-ran-schemathesis-dredd-and-our-own-contract-test-runner-against-30-real-world-openapi-specs-2dm2
- **Postman/Newman**: nothing substantive found on contract/drift handling (search gap).
  Collections are hand-authored, so they do not follow spec changes automatically (inference).
- **Pact**: verifies only what consumers declare; provider states are costly; the Pact FAQ
  advises against generating pacts from Swagger (https://docs.pact.io/faq). It is not a
  zero-config tool for an unknown running server.
- **pytest + httpx / TestClient**: in-process, requires overrides, fixtures, lifespan
  handling (https://fastapi.tiangolo.com/advanced/async-tests). No spec-derived cases.
- **Hypothesis / Tavern**: not covered by any search result; no claims made.
- **k6 / Locust**: built for load; k6 defaults omit p99 and a p95 threshold can hide tail
  problems (https://tessl.io/registry/testland/k6-load-testing/files/references/latency-percentiles.md).
  Heavy for a quick per-endpoint latency snapshot (inference). Locust behavior not verified.
- **Fuzzers for production-like use** (e.g., WuppieFuzz, https://www.thoughtworks.com/en-br/radar/tools/wuppiefuzz):
  coverage feedback needs server instrumentation, so they suit staging only.
- **Undeclared errors**: tools that read the spec cannot test responses the spec never mentions;
  FastAPI leaves `HTTPException` statuses undeclared by default
  (https://git.tdem.in/tiangolo/fastapi/discussions/9124), and add-ons rely on static scanning
  (https://github.com/Saran33/fastapi-docx).

## Recommendations for fastapi-ghost (ranked by value)

1. **Spec-vs-reality drift report.** Compare observed status codes, content types and
   response bodies to the OpenAPI doc; emit "observed but undeclared" (esp. 401/403/404/409/422)
   and "declared but never observed". Directly addresses the HTTPException and drift gaps.
2. **Safe-by-default probing.** Read-only methods by default; explicit opt-in for POST/PUT/
   PATCH/DELETE; host allowlist; dry-run that lists planned requests; rate limiting and 429
   backoff. Addresses the main fear around side effects.
3. **Generated API contract file** (the observed contract, versioned) that can be diffed across
   deploys to catch regressions; this is the artifact the other tools do not produce.
4. **Validation-error behavior probe.** Send invalid inputs derived from schema constraints and
   record the real status (400 vs 422) and error body shape; flag deviations from the spec.
5. **Latency percentiles per endpoint** (median, p95, p99, sample count, warm-up discarded),
   labeled as a probe, not a load test.
6. **Auth handling as first-class.** Accept a token/header; report 401/403 distinctly;
   test with/without credentials without counting expected rejections as failures
   (mirrors the Schemathesis false-positive lessons).
7. **Confidence labels** (pass / fail / needs review) for schema-valid but semantically
   odd inputs, to limit false positives.
8. **Optional light chaining and seed values** (create then read then delete; user-supplied
   example values per parameter) to reach endpoints that need real IDs without full stateful
   machinery.

## Open questions (searches did not answer)

- Authoritative guidance on 400 vs 422 conventions for FastAPI.
- Hypothesis, Tavern, Newman and Locust specifics.
- Safe patterns for non-idempotent endpoints against shared environments.
- BackgroundTasks testing patterns.
