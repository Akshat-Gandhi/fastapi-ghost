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


# --- default admin and forced password change ---------------------------------

def login(c, who="admin", pw="admin"):
    return c.post("/login", data={"email": who, "password": pw})


def test_fresh_install_has_admin_that_must_change_password(client):
    r = login(client)
    assert r.status_code == 303 and r.headers["location"] == "/change-password"
    for path in ("/projects", "/projects/1", "/runs/1"):
        assert client.get(path).headers["location"] == "/change-password"
    assert client.get("/change-password").status_code == 200
    assert client.post("/projects", data={"name": "x", "base_url": "http://x"}).headers["location"] == "/change-password"


def test_change_password_rules_and_effect(client):
    login(client)
    def change(cur, new, conf=None):
        return client.post("/change-password", data={"current_password": cur, "new_password": new, "confirm_password": conf or new})
    assert change("wrong", "newpassw0rd").status_code == 400
    assert change("admin", "newpassw0rd", "different1").status_code == 400
    assert change("admin", "short").status_code == 400
    assert change("admin", "admin").status_code == 400
    assert client.get("/projects").headers["location"] == "/change-password"  # still forced
    ok = change("admin", "a-much-better-pass")
    assert ok.status_code == 303 and ok.headers["location"] == "/projects"
    assert client.get("/projects").status_code == 200
    fresh = TestClient(client.app, follow_redirects=False)
    assert login(fresh).status_code == 401                          # admin/admin no longer works
    assert login(fresh, pw="a-much-better-pass").headers["location"] == "/projects"


def test_other_sessions_are_signed_out_on_password_change(client):
    other = TestClient(client.app, follow_redirects=False)
    login(other)
    login(client)
    client.post("/change-password", data={"current_password": "admin", "new_password": "a-much-better-pass", "confirm_password": "a-much-better-pass"})
    assert other.get("/change-password").headers["location"] == "/login"
    assert client.get("/projects").status_code == 200


def test_admin_is_only_seeded_on_empty_database(tmp_path, monkeypatch):
    from ghost.web.db import User
    monkeypatch.setenv("GHOST_DATA_DIR", str(tmp_path))
    factory = make_session_factory("sqlite:///:memory:")
    app = create_app(factory)
    with factory() as db:
        assert db.query(User).filter_by(email="admin").count() == 1
    create_app(factory)  # second start must not add or reset anything
    with factory() as db:
        assert db.query(User).count() == 1


def test_no_admin_is_added_to_an_instance_that_already_has_users(tmp_path, monkeypatch):
    from ghost.web.db import User, ensure_admin
    from ghost.web.security import hash_password
    factory = make_session_factory("sqlite:///:memory:")
    with factory() as db:
        db.add(User(email="real@x.io", password_hash=hash_password("password123")))
        db.commit()
    assert ensure_admin(factory) is False
    with factory() as db:
        assert db.query(User).filter_by(email="admin").count() == 0


def test_env_password_skips_forced_change(tmp_path, monkeypatch):
    monkeypatch.setenv("GHOST_ADMIN_PASSWORD", "operator-chosen-pw")
    c = TestClient(create_app(make_session_factory("sqlite:///:memory:")), follow_redirects=False)
    assert login(c, pw="admin").status_code == 401
    assert login(c, pw="operator-chosen-pw").headers["location"] == "/projects"


def test_account_still_on_weak_password_is_forced_to_change(client):
    register(client, "weak@x.io", "password1")  # allowed length-wise, but a known weak default
    client.post("/logout")
    assert login(client, "weak@x.io", "password1").headers["location"] == "/change-password"


def test_existing_database_gets_the_new_column(tmp_path):
    import sqlalchemy as sa
    url = f"sqlite:///{tmp_path}/old.db"
    eng = sa.create_engine(url)
    with eng.begin() as conn:
        conn.execute(sa.text("CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR(255) UNIQUE, password_hash VARCHAR(255), created_at DATETIME)"))
        conn.execute(sa.text("INSERT INTO users (email, password_hash) VALUES ('old@x.io', 'x')"))
    factory = make_session_factory(url)
    with factory() as db:
        from ghost.web.db import User
        assert db.query(User).filter_by(email="old@x.io").one().must_change_password is False
        assert db.query(User).filter_by(email="admin").count() == 0  # existing instance: no default account appears
