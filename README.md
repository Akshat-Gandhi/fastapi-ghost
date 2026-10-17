# fastapi-ghost

A harness that tells you how a FastAPI service actually behaves. Point it at a running server and it reads the OpenAPI spec, writes an API contract, generates test cases, runs them, measures latency, and produces a report per API: which status codes were expected vs. observed, the exact error body a client sees, where in the code that error is raised, and what changed since the last run.

It is a **report, not a gate**: findings never fail a build or change an exit code.

## Quick start (Docker, with a login UI)

```bash
git clone https://github.com/Akshat-Gandhi/fastapi-ghost && cd fastapi-ghost
cp .env.example .env          # optional: set a DB password, disable sign-up
docker compose up -d
open http://127.0.0.1:8080     # sign in as admin / admin, choose a new password, enroll a project, press Run
```

On a brand-new install the only account is `admin` / `admin`, and you are sent to a change-password page before you can do anything else (any account still using a known default password is forced to change it too). Admins get a **Users** page to add users and reset a forgotten password (the person must choose a new one at next sign-in); if everyone is locked out, run `docker compose exec app ghost reset-password admin`. There is no email, so there is no emailed reset link. Set `GHOST_ADMIN_PASSWORD` in `.env` to choose the initial password instead. The default account is created only when there are no users at all, so it never appears on an existing instance.

Everything persists in Docker volumes (`ghost_pgdata` for Postgres, `ghost_data` for per-project workspaces and the encryption key). The UI is bound to `127.0.0.1` only.

To enroll a project, give it a name and a server URL, plus any headers needed for auth (`Authorization: Bearer ...`). Headers are stored encrypted and only their names are shown again. A `localhost` URL is routed to your machine from inside Docker, **but your API must listen on `0.0.0.0`**, not `127.0.0.1`, for the container to reach it:

```bash
uvicorn myapp:app --host 0.0.0.0 --port 8000
```

Optionally give a **source code folder**: put the code of the APIs you test under `./sources` (or set `GHOST_SOURCES` in `.env`; it is mounted read-only at `/sources`) and enter the folder name in the project form. The report then shows where in your code each error is raised.

Ticking "allow writes" lets Ghost call POST/PUT/PATCH/DELETE on that host. Use a dev or sandbox copy, never production.

## Quick start (CLI)

```bash
pip install "fastapi-ghost"            # PyPI release pending; for now: pip install -e ".[dev]"
ghost init todo --url http://localhost:8000 --source ./my-app     # add --writes to allow writes
export MY_TOKEN="Bearer ..."           # then name the variable in .ghost/todo/api.toml under [auth]
ghost pipeline --api todo              # discover -> contract -> cases -> run -> report
open .ghost/todo/report.md
```

`ghost serve` runs the web app without Docker (SQLite by default; set `GHOST_DATABASE_URL` for Postgres). Install the extra first: `pip install "fastapi-ghost[web]"`.

## Authentication

`api.toml` never holds secrets, only the **names** of environment variables.

```toml
[auth]                       # a static header
Authorization = "MY_API_TOKEN"        # env var holding e.g. "Bearer abc..."

[auth_query]                 # a token carried in the URL
token = "MY_API_TOKEN"

[login]                      # sign in once per run and send the returned token
path = "/api/v1/login/access-token"
as = "form"                  # or "json"
fields = { username = "MY_USER", password = "MY_PASSWORD" }   # "user.email" builds nested JSON
extra = { scope = "me items" }       # fixed, non-secret fields
token_path = "access_token"  # dotted path in the JSON reply, e.g. "data.token"
header = "Authorization"
prefix = "Bearer "           # e.g. "Token " for some APIs
```

The login token lives in memory for the run and is scrubbed from everything Ghost stores. If the API publishes its OpenAPI document somewhere other than `/openapi.json`, Ghost finds it through the Swagger page; otherwise set `openapi_path = "/api/v1/openapi.json"`.

## What it produces

For each endpoint: declared vs. observed status codes, latency (min/avg/p50/p95/p99), the response body the client received for each error, the `raise HTTPException` site behind it (when `--source` is given), spec drift since the previous run, and flags:

| Flag | Meaning |
|---|---|
| `5XX` | a server error was observed |
| `UNDECLARED` | a status was observed that the spec does not declare |
| `CASE_FAILED` | a case got a status other than the expected one |
| `FLAKY` | the same request returned different statuses |
| `SLOW` | p95 above `slow_ms` (needs at least 5 samples) |
| `DRIFT` | the endpoint changed in the spec since the last run |

