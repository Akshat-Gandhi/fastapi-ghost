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
from .state import BUILTIN, CAPTURE_NAME, names_in, sample_nonce_only

_EXPECT_RANGES = {
    "happy": lambda c: 200 <= c < 300,
    "invalid_payload": lambda c: c in (400, 422),
    # Apps differ: bad credentials often answer 400 (or 423 when locked), a duplicate often answers 400.
    "missing_auth": lambda c: c in (400, 401, 403, 422),
    "bad_auth": lambda c: c in (400, 401, 403, 423),
    "not_found": lambda c: c == 404,
    "conflict": lambda c: c in (400, 409),
}
_EXPECT_HINT = {"happy": "2xx", "invalid_payload": "400 or 422", "missing_auth": "400, 401, 403 or 422",
                "bad_auth": "400, 401, 403 or 423", "not_found": "404", "conflict": "400 or 409"}
_AUTH_FOR = {"missing_auth": "none", "bad_auth": "bad"}
HARD_DROP = {"writes_disabled", "host_not_allowed_for_writes", "blocked_path", "duplicate", "teardown_not_allowed"}


def _contains_secret(node, secrets: list[str]) -> bool:
    """A literal credential among the case's values. Compared on values, not keys or substrings of short words:
    a short password such as "secret" must not trip on a field named client_secret."""
    if isinstance(node, dict):
        return any(_contains_secret(v, secrets) for v in node.values())
    if isinstance(node, list):
        return any(_contains_secret(v, secrets) for v in node)
    if isinstance(node, str):
        return any(node == s or (len(s) >= 8 and s in node) for s in secrets)
    return False


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
        warnings.append(f"hint: a '{cat}' case must expect {_EXPECT_HINT[cat]}; change the category, not the status")

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
    if leaked or _contains_secret(case, workspace.secret_values(cfg)):
        problems.append("credentials_in_case")  # cases must use the `auth` mode, never literal credentials
    if cat in _AUTH_FOR and req.get("auth") != _AUTH_FOR[cat]:
        problems.append(f"auth_mode_should_be_{_AUTH_FOR[cat]}")
    if req.get("auth") not in ("none", "valid", "bad"):
        problems.append("bad_auth_mode")
    if cat == "happy" and ep.secured and not cfg.has_auth():
        warnings.append("endpoint_is_secured_but_no_auth_configured")

    if ep.body_schema is not None and "body" in req:
        body, dynamic = sample_nonce_only(req["body"])  # {{nonce}} is checked as a stand-in of the same length
        if dynamic:
            warnings.append("body_uses_captured_values_not_schema_checked")
        else:
            ok = conforms(body, ep.body_schema, spec)
            if cat == "happy" and not ok:
                problems.append("happy_body_violates_schema")
            if cat == "invalid_payload" and ok:
                problems.append("invalid_case_body_is_valid")
    if cat == "happy" and ep.body_schema is not None and ep.body_required and req.get("body") is None:
        problems.append("happy_missing_required_body")
    return problems, warnings


def _setup_closure(case: dict, index: dict[str, tuple[dict, Endpoint]]) -> tuple[set[str], list[str]]:
    """(ids reachable through `setup`, problems found while walking: unknown ids, cycles)."""
    problems: list[str] = []
    seen: set[str] = set()
    stack = list(case.get("setup") or [])
    while stack:
        sid = stack.pop()
        if sid in seen:
            continue
        seen.add(sid)
        if sid not in index:
            continue
        stack.extend(index[sid][0].get("setup") or [])
    if case.get("id") in seen:
        problems.append("setup_cycle")
    return seen, problems


