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
    assert refine_outcome("mismatch", case("happy", request={"path_params": {"id": "ghost"}}), ep(), 404) == "inconclusive"
    chained = {"request": {"path_params": {"id": "{{id}}"}}}
    assert refine_outcome("mismatch", case("happy", setup=["create"], **chained), ep(), 404) == "mismatch"
    assert refine_outcome("mismatch", case("invalid_payload", **chained), ep(), 404) == "mismatch"
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


def test_login_with_nested_json_body_and_custom_prefix(home, monkeypatch):
    app = FastAPI()

    @app.post("/api/users/login")
    def login(body: dict):
        assert body == {"user": {"email": "e@x.io", "password": "pw"}}
        return {"user": {"token": "jwt-1"}}

    monkeypatch.setenv("T_E", "e@x.io")
    monkeypatch.setenv("T_P", "pw")
    workspace.write_default_config("n2", "http://localhost:1", None, False)
    path = workspace.api_dir("n2") / "api.toml"
    path.write_text(path.read_text() + '\n[login]\npath = "/api/users/login"\nas = "json"\n'
                    'fields = { "user.email" = "T_E", "user.password" = "T_P" }\n'
                    'token_path = "user.token"\nprefix = "Token "\n')
    ensure_login("n2", TestClient(app, base_url="http://localhost:1"))
    assert workspace.resolve_auth(workspace.load_config("n2"), "valid") == {"Authorization": "Token jwt-1"}


# --- chains over wrapped responses and sub-resources --------------------------------------------

def test_id_inside_a_single_wrapper_key_is_found():
    from ghost.state import _id_field
    spec = {"components": {"schemas": {"Art": {"type": "object", "properties": {"slug": {"type": "string"}}}}}}
    props = {"article": {"$ref": "#/components/schemas/Art"}}
    assert _id_field(props, "slug", spec) == "article.slug"
    assert _id_field({"id": {"type": "integer"}}, "item_id", spec) == "id"
    assert _id_field({"a": {"type": "object", "properties": {}}, "b": {"type": "object", "properties": {}}}, "x", spec) is None


def test_sub_resources_use_the_parent_chain():
    from ghost.gen import _chain_for
    from ghost.state import Chain

    chain = Chain(ep(path="/a"), "post_a.happy.1", "slug", "article.slug", "/a/{slug}", "slug", None)
    chains = {"/a/{slug}": chain}
    mk = lambda method, path: Endpoint(method=method, path=path, summary="", params=[], body_schema=None,
                                       body_required=False, declared_codes=[], secured=False)
    assert _chain_for(mk("GET", "/a/{slug}"), chains) is chain
    assert _chain_for(mk("POST", "/a/{slug}/comments"), chains) is chain
    assert _chain_for(mk("POST", "/a/{slug}"), chains) is None
    assert _chain_for(mk("GET", "/b/{id}"), chains) is None


def test_token_in_the_url_is_sent_hidden_and_swapped(home, monkeypatch):
    from ghost.runner import build_request
    monkeypatch.setenv("URL_TOKEN", "url-secret")
    workspace.write_default_config("q", "http://localhost:1", None, False)
    path = workspace.api_dir("q") / "api.toml"
    path.write_text(path.read_text() + '\n[auth_query]\ntoken = "URL_TOKEN"\n')
    cfg = workspace.load_config("q")
    e = ep()
    sent = lambda mode: build_request({"request": {"query": {"token": "string", "x": 1}, "auth": mode}}, e, cfg)["params"]
    assert sent("valid") == {"x": 1, "token": "url-secret"}
    assert sent("none") == {"x": 1}
    assert sent("bad")["token"] != "url-secret"
    assert cfg.has_auth() and "url-secret" in workspace.secret_values(cfg)


def test_pattern_strings_and_short_password_false_positive():
    from ghost.schema import example_from_schema
    from ghost.validate import _contains_secret
    assert example_from_schema({"type": "string", "pattern": "^password$"}, {}) == "password"
    assert example_from_schema({"type": "string", "pattern": "^(a|b)$"}, {}) == "a"
    assert example_from_schema({"type": "string", "pattern": r"^\d{6}$"}, {}) == "123456"
    assert not _contains_secret({"body": {"client_secret": "x"}, "title": "a secret thing"}, ["secret"])
    assert _contains_secret({"body": {"password": "secret"}}, ["secret"])
    assert _contains_secret({"h": "Bearer abcdefgh1234"}, ["abcdefgh1234"])


def test_form_bodies_are_sent_as_forms():
    from ghost.discover import parse_spec
    from ghost.runner import build_request
    spec = {"paths": {"/token": {"post": {"requestBody": {"required": True, "content": {
        "application/x-www-form-urlencoded": {"schema": {"type": "object", "properties": {"username": {"type": "string"}}}}}},
        "responses": {"200": {}}}}}}
    (endpoint,) = parse_spec(spec)
    assert endpoint.body_format == "form"
    cfg = workspace.ApiConfig(base_url="http://localhost:1")
    sent = build_request({"request": {"body": {"username": "u", "n": 3}, "auth": "none"}}, endpoint, cfg)
    assert sent["data"] == {"username": "u", "n": "3"} and "json" not in sent
