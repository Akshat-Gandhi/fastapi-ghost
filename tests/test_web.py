import json

import httpx

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


# --- user management, forgot password -----------------------------------------

def admin_client(app, pw="a-much-better-pass"):
    c = TestClient(app, follow_redirects=False)
    login(c)
    c.post("/change-password", data={"current_password": "admin", "new_password": pw, "confirm_password": pw})
    return c


def test_admin_creates_user_who_must_change_password(client):
    adm = admin_client(client.app)
    r = adm.post("/admin/users", data={"identifier": "Sam", "password": "initial-pass-1"})
    assert r.status_code == 200 and "Created sam" in r.text
    sam = TestClient(client.app, follow_redirects=False)
    assert login(sam, "sam", "initial-pass-1").headers["location"] == "/change-password"
    assert adm.post("/admin/users", data={"identifier": "sam", "password": "another-pass-1"}).status_code == 400
    assert adm.post("/admin/users", data={"identifier": "bad name", "password": "another-pass-1"}).status_code == 400
    assert adm.post("/admin/users", data={"identifier": "x", "password": "admin"}).status_code == 400


def test_non_admins_cannot_reach_user_management(client):
    adm = admin_client(client.app)
    adm.post("/admin/users", data={"identifier": "sam", "password": "initial-pass-1"})
    sam = TestClient(client.app, follow_redirects=False)
    login(sam, "sam", "initial-pass-1")
    sam.post("/change-password", data={"current_password": "initial-pass-1", "new_password": "sams-new-pass-9", "confirm_password": "sams-new-pass-9"})
    assert sam.get("/admin/users").status_code == 404
    assert sam.post("/admin/users", data={"identifier": "evil", "password": "initial-pass-1"}).status_code == 404
    assert sam.post("/admin/users/1/reset-password").status_code == 404
    assert TestClient(client.app, follow_redirects=False).get("/admin/users").headers["location"] == "/login"


def test_forgot_password_admin_reset(client):
    adm = admin_client(client.app)
    adm.post("/admin/users", data={"identifier": "sam", "password": "initial-pass-1"})
    sam = TestClient(client.app, follow_redirects=False)
    login(sam, "sam", "initial-pass-1")
    sam.post("/change-password", data={"current_password": "initial-pass-1", "new_password": "sams-new-pass-9", "confirm_password": "sams-new-pass-9"})
    assert sam.get("/projects").status_code == 200
    sam_id = 2
    page = adm.post(f"/admin/users/{sam_id}/reset-password")
    temp = page.text.split("Temporary password for sam: ")[1].split(" ")[0].strip()
    assert sam.get("/projects").headers["location"] == "/login"          # old session is dead
    assert login(TestClient(client.app, follow_redirects=False), "sam", "sams-new-pass-9").status_code == 401
    forced = TestClient(client.app, follow_redirects=False)
    assert login(forced, "sam", temp).headers["location"] == "/change-password"


def test_cannot_delete_self_or_last_admin(client):
    adm = admin_client(client.app)
    assert adm.post("/admin/users/1/delete").status_code == 400
    adm.post("/admin/users", data={"identifier": "sam", "password": "initial-pass-1"})
    assert adm.post("/admin/users/2/delete").status_code == 200
    assert "sam" not in adm.get("/admin/users").text


