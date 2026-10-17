"""Unit tests for stateful-run support: unique values, setup/captures/teardown, [reset], validator rules."""

import json

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from ghost import workspace
from ghost.contract import run_contract
from ghost.discover import snapshot_spec
from ghost.gen import run_gen
from ghost.runner import run_all
from ghost.schema import example_from_schema
from ghost.scan import run_scan
from ghost.state import MissingValue, extract, resolve, uniquify, unique_string
from ghost.validate import conforms, run_validate


# --- placeholders and unique values -----------------------------------------

def test_resolve_keeps_types_and_nonce_width():
    vals = {"todo_id": 7}
    assert resolve("{{todo_id}}", vals, "abcdef") == 7
    assert resolve("t-{{todo_id}}-{{nonce}}-{{nonce:3}}", vals, "abcdef") == "t-7-abcdef-abc"
    assert resolve({"a": ["{{nonce:2}}", 5]}, vals, "abcdef") == {"a": ["ab", 5]}
    with pytest.raises(MissingValue):
        resolve("{{nope}}", vals, "abcdef")


def test_extract_dotted_paths_scalars_only():
    body = {"id": 3, "data": {"uid": "x"}, "items": [{"id": 9}], "flag": True, "obj": {}}
    assert extract(body, "id") == 3 and extract(body, "data.uid") == "x" and extract(body, "items.0.id") == 9
    assert extract(body, "flag") is None and extract(body, "obj") is None and extract(body, "zzz") is None


@pytest.mark.parametrize("schema", [
    {"type": "string"},
    {"type": "string", "minLength": 1, "maxLength": 60},
    {"type": "string", "maxLength": 8},
    {"type": "string", "maxLength": 3},
    {"type": "string", "minLength": 20, "maxLength": 25},
    {"type": "string", "format": "email"},
    {"type": "string", "format": "email", "maxLength": 20},
    {"anyOf": [{"type": "string", "maxLength": 10}, {"type": "null"}]},
])
def test_unique_string_always_satisfies_schema(schema):
    tpl = unique_string(schema, {})
    assert tpl is not None
    for nonce in ("a1b2c3", "ffffff", "000000"):
        value = resolve(tpl, {}, nonce)
        sub = schema if "anyOf" not in schema else schema["anyOf"][0]
        assert conforms(value, sub, {}), (tpl, value)


@pytest.mark.parametrize("schema", [
    {"type": "string", "enum": ["a", "b"]}, {"type": "string", "pattern": "^[a-z]+$"},
    {"type": "string", "format": "uuid"}, {"type": "string", "maxLength": 0}, {"type": "integer"},
    {"type": "string", "format": "email", "maxLength": 12},
])
def test_unique_string_leaves_constrained_fields_alone(schema):
    assert unique_string(schema, {}) is None


def test_uniquify_only_touches_likely_unique_strings():
    schema = {"type": "object", "properties": {
        "title": {"type": "string", "maxLength": 60}, "note": {"type": "string"},
        "email": {"type": "string", "format": "email"}, "kind": {"type": "string", "enum": ["a"]},
        "name": {"type": "string", "pattern": "^g"}, "priority": {"type": "integer"},
        "owner": {"type": "object", "properties": {"username": {"type": "string"}}}}}
    ex = example_from_schema(schema, {})
    out = uniquify(ex, schema, {})
    assert out["title"].startswith("ghost-") and "{{nonce" in out["title"] and out["email"].endswith("@example.com")
    assert out["note"] == ex["note"] and out["kind"] == ex["kind"] and out["name"] == ex["name"]
    assert "{{nonce" in out["owner"]["username"]
    assert conforms(resolve(out, {}, "abc123"), schema, {})


def test_reset_config_is_http_only(tmp_path, monkeypatch):
    monkeypatch.setenv("GHOST_HOME", str(tmp_path / ".ghost"))
    workspace.write_default_config("a", "http://localhost:1", None, True)
    p = workspace.api_dir("a") / "api.toml"
    base = p.read_text()
    p.write_text(base + '\n[reset]\nmethod = "post"\npath = "/admin/reset"\n')
    assert workspace.load_config("a").reset == {"method": "POST", "path": "/admin/reset"}
    for bad in ('command = "rm -rf /"', 'method = "GET"\npath = "/x"', 'path = "http://evil.example/x"', 'path = "//evil/x"'):
        p.write_text(base + f"\n[reset]\n{bad}\n")
        with pytest.raises(ValueError):
            workspace.load_config("a")


# --- generation + validation + runner on an in-process stateful app ----------

TOKEN = "Bearer t0k"


class ItemOut(BaseModel):
    id: int
    title: str


