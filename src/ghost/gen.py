"""Baseline test case generation from the contract (S4). Cases are data (JSON), not code."""

from __future__ import annotations

from typing import Any

from . import workspace
from .contract import key_of
from .discover import Endpoint, load_endpoints, load_spec, operation_id
from .probe import _sample_value
from .schema import deref, example_from_schema, mutate_invalid

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CATEGORIES = {"happy", "invalid_payload", "missing_auth", "bad_auth", "not_found", "conflict", "observe_5xx"}
MAX_CASES_PER_ENDPOINT = 12


def _path_params(ep: Endpoint, spec: dict, missing: bool = False) -> dict[str, Any]:
    out = {}
    for p in ep.params:
        if p.location != "path":
            continue
        schema = deref(p.schema, spec)
        if missing:
            out[p.name] = 999999999 if schema.get("type") in ("integer", "number") else "ghost-missing-id"
        else:
            out[p.name] = example_from_schema(schema, spec) if schema else _sample_value(p.schema)
    return out


def _query(ep: Endpoint, spec: dict) -> dict[str, Any]:
    return {
        p.name: example_from_schema(deref(p.schema, spec), spec)
        for p in ep.params if p.location == "query" and p.required
    }


def _case(op: str, n: dict, category: str, title: str, ep: Endpoint, request: dict, status: list[int]) -> dict:
    n[category] = n.get(category, 0) + 1
    return {
        "id": f"{op}.{category}.{n[category]}", "category": category, "title": title,
        "request": request, "expect": {"status": status},
        "destructive": ep.method not in SAFE_METHODS, "origin": "baseline",
    }


def baseline_cases(ep: Endpoint, entry: dict, spec: dict, has_auth: bool = False, public: bool = False) -> list[dict]:
    """has_auth: api.toml configures credentials. Many apps check auth without declaring it in OpenAPI,
    so when credentials exist every case sends them by default."""
    op, n = operation_id(ep), {}
    base = {"path_params": _path_params(ep, spec), "query": _query(ep, spec), "headers": {},
            "body": entry.get("request_example"), "auth": "valid" if (ep.secured or has_auth) else "none"}
    expected_codes = [e["status"] for e in entry["expected"]]
    success = [c for c in expected_codes if 200 <= c < 300] or [200]
    cases = [_case(op, n, "happy", "valid request returns success", ep, base, success)]

    if ep.body_schema is not None:
        for desc, payload in mutate_invalid(ep.body_schema, entry.get("request_example"), spec):
            cases.append(_case(op, n, "invalid_payload", desc, ep, {**base, "body": payload}, [422, 400]))
    if ep.secured or (has_auth and not public):
        cases.append(_case(op, n, "missing_auth", "no credentials", ep, {**base, "auth": "none"}, [401, 403]))
        cases.append(_case(op, n, "bad_auth", "invalid credentials", ep, {**base, "auth": "bad"}, [401, 403]))
    if 404 in expected_codes and any(p.location == "path" for p in ep.params):
        cases.append(_case(op, n, "not_found", "unknown resource id", ep,
                           {**base, "path_params": _path_params(ep, spec, missing=True)}, [404]))
    return cases[:MAX_CASES_PER_ENDPOINT]


def run_gen(api: str) -> int:
    endpoints, spec = load_endpoints(api), load_spec(api)
    cfg = workspace.load_config(api)
    has_auth = cfg.has_auth()
    contract = workspace.read_json(api, "contract.json")
    if not contract:
        raise FileNotFoundError(f"No contract for {api}. Run: ghost contract --api {api}")
    total = 0
    for ep in endpoints:
        entry = contract.get(key_of(ep))
        if not entry:
            continue
        cases = baseline_cases(ep, entry, spec, has_auth, public=ep.path in cfg.public_paths)
        existing = workspace.read_json(api, f"cases/{operation_id(ep)}.json", {"cases": []})["cases"]
        cases += [c for c in existing if c.get("origin") == "agent"]  # never discard the writer agent's work
        workspace.write_json(
            api, f"cases/{operation_id(ep)}.json",
            {"operation_id": operation_id(ep), "method": ep.method, "path": ep.path, "cases": cases},
        )
        total += len(cases)
    return total


def load_case_files(api: str) -> list[dict]:
    folder = workspace.api_dir(api) / "cases"
    import json
    return [json.loads(p.read_text()) for p in sorted(folder.glob("*.json"))] if folder.exists() else []
