import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from ghost import workspace
from ghost.discover import load_endpoints, operation_id, snapshot_spec
from ghost.scan import map_sites_to_endpoints, scan_source

FIXTURE = Path(__file__).parent / "fixtures" / "mini_app"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("GHOST_HOME", str(tmp_path / ".ghost"))
    return tmp_path


def test_api_slug_is_validated(home):
    with pytest.raises(ValueError):
        workspace.api_dir("../evil")


def test_config_roundtrip_and_no_tokens(home):
    workspace.write_default_config("todo", "http://localhost:8000/", str(FIXTURE), False)
    cfg = workspace.load_config("todo")
    assert cfg.base_url == "http://localhost:8000" and cfg.allow_writes is False
    with pytest.raises(FileExistsError):
        workspace.write_default_config("todo", "http://x", None, False)


def test_auth_modes_and_scrub(home, monkeypatch):
    workspace.write_default_config("a", "http://localhost:1", None, False)
    p = workspace.api_dir("a") / "api.toml"
    p.write_text(p.read_text() + 'Authorization = "TOK"\n')
    monkeypatch.setenv("TOK", "Bearer s3cret")
    cfg = workspace.load_config("a")
    assert workspace.resolve_auth(cfg, "valid") == {"Authorization": "Bearer s3cret"}
    assert workspace.resolve_auth(cfg, "bad") == {"Authorization": workspace.INVALID_TOKEN}
    assert workspace.resolve_auth(cfg, "none") == {}
    assert "s3cret" not in workspace.scrub_secrets("echo Bearer s3cret", cfg)


def test_writes_only_to_local_or_allowed_hosts():
    assert workspace.ApiConfig("http://localhost:1").host_allowed()
    assert not workspace.ApiConfig("https://api.prod.com").host_allowed()
    assert workspace.ApiConfig("https://api.prod.com", allowed_hosts=["api.prod.com"]).host_allowed()


def test_snapshot_rotates_spec(home):
    app = FastAPI()

    @app.get("/a")
    def a(): ...

    workspace.write_default_config("t", "http://t", None, False)
    c = TestClient(app, base_url="http://t")
    snapshot_spec("t", c)
    assert not (workspace.api_dir("t") / "spec.prev.json").exists()
    snapshot_spec("t", c)
    assert (workspace.api_dir("t") / "spec.prev.json").exists()
    eps = load_endpoints("t")
    assert [operation_id(e) for e in eps] == ["get_a"]


def test_scan_finds_literal_constant_fstring_and_nested(home):
    sites = {(s.line, s.status_code): s for s in scan_source(FIXTURE)}
    by_status = {s.status_code: s for s in sites.values()}
    assert set(by_status) == {400, 404, 409, 403}
    assert by_status[404].detail == "Item not found"
    assert by_status[409].detail == "f'{name} exists'"  # dynamic detail keeps its expression
    assert by_status[403].function == "inner" and by_status[403].route == ("POST", "/items")
    assert by_status[400].route is None  # helper function, not directly routed


def test_sites_map_to_prefixed_endpoints(home):
    app = FastAPI()
    from fastapi import APIRouter
    r = APIRouter(prefix="/v1")

    @r.get("/items/{id}")
    def g(id: int): ...

    app.include_router(r)
    workspace.write_default_config("m", "http://t", str(FIXTURE), False)
    snapshot_spec("m", TestClient(app, base_url="http://t"))
    mapped = map_sites_to_endpoints(scan_source(FIXTURE), load_endpoints("m"))
    assert [s.status_code for s in mapped["get_v1_items_id"]] == [404]


def test_cli_init_and_scan(home):
    from ghost.cli import app as cli
    runner = CliRunner()
    assert runner.invoke(cli, ["init", "demo", "--url", "http://localhost:1", "--source", str(FIXTURE)]).exit_code == 0
    assert runner.invoke(cli, ["scan", "--api", "demo"]).exit_code == 0
    data = json.loads((workspace.api_dir("demo") / "source_errors.json").read_text())
    assert len(data["sites"]) == 4 and data["schema_version"] == 1
    assert runner.invoke(cli, ["scan", "--api", "missing"]).exit_code == 2
