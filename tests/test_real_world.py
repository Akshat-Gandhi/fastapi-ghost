"""Gaps found by running Ghost on real open-source FastAPI projects."""

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from ghost import workspace
from ghost.discover import Endpoint, Param, fetch_spec
from ghost.gen import _missing_id
from ghost.login import ensure_login
from ghost.report import build_report, render_markdown
from ghost.runner import refine_outcome


def ep(secured=False, path="/x/{id}"):
    return Endpoint(method="GET", path=path, summary="", params=[Param("id", "path", True, {"type": "string"})],
                    body_schema=None, body_required=False, declared_codes=["200"], secured=secured)


# --- finding the OpenAPI document -------------------------------------------------------------

def test_spec_at_custom_openapi_url_is_found_through_docs_page():
    app = FastAPI(openapi_url="/api/v1/openapi.json", docs_url="/docs")
    client = TestClient(app, base_url="http://localhost:1")
    assert "paths" in fetch_spec("http://localhost:1", client)


def test_spec_path_can_be_configured():
    app = FastAPI(openapi_url="/hidden/spec.json", docs_url=None)
    client = TestClient(app, base_url="http://localhost:1")
    assert "paths" in fetch_spec("http://localhost:1", client, path="/hidden/spec.json")


def test_html_instead_of_spec_gives_a_clear_message():
    app = FastAPI(openapi_url=None)

    @app.get("/openapi.json")
    def page():
        return HTMLResponse("<html>please finish setup</html>")

    with pytest.raises(ValueError, match="text/html, not JSON"):
        fetch_spec("http://localhost:1", TestClient(app, base_url="http://localhost:1"))


def test_spec_behind_login_says_so():
    app = FastAPI(openapi_url=None)

    @app.get("/openapi.json")
    def spec():
        raise HTTPException(401, "no")

    with pytest.raises(ValueError, match="needs credentials"):
        fetch_spec("http://localhost:1", TestClient(app, base_url="http://localhost:1"))


# --- [login] ----------------------------------------------------------------------------------

def login_app():
    app = FastAPI()

    @app.post("/token")
    def token(username: str = __import__("fastapi").Form(...), password: str = __import__("fastapi").Form(...)):
        if (username, password) != ("u", "p"):
            raise HTTPException(400, "bad")
        return {"data": {"token": "tok-123"}}

    return app


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("GHOST_HOME", str(tmp_path / ".ghost"))
    workspace._SESSION_AUTH.clear()
    yield tmp_path
    workspace._SESSION_AUTH.clear()


def write_login(api, extra=""):
    workspace.write_default_config(api, "http://localhost:1", None, False)
    path = workspace.api_dir(api) / "api.toml"
    path.write_text(path.read_text() + '\n[login]\npath = "/token"\nfields = { username = "T_U", password = "T_P" }\n'
                    'token_path = "data.token"\n' + extra)


def test_login_token_is_used_and_kept_in_memory(home, monkeypatch):
    monkeypatch.setenv("T_U", "u")
    monkeypatch.setenv("T_P", "p")
    write_login("l")
    assert ensure_login("l", TestClient(login_app(), base_url="http://localhost:1"))
    cfg = workspace.load_config("l")
    assert workspace.resolve_auth(cfg, "valid") == {"Authorization": "Bearer tok-123"}
    assert cfg.has_auth() and "Authorization" in workspace.resolve_auth(cfg, "bad")
    assert "tok-123" in workspace.secret_values(cfg)
    assert "tok-123" not in (workspace.api_dir("l") / "api.toml").read_text()


def test_login_failure_and_missing_env_are_readable(home, monkeypatch):
    write_login("l")
    client = TestClient(login_app(), base_url="http://localhost:1")
    with pytest.raises(ValueError, match="T_U"):
        ensure_login("l", client)
    monkeypatch.setenv("T_U", "u")
    monkeypatch.setenv("T_P", "wrong")
    with pytest.raises(ValueError, match="returned 400"):
        ensure_login("l", client)


def test_login_rejects_unknown_keys_and_absolute_urls(home):
    with pytest.raises(ValueError):
        workspace.parse_login({"path": "/t", "fields": {"a": "B"}, "command": "rm"})
    with pytest.raises(ValueError):
        workspace.parse_login({"path": "http://evil/t", "fields": {"a": "B"}})
    with pytest.raises(ValueError):
        workspace.parse_login({"path": "/t", "fields": {}})


# --- outcomes ----------------------------------------------------------------------------------

def case(category, **extra):
    return {"category": category, **extra}


def test_open_endpoint_is_informational_not_a_failure():
    assert refine_outcome("mismatch", case("missing_auth"), ep(secured=False), 200) == "open"
    assert refine_outcome("mismatch", case("missing_auth"), ep(secured=True), 200) == "mismatch"  # really unprotected


def test_refused_before_login_is_inconclusive():
    assert refine_outcome("mismatch", case("bad_auth"), ep(), 422) == "inconclusive"
    assert refine_outcome("mismatch", case("bad_auth"), ep(), 500) == "mismatch"


def test_happy_404_on_made_up_id_is_inconclusive_unless_the_record_was_created():
    assert refine_outcome("mismatch", case("happy"), ep(), 404) == "inconclusive"
    assert refine_outcome("mismatch", case("happy", setup=["create"]), ep(), 404) == "mismatch"
    assert refine_outcome("mismatch", case("happy"), ep(path="/x"), 200) == "mismatch"


def test_missing_ids_match_the_declared_type():
    assert _missing_id({"type": "string", "format": "uuid"}) == "00000000-0000-4000-8000-00000000dead"
    assert _missing_id({"type": "integer"}) == 999999999
    assert "@" in _missing_id({"type": "string", "format": "email"})


# --- report ------------------------------------------------------------------------------------

def test_report_names_why_an_endpoint_was_not_tested(home):
    from ghost.discover import snapshot_spec
    from ghost.contract import run_contract
    from ghost.gen import run_gen
    from ghost.validate import run_validate
    from ghost.runner import run_all
    from ghost.report import write_report

    app = FastAPI()

    @app.get("/a")
    def a():
        return {}

    @app.delete("/a")
    def d():
        return {}

    workspace.write_default_config("n", "http://localhost:1", None, False)
    client = TestClient(app, base_url="http://localhost:1")
    snapshot_spec("n", client)
    run_contract("n")
    run_gen("n")
    run_validate("n")
    run_all("n", client)
    rep = write_report("n")
    delete = next(e for e in rep["endpoints"] if e["method"] == "DELETE")
    assert delete["not_tested"] == "changes data and writes are off"
    assert "not tested: changes data and writes are off" in render_markdown(rep)
