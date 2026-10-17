"""End to end: real uvicorn server + the full `ghost pipeline` against examples/todo_app."""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from ghost import workspace
from ghost.cli import app as cli

ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def server():
    port = free_port()
    env = {**os.environ, "TODO_TOKEN": "e2e-secret-token", "PYTHONPATH": str(ROOT)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "examples.todo_app.main:app", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env,
    )
    for _ in range(50):
        try:
            httpx.get(f"http://127.0.0.1:{port}/health", timeout=0.5)
            break
        except httpx.HTTPError:
            time.sleep(0.2)
    yield port
    proc.terminate()
    proc.wait(timeout=5)


def test_full_pipeline_on_todo_app(server, tmp_path, monkeypatch):
    monkeypatch.setenv("GHOST_HOME", str(tmp_path / ".ghost"))
    monkeypatch.setenv("TODO_AUTH_HEADER", "Bearer e2e-secret-token")
    runner = CliRunner()
    url = f"http://127.0.0.1:{server}"
    src = str(ROOT / "examples" / "todo_app")
    assert runner.invoke(cli, ["init", "todo", "--url", url, "--source", src, "--writes"]).exit_code == 0
    cfg = workspace.api_dir("todo") / "api.toml"
    cfg.write_text(cfg.read_text().replace('# Authorization = "MY_API_TOKEN_HEADER_VALUE"', 'Authorization = "TODO_AUTH_HEADER"'))
    cfg.write_text(cfg.read_text().replace("slow_ms = 500", "slow_ms = 300"))

    assert runner.invoke(cli, ["discover", "--api", "todo"]).exit_code == 0
    assert runner.invoke(cli, ["scan", "--api", "todo"]).exit_code == 0
    assert runner.invoke(cli, ["contract", "--api", "todo"]).exit_code == 0
    assert runner.invoke(cli, ["gen", "--api", "todo"]).exit_code == 0

    # What the case-writer agent would add: a conflict that needs setup state (the earlier POST).
    cf = workspace.api_dir("todo") / "cases" / "post_todos.json"
    data = json.loads(cf.read_text())
    happy = next(c for c in data["cases"] if c["category"] == "happy")
    data["cases"].append({**happy, "id": "post_todos.conflict.1", "category": "conflict", "title": "duplicate title",
                          "expect": {"status": [409]}, "origin": "agent"})
    cf.write_text(json.dumps(data))

    out = runner.invoke(cli, ["pipeline", "--api", "todo"])
    assert out.exit_code == 0, out.output  # findings never fail the run

    rep = json.loads((workspace.api_dir("todo") / "report.json").read_text())
    ep = {f"{e['method']} {e['path']}": e for e in rep["endpoints"]}

    # contract + codes
    assert {201, 422, 409, 401}.issubset(set(ep["POST /todos"]["observed_codes"]))
    assert 404 in ep["GET /todos/{todo_id}"]["observed_codes"]
    conflict = next(c for c in ep["POST /todos"]["cases"] if c["category"] == "conflict")
    assert conflict["status"] == 409 and conflict["outcome"] == "expected"

    # exact client-visible error body + internal raise site
    nf = next(e for e in ep["GET /todos/{todo_id}"]["error_responses"] if e["status"] == 404)
    assert nf["body"] == {"detail": "Todo not found"} and nf["internal_raise_site"].startswith("main.py:")

    # findings: slow endpoint, public path skipped for auth cases, auth cases elsewhere pass
    assert "SLOW" in ep["GET /slow"]["flags"]
    assert not any(c["category"] == "missing_auth" for c in ep["GET /health"]["cases"])
    assert all(c["outcome"] == "expected" for c in ep["GET /todos"]["cases"])
    assert (workspace.api_dir("todo") / "report.md").exists()

    # secrets never persisted
    for f in workspace.api_dir("todo").rglob("*"):
        if f.is_file():
            assert "e2e-secret-token" not in f.read_text(), f

    # drift: second snapshot against the same server is clean
    runner.invoke(cli, ["discover", "--api", "todo"])
    runner.invoke(cli, ["contract", "--api", "todo"])
    assert json.loads((workspace.api_dir("todo") / "drift.json").read_text())["items"] == []


