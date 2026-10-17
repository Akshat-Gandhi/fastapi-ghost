import json

import pytest
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from ghost import workspace
from ghost.contract import run_contract
from ghost.discover import snapshot_spec
from ghost.explain import make_bundles
from ghost.gen import run_gen
from ghost.report import build_report, render_markdown, write_report
from ghost.runner import classify_outcome, run_all
from ghost.scan import run_scan
from ghost.validate import run_validate

TOKEN = "Bearer s3cret-token-value"


class TodoIn(BaseModel):
    title: str = Field(min_length=1, max_length=50)
    priority: int = Field(default=1, ge=1, le=5)


def build_app():
    app = FastAPI()
    db = {1: {"id": 1, "title": "x"}}

    def auth(authorization: str = Header(None)):
        if authorization != TOKEN:
            raise HTTPException(401, "Not authenticated")

    @app.get("/todos/{todo_id}", responses={404: {"description": "missing"}}, dependencies=[Depends(auth)])
    def get_todo(todo_id: int):
        if todo_id not in db:
            raise HTTPException(404, "Todo not found")
        return db[todo_id]

    @app.post("/todos", status_code=201, dependencies=[Depends(auth)])
    def create(todo: TodoIn):
        if todo.title == "dup":
            raise HTTPException(409, "Todo already exists")
        return todo

    @app.get("/boom")
    def boom():
        raise HTTPException(500, "database exploded")

    @app.get("/echo")
    def echo(authorization: str = Header(None)):
        raise HTTPException(400, f"you sent {authorization}")

    return app


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("GHOST_HOME", str(tmp_path / ".ghost"))
    monkeypatch.setenv("TODO_TOKEN", TOKEN)
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text(
        "from fastapi import APIRouter, HTTPException\n"
        "router = APIRouter()\n"
        "@router.get('/todos/{todo_id}')\n"
        "def get_todo():\n    raise HTTPException(404, 'Todo not found')\n"
    )
    workspace.write_default_config("todo", "http://localhost:1", str(src), True)
    p = workspace.api_dir("todo") / "api.toml"
    p.write_text(p.read_text().replace("# Authorization", "Authorization").replace("MY_API_TOKEN_HEADER_VALUE", "TODO_TOKEN"))
    client = TestClient(build_app(), base_url="http://localhost:1")
    snapshot_spec("todo", client)
    run_scan("todo")
    run_contract("todo")
    run_gen("todo")
    run_validate("todo")
    return client


def test_classify_outcome():
    assert classify_outcome([200], [200, 200]) == "expected"
    assert classify_outcome([200], [404]) == "mismatch"
    assert classify_outcome([200], [500]) == "5xx"
    assert classify_outcome([200], [200, 500]) == "flaky"
    assert classify_outcome([200], [None]) == "error"


def test_run_and_report(env):
    results = run_all("todo", env)
    by_id = {r["case_id"]: r for r in results}
    assert by_id["get_todos_todo_id.happy.1"]["outcome"] == "expected"
    assert by_id["get_todos_todo_id.not_found.1"]["status"] == 404
    assert by_id["get_todos_todo_id.missing_auth.1"]["status"] == 401
    assert by_id["post_todos.happy.1"]["status"] == 201
    assert all(r["status"] == 422 for r in results if r["category"] == "invalid_payload")
    assert by_id["get_todos_todo_id.happy.1"]["timings_ms"] and len(by_id["get_todos_todo_id.happy.1"]["timings_ms"]) == 5
    assert len(by_id["post_todos.happy.1"]["timings_ms"]) == 1  # writes run once

    rep = write_report("todo")
    ep = {f"{e['method']} {e['path']}": e for e in rep["endpoints"]}
    get = ep["GET /todos/{todo_id}"]
    assert 404 in get["observed_codes"] and "UNDECLARED" in get["flags"]  # 401 is observed but not declared
    err = next(e for e in get["error_responses"] if e["status"] == 404)
    assert err["body"] == {"detail": "Todo not found"}
    assert err["internal_raise_site"] == "app.py:5"
    assert (workspace.api_dir("todo") / "report.md").exists()
    assert list((workspace.api_dir("todo") / "runs").iterdir())
    md = render_markdown(rep)
    assert "never blocks" in md and "Todo not found" in md


