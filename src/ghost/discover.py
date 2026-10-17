"""Build a model of a server's endpoints from its /openapi.json."""

from __future__ import annotations

from dataclasses import dataclass, field

import re

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


DEFAULT_SPEC_PATH = "/openapi.json"
_DOCS_URL = re.compile(r"""url:\s*['"]([^'"]+)['"]""")


def _spec_path_from_docs(base_url: str, client: httpx.Client, headers: dict[str, str] | None) -> str | None:
    """Apps with FastAPI(openapi_url=...) still point their Swagger page at the real document."""
    for docs in ("/docs", "/api/docs", "/api/v1/docs"):
        try:
            page = client.get(f"{base_url.rstrip('/')}{docs}", headers=headers or None)
        except httpx.HTTPError:
            continue
        match = _DOCS_URL.search(page.text) if page.status_code == 200 else None
        if match and match.group(1).startswith("/") and not match.group(1).startswith("//"):
            return match.group(1)
    return None


def fetch_spec(
    base_url: str, client: httpx.Client, headers: dict[str, str] | None = None, path: str = "/openapi.json"
) -> dict:
    url = f"{base_url.rstrip('/')}{path}"
    resp = client.get(url, headers=headers or None)
    if resp.status_code == 404 and path == DEFAULT_SPEC_PATH:
        found = _spec_path_from_docs(base_url, client, headers)
        if found and found != path:
            return fetch_spec(base_url, client, headers, found)
    if resp.status_code in (401, 403):
        raise ValueError(
            f"GET {path} returned {resp.status_code}: the API description needs credentials. "
            "Add them under [auth] in api.toml (or as headers in the web form)."
        )
    if resp.status_code >= 400:
        raise ValueError(
            f"GET {path} returned {resp.status_code}. If the API publishes its description elsewhere "
            "(a custom openapi_url or a sub-path), set openapi_path in api.toml."
        )
    try:
        spec = resp.json()
    except ValueError:
        kind = resp.headers.get("content-type", "unknown type").split(";")[0]
        raise ValueError(
            f"GET {path} returned {kind}, not JSON. The server may be showing a setup or login page "
            "instead of the API description, or publishes it at another path (set openapi_path in api.toml)."
        ) from None
    if not isinstance(spec, dict) or not isinstance(spec.get("paths"), dict):
        raise ValueError(f"GET {path} returned JSON, but it is not an OpenAPI document (no 'paths').")
    return spec


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
    from .login import ensure_login

    ensure_login(api, client)  # a token first: some APIs hide their description behind sign-in
    cfg = workspace.load_config(api)
    spec = fetch_spec(cfg.base_url, client, workspace.resolve_auth(cfg, "valid"), cfg.openapi_path)
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