def test_cli_reset_password(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from ghost.cli import app as cli
    url = f"sqlite:///{tmp_path}/cli.db"
    monkeypatch.setenv("GHOST_DATABASE_URL", url)
    monkeypatch.setenv("GHOST_DATA_DIR", str(tmp_path))
    app = create_app(make_session_factory(url))
    c = TestClient(app, follow_redirects=False)
    login(c)
    c.post("/change-password", data={"current_password": "admin", "new_password": "a-much-better-pass", "confirm_password": "a-much-better-pass"})
    out = CliRunner().invoke(cli, ["reset-password", "admin"])
    assert out.exit_code == 0 and "Temporary password for admin:" in out.output
    temp = out.output.split("Temporary password for admin: ")[1].split()[0]
    assert c.get("/projects").headers["location"] == "/login"
    fresh = TestClient(app, follow_redirects=False)
    assert login(fresh, "admin", "a-much-better-pass").status_code == 401
    assert login(fresh, "admin", temp).headers["location"] == "/change-password"
    assert CliRunner().invoke(cli, ["reset-password", "nobody"]).exit_code == 2
    assert CliRunner().invoke(cli, ["reset-password", "admin", "--password", "short"]).exit_code == 2


def test_old_instance_first_user_becomes_admin(tmp_path):
    import sqlalchemy as sa
    url = f"sqlite:///{tmp_path}/old2.db"
    eng = sa.create_engine(url)
    with eng.begin() as conn:
        conn.execute(sa.text("CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR(255) UNIQUE, password_hash VARCHAR(255), created_at DATETIME)"))
        conn.execute(sa.text("INSERT INTO users (email, password_hash) VALUES ('first@x.io', 'x'), ('second@x.io', 'x')"))
    from ghost.web.db import User
    with make_session_factory(url)() as db:
        assert db.query(User).filter_by(email="first@x.io").one().is_admin is True
        assert db.query(User).filter_by(email="second@x.io").one().is_admin is False


def test_resolve_source_confined_to_mount(tmp_path, monkeypatch):
    from ghost.web.runner import resolve_source
    (tmp_path / "app").mkdir()
    monkeypatch.setenv("GHOST_IN_DOCKER", "1")
    monkeypatch.setenv("GHOST_SOURCES_DIR", str(tmp_path))
    assert resolve_source("app") == (tmp_path / "app").resolve()
    assert resolve_source("../etc") is None
    assert resolve_source("missing") is None
    assert resolve_source("") is None


def test_project_saves_source_path(client):
    register(client)
    r = client.post("/projects", data={"name": "p", "base_url": "http://localhost:1", "source_path": " my-app "})
    page = client.get(r.headers["location"])
    assert "my-app" in page.text


def test_old_projects_table_gets_source_path(tmp_path):
    import sqlalchemy as sa
    url = f"sqlite:///{tmp_path}/old3.db"
    eng = sa.create_engine(url)
    with eng.begin() as conn:
        conn.execute(sa.text("CREATE TABLE projects (id INTEGER PRIMARY KEY, user_id INTEGER, name VARCHAR(120), base_url VARCHAR(500), headers_enc TEXT, allow_writes BOOLEAN, samples INTEGER, created_at DATETIME)"))
    make_session_factory(url)
    assert "source_path" in {c["name"] for c in sa.inspect(sa.create_engine(url)).get_columns("projects")}


def test_connection_refused_in_docker_explains_the_0_0_0_0_requirement(tmp_path, monkeypatch):
    from ghost.web.db import Project, Run, User
    from ghost.web.runner import execute_run
    monkeypatch.setenv("GHOST_IN_DOCKER", "1")
    monkeypatch.setenv("GHOST_HOME", str(tmp_path / "ws"))
    monkeypatch.setenv("GHOST_DATA_DIR", str(tmp_path))
    factory = make_session_factory("sqlite:///:memory:")
    with factory() as db:
        user = User(email="u@x.io", password_hash="x")
        db.add(user); db.commit()
        project = Project(user_id=user.id, name="p", base_url="http://localhost:9", headers_enc="")
        db.add(project); db.commit()
        run = Run(project_id=project.id)
        db.add(run); db.commit()
        rid = run.id
    execute_run(factory, rid)
    with factory() as db:
        assert "0.0.0.0" in db.get(Run, rid).error


def test_project_with_signin_call_and_url_credentials_runs_authenticated(tmp_path, monkeypatch):
    """The web form can describe a sign-in call and URL credentials; both stay encrypted and are used in memory."""
    from fastapi import Form
    from ghost.web.db import Project, Run, User
    from ghost.web.runner import execute_run
    from ghost.web.security import build_login, encrypt_headers, encrypt_json
    import ghost.web.runner as web_runner

    monkeypatch.setenv("GHOST_HOME", str(tmp_path / "ws"))
    monkeypatch.setenv("GHOST_DATA_DIR", str(tmp_path))
    target = FastAPI()

    @target.post("/token")
    def token(username: str = Form(...), password: str = Form(...)):
        if (username, password) != ("me", "s3cret-pw"):
            raise HTTPException(400, "bad")
        return {"access_token": "tok-web-1"}

    @target.get("/me")
    def me(authorization: str = Header(None), key: str = ""):
        if authorization != "Bearer tok-web-1" or key != "k-1":
            raise HTTPException(401, "no")
        return {"ok": True}

    real_client = httpx.Client
    monkeypatch.setattr(web_runner.httpx, "Client", lambda **kw: TestClient(target, base_url="http://localhost:1"))
    factory = make_session_factory("sqlite:///:memory:")
    login = build_login("/token", "form", "username: me\npassword: s3cret-pw", "", "access_token", "Authorization", "Bearer ")
    assert login["values"]["password"] == "s3cret-pw"
    with factory() as db:
        user = User(email="u@x.io", password_hash="x")
        db.add(user); db.commit()
        project = Project(user_id=user.id, name="p", base_url="http://localhost:1", headers_enc="",
                          login_enc=encrypt_json(login), query_enc=encrypt_headers({"key": "k-1"}))
        db.add(project); db.commit()
        assert "s3cret-pw" not in project.login_enc  # stored encrypted
        run = Run(project_id=project.id)
        db.add(run); db.commit()
        rid = run.id
    execute_run(factory, rid)
    with factory() as db:
        done = db.get(Run, rid)
        assert done.status == "done", done.error
        rep = done.results
        me_row = next(e for e in rep["endpoints"] if e["path"] == "/me")
        assert 200 in me_row["observed_codes"]
        assert "s3cret-pw" not in json.dumps(rep) and "tok-web-1" not in json.dumps(rep)
    import ghost.workspace as ws
    assert "project-1" not in ws._SESSION_AUTH  # the token did not outlive the run
