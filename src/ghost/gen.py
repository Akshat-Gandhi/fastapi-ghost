"""Baseline test case generation from the contract (S4). Cases are data (JSON), not code."""

from __future__ import annotations

from typing import Any

from . import workspace
from .contract import key_of
from .discover import Endpoint, load_endpoints, load_spec, operation_id
from .probe import _sample_value
from .schema import deref, example_from_schema, mutate_invalid
from .state import Chain, find_chains, uniquify

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CATEGORIES = {"happy", "invalid_payload", "missing_auth", "bad_auth", "not_found", "conflict", "observe_5xx"}
MAX_CASES_PER_ENDPOINT = 12


def _chain_for(ep: Endpoint, chains: dict[str, Chain]) -> Chain | None:
    """The chain whose created record this endpoint addresses: the item path itself, or a sub-resource of it
    (`/articles/{slug}/comments`) which only needs the parent's id."""
    if ep.method == "POST" and ep.path in chains:
        return None  # that is the creating call
    if ep.path in chains:
        return chains[ep.path] if ep.method in ("GET", "PUT", "PATCH", "DELETE") else None
    parents = [p for p in chains if ep.path.startswith(p + "/")]
    return chains[max(parents, key=len)] if parents else None


def _missing_id(schema: dict) -> Any:
    """An id of the right type that no record has, so the answer is 404 rather than a validation error."""
    if schema.get("type") in ("integer", "number"):
        return 999999999
    return {"uuid": "00000000-0000-4000-8000-00000000dead", "email": "ghost-missing@example.com",
            "date": "1999-01-01", "date-time": "1999-01-01T00:00:00Z"}.get(schema.get("format"), "ghost-missing-id")


def _path_params(ep: Endpoint, spec: dict, missing: bool = False) -> dict[str, Any]:
    out = {}
    for p in ep.params:
        if p.location != "path":
            continue
        schema = deref(p.schema, spec)
        if missing:
            out[p.name] = _missing_id(schema)
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


def baseline_cases(ep: Endpoint, entry: dict, spec: dict, has_auth: bool = False, public: bool = False,
                   chains: dict[str, Chain] | None = None) -> list[dict]:
    """has_auth: api.toml configures credentials. Many apps check auth without declaring it in OpenAPI,
    so when credentials exist every case sends them by default.
    chains: item path -> Chain (see state.find_chains). With a chain, cases on the item path use the id that
    the creating POST returned, and that POST deletes what it created afterwards."""
    chains = chains or {}
    op, n = operation_id(ep), {}
    example = entry.get("request_example")
    auth = "valid" if (ep.secured or has_auth) else "none"
    base = {"path_params": _path_params(ep, spec), "query": _query(ep, spec), "headers": {},
            "body": example, "auth": auth}
    expected_codes = [e["status"] for e in entry["expected"]]
    success = [c for c in expected_codes if 200 <= c < 300] or [200]

    happy_req = dict(base)
    if ep.method in ("POST", "PUT", "PATCH") and ep.body_schema is not None and example is not None:
        happy_req["body"] = uniquify(example, ep.body_schema, spec)  # fresh values each run: no 409 on re-run
    chain = _chain_for(ep, chains)
    chained_params = {chain.param: "{{%s}}" % chain.capture} if chain else None
    if chain:
        happy_req["path_params"] = {**base["path_params"], **chained_params}
    happy = _case(op, n, "happy", "valid request returns success", ep, happy_req, success)
    if chain:
        happy["setup"] = [chain.create_case_id]
    creates = next((c for c in chains.values() if c.create_ep is ep), None) if ep.method == "POST" else None
    if creates:
        happy["captures"] = {creates.capture: creates.capture_path}
        if creates.delete_ep is not None:
            happy["teardown"] = {
                "operation_id": operation_id(creates.delete_ep), "method": "DELETE", "path": creates.item_path,
                "path_params": {creates.param: "{{%s}}" % creates.capture}, "destructive": True,
                "auth": "valid" if (creates.delete_ep.secured or has_auth) else "none",
            }
    cases = []
    if not (ep.method == "DELETE" and not (chain and chain.item_path == ep.path)):
        # A DELETE with no chain would remove data Ghost did not create (seed data): no happy case for it.
        cases.append(happy)

    def add(category: str, title: str, request: dict, status: list[int], chained: bool = True) -> None:
        c = _case(op, n, category, title, ep, request, status)
        if chain and chained:
            c["request"] = {**request, "path_params": {**request["path_params"], **chained_params}}
            c["setup"] = [chain.create_case_id]
        elif ep.method == "DELETE" and request["auth"] != "valid" and not chain:
            c["request"] = {**request, "path_params": _path_params(ep, spec, missing=True)}  # never hit real data
        cases.append(c)

    if ep.body_schema is not None:
        for desc, payload in mutate_invalid(ep.body_schema, example, spec):
            add("invalid_payload", desc, {**base, "body": payload}, [422, 400])
    if ep.secured or (has_auth and not public):
        add("missing_auth", "no credentials", {**base, "auth": "none"}, [401, 403])
        add("bad_auth", "invalid credentials", {**base, "auth": "bad"}, [401, 403])
    if 404 in expected_codes and any(p.location == "path" for p in ep.params):
        add("not_found", "unknown resource id", {**base, "path_params": _path_params(ep, spec, missing=True)}, [404],
            chained=False)
    return cases[:MAX_CASES_PER_ENDPOINT]


def run_gen(api: str) -> int:
    endpoints, spec = load_endpoints(api), load_spec(api)
    cfg = workspace.load_config(api)
    has_auth = cfg.has_auth()
    contract = workspace.read_json(api, "contract.json")
    if not contract:
        raise FileNotFoundError(f"No contract for {api}. Run: ghost contract --api {api}")
    total = 0
    chains: dict[str, Chain] = {}
    if cfg.allow_writes and cfg.host_allowed():  # chains create data: only when writes are allowed
        chains = {
            path: ch for path, ch in find_chains(endpoints, spec, operation_id).items()
            if not any(path.startswith(b) or ch.create_ep.path.startswith(b) for b in cfg.blocked_paths)
        }
    for ep in endpoints:
        entry = contract.get(key_of(ep))
        if not entry:
            continue
        cases = baseline_cases(ep, entry, spec, has_auth, public=ep.path in cfg.public_paths, chains=chains)
        if cfg.login and ep.path == cfg.login["path"]:
            for c in cases:  # Ghost never puts real credentials in a case; made-up ones are rightly refused
                if c["category"] == "happy":
                    c["title"] = "login with made-up credentials is refused (or accepted)"
                    c["expect"]["status"] = sorted({*c["expect"]["status"], 400, 401, 403, 422})
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
