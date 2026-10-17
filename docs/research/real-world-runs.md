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
