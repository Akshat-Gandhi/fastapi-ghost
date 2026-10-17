"""Helper-layer raise sites: call graph, dependencies, route discovery and exception handlers."""

import importlib
import sys
from pathlib import Path

import pytest

from ghost.contract import build_contract
from ghost.discover import operation_id, parse_spec
from ghost.report import render_markdown
from ghost.scan import RaiseSite, map_sites_to_endpoints, scan_source

ROOT = Path(__file__).parent
SERVICE_APP = ROOT / "fixtures" / "service_app"
TODO_APP = ROOT.parent / "examples" / "todo_app"


@pytest.fixture(scope="module")
def service_app(tmp_path_factory):
    """The real OpenAPI document of the fixture app, so exact route paths are checked against a real spec."""
    sys.path.insert(0, str(SERVICE_APP))
    try:
        spec = importlib.import_module("shop.main").app.openapi()
    finally:
        sys.path.remove(str(SERVICE_APP))
        for name in [m for m in sys.modules if m == "shop" or m.startswith("shop.")]:
            del sys.modules[name]
    return spec


@pytest.fixture(scope="module")
def sites():
    return scan_source(SERVICE_APP)


@pytest.fixture(scope="module")
def mapped(sites, service_app):
    eps = parse_spec(service_app)
    by_key = {f"{e.method} {e.path}": operation_id(e) for e in eps}
    m = map_sites_to_endpoints(sites, eps)
    # (status, via, confidence) per "METHOD path", ignoring the app-wide maintenance dependency
    return {k: sorted((s.status_code, tuple(s.via), s.confidence) for s in m.get(op, [])
                      if s.function != "check_maintenance") for k, op in by_key.items()}


MAINT = ("check_maintenance",)


def test_every_route_gets_app_level_dependency(sites, service_app):
    eps = parse_spec(service_app)
    m = map_sites_to_endpoints(sites, eps)
    for ep in eps:
        got = [(s.status_code, s.via, s.confidence) for s in m[operation_id(ep)] if s.function == "check_maintenance"]
        assert got == [(503, ["check_maintenance"], "via_dependency")], ep.path


def test_route_to_service_to_crud_with_alias_and_handler(mapped):
    assert mapped["GET /api/v1/items/{item_id}"] == [
        (400, ("services.get_item", "fetch"), "via_call"),
        (401, ("get_current_user",), "via_dependency"),  # from the Annotated alias CurrentUser
        (404, ("services.get_item", "fetch"), "via_call"),  # NotFoundError -> add_exception_handler -> 404
    ]


def test_module_attribute_and_instance_self_calls(mapped):
    assert mapped["GET /api/v1/items/"] == [(410, ("crud.list_all",), "via_call")]
    assert mapped["POST /api/v1/items/"] == [(409, ("services.service.create", "self._ensure_unique"), "via_call")]


def test_router_level_and_nested_include_dependencies(mapped):
    for path in ("GET /api/v1/admin/stats", "GET /api/v1/admin/reports/summary"):  # prefix via include_router chain
        assert mapped[path] == [
            (401, ("require_admin", "get_current_user"), "via_dependency"),
            (403, ("require_admin",), "via_dependency"),
        ]


def test_add_api_route_routes_are_direct(mapped):
    assert mapped["GET /ping"] == [(504, (), "direct")]
    assert mapped["GET /legacy/{rid}"] == [(404, (), "direct")]


def test_custom_exception_statuses(sites, mapped):
    assert mapped["GET /api/v1/items/quota"] == [(429, (), "direct")]  # @app.exception_handler(QuotaError)
    assert mapped["GET /api/v1/items/teapot"] == [(418, ("services.brew",), "via_call")]  # HTTPException subclass
    quota = next(s for s in sites if s.exception == "QuotaError")
    assert quota.handler == "shop/main.py:13" and quota.status_code == 429
    teapot = next(s for s in sites if s.exception == "TeapotError")
    assert teapot.detail == "short and stout" and teapot.handler is None


def test_cycle_terminates(mapped):
    assert mapped["GET /api/v1/items/loop"] == [(508, ("services.ping", "pong"), "via_call")]


def test_unresolved_dynamic_calls_make_no_edges(sites, mapped):
    assert mapped["GET /api/v1/items/dyn/{name}"] == []
    secret = next(s for s in sites if s.status_code == 500)
    dead = next(s for s in sites if s.status_code == 507 and s.function == "never_called")
    assert secret.routes == [] and dead.routes == [] and secret.confidence is None


