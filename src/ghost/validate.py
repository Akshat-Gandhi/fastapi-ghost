"""Static validation of generated cases before anything is sent (S5)."""

from __future__ import annotations

import json
from typing import Any

import jsonschema

from . import workspace
from .discover import Endpoint, load_endpoints, load_spec, operation_id
from .gen import CATEGORIES, SAFE_METHODS, load_case_files
from .redact import SENSITIVE_HEADERS
from .schema import with_components

_EXPECT_RANGES = {
    "happy": lambda c: 200 <= c < 300,
    "invalid_payload": lambda c: c in (400, 422),
    "missing_auth": lambda c: c in (401, 403),
    "bad_auth": lambda c: c in (401, 403),
    "not_found": lambda c: c == 404,
    "conflict": lambda c: c == 409,
}
_AUTH_FOR = {"missing_auth": "none", "bad_auth": "bad"}
HARD_DROP = {"writes_disabled", "host_not_allowed_for_writes", "blocked_path", "duplicate"}


def conforms(payload: Any, schema: dict, spec: dict) -> bool:
    try:
        jsonschema.validate(payload, with_components(schema, spec))
        return True
    except jsonschema.ValidationError:
        return False
    except jsonschema.SchemaError:
        return True  # unusable schema: do not block the case on it


def validate_case(case: dict, ep: Endpoint, cfg: workspace.ApiConfig, spec: dict) -> tuple[list[str], list[str]]:
    """Return (problems, warnings). Any problem stops the case from running."""
    problems: list[str] = []
    warnings: list[str] = []
    cat = case.get("category")
    req = case.get("request", {})
    statuses = case.get("expect", {}).get("status")

    if cat not in CATEGORIES:
        problems.append("unknown_category")
    if not isinstance(statuses, list) or not statuses or not all(isinstance(s, int) and 100 <= s <= 599 for s in statuses):
        problems.append("bad_expected_status")
    elif cat in _EXPECT_RANGES and not any(_EXPECT_RANGES[cat](s) for s in statuses):
        problems.append("expected_status_does_not_match_category")

    is_write = ep.method not in SAFE_METHODS
    if is_write and not case.get("destructive"):
        problems.append("destructive_mislabelled")
    if case.get("destructive") or is_write:
        if not cfg.allow_writes:
            problems.append("writes_disabled")
        elif not cfg.host_allowed():
            problems.append("host_not_allowed_for_writes")
    if any(ep.path.startswith(b) for b in cfg.blocked_paths):
        problems.append("blocked_path")

    names = {p.name for p in ep.params if p.location == "path"}
    given = set(req.get("path_params", {}))
    if names - given:
        problems.append(f"missing_path_params:{sorted(names - given)}")
    if given - names:
        problems.append(f"unknown_path_params:{sorted(given - names)}")

    leaked = [h for h in req.get("headers", {}) if h.lower() in SENSITIVE_HEADERS]
    if leaked or any(secret in json.dumps(case) for secret in workspace.secret_values(cfg)):
        problems.append("credentials_in_case")  # cases must use the `auth` mode, never literal credentials
    if cat in _AUTH_FOR and req.get("auth") != _AUTH_FOR[cat]:
        problems.append(f"auth_mode_should_be_{_AUTH_FOR[cat]}")
    if req.get("auth") not in ("none", "valid", "bad"):
        problems.append("bad_auth_mode")
    if cat == "happy" and ep.secured and not cfg.auth:
        warnings.append("endpoint_is_secured_but_no_auth_configured")

    if ep.body_schema is not None and "body" in req:
        ok = conforms(req["body"], ep.body_schema, spec)
        if cat == "happy" and not ok:
            problems.append("happy_body_violates_schema")
        if cat == "invalid_payload" and ok:
            problems.append("invalid_case_body_is_valid")
    if cat == "happy" and ep.body_schema is not None and ep.body_required and req.get("body") is None:
        problems.append("happy_missing_required_body")
    return problems, warnings


def run_validate(api: str) -> dict:
    cfg, spec = workspace.load_config(api), load_spec(api)
    by_op = {operation_id(e): e for e in load_endpoints(api)}
    verdicts: dict[str, dict] = {}
    seen: set[str] = set()
    for cf in load_case_files(api):
        ep = by_op.get(cf["operation_id"])
        for case in cf["cases"]:
            if ep is None:
                verdicts[case["id"]] = {"verdict": "drop", "problems": ["unknown_endpoint"], "warnings": []}
                continue
            problems, warnings = validate_case(case, ep, cfg, spec)
            fingerprint = json.dumps([ep.method, ep.path, case["request"]], sort_keys=True, default=str)
            if fingerprint in seen:
                problems.append("duplicate")
            seen.add(fingerprint)
            verdict = "ok" if not problems else ("drop" if set(problems) & HARD_DROP else "fix")
            verdicts[case["id"]] = {"verdict": verdict, "problems": problems, "warnings": warnings}
    counts: dict[str, int] = {}
    for v in verdicts.values():
        counts[v["verdict"]] = counts.get(v["verdict"], 0) + 1
    result = {"cases": verdicts, "summary": counts}
    workspace.write_json(api, "validation.json", result)
    return result
