# Running Ghost on real FastAPI projects

Seven targets, run locally against throwaway databases (writes on, sandbox only). Everything below was found by
running the tool, then fixed in `fix/real-world-gaps`.

| Target | Why it is interesting | Endpoints | Cases | Real bugs found in the target |
|---|---|---|---|---|
| fastapi-users (example app) | JWT login by form | 13 | 47 | none |
| full-stack-fastapi-template (backend) | `openapi_url` under `/api/v1`, Postgres, OAuth2 form login | 23 | 121 | `POST /private/users/` accepts a malformed email, then `GET /users/` returns 500 because the stored row no longer validates |
| nsidnev/fastapi-realworld-example-app | old FastAPI 0.79 / pydantic v1, `Token` prefix, nested JSON login, custom error handlers | 19 | 71-82 | `PUT /articles/{slug}` returns 500 when the body holds a NUL character (Postgres rejects it, nothing catches it) |
| FileCodeBox | first-run setup wizard, JSON login, non-ASCII messages | 59 | 247 | none confirmed |
| FastAPI docs: bigger_applications | token in the query string plus a required header | 8 | 24 | none |
| FastAPI docs: security tutorial 5 | OAuth2 scopes, password form | 4 | 17 | none |

## Gaps in Ghost this exposed (all fixed unless noted)

1. `/openapi.json` missing or served as HTML gave "Expecting value". Now a plain sentence, plus `openapi_path` and automatic lookup through the Swagger page.
2. Authenticated APIs could not be tested. Added `[login]` (form or JSON, nested fields, token path, header prefix, fixed extras like `scope`) and `[auth_query]` for tokens in the URL. Tokens live in memory only and are scrubbed from output.
3. Form-encoded request bodies were sent as JSON. Now sent as forms.
4. Noise: results that say nothing about the endpoint are now `inconclusive` (404 on a made-up id, 400/422 before the login check), and a no-login 200 on an endpoint the docs never call protected is `open`. 401/403 provoked by a login test is no longer "Not in the docs".
5. Endpoints with no cases showed blank rows. Now "not tested: changes data and writes are off".
6. UUID/email path ids: the not-found probe used a string and got 422. Now typed.
7. Create chains missed wrapped ids (`article.slug`) and sub-resources (`/articles/{slug}/comments`). Fixed; DELETE cases run deepest-first so a parent is not removed before its children are tested.
8. Short passwords tripped the "credential in case" check on a field named `client_secret`. Values are compared, not substrings. Regex `pattern` strings (`^password$`) are now honoured in samples.
9. After a 500 the next request reused a closed connection and showed a bogus "connection reset". Pooled connections are dropped after a 5xx.
10. Evidence bundles lacked the request that was sent, the base URL and a timestamp. Added.
11. Docs the agents tripped on were corrected (contract schema extras, case-writer allowed commands and ids, status ranges per category, repro format, `GHOST_BIN`).

## Known limits still open

- The source scan does not see raises of a pre-built exception variable, auth dependencies not linked to a route, or an `add_exception_handler(HTTPException, ...)` that rewrites every error body (the contract-keeper agent had to patch these by hand).
- A happy-path `PUT` on the logged-in user can change the credentials Ghost logs in with (the validator agent caught it; Ghost does not detect it).
- Generated bodies are schema-valid, not meaningful: reset-password tokens, existing-email checks and similar rules show up as "Unexpected response". Per-operation example overrides are not built yet.

## Round 2: the staged agent workflow (S3-S8) on five more APIs

Each target went through contract-keeper, case-writer, case-validator, the runner, failure-explainer and report-writer
(one agent per target played the roles in turn; the RealWorld app used a separate subagent per role). Every agent was
asked to report what in the harness got in its way.

| Slug | Target | Cases run | Real findings in the target |
|---|---|---|---|
| rwa | RealWorld app | 82 | NUL in an article body gives 500 |
| usersa | fastapi-users example | 65 | logout answers 204 where the spec says 200; register accepts a 5000-character password; admin routes need a superuser |
| fcba | FileCodeBox | 182 | three more 500s (`chunk_size=0` divides by zero, a huge `expire_value` overflows `timedelta`, an id above int64 in batch-delete); "not found" answered as HTTP 200 with `{"code":404}` in the body |
| fsfta | full-stack-fastapi-template | 126 | negative `skip`/`limit` gives 500 on `/items/` and `/users/`; duplicate email on `/private/users/` gives 500 |
| seca / biga | FastAPI docs apps | 15 / 25 | 401s undeclared in the spec; 404/418 declared but never raised |