def test_ambiguous_name_keeps_all_candidates(mapped):
    assert mapped["GET /api/v1/items/pick"] == [(502, ("find",), "ambiguous"), (507, ("find",), "ambiguous")]


def test_depth_limit(tmp_path):
    chain = "\n".join(f"def f{i}():\n    f{i + 1}()\n" for i in range(9)) + "def f9():\n    raise HTTPException(500)\n"
    (tmp_path / "m.py").write_text(
        "from fastapi import APIRouter, HTTPException\nrouter = APIRouter()\n"
        "@router.get('/x')\ndef x():\n    f0()\n" + chain)
    (site,) = scan_source(tmp_path)
    assert site.routes == []  # 10 hops away: beyond the depth limit


def test_ambiguous_module_suffix(tmp_path):
    for pkg in ("a", "b"):
        (tmp_path / pkg).mkdir()
        (tmp_path / pkg / "util.py").write_text("from fastapi import HTTPException\ndef go():\n    raise HTTPException(%d)\n"
                                                % (418 if pkg == "a" else 419))
    (tmp_path / "r.py").write_text("from fastapi import APIRouter\nimport util\nrouter = APIRouter()\n"
                                   "@router.get('/x')\ndef x():\n    util.go()\n")
    got = sorted((s.status_code, s.confidence) for s in scan_source(tmp_path))
    assert got == [(418, "ambiguous"), (419, "ambiguous")]


def test_local_variable_shadows_module_name(tmp_path):
    (tmp_path / "crud.py").write_text("from fastapi import HTTPException\ndef get():\n    raise HTTPException(500)\n")
    (tmp_path / "r.py").write_text("from fastapi import APIRouter\nimport crud\nrouter = APIRouter()\n"
                                   "@router.get('/x')\ndef x(crud):\n    crud.get()\n")
    assert all(not s.routes for s in scan_source(tmp_path))


def test_contract_carries_via_and_confidence(sites, service_app):
    eps = parse_spec(service_app)
    contract = build_contract(eps, map_sites_to_endpoints(sites, eps), service_app)
    exp = contract["GET /api/v1/items/{item_id}"]["expected"]
    e404 = next(e for e in exp if e["status"] == 404 and e["source"] == "code")
    assert e404["raise_via"] == ["services.get_item", "fetch"] and e404["raise_confidence"] == "via_call"
    assert e404["raise_site"] == "shop/crud.py:10" and e404["raise_handler"] == "shop/main.py:17"
    assert e404["error_body_shape"] == "unknown" and e404["raise_exception"] == "NotFoundError"
    direct = next(e for e in contract["GET /ping"]["expected"] if e["status"] == 504)
    assert direct["raise_via"] == [] and direct["raise_confidence"] == "direct"


def test_old_source_errors_files_still_load():
    old = {"file": "a.py", "line": 3, "status_code": 404, "detail": "x", "function": "f", "route": ["GET", "/a"]}
    s = RaiseSite.from_dict(old)
    assert s.route == ("GET", "/a") and s.via == [] and s.routes == [] and s.confidence is None


def test_report_markdown_shows_via():
    rep = {"api": "x", "base_url": "http://t", "summary": {"endpoints": 1, "cases_run": 1, "outcomes": {}, "with_findings": 0,
                                                              "drift_items": 0},
           "drift": [], "endpoints": [{
               "method": "GET", "path": "/i", "declared_codes": [], "observed_codes": [], "flags": [],
               "latency_ms": {"p95": None}, "cases": [], "explanations": {},
               "error_responses": [{"case_id": "c", "status": 404, "body": {}, "internal_raise_site": "crud.py:9",
                                    "internal_raise_via": ["get_item", "crud.get_by_id"]}]}]}
    assert "raised at `crud.py:9` via get_item -> crud.get_by_id" in render_markdown(rep)


def test_todo_app_unchanged_direct_raises_plus_dependency():
    sites = scan_source(TODO_APP)
    direct = sorted((s.status_code, s.route) for s in sites if s.confidence == "direct")
    assert direct == [(400, ("GET", "/divide")), (404, ("DELETE", "/{todo_id}")), (404, ("GET", "/{todo_id}")),
                      (404, ("PUT", "/{todo_id}")), (409, ("POST", ""))]
    (auth,) = [s for s in sites if s.status_code == 401]
    assert auth.confidence == "via_dependency" and auth.via == ["require_token"]
    assert {(e["method"], e["full_path"]) for e in auth.routes} == {
        ("GET", "/todos"), ("POST", "/todos"), ("GET", "/todos/{todo_id}"), ("PUT", "/todos/{todo_id}"),
        ("DELETE", "/todos/{todo_id}")}