def _setup_api(runner, port, writes=True):
    url = f"http://127.0.0.1:{port}"
    src = str(ROOT / "examples" / "todo_app")
    args = ["init", "todo", "--url", url, "--source", src] + (["--writes"] if writes else [])
    assert runner.invoke(cli, args).exit_code == 0
    cfg = workspace.api_dir("todo") / "api.toml"
    cfg.write_text(cfg.read_text().replace('# Authorization = "MY_API_TOKEN_HEADER_VALUE"', 'Authorization = "TODO_AUTH_HEADER"'))
    return url


def _outcomes(api="todo"):
    rep = json.loads((workspace.api_dir(api) / "report.json").read_text())
    return {c["case_id"]: (c["status"], c["outcome"]) for e in rep["endpoints"] for c in e["cases"]}, rep


def test_pipeline_twice_on_same_server_gives_same_outcomes(server, tmp_path, monkeypatch):
    monkeypatch.setenv("GHOST_HOME", str(tmp_path / ".ghost"))
    monkeypatch.setenv("TODO_AUTH_HEADER", "Bearer e2e-secret-token")
    runner = CliRunner()
    url = _setup_api(runner, server)
    auth = {"Authorization": "Bearer e2e-secret-token"}

    def server_todos():
        return httpx.get(f"{url}/todos", headers=auth).json()

    seed = server_todos()
    assert [t["id"] for t in seed] == [1]

    runs = []
    for _ in range(3):
        out = runner.invoke(cli, ["pipeline", "--api", "todo"])
        assert out.exit_code == 0, out.output
        runs.append(_outcomes())
        # Ghost cleaned up after itself: the server holds exactly its seed data again.
        assert server_todos() == seed

    first, rep = runs[0]
    for later, _ in runs[1:]:
        assert later == first  # same statuses and outcomes on every re-run
    # Ghost's own writes cause no mismatches: the happy GET/PUT/DELETE use the id it created.
    ids = {cid: v for cid, v in first.items()}
    assert ids["post_todos.happy.1"] == (201, "expected")
    assert ids["get_todos_todo_id.happy.1"] == (200, "expected")
    assert ids["put_todos_todo_id.happy.1"] == (200, "expected")
    assert ids["delete_todos_todo_id.happy.1"] == (204, "expected")
    todo_cases = {cid: v for cid, v in first.items() if cid.split("_")[0] in ("post", "get", "put", "delete") and "todos" in cid}
    assert todo_cases and not any(o == "mismatch" for _, o in todo_cases.values()), todo_cases
    assert rep["state"]["teardown"] and not rep["state"]["teardown_problems"]
    # seed id 1 survived (the old DELETE happy removed it)
    assert httpx.get(f"{url}/todos/1", headers=auth).status_code == 200


def test_reset_hook_is_an_http_call_and_respects_write_gate(server, tmp_path, monkeypatch):
    monkeypatch.setenv("GHOST_HOME", str(tmp_path / ".ghost"))
    monkeypatch.setenv("TODO_AUTH_HEADER", "Bearer e2e-secret-token")
    runner = CliRunner()
    _setup_api(runner, server, writes=False)
    cfg = workspace.api_dir("todo") / "api.toml"
    cfg.write_text(cfg.read_text() + '\n[reset]\nmethod = "POST"\npath = "/calc/divide"\n')
    assert runner.invoke(cli, ["pipeline", "--api", "todo"]).exit_code == 0
    res = json.loads((workspace.api_dir("todo") / "results" / "results.json").read_text())
    assert res["reset"]["outcome"] == "skipped" and res["reset"]["status"] is None  # writes not allowed: no call
    assert res["teardown"] == []