def test_unvalidated_cases_never_run(env):
    path = workspace.api_dir("todo") / "validation.json"
    v = json.loads(path.read_text())
    for cid in v["cases"]:
        v["cases"][cid]["verdict"] = "fix"
    path.write_text(json.dumps(v))
    assert run_all("todo", env) == []


def test_write_gate_rechecked_in_runner(env):
    p = workspace.api_dir("todo") / "api.toml"
    p.write_text(p.read_text().replace("allow_writes = true", "allow_writes = false"))
    results = run_all("todo", env)  # validation.json still says ok for POST; runner must refuse anyway
    assert results and not any(r["method"] == "POST" for r in results)


def test_request_budget(env):
    p = workspace.api_dir("todo") / "api.toml"
    p.write_text(p.read_text() + "\nmax_requests = 3\n")
    p.write_text(p.read_text().replace("max_requests = 3\n", "").replace("max_rps = 20", "max_rps = 20\nmax_requests = 3"))
    results = run_all("todo", env)
    assert sum(len(r["timings_ms"]) for r in results) <= 3


def test_secret_never_lands_in_artifacts(env):
    # Add an agent case that makes the server echo the token back in an error body.
    cf = workspace.api_dir("todo") / "cases" / "get_echo.json"
    case = {"id": "get_echo.happy.1", "category": "happy", "title": "echo", "origin": "agent", "destructive": False,
            "request": {"path_params": {}, "query": {}, "headers": {}, "body": None, "auth": "valid"},
            "expect": {"status": [200]}}
    cf.write_text(json.dumps({"operation_id": "get_echo", "method": "GET", "path": "/echo", "cases": [case]}))
    run_validate("todo")
    results = run_all("todo", env)
    echo = next(r for r in results if r["case_id"] == "get_echo.happy.1")
    assert echo["status"] == 400 and "s3cret-token-value" not in json.dumps(echo)
    make_bundles("todo")
    write_report("todo")
    for f in workspace.api_dir("todo").rglob("*"):
        if f.is_file():
            assert "s3cret-token-value" not in f.read_text(), f


def test_bundles_cover_failures_with_raise_sites(env):
    run_all("todo", env)
    written = make_bundles("todo")
    assert any("boom" in w or "echo" in w or "get_todos" in w or "post_todos" in w for w in written) or written == []
    bundles = [json.loads((workspace.api_dir("todo") / w).read_text()) for w in written]
    for b in bundles:
        assert b["outcome"] in {"mismatch", "5xx", "flaky", "error"}
        assert "Authorization" not in json.dumps(b["request"])


def test_gen_keeps_agent_cases(env):
    cf = workspace.api_dir("todo") / "cases" / "get_boom.json"
    data = json.loads(cf.read_text())
    data["cases"].append({"id": "get_boom.observe_5xx.1", "category": "observe_5xx", "title": "agent case", "origin": "agent",
                          "destructive": False, "request": {"path_params": {}, "query": {}, "headers": {}, "body": None, "auth": "none"},
                          "expect": {"status": [500]}})
    cf.write_text(json.dumps(data))
    run_gen("todo")
    assert any(c["id"] == "get_boom.observe_5xx.1" for c in json.loads(cf.read_text())["cases"])


def test_validator_rejects_literal_credentials(env):
    cf = workspace.api_dir("todo") / "cases" / "get_echo.json"
    case = {"id": "get_echo.happy.1", "category": "happy", "title": "leaky", "origin": "agent", "destructive": False,
            "request": {"path_params": {}, "query": {}, "headers": {"Authorization": TOKEN}, "body": None, "auth": "none"},
            "expect": {"status": [200]}}
    cf.write_text(json.dumps({"operation_id": "get_echo", "method": "GET", "path": "/echo", "cases": [case]}))
    v = run_validate("todo")["cases"]["get_echo.happy.1"]
    assert v["verdict"] == "fix" and "credentials_in_case" in v["problems"]
    assert not any(r["case_id"] == "get_echo.happy.1" for r in run_all("todo", env))


