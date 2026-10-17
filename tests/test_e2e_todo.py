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
