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

## Known limits

- Status codes are inferred from the spec and from direct `raise HTTPException(...)` in route functions. Errors raised in helper/service layers are not mapped to endpoints yet, and custom exception handlers are not followed.
- Baseline cases cover happy path, invalid payload (one fault each), missing/bad auth, and 404. Conflicts (409), 403 by role, 429 and business rules need state or knowledge of the app: that is what `case-writer` is for.
- Only JSON bodies; no multipart/file uploads or OAuth flows yet. Auth is static headers.
- Endpoints that are intentionally public get a `CASE_FAILED` on the "no credentials" case unless listed in `public_paths`.
- 1xx and 3xx are not targeted: they are rare in JSON APIs.
- Write cases change the target's data (a `DELETE` removes id 1, a `POST` creates a title that then conflicts), so re-running against the same stateful server gives noisier results. Run against a sandbox you can reset. Setup/teardown and snapshot/reset are on the roadmap.
- Schema migrations are `create_all` only; there is no Alembic yet.

## Development

Branches: `main` is release-only. Work happens on `feat/*` and `docs/*` branches, merged into `develop` once tested, and `develop` is promoted to `main` after a full run (tests plus the Docker stack).

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