Each case also gets an outcome: *as expected*, *unexpected response*, *server error*, *inconsistent*, *could not connect*, *not sent*, **open to everyone** (a request without login worked on an endpoint the docs never call protected) or **inconclusive** (the request was refused for a reason unrelated to the case, e.g. a made-up id returned 404). Inconclusive and open results are information, not failures. Endpoints Ghost did not test say why ("changes data and writes are off").

```
GET  /calc/average   declared 200,422   observed 500   5XX UNDECLARED
GET  /slow           declared 200       observed 200   p95 706ms  SLOW
```

## How it works

```
S1 discover  ->  S2 scan source  ->  S3 contract  ->  S4 cases  ->  S5 validate  ->  S6 execute  ->  S7 explain  ->  S8 report
```

| Verb | Writes (under `.ghost/<api>/`) |
|---|---|
| `ghost init <api> --url URL` | `api.toml` |
| `ghost discover` | `spec.json`, `spec.prev.json`, `endpoints.json` |
| `ghost scan` | `source_errors.json` |
| `ghost contract` | `contract.json`, `drift.json` |
| `ghost gen` | `cases/*.json` (cases are data, not code) |
| `ghost validate` | `validation.json` |
| `ghost exec` | `results/results.json` |
| `ghost bundles` | `results/bundles/*.json` (evidence for failures) |
| `ghost report` | `report.json`, `report.md`, `runs/<timestamp>/` |
| `ghost pipeline` | all of the above, no agents |

Commit `contract.json` and `cases/` so API changes show up in diffs. `results/` and `runs/` are git-ignored.

## Claude Code layer (agents and skills)

`ghost pipeline` is the deterministic baseline. For judgement-heavy parts there are subagents and skills under `.claude/`, driven by `/ghost-api <api>` (see [docs/WORKFLOW.md](docs/WORKFLOW.md)):

| Agent | Stage | Job |
|---|---|---|
| `contract-keeper` | S3 | reads source around raise sites, classifies drift, keeps `pinned` entries |
| `case-writer` | S4 | adds cases a schema cannot give (409 setups, boundary values, business rules) |
| `case-validator` | S5 | reviews cases read-only; may only make verdicts stricter |
| `failure-explainer` | S7 | explains each failure from an evidence bundle, or says "inconclusive" |
| `report-writer` | S8 | summary and prioritised findings |

Agents never call your API; they use `ghost` verbs. Regenerating cases keeps agent-written ones.

## Safety

- GET-only unless `allow_writes = true`. Enforced in the validator **and** again in the runner.
- Writes run only against `localhost` or hosts listed in `allowed_hosts`.
- `blocked_paths`, `max_rps` and `max_requests` bound what a run can do. Reads run before writes.
- `api.toml` holds **environment variable names**, never tokens. Stored responses are redacted and scrubbed of the literal token values. Case files containing credentials are rejected.
- Accounts live in the database (`users` table). Passwords are salted scrypt hashes, session tokens are stored hashed, and project headers are encrypted with `GHOST_SECRET_KEY` (auto-generated into the data volume if unset). Changing a password signs out every other session.
- Sign-up can be disabled (`GHOST_ALLOW_SIGNUP=0`); the built-in `admin` account is the way in then.

## Repeat runs against a stateful server

Write cases would normally leave data behind (a created title then 409s next time, a `DELETE` removes seed data). Ghost avoids that without restarting your server:

- **Unique values per run.** In happy `POST/PUT/PATCH` bodies, string fields named like `title, name, email, username, slug, code` (also `*_name`, `*_email`, ...) get `ghost-{{nonce}}`, where the nonce is 6 random hex characters chosen per run. The value is shortened or padded to respect `minLength`/`maxLength`, `format: email` keeps a valid address, and fields with `enum`, `pattern` or another `format` are left alone. Invalid-payload cases are never touched.
- **Setup, captures, teardown.** When the contract shows `POST /things` returning an id (in its documented success response) and an item path `/things/{id}`, the baseline cases chain: the POST happy case `captures` the id, happy GET/PUT/DELETE on the item path declare `setup` (the POST case) and use `{{thing_id}}`, and the POST case gets a generated `teardown` (`DELETE /things/{id}`) that runs after all cases, newest first. So Ghost deletes only what it created. A teardown that gets 404 (the DELETE case already removed it) is recorded as `gone`; a failed teardown is reported (`state.teardown_problems` in `report.json`) and never fails the run.
- **No chain, no destructive happy case.** If there is no creating `POST`, no `DELETE` happy case is generated (it would delete data Ghost did not create); GET/PUT happy cases use the spec example as before. Chains are only generated when writes are allowed for the host and the paths are not in `blocked_paths`.
- **Optional reset hook.** `api.toml` may name one HTTP call on the same `base_url`, run once before the cases, only when `allow_writes` is true and the host is allowed (and the path is not in `blocked_paths`; it counts toward `max_requests`). It is sent with the valid credentials. There is no shell command option, by design.