class ItemIn(BaseModel):
    title: str = Field(min_length=1, max_length=20)
    email: str | None = None


def build_app(state):
    app = FastAPI()
    state["items"] = {1: {"id": 1, "title": "seed"}}
    state["next"] = 2
    state["calls"] = []

    @app.post("/items", status_code=201, response_model=ItemOut)
    def create(item: ItemIn):
        state["calls"].append(("POST", "/items"))
        if any(i["title"] == item.title for i in state["items"].values()):
            raise HTTPException(409, "exists")
        i = {"id": state["next"], "title": item.title}
        state["items"][state["next"]] = i
        state["next"] += 1
        return i

    @app.get("/items/{item_id}", responses={404: {"description": "x"}}, response_model=ItemOut)
    def get(item_id: int):
        if item_id not in state["items"]:
            raise HTTPException(404, "nf")
        return state["items"][item_id]

    @app.put("/items/{item_id}", responses={404: {"description": "x"}}, response_model=ItemOut)
    def put(item_id: int, item: ItemIn):
        if item_id not in state["items"]:
            raise HTTPException(404, "nf")
        state["items"][item_id]["title"] = item.title
        return state["items"][item_id]

    @app.delete("/items/{item_id}", status_code=204, responses={404: {"description": "x"}})
    def delete(item_id: int):
        state["calls"].append(("DELETE", item_id))
        if item_id not in state["items"]:
            raise HTTPException(404, "nf")
        del state["items"][item_id]

    @app.post("/admin/reset", include_in_schema=False)
    def reset():
        state["calls"].append(("RESET", None))
        state["items"] = {1: {"id": 1, "title": "seed"}}
        return {"ok": True}

    return app


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("GHOST_HOME", str(tmp_path / ".ghost"))
    state: dict = {}
    client = TestClient(build_app(state), base_url="http://localhost:1")

    def prepare(extra_toml="", writes=True, top=""):
        workspace.write_default_config("x", "http://localhost:1", None, writes)
        p = workspace.api_dir("x") / "api.toml"
        p.write_text(p.read_text().replace("samples = 5", "samples = 2\n" + top) + extra_toml)
        snapshot_spec("x", client)
        run_scan("x")
        run_contract("x")
        run_gen("x")
        return run_validate("x")

    return state, client, prepare


def cases(op):
    return {c["id"]: c for c in json.loads((workspace.api_dir("x") / "cases" / f"{op}.json").read_text())["cases"]}


def test_gen_chains_and_unique_values(env):
    state, client, prepare = env
    prepare()
    post = cases("post_items")["post_items.happy.1"]
    assert post["request"]["body"]["title"].startswith("ghost-") and "{{nonce" in post["request"]["body"]["title"]
    assert post["captures"] == {"item_id": "id"}
    td = post["teardown"]
    assert td["method"] == "DELETE" and td["path"] == "/items/{item_id}" and td["destructive"] is True
    get = next(c for c in cases("get_items_item_id").values() if c["category"] == "happy")
    assert get["setup"] == [post["id"]] and get["request"]["path_params"] == {"item_id": "{{item_id}}"}
    nf = next(c for c in cases("get_items_item_id").values() if c["category"] == "not_found")
    assert "setup" not in nf and nf["request"]["path_params"]["item_id"] == 999999999
    assert next(c for c in cases("delete_items_item_id").values() if c["category"] == "happy")["setup"] == [post["id"]]
    # invalid payloads stay unmodified, so they still fail the schema
    inv = [c for c in cases("post_items").values() if c["category"] == "invalid_payload"]
    assert inv and all("{{" not in json.dumps(c["request"]["body"]) for c in inv)


def test_writes_disabled_means_no_chains_and_reads_still_run(env):
    state, client, prepare = env
    v = prepare(writes=False)
    get = next(c for c in cases("get_items_item_id").values() if c["category"] == "happy")
    assert "setup" not in get and get["request"]["path_params"] == {"item_id": 1}
    assert v["cases"][get["id"]]["verdict"] == "ok"
    # No DELETE happy without a chain: it would delete seed data.
    assert not any(c["category"] == "happy" for c in cases("delete_items_item_id").values())
    results = run_all("x", client)
    assert not any(r["method"] != "GET" for r in results) and ("DELETE", 1) not in state["calls"]