### Gaps fixed from the agents' reports (`fix/agent-run-gaps`)

- No `missing_auth`/`bad_auth` cases for endpoints the docs leave unsecured when other endpoints are secured (login, signup, health).
- No "wrong type" cases for form bodies (a form only carries text, so the case was valid).
- Unique values differ per operation (`gh3f1-<nonce>@example.com`), so signup, update and admin-create no longer collide on one email.
- No happy `PUT`/`PATCH`/`DELETE` on `/me` when Ghost signs in (it would change the account Ghost uses).
- The report's Declared column now means "in the OpenAPI docs"; statuses found only in the source are listed apart (`code_only_codes`).
- `ghost scan` warns about, and records, source files it could not parse (the template uses Python 3.14 syntax).
- Docs: working directory and `GHOST_HOME`/`GHOST_BIN`, fix-loop definition, sticky review file, teardown path templates, APIs implemented in a library, multipart not supported.

### Still open after round 2

- Error bodies in the report are matched to raise sites by status code only, so two different 400s on one endpoint can be attributed to the wrong line.
- Rate limiting (423/429) hides the real answers of later cases; there is no pacing or backoff by response.
- `[login] extra` is fixed per run, so scope-denial cases cannot be written.
- Multipart file uploads are not generated.
- Placeholder bodies in the contract for errors raised from library code (`{"detail": "ghost"}`).

### Round 3: FastAPI-Template (Tortoise ORM admin backend, SQLite, own Redis)

Staged workflow `jta`: 37 endpoints, 149 cases run. Findings: `GET /menu/get` returns 500 for an existing menu (an ORM object is returned without `to_dict()`); `GET /users/get` answers 400 for a missing user; `GET /auditlog/list` returns 422 on a default request (`Query("")` on a datetime). Error bodies are `{code,msg,data}` while the docs declare `{detail:[...]}`.
Library gap fixed: a login **username** (e.g. `admin`) is no longer treated as a secret, so spec examples that use it are not rejected as "credentials in case"; only password/token/secret-like login fields are.
Still open: baseline gen builds update/delete happy cases from spec-example ids and `admin` values, which are dangerous against a live admin account (the agents removed 42 by hand); re-running `gen.sh` overwrites hand edits to baseline cases.


### Round 4: `/ghost-api` with the registered subagents on six more slugs

The command and its five subagents (contract-keeper, case-writer, case-validator, failure-explainer, report-writer) ran for `usersa`, `fcba`, `fsfta`, `seca`, `biga` and `jta`, with fix loops where the validator asked for them. The validators' semantic reviews found problems the mechanical validation cannot, and the recurring ones became library fixes:

- Baseline cases for **public routes** (the contract says `auth: none` while other routes need auth) no longer include missing/bad-auth cases (about 100 of 251 FileCodeBox baselines were wrong).
- **Destructive cases never carry seed ids** (`user_id=1` in a query or body); a delete/update of a seeded record is never a happy case. Previously a failed-open auth check on `DELETE ...?user_id=1` would have removed the admin.
- Category ranges fit real apps: bad credentials answer 400 or 423, duplicates 400, a missing required token 422.
- A contract with `"status": "unknown"` or an `unexpected` entry in another shape no longer crashes `gen`/`exec`.
- Ids stay unique (agent cases that reused a baseline id are renamed; duplicates are flagged), and each case uses the capture made by its own setup.
- A Docker connection-refused run now says to start the API with `--host 0.0.0.0`.
- The web form accepts a sign-in request and credentials in the URL, like `api.toml`.

New findings in the targets: FileCodeBox answers 500 for `chunk_size=0`, an oversized `expire_value` and an oversized id in batch-delete, and accepts `file_size=-1`; full-stack-fastapi-template answers 500 for a huge or negative `skip`; FastAPI-Template answers 500 on `GET /menu/get` and 422 on a default `GET /auditlog/list`; fastapi-users logout answers 204 where the spec says 200.
