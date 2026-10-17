import json
from typing import Optional

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field
from typer.testing import CliRunner

from ghost import workspace
from ghost.cli import app as cli
from ghost.contract import diff_specs
from ghost.discover import snapshot_spec
from ghost.schema import example_from_schema, mutate_invalid
from ghost.validate import conforms


class TodoIn(BaseModel):
    title: str = Field(min_length=1, max_length=50)
    priority: int = Field(default=1, ge=1, le=5)
    done: bool = False
    note: Optional[str] = None


def make_app(extra_field=False):
    app = FastAPI()

    class In(TodoIn):
        pass

    if extra_field:
        class In(TodoIn):  # noqa: F811
            owner: str

    @app.get("/todos/{todo_id}", responses={404: {"description": "missing"}})
    def get_todo(todo_id: int):
        raise HTTPException(404, "Todo not found")

    @app.post("/todos", status_code=201)
    def create(todo: In, authorization: str = Header(...)):
        return todo

    return app


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("GHOST_HOME", str(tmp_path / ".ghost"))
    workspace.write_default_config("todo", "http://localhost:1", None, True)
    snapshot_spec("todo", TestClient(make_app(), base_url="http://t"))
    return "todo"


def test_example_satisfies_schema_and_mutations_break_it(api):
    spec = json.loads((workspace.api_dir(api) / "spec.json").read_text())["openapi_spec"]
    schema = spec["components"]["schemas"]["In"]
    ex = example_from_schema({"$ref": "#/components/schemas/In"}, spec)
    assert conforms(ex, schema, spec)
    muts = mutate_invalid(schema, ex, spec)
    assert len(muts) >= 6
    for desc, payload in muts:
        assert not conforms(payload, schema, spec), desc


def test_contract_gen_validate_pipeline(api):
    runner = CliRunner()
    for verb in ("contract", "gen", "validate"):
        assert runner.invoke(cli, [verb, "--api", api]).exit_code == 0, verb
    contract = json.loads((workspace.api_dir(api) / "contract.json").read_text())
    post = contract["POST /todos"]
    assert {e["status"] for e in post["expected"]} >= {201, 422}
    assert post["request_example"]["title"]
    v = json.loads((workspace.api_dir(api) / "validation.json").read_text())
    assert v["summary"].get("fix", 0) == 0 and v["summary"].get("ok", 0) > 5
    cases = json.loads((workspace.api_dir(api) / "cases" / "post_todos.json").read_text())["cases"]
    cats = {c["category"] for c in cases}
    assert {"happy", "invalid_payload"} <= cats
    assert all(c["destructive"] for c in cases)  # POST
    nf = json.loads((workspace.api_dir(api) / "cases" / "get_todos_todo_id.json").read_text())["cases"]
    assert any(c["category"] == "not_found" and c["expect"]["status"] == [404] for c in nf)


def test_writes_disabled_drops_destructive_cases(api):
    p = workspace.api_dir(api) / "api.toml"
    p.write_text(p.read_text().replace("allow_writes = true", "allow_writes = false"))
    runner = CliRunner()
    for verb in ("contract", "gen", "validate"):
        runner.invoke(cli, [verb, "--api", api])
    v = json.loads((workspace.api_dir(api) / "validation.json").read_text())["cases"]
    post_ids = [k for k in v if k.startswith("post_todos")]
    assert post_ids and all(v[k]["verdict"] == "drop" and "writes_disabled" in v[k]["problems"] for k in post_ids)
    assert v["get_todos_todo_id.happy.1"]["verdict"] == "ok"


def test_remote_host_blocks_writes_unless_allowed(api):
    p = workspace.api_dir(api) / "api.toml"
    p.write_text(p.read_text().replace("http://localhost:1", "https://api.prod.example.com"))
    runner = CliRunner()
    for verb in ("contract", "gen", "validate"):
        runner.invoke(cli, [verb, "--api", api])
    v = json.loads((workspace.api_dir(api) / "validation.json").read_text())["cases"]
    assert "host_not_allowed_for_writes" in v["post_todos.happy.1"]["problems"]


def test_validator_catches_bad_cases(api):
    runner = CliRunner()
    for verb in ("contract", "gen"):
        runner.invoke(cli, [verb, "--api", api])
    path = workspace.api_dir(api) / "cases" / "post_todos.json"
    data = json.loads(path.read_text())
    good = data["cases"][0]
    bad_happy = {**good, "id": "x1", "request": {**good["request"], "body": {"title": ""}}}
    valid_invalid = {**good, "id": "x2", "category": "invalid_payload", "expect": {"status": [422]}}
    wrong_status = {**good, "id": "x3", "expect": {"status": [404]}}
    unlabeled = {**good, "id": "x4", "destructive": False}
    dup = {**good, "id": "x5"}
    data["cases"] += [bad_happy, valid_invalid, wrong_status, unlabeled, dup]
    path.write_text(json.dumps(data))
    runner.invoke(cli, ["validate", "--api", api])
    v = json.loads((workspace.api_dir(api) / "validation.json").read_text())["cases"]
    assert "happy_body_violates_schema" in v["x1"]["problems"]
    assert "invalid_case_body_is_valid" in v["x2"]["problems"]
    assert "expected_status_does_not_match_category" in v["x3"]["problems"]
    assert "destructive_mislabelled" in v["x4"]["problems"]
    assert v["x5"]["verdict"] == "drop" and "duplicate" in v["x5"]["problems"]
    assert v["post_todos.happy.1"]["verdict"] == "ok"


def test_drift_classification(api):
    old = json.loads((workspace.api_dir(api) / "spec.json").read_text())["openapi_spec"]
    new = TestClient(make_app(extra_field=True), base_url="http://t").get("/openapi.json").json()
    items = diff_specs(old, new)
    assert any(i["kind"] == "field_added" and i["severity"] == "breaking" and "owner" in i["detail"] for i in items)
    assert diff_specs(old, old) == []
    removed = json.loads(json.dumps(new))
    del removed["paths"]["/todos"]
    assert any(i["kind"] == "endpoint_removed" for i in diff_specs(new, removed))


def test_contract_keeps_pinned_entries_and_records_drift(api):
    runner = CliRunner()
    runner.invoke(cli, ["contract", "--api", api])
    path = workspace.api_dir(api) / "contract.json"
    c = json.loads(path.read_text())
    c["GET /todos/{todo_id}"]["pinned"] = True
    c["GET /todos/{todo_id}"]["summary"] = "HUMAN EDIT"
    path.write_text(json.dumps(c))
    snapshot_spec(api, TestClient(make_app(extra_field=True), base_url="http://t"))
    runner.invoke(cli, ["contract", "--api", api])
    c2 = json.loads(path.read_text())
    assert c2["GET /todos/{todo_id}"]["summary"] == "HUMAN EDIT"
    drift = json.loads((workspace.api_dir(api) / "drift.json").read_text())["items"]
    assert any(i["severity"] == "breaking" for i in drift)