def test_run_twice_leaves_server_clean_and_outcomes_identical(env):
    state, client, prepare = env
    prepare()
    outs = []
    for _ in range(3):
        res = run_all("x", client)
        outs.append({r["case_id"]: (r["status"], r["outcome"]) for r in res})
        assert list(state["items"]) == [1]  # only the seed remains
    assert outs[0] == outs[1] == outs[2]
    assert outs[0]["post_items.happy.1"] == (201, "expected")
    assert outs[0]["delete_items_item_id.happy.1"] == (204, "expected")
    assert ("DELETE", 1) not in state["calls"]  # seed data never deleted
    saved = json.loads((workspace.api_dir("x") / "results" / "results.json").read_text())
    assert saved["teardown"][0]["outcome"] == "gone"  # DELETE happy already removed it
    assert "captured" not in json.dumps(saved["results"])


def test_teardown_deletes_what_it_created_when_nothing_else_does(env):
    state, client, prepare = env
    prepare()
    # drop the DELETE happy so only the teardown removes the created item
    path = workspace.api_dir("x") / "cases" / "delete_items_item_id.json"
    data = json.loads(path.read_text())
    data["cases"] = [c for c in data["cases"] if c["category"] != "happy"]
    path.write_text(json.dumps(data))
    run_validate("x")
    run_all("x", client)
    saved = json.loads((workspace.api_dir("x") / "results" / "results.json").read_text())
    assert saved["teardown"][0]["outcome"] == "ok" and saved["teardown"][0]["status"] == 204
    assert list(state["items"]) == [1]


def test_teardown_failure_is_reported_not_raised(env):
    state, client, prepare = env
    prepare()
    real = client.request

    def flaky(method, url, **kw):
        if method == "DELETE" and "params" not in kw:  # only the runner's teardown call has no `params` key
            raise httpx.ConnectError("boom")
        return real(method, url, **kw)

    client.request = flaky
    run_all("x", client)  # does not raise
    saved = json.loads((workspace.api_dir("x") / "results" / "results.json").read_text())
    assert saved["teardown"][0]["outcome"] == "failed" and "ConnectError" in saved["teardown"][0]["error"]
    assert all(r["outcome"] != "error" for r in saved["results"] if r["case_id"].startswith("post_items.happy"))
    from ghost.report import build_report, render_markdown
    rep = build_report("x")
    assert rep["state"]["teardown"] == {"failed": 1} and rep["state"]["teardown_problems"]
    assert "Cleanup of data Ghost created: 1 failed" in render_markdown(rep)


def test_reset_runs_first_only_when_writes_allowed(env):
    state, client, prepare = env
    prepare('\n[reset]\nmethod = "POST"\npath = "/admin/reset"\n')
    run_all("x", client)
    assert state["calls"][0] == ("RESET", None)
    saved = json.loads((workspace.api_dir("x") / "results" / "results.json").read_text())
    assert saved["reset"]["outcome"] == "ok" and saved["reset"]["status"] == 200


def test_reset_skipped_when_writes_disabled_or_blocked(env):
    state, client, prepare = env
    prepare('\n[reset]\nmethod = "POST"\npath = "/admin/reset"\n', writes=False)
    run_all("x", client)
    assert ("RESET", None) not in state["calls"]
    assert json.loads((workspace.api_dir("x") / "results" / "results.json").read_text())["reset"]["outcome"] == "skipped"
    p = workspace.api_dir("x") / "api.toml"
    p.write_text(p.read_text().replace("allow_writes = false", "allow_writes = true").replace("blocked_paths = []", 'blocked_paths = ["/admin"]'))
    run_validate("x")
    run_all("x", client)
    assert ("RESET", None) not in state["calls"]
    assert "blocked_paths" in json.loads((workspace.api_dir("x") / "results" / "results.json").read_text())["reset"]["error"]


def test_max_requests_still_bounds_everything_including_teardown(env):
    state, client, prepare = env
    prepare(top='max_requests = 6')
    before = len(state["calls"])
    run_all("x", client)
    saved = json.loads((workspace.api_dir("x") / "results" / "results.json").read_text())
    sent = sum(len(r["timings_ms"]) for r in saved["results"]) + sum(1 for t in saved["teardown"] if t["status"] is not None)
    assert sent <= 6
    assert list(state["items"]) == [1]  # budget was reserved for cleanup


def test_missing_capture_skips_dependent_case(env):
    state, client, prepare = env
    prepare()
    # Make the creating POST fail so nothing is captured.
    path = workspace.api_dir("x") / "cases" / "post_items.json"
    data = json.loads(path.read_text())
    for c in data["cases"]:
        if c["id"] == "post_items.happy.1":
            c["request"]["body"] = {"title": "x" * 20, "email": None}
            c["expect"] = {"status": [201]}
    path.write_text(json.dumps(data))
    state["items"][2] = {"id": 2, "title": "x" * 20}  # now 409
    state["next"] = 3
    results = {r["case_id"]: r for r in run_all("x", client)}
    assert results["post_items.happy.1"]["status"] == 409
    g = results["get_items_item_id.happy.1"]
    assert g["outcome"] == "skipped" and g["status"] is None and "item_id" in g["error"]


