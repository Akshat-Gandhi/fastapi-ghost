# Bugs Ghost found in other projects

Ready-to-file notes for the maintainers of the projects Ghost was run on. Nothing in those projects was modified; every item was seen as a response from a local sandbox copy. Causes were read from the source by the `failure-explainer` agent.

## FileCodeBox (vastsa/FileCodeBox)

1. **`POST /chunk/upload/init/` with `chunk_size: 0` returns 500.** `apps/base/views.py:304` divides by `chunk_size`. Suggested fix: reject `chunk_size < 1` (and a negative `file_size`) with 400/422 before dividing.
2. **`POST /chunk/upload/init/` accepts `file_size: -1`** (200, zero chunks). `apps/base/schemas.py:11` has no lower bound.
3. **`POST /share/text/` with a very large `expire_value` returns 500.** `apps/base/utils.py:76` builds a `timedelta` that overflows before the maximum-duration check at `:91` runs. Suggested fix: bound `expire_value` first.
4. **`POST /admin/file/batch-delete` with an id above int64 returns 500.** `apps/admin/services.py:115` binds the oversized integer into the query (`OverflowError`). Suggested fix: validate ids as 64-bit.
5. Behaviour worth a look: an unknown share code answers HTTP 200 with `{"code": 404}` in the body, and the global 404 handler replaces every API 404 detail with a generic message.

## full-stack-fastapi-template (fastapi/full-stack-fastapi-template)

1. **`GET /api/v1/items/` and `GET /api/v1/users/` return 500 for a negative or enormous `skip`/`limit`.** `skip` and `limit` are plain `int` (`items.py:15`, `users.py:37`) and reach SQL `OFFSET`/`LIMIT` unchanged. Suggested fix: `Query(0, ge=0, le=2**63 - 1)` for `skip`, a bounded `limit`.
2. **`POST /api/v1/private/users/` (development only) stores a malformed email** (the field is `str`, not `EmailStr`); afterwards `GET /users/` returns 500 because the stored row fails response validation. A duplicate email there also returns 500.
3. `api/deps.py` uses `except A, B:` without parentheses (Python 3.14 syntax); older interpreters cannot parse it.

## FastAPI-Template (JiayuXu0/FastAPI-Template)

1. **`GET /api/v1/menu/get` returns 500 for an existing menu.** The route passes the ORM object to `Success(...)` without `await obj.to_dict()` (`src/api/v1/menus/menus.py:49`), so JSON serialisation fails.
2. **`GET /api/v1/auditlog/list` returns 422 on a default request.** `start_time` and `end_time` are `datetime = Query("")` (`src/api/v1/auditlog/auditlog.py:23`); the empty default fails validation. Suggested fix: `datetime | None = Query(None)`.
3. `GET /api/v1/users/get` answers 400 for a missing user instead of 404 (`services/user_service.py:62` turns `DoesNotExist` into a failure body).
4. Error bodies are `{code, msg, data}` while the OpenAPI document declares FastAPI's `{detail: [...]}`.

## RealWorld example app (nsidnev/fastapi-realworld-example-app)

1. **`PUT /api/articles/{slug}` returns 500 when the body contains a NUL character.** Postgres rejects it (`asyncpg.CharacterNotInRepertoireError`, `app/db/repositories/articles.py:81`) and nothing catches it. Suggested fix: reject NUL in the schema or map the database error to 422.

## fastapi-users (fastapi-users/fastapi-users)

1. `POST /auth/jwt/logout` answers 204 with an empty body while the OpenAPI document says 200 (the bearer transport declares no success response, so FastAPI's default 200 is documented).
2. `POST /auth/register` accepts a 5000-character password and a 200-character email local part (201).