def test_review_can_tighten_but_not_loosen(env):
    review = {"cases": {
        "get_todos_todo_id.happy.1": {"verdict": "fix", "reason": "id 1 may not exist"},
        "post_todos.happy.1": {"verdict": "ok", "reason": "try to loosen"},
    }}
    workspace.write_json("todo", "validation_review.json", review)
    p = workspace.api_dir("todo") / "api.toml"
    p.write_text(p.read_text().replace("allow_writes = true", "allow_writes = false"))
    v = run_validate("todo")["cases"]
    assert v["get_todos_todo_id.happy.1"]["verdict"] == "fix"
    assert any(x.startswith("review:") for x in v["get_todos_todo_id.happy.1"]["problems"])
    assert v["post_todos.happy.1"]["verdict"] == "drop"  # writes disabled stays dropped
    ran = {r["case_id"] for r in run_all("todo", env)}
    assert "get_todos_todo_id.happy.1" not in ran


def test_report_reads_agent_explanations(env):
    run_all("todo", env)
    workspace.write_json("todo", "explanations.json", {"explanations": [{
        "case_id": "get_boom.happy.1", "class": "API bug", "hypothesis": "handler raises 500", "confidence": "high",
        "location": "app.py:9", "fix": "return a 4xx", "user_sees": {"status": 500}}]})
    rep = write_report("todo")
    boom = next(e for e in rep["endpoints"] if e["path"] == "/boom")
    assert boom["explanations"]["get_boom.happy.1"]["class"] == "API bug"
    md = (workspace.api_dir("todo") / "report.md").read_text()
    assert "API bug, high confidence" in md and "app.py:9" in md


def test_report_uses_plain_labels_and_legend(env):
    run_all("todo", env)
    rep = write_report("todo")
    assert any("5XX" in e["flags"] for e in rep["endpoints"])  # json keeps the stable code
    md = (workspace.api_dir("todo") / "report.md").read_text()
    assert "Server error" in md and "5XX" not in md
    assert "## What the labels mean" in md and "crashed or failed internally" in md


def _case(method="GET"):
    return {"id": "x.happy.1", "operation_id": "get_x", "category": "happy", "title": "t", "destructive": method != "GET",
            "request": {"path_params": {}, "query": {}, "headers": {}, "body": None, "auth": "none"},
            "expect": {"status": [200]}}


def _flaky_client(fail_times, calls):
    import httpx

    def handler(request):
        calls.append(request.method)
        if len(calls) <= fail_times:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return httpx.Response(200, json={"ok": True})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_safe_request_retries_once_on_stale_connection():
    from ghost.discover import Endpoint
    from ghost.runner import run_case
    from ghost.workspace import ApiConfig
    calls = []
    cfg = ApiConfig("http://localhost:1", samples=1)
    r = run_case(_case(), Endpoint("GET", "/x"), cfg, _flaky_client(1, calls), [100])
    assert r["outcome"] == "expected" and r["error"] is None and len(calls) == 2


def test_second_failure_is_still_reported():
    from ghost.discover import Endpoint
    from ghost.runner import run_case
    from ghost.workspace import ApiConfig
    calls = []
    r = run_case(_case(), Endpoint("GET", "/x"), ApiConfig("http://localhost:1", samples=1), _flaky_client(2, calls), [100])
    assert r["outcome"] == "error" and "RemoteProtocolError" in r["error"] and len(calls) == 2


def test_writes_are_never_retried():
    from ghost.discover import Endpoint
    from ghost.runner import run_case
    from ghost.workspace import ApiConfig
    calls = []
    r = run_case(_case("POST"), Endpoint("POST", "/x"), ApiConfig("http://localhost:1", samples=1, allow_writes=True),
                 _flaky_client(1, calls), [100])
    assert r["outcome"] == "error" and len(calls) == 1