def validate_state(case: dict, ep: Endpoint, cfg: workspace.ApiConfig, index: dict[str, tuple[dict, Endpoint]],
                   endpoints: dict[tuple[str, str], Endpoint]) -> list[str]:
    """Checks for the stateful fields: setup (order exists), captures (referenced ones exist), teardown (honest)."""
    problems: list[str] = []
    setup, captures, td = case.get("setup"), case.get("captures"), case.get("teardown")
    if setup is not None and not (isinstance(setup, list) and all(isinstance(x, str) for x in setup)):
        problems.append("bad_setup")
        setup = []
    if captures is not None and not (
        isinstance(captures, dict) and all(CAPTURE_NAME.match(k) and k not in BUILTIN and isinstance(v, str) and v
                                           for k, v in captures.items())
    ):
        problems.append("bad_captures")
        captures = {}
    for sid in setup or []:
        if sid == case.get("id"):
            problems.append("setup_self")
        elif sid not in index:
            problems.append(f"setup_unknown:{sid}")
    closure, cyc = _setup_closure({**case, "setup": setup or []}, index)
    problems += cyc
    available = {name for sid in closure if sid in index for name in (index[sid][0].get("captures") or {})}
    req = case.get("request", {})
    for name in sorted(names_in(req) - BUILTIN - available):
        problems.append(f"capture_unknown:{name}")
    if td is not None:
        if not isinstance(td, dict):
            return problems + ["bad_teardown"]
        tep = endpoints.get((str(td.get("method")), str(td.get("path"))))
        if tep is None:
            problems.append("teardown_unknown_endpoint")
        else:
            unsafe = tep.method not in SAFE_METHODS
            if bool(td.get("destructive")) != unsafe:
                problems.append("teardown_destructive_mislabelled")
            if unsafe and (not cfg.allow_writes or not cfg.host_allowed()) or any(tep.path.startswith(b) for b in cfg.blocked_paths):
                problems.append("teardown_not_allowed")
            given, needed = set(td.get("path_params", {})), {p.name for p in tep.params if p.location == "path"}
            if given != needed:
                problems.append(f"teardown_path_params_mismatch:{sorted(needed ^ given)}")
        if td.get("auth", "none") not in ("none", "valid", "bad"):
            problems.append("bad_auth_mode")
        own = set(captures or {})
        for name in sorted(names_in(td.get("path_params", {})) - BUILTIN - own):
            problems.append(f"teardown_needs_capture:{name}")
    return problems


def run_validate(api: str) -> dict:
    cfg, spec = workspace.load_config(api), load_spec(api)
    by_op = {operation_id(e): e for e in load_endpoints(api)}
    verdicts: dict[str, dict] = {}
    seen: set[str] = set()
    files = load_case_files(api)
    index = {c["id"]: (c, by_op[cf["operation_id"]]) for cf in files if cf["operation_id"] in by_op for c in cf["cases"]}
    by_key = {(e.method, e.path): e for e in by_op.values()}
    for cf in files:
        ep = by_op.get(cf["operation_id"])
        for case in cf["cases"]:
            if ep is None:
                verdicts[case["id"]] = {"verdict": "drop", "problems": ["unknown_endpoint"], "warnings": []}
                continue
            if case["id"] in verdicts:  # two cases with one id would silently hide each other
                verdicts[case["id"]]["problems"].append("duplicate_id")
                verdicts[case["id"]]["verdict"] = "fix" if verdicts[case["id"]]["verdict"] == "ok" else verdicts[case["id"]]["verdict"]
                continue
            problems, warnings = validate_case(case, ep, cfg, spec)
            problems += validate_state(case, ep, cfg, index, by_key)
            fingerprint = json.dumps([ep.method, ep.path, case["request"], case.get("expect")], sort_keys=True, default=str)
            if fingerprint in seen:
                problems.append("duplicate")
            seen.add(fingerprint)
            verdict = "ok" if not problems else ("drop" if set(problems) & HARD_DROP else "fix")
            verdicts[case["id"]] = {"verdict": verdict, "problems": problems, "warnings": warnings}
    # A case whose setup case will not run cannot run either (repeat until chains settle).
    for _ in range(len(verdicts) + 1):
        changed = False
        for cf in files:
            for case in cf["cases"]:
                v = verdicts.get(case["id"])
                if not v or v["verdict"] != "ok":
                    continue
                bad = [sid for sid in case.get("setup") or [] if sid in verdicts and verdicts[sid]["verdict"] != "ok"]
                if bad:
                    v["verdict"] = "fix"
                    v["problems"] += [f"setup_not_runnable:{sid}" for sid in bad]
                    changed = True
        if not changed:
            break
    # The case-validator agent's semantic judgement. It may only make a verdict stricter, never loosen one.
    review = workspace.read_json(api, "validation_review.json", {"cases": {}}).get("cases", {})
    rank = {"ok": 0, "fix": 1, "drop": 2}
    for cid, r in review.items():
        mine = verdicts.get(cid)
        if mine and r.get("verdict") in rank and rank[r["verdict"]] > rank[mine["verdict"]]:
            mine["verdict"] = r["verdict"]
            mine["problems"].append(f"review: {r.get('reason', 'no reason given')}")
    counts: dict[str, int] = {}
    for v in verdicts.values():
        counts[v["verdict"]] = counts.get(v["verdict"], 0) + 1
    result = {"cases": verdicts, "summary": counts}
    workspace.write_json(api, "validation.json", result)
    return result