# --- validator ---------------------------------------------------------------

def _mutate(op, fn):
    path = workspace.api_dir("x") / "cases" / f"{op}.json"
    data = json.loads(path.read_text())
    fn(data["cases"])
    path.write_text(json.dumps(data))
    return run_validate("x")["cases"]


def test_validator_state_rules(env):
    state, client, prepare = env
    prepare()
    get_happy = "get_items_item_id.happy.1"

    def bad(cs):
        h = next(c for c in cs if c["id"] == get_happy)
        cs += [
            {**h, "id": "g.unknown", "setup": ["nope.1"]},
            {**h, "id": "g.self", "setup": ["g.self"]},
            {**h, "id": "g.cycleA", "setup": ["g.cycleB"], "request": {**h["request"], "headers": {"X-A": "1"}}},
            {**h, "id": "g.cycleB", "setup": ["g.cycleA"], "request": {**h["request"], "headers": {"X-B": "1"}}},
            {**h, "id": "g.nocapture", "setup": [],
             "request": {**h["request"], "headers": {"X-C": "1"}}},
            {**h, "id": "g.undeclared", "setup": [h["setup"][0]],
             "request": {**h["request"], "path_params": {"item_id": "{{other}}"}}},
            {**h, "id": "g.badcap", "captures": {"1bad": "id"}, "request": {**h["request"], "headers": {"X-D": "1"}}},
        ]

    v = _mutate("get_items_item_id", bad)
    assert any(p.startswith("setup_unknown") for p in v["g.unknown"]["problems"])
    assert "setup_self" in v["g.self"]["problems"]
    assert "setup_cycle" in v["g.cycleA"]["problems"] and "setup_cycle" in v["g.cycleB"]["problems"]
    assert "capture_unknown:item_id" in v["g.nocapture"]["problems"]  # references a capture without a setup that makes it
    assert "capture_unknown:other" in v["g.undeclared"]["problems"]
    assert "bad_captures" in v["g.badcap"]["problems"]
    assert v[get_happy]["verdict"] == "ok" and all(v[k]["verdict"] != "ok" for k in ("g.unknown", "g.self", "g.nocapture"))


def test_validator_teardown_honesty_and_gates(env):
    state, client, prepare = env
    prepare()

    def bad(cs):
        h = next(c for c in cs if c["id"] == "post_items.happy.1")
        td = h["teardown"]
        cs += [
            {**h, "id": "t.lie", "teardown": {**td, "destructive": False}, "request": {**h["request"], "headers": {"X-1": "1"}}},
            {**h, "id": "t.unknown", "teardown": {**td, "path": "/nope/{item_id}"}, "request": {**h["request"], "headers": {"X-2": "1"}}},
            {**h, "id": "t.nocap", "captures": {}, "request": {**h["request"], "headers": {"X-3": "1"}}},
            {**h, "id": "t.badparams", "teardown": {**td, "path_params": {"wrong": "{{item_id}}"}},
             "request": {**h["request"], "headers": {"X-4": "1"}}},
        ]

    v = _mutate("post_items", bad)
    assert "teardown_destructive_mislabelled" in v["t.lie"]["problems"]
    assert "teardown_unknown_endpoint" in v["t.unknown"]["problems"]
    assert "teardown_needs_capture:item_id" in v["t.nocap"]["problems"]
    assert any(p.startswith("teardown_path_params_mismatch") for p in v["t.badparams"]["problems"])
    assert v["post_items.happy.1"]["verdict"] == "ok"

    p = workspace.api_dir("x") / "api.toml"
    p.write_text(p.read_text().replace("blocked_paths = []", 'blocked_paths = ["/items/"]'))
    v = run_validate("x")["cases"]
    assert "teardown_not_allowed" in v["post_items.happy.1"]["problems"] and v["post_items.happy.1"]["verdict"] == "drop"


def test_setup_case_that_cannot_run_blocks_dependents(env):
    state, client, prepare = env
    prepare()
    v = _mutate("post_items", lambda cs: next(c for c in cs if c["id"] == "post_items.happy.1").update(destructive=False))
    assert v["post_items.happy.1"]["verdict"] == "fix"
    assert "setup_not_runnable:post_items.happy.1" in v["get_items_item_id.happy.1"]["problems"]
    assert v["get_items_item_id.happy.1"]["verdict"] == "fix"
