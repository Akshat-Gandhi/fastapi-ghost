import json

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from ghost.web.app import create_app
from ghost.web.db import make_session_factory


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GHOST_DATA_DIR", str(tmp_path))
    app = create_app(make_session_factory("sqlite:///:memory:"))
    return TestClient(app, follow_redirects=False)


def register(c, email="a@x.io", pw="password1"):
    return c.post("/register", data={"email": email, "password": pw})


def test_redirects_to_login_when_anonymous(client):
    assert client.get("/projects").headers["location"] == "/login"


def test_register_login_logout(client):
    assert register(client).status_code == 303
    assert client.get("/projects").status_code == 200
    client.post("/logout")
    assert client.get("/projects").status_code == 303
    assert client.post("/login", data={"email": "a@x.io", "password": "nope"}).status_code == 401
    assert client.post("/login", data={"email": "a@x.io", "password": "password1"}).status_code == 303


def test_signup_can_be_disabled_after_first_user(client, monkeypatch):
    register(client)
    monkeypatch.setenv("GHOST_ALLOW_SIGNUP", "0")
    assert register(TestClient(client.app), "b@x.io").status_code == 400


def test_projects_are_private_per_user(client):
    register(client)
    r = client.post("/projects", data={"name": "p", "base_url": "http://x:1"})
    pid = r.headers["location"].split("/")[-1]
    other = TestClient(client.app, follow_redirects=False)
    register(other, "b@x.io")
    assert other.get(f"/projects/{pid}").status_code == 404


def test_headers_are_encrypted_at_rest(client):
    register(client)
    client.post("/projects", data={"name": "p", "base_url": "http://x:1", "headers": "Authorization: Bearer s3cret"})
    with client.app.state.factory() as db:
        from ghost.web.db import Project
        blob = db.query(Project).one().headers_enc
    assert "s3cret" not in blob and blob


def make_target():
    target = FastAPI()

    @target.get("/data")
    def data_ep(authorization: str = Header(None)):
        if authorization != "Bearer tok":
            raise HTTPException(401, "bad token")
        return {"ok": True}

    @target.get("/boom")
    def boom(authorization: str = Header(None)):
        if authorization != "Bearer tok":
            raise HTTPException(401, "bad token")
        raise HTTPException(500, "x")

    return target


def test_run_cycle_end_to_end(client, monkeypatch, tmp_path):
    """Enroll a project with auth headers, run the full pipeline, read the report."""
    import ghost.web.runner as runner
    monkeypatch.setenv("GHOST_HOME", str(tmp_path / "ws"))
    target = make_target()
    monkeypatch.setattr(
        runner.httpx, "Client", lambda **kw: TestClient(target, base_url="http://t", headers=kw.get("headers"))
    )
    register(client)
    pid = client.post(
        "/projects", data={"name": "p", "base_url": "http://localhost:9", "samples": "2",
                           "headers": "Authorization: Bearer tok"},
    ).headers["location"].split("/")[-1]
    run_url = client.post(f"/projects/{pid}/run").headers["location"]
    data = client.get(run_url + "/report.json").json()
    assert data["status"] == "done", data
    eps = {e["path"]: e for e in data["endpoints"]}
    assert eps["/data"]["flags"] == [] or "UNDECLARED" in eps["/data"]["flags"]
    happy = next(c for c in eps["/data"]["cases"] if c["category"] == "happy")
    assert happy["outcome"] == "expected" and happy["status"] == 200  # stored header was sent
    assert "5XX" in eps["/boom"]["flags"]
    assert any(c["category"] == "missing_auth" and c["status"] == 401 for c in eps["/data"]["cases"])
    page = client.get(run_url)
    assert page.status_code == 200 and "/boom" in page.text and "never blocks" in page.text
    md = client.get(run_url + "/report.md")
    assert md.status_code == 200 and "# API report" in md.text
    assert "Bearer tok" not in md.text and "Bearer tok" not in json.dumps(data)


def test_failed_run_is_reported_not_lost(client, monkeypatch, tmp_path):
    monkeypatch.setenv("GHOST_HOME", str(tmp_path / "ws"))
    register(client)
    pid = client.post("/projects", data={"name": "p", "base_url": "http://127.0.0.1:1"}).headers["location"].split("/")[-1]
    run_url = client.post(f"/projects/{pid}/run").headers["location"]
    data = client.get(run_url + "/report.json").json()
    assert data["status"] == "failed" and data["error"]
