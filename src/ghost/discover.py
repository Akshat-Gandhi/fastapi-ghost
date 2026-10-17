"""Build a model of a server's endpoints from its /openapi.json."""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


@dataclass
class Param:
    name: str
    location: str  # path | query | header | cookie
    required: bool
    schema: dict = field(default_factory=dict)


@dataclass
class Endpoint:
    method: str
    path: str
    summary: str = ""
    params: list[Param] = field(default_factory=list)
    body_schema: dict | None = None
    body_required: bool = False
    declared_codes: list[str] = field(default_factory=list)
    secured: bool = False


def fetch_spec(base_url: str, client: httpx.Client) -> dict:
    resp = client.get(f"{base_url.rstrip('/')}/openapi.json")
    resp.raise_for_status()
    return resp.json()


def _resolve(ref_or_schema: dict, spec: dict) -> dict:
    """Follow a local $ref like '#/components/schemas/Item'."""
    ref = ref_or_schema.get("$ref")
    if not ref or not ref.startswith("#/"):
        return ref_or_schema
    node: dict = spec
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def parse_spec(spec: dict) -> list[Endpoint]:
    endpoints: list[Endpoint] = []
    global_security = bool(spec.get("security"))
    for path, item in spec.get("paths", {}).items():
        shared = item.get("parameters", [])
        for method, op in item.items():
            if method not in HTTP_METHODS:
                continue
            params = [
                Param(
                    name=p["name"],
                    location=p["in"],
                    required=p.get("required", p["in"] == "path"),
                    schema=_resolve(p.get("schema", {}), spec),
                )
                for p in (_resolve(p, spec) for p in shared + op.get("parameters", []))
            ]
            body = op.get("requestBody")
            body_schema = None
            if body:
                body = _resolve(body, spec)
                content = body.get("content", {}).get("application/json")
                if content:
                    body_schema = _resolve(content.get("schema", {}), spec)
            endpoints.append(
                Endpoint(
                    method=method.upper(),
                    path=path,
                    summary=op.get("summary", ""),
                    params=params,
                    body_schema=body_schema,
                    body_required=bool(body and body.get("required")),
                    declared_codes=sorted(op.get("responses", {})),
                    secured=bool(op.get("security")) or global_security,
                )
            )
    return endpoints


def discover(base_url: str, client: httpx.Client) -> list[Endpoint]:
    return parse_spec(fetch_spec(base_url, client))


# --- per-API artifacts -------------------------------------------------------

import re
from dataclasses import asdict

from . import workspace


def operation_id(ep: Endpoint) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", ep.path.lower()).strip("_")
    return f"{ep.method.lower()}_{slug or 'root'}"


def snapshot_spec(api: str, client: httpx.Client) -> list[Endpoint]:
    """S1: save spec.json (rotating the old one to spec.prev.json) and endpoints.json."""
    cfg = workspace.load_config(api)
    spec = fetch_spec(cfg.base_url, client)
    old = workspace.api_dir(api) / "spec.json"
    if old.exists():
        (workspace.api_dir(api) / "spec.prev.json").write_text(old.read_text())
    workspace.write_json(api, "spec.json", {"openapi_spec": spec})
    endpoints = parse_spec(spec)
    workspace.write_json(
        api, "endpoints.json",
        {"endpoints": [{"operation_id": operation_id(e), **asdict(e)} for e in endpoints]},
    )
    return endpoints


def load_endpoints(api: str) -> list[Endpoint]:
    data = workspace.read_json(api, "endpoints.json", {"endpoints": []})
    out = []
    for e in data["endpoints"]:
        e = {k: v for k, v in e.items() if k != "operation_id"}
        e["params"] = [Param(**p) for p in e["params"]]
        out.append(Endpoint(**e))
    return out


def load_spec(api: str) -> dict:
    return workspace.read_json(api, "spec.json", {}).get("openapi_spec", {})