```toml
[reset]
method = "POST"        # POST, PUT, PATCH or DELETE
path = "/admin/reset"  # relative to base_url
```

Everything captured stays in memory for the run. Case files and results never contain captured values. Gates are unchanged: `allow_writes`, `allowed_hosts`, `blocked_paths`, `max_requests` and the validation verdict apply to setup cases, teardown and reset alike; reads still run before writes except where a `setup` chain needs a creating case earlier. `ghost validate` checks the new fields (see `.claude/skills/ghost-validate/references/validation-checklist.md`).

## Known limits

- Status codes come from the spec and from a static scan of the source. The scan builds a project-wide call graph and follows it from each route (depth limit 6): helper/service/crud calls, `module.func()` and aliased imports, `self.method()` and module-level instances, and `Depends()`/`Security()` dependencies (parameters, `Annotated` aliases, and router/app/`include_router` `dependencies=`). Each raise site records how it was reached (`via`) and a `confidence`: `direct`, `via_call`, `via_dependency` or `ambiguous` (several candidates, all kept). Custom exceptions count when an `exception_handler`/`add_exception_handler` with a literal status maps them, or when they subclass `HTTPException` with a fixed status.
- Still not resolved, on purpose (no edge is invented): calls through parameters, local variables, `getattr`, dicts or callbacks; calls on objects whose class is not obvious from `x = Cls()` / `self.x = Cls()`; functions passed to `run_in_threadpool`/`to_thread`; middleware; mounted sub-apps; routers whose prefix is not a string literal (the path then falls back to suffix matching, which can attach a site to several endpoints); handlers whose status is not a single literal (status stays unknown); and statuses raised by libraries. Errors raised in code the scan cannot reach are simply not linked to an endpoint.
- Baseline cases cover happy path, invalid payload (one fault each), missing/bad auth, and 404. Conflicts (409), 403 by role, 429 and business rules need state or knowledge of the app: that is what `case-writer` is for.
- JSON and form-encoded bodies only; no multipart/file uploads. Auth is a static header, a token in the URL, or one scripted login call (no OAuth browser flows).
- When the docs declare security on some endpoints, endpoints without it are treated as public. When they declare none, list intentionally public paths in `public_paths` or their "no credentials" case reports `open to everyone`.
- Generated request bodies are valid for the schema, not meaningful: rules like "token must exist" or "email already taken" show up as unexpected responses. Say so in `case-writer` cases, or ignore them.
- 1xx and 3xx are not targeted: they are rare in JSON APIs.
- Write cases change the target's data, so Ghost keeps repeat runs quiet instead of relying on a fresh server (see "Repeat runs against a stateful server"). It handles the common create/read/update/delete shape; it cannot clean up side effects it cannot see (emails sent, counters, audit rows, ids the response does not return), uniqueness rules on fields other than `title/name/email/username/slug/code`, or resources created by non-`POST` flows. For those, use the `[reset]` hook or a sandbox you can reset.
- Schema migrations are `create_all` only; there is no Alembic yet.

## Tried on real projects

Ghost has been run on seven open-source FastAPI projects (fastapi-users, full-stack-fastapi-template, the RealWorld example app, FileCodeBox, FastAPI-Template and two FastAPI docs apps). It found real bugs (500s on a NUL character, divide-by-zero, negative `limit`, ...) and the gaps it exposed in Ghost itself were fixed. See [docs/research/real-world-runs.md](docs/research/real-world-runs.md).

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md). Branches: `main` is release-only. Work happens on `feat/*` and `docs/*` branches, merged into `develop` once tested, and `develop` is promoted to `main` after a full run (tests plus the Docker stack).

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium  # once, for the browser tests
pytest                       # unit, end-to-end (examples/todo_app) and browser UI tests (tests/ui)
GHOST_SHOT_DIR=shots pytest tests/ui   # keep screenshots
```

`examples/todo_app` is a small API with deliberate problems (an undeclared 500, a slow route, auth, a 409) used to test the harness. Research notes on what developers struggle with are in [docs/research/pain-points.md](docs/research/pain-points.md); the design is in [docs/PLAN.md](docs/PLAN.md).

## License

MIT
