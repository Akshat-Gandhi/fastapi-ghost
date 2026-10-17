"""The API contract: what each endpoint accepts and may return, and drift between spec versions (S3)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Any

from . import workspace
from .discover import Endpoint, load_endpoints, load_spec, operation_id, parse_spec
from .scan import RaiseSite, map_sites_to_endpoints
from .schema import deref, example_from_schema

VALIDATION_SHAPE = {"detail": [{"loc": ["body", "<field>"], "msg": "<message>", "type": "<error type>"}]}


def statuses(entries: list[dict]) -> list[int]:
    """Integer HTTP statuses of contract entries. An agent may write "unknown" or "4xx" when the exact status
    is not known; those are skipped rather than crashing every later stage."""
    out = []
    for e in entries:
        s = e.get("status")
        if isinstance(s, str) and s.isdigit():
            s = int(s)
        if isinstance(s, int) and not isinstance(s, bool):
            out.append(s)
    return out


def key_of(ep: Endpoint) -> str:
    return f"{ep.method} {ep.path}"


def spec_hash(spec: dict) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:16]


def _spec_responses(ep: Endpoint, spec: dict) -> dict[str, dict]:
    item = spec.get("paths", {}).get(ep.path, {}).get(ep.method.lower(), {})
    return item.get("responses", {})


def _shape_from_response(resp: dict, spec: dict) -> Any:
    resp = deref(resp, spec)
    schema = resp.get("content", {}).get("application/json", {}).get("schema")
    return example_from_schema(schema, spec) if schema else "unknown"


def build_contract(
    endpoints: list[Endpoint], sites: dict[str, list[RaiseSite]], spec: dict, previous: dict | None = None,
) -> dict:
    """Merge spec + source scan into a contract. Entries marked `pinned` are kept as-is."""
    previous = previous or {}
    out: dict[str, Any] = {}
    for ep in endpoints:
        key = key_of(ep)
        if previous.get(key, {}).get("pinned"):
            out[key] = previous[key]
            continue
        responses = _spec_responses(ep, spec)
        expected: list[dict] = []
        for code, resp in responses.items():
            if not code.isdigit():
                continue
            shape = VALIDATION_SHAPE if code == "422" else _shape_from_response(resp, spec)
            expected.append({"status": int(code), "source": "spec", "error_body_shape": shape if int(code) >= 400 else None,
                             "description": deref(resp, spec).get("description", "")})
        declared = set(statuses(expected))
        for site in sites.get(operation_id(ep), []):
            if site.status_code is None:
                continue
            entry = {
                "status": site.status_code, "source": "code",
                "error_body_shape": "unknown" if site.handler else
                {"detail": site.detail if site.detail is not None else "unknown"},
                "raise_site": f"{site.file}:{site.line}",
                "raise_via": list(site.via),
                "raise_confidence": site.confidence or "direct",
                "undeclared_in_spec": site.status_code not in declared,
            }
            if site.exception:
                entry["raise_exception"] = site.exception
            if site.handler:
                entry["raise_handler"] = site.handler
            if site.detail and not site.detail.startswith(("f'", 'f"')) and site.detail.isascii():
                entry["detail_is_literal"] = site.detail == str(site.detail)
            expected.append(entry)
        body_example = example_from_schema(ep.body_schema, spec) if ep.body_schema else None
        old = previous.get(key, {})
        out[key] = {
            "operation_id": operation_id(ep),
            "summary": ep.summary,
            "auth": "required" if ep.secured else "none",
            "params": [{"name": p.name, "in": p.location, "required": p.required, "schema": p.schema} for p in ep.params],
            "request_body": {"required": ep.body_required, "schema": ep.body_schema} if ep.body_schema else None,
            "request_example": body_example,
            "expected": expected,
            "unexpected": old.get("unexpected", []),
            "spec_hash": spec_hash(spec),
        }
    return out


# --- drift -------------------------------------------------------------------

def _props(schema: dict | None) -> tuple[dict, set]:
    schema = schema or {}
    return schema.get("properties", {}), set(schema.get("required", []))


def _item(kind: str, severity: str, where: str, detail: str) -> dict:
    return {"kind": kind, "severity": severity, "where": where, "detail": detail}


def diff_specs(old_spec: dict, new_spec: dict) -> list[dict]:
    """Compare two OpenAPI documents. severity: breaking | additive | cosmetic."""
    old = {key_of(e): e for e in parse_spec(old_spec)}
    new = {key_of(e): e for e in parse_spec(new_spec)}
    items: list[dict] = []
    for key in sorted(old.keys() - new.keys()):
        items.append(_item("endpoint_removed", "breaking", key, "endpoint no longer in spec"))
    for key in sorted(new.keys() - old.keys()):
        items.append(_item("endpoint_added", "additive", key, "new endpoint"))
    for key in sorted(old.keys() & new.keys()):
        o, n = old[key], new[key]
        for code in sorted(set(o.declared_codes) - set(n.declared_codes)):
            items.append(_item("response_removed", "breaking", key, f"status {code} no longer declared"))
        for code in sorted(set(n.declared_codes) - set(o.declared_codes)):
            items.append(_item("response_added", "additive", key, f"status {code} now declared"))
        op = {(p.name, p.location): p for p in o.params}
        np_ = {(p.name, p.location): p for p in n.params}
        for k in sorted(op.keys() - np_.keys()):
            items.append(_item("param_removed", "breaking", key, f"{k[1]} param '{k[0]}' removed"))
        for k in sorted(np_.keys() - op.keys()):
            sev = "breaking" if np_[k].required else "additive"
            items.append(_item("param_added", sev, key, f"{k[1]} param '{k[0]}' added ({'required' if np_[k].required else 'optional'})"))
        for k in sorted(op.keys() & np_.keys()):
            if op[k].schema.get("type") != np_[k].schema.get("type"):
                items.append(_item("param_type_changed", "breaking", key, f"param '{k[0]}' type {op[k].schema.get('type')} -> {np_[k].schema.get('type')}"))
            if not op[k].required and np_[k].required:
                items.append(_item("param_now_required", "breaking", key, f"param '{k[0]}' is now required"))
        if (o.body_schema is None) != (n.body_schema is None):
            sev = "breaking" if n.body_schema and n.body_required else "additive"
            items.append(_item("body_changed", sev, key, "request body added" if n.body_schema else "request body removed"))
        elif o.body_schema and n.body_schema:
            (op_props, o_req), (np_props, n_req) = _props(o.body_schema), _props(n.body_schema)
            for f in sorted(op_props.keys() - np_props.keys()):
                items.append(_item("field_removed", "breaking", key, f"body field '{f}' removed"))
            for f in sorted(np_props.keys() - op_props.keys()):
                sev = "breaking" if f in n_req else "additive"
                items.append(_item("field_added", sev, key, f"body field '{f}' added ({'required' if f in n_req else 'optional'})"))
            for f in sorted(n_req - o_req):
                if f in op_props:
                    items.append(_item("field_now_required", "breaking", key, f"body field '{f}' is now required"))
            for f in sorted(op_props.keys() & np_props.keys()):
                if op_props[f].get("type") != np_props[f].get("type"):
                    items.append(_item("field_type_changed", "breaking", key, f"body field '{f}' type {op_props[f].get('type')} -> {np_props[f].get('type')}"))
        if o.summary != n.summary:
            items.append(_item("summary_changed", "cosmetic", key, f"'{o.summary}' -> '{n.summary}'"))
    return items


def update_from_results(contract: dict, results: list[dict]) -> dict:
    """Record observed statuses that the contract did not expect, under `unexpected`."""
    for r in results:
        entry = contract.get(f"{r['method']} {r['path']}")
        if not entry or r.get("status") is None:
            continue
        known = set(statuses(entry["expected"]))
        if r["status"] not in known:
            # An agent may have written its own `unexpected` entries (a different shape, no `cases`): tolerate them.
            unexpected = entry.setdefault("unexpected", [])
            seen = next((u for u in unexpected if isinstance(u, dict) and u.get("status") == r["status"]), None)
            if seen is not None:
                seen["cases"] = sorted(set(seen.get("cases") or []) | {r["case_id"]})
            else:
                unexpected.append({"status": r["status"], "source": "observed", "cases": [r["case_id"]]})
    return contract


def run_contract(api: str) -> tuple[dict, list[dict]]:
    endpoints = load_endpoints(api)
    spec = load_spec(api)
    sites_raw = workspace.read_json(api, "source_errors.json", {"sites": []})["sites"]
    sites = [RaiseSite.from_dict(s) for s in sites_raw]
    mapped = map_sites_to_endpoints(sites, endpoints)
    previous = workspace.read_json(api, "contract.json", {}) or {}
    previous = {k: v for k, v in previous.items() if k != "schema_version"}
    contract = build_contract(endpoints, mapped, spec, previous)
    prev_spec_doc = workspace.read_json(api, "spec.prev.json")
    drift = diff_specs(prev_spec_doc["openapi_spec"], spec) if prev_spec_doc else []
    workspace.write_json(api, "contract.json", contract)
    workspace.write_json(api, "drift.json", {"items": drift})
    return contract, drift
