"""Execute validated cases against the live API and record outcomes (S6)."""

from __future__ import annotations

import json
import statistics
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from . import workspace
from .contract import update_from_results
from .discover import Endpoint, load_endpoints, operation_id
from .gen import SAFE_METHODS, load_case_files
from .redact import redact_body, redact_headers
from .state import MissingValue, extract, names_in, new_nonce, resolve
from .workspace import ApiConfig, resolve_auth, resolve_auth_query, scrub_secrets

MAX_BODY_CHARS = 2000
# A pooled keep-alive connection can be closed by the server just as we reuse it. That is not an API
# failure, so safe requests get one retry on a fresh connection. Writes are never retried.
_STALE_CONNECTION = (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError, httpx.ConnectError)


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    data = sorted(values)
    return data[min(len(data) - 1, int(round(p / 100 * (len(data) - 1))))]


@dataclass
class RunContext:
    """In-memory state of one run: the nonce, values captured from responses, and pending teardowns.
    Never written to disk (captured values may be ids or tokens)."""
    nonce: str = field(default_factory=new_nonce)
    values: dict[str, Any] = field(default_factory=dict)
    teardowns: list[dict] = field(default_factory=list)


def build_request(case: dict, ep: Endpoint, cfg: ApiConfig, ctx: RunContext | None = None) -> dict:
    ctx = ctx or RunContext()
    req = resolve(case["request"], ctx.values, ctx.nonce)  # raises MissingValue if a capture is absent
    path = ep.path
    for name, value in req.get("path_params", {}).items():
        path = path.replace("{" + name + "}", quote(str(value), safe=""))
    headers = {**req.get("headers", {}), **resolve_auth(cfg, req.get("auth", "none"))}
    query = {k: v for k, v in (req.get("query") or {}).items() if k not in cfg.auth_query}
    query.update(resolve_auth_query(cfg, req.get("auth", "none")))
    kwargs: dict[str, Any] = {"params": query or None, "headers": headers}
    if req.get("body") is not None:
        if ep.body_format == "form" and isinstance(req["body"], dict):
            kwargs["data"] = {k: v if isinstance(v, str) else json.dumps(v) for k, v in req["body"].items()}
        else:
            kwargs["json"] = req["body"]
    return {"method": ep.method, "url": cfg.base_url + path, **kwargs}


def classify_outcome(expected: list[int], statuses: list[int | None]) -> str:
    if any(s is None for s in statuses):
        return "error"
    if len(set(statuses)) > 1:
        return "flaky"
    status = statuses[0]
    if status in expected:
        return "expected"
    return "5xx" if status >= 500 else "mismatch"


AUTH_CATEGORIES = ("missing_auth", "bad_auth")
# Statuses that mean "your request was rejected before login mattered" (bad body, unknown id, locked item, ...).
PRECONDITION_STATUSES = {400, 404, 405, 409, 415, 422, 423}


def refine_outcome(outcome: str, case: dict, ep: Endpoint, status: int | None) -> str:
    """Separate real surprises from results that say nothing about the endpoint.

    open: a no-login request worked on an endpoint the docs never call protected (informational).
    inconclusive: the request was refused for a reason unrelated to what the case tests, e.g. the sample
    record does not exist, so there is nothing to learn from it."""
    if outcome != "mismatch" or status is None:
        return outcome
    category = case["category"]
    if category in AUTH_CATEGORIES:
        if 200 <= status < 300 and not ep.secured:
            return "open"
        if status in PRECONDITION_STATUSES:
            return "inconclusive"
    if category in ("happy", "invalid_payload") and status == 404:
        given = (case.get("request") or {}).get("path_params") or {}
        made_up = [p for p in ep.params if p.location == "path" and not str(given.get(p.name, "")).startswith("{{")]
        if made_up:
            return "inconclusive"  # nothing exists at the made-up id; a record Ghost created would be needed
    return outcome


def _capture(resp: httpx.Response, cfg: ApiConfig) -> tuple[Any, bool]:
    text = resp.text
    truncated = len(text) > MAX_BODY_CHARS
    try:
        body: Any = redact_body(resp.json()) if not truncated else text[:MAX_BODY_CHARS]
    except ValueError:
        body = text[:MAX_BODY_CHARS]
    if isinstance(body, str):
        body = scrub_secrets(body, cfg)
    else:
        body = json.loads(scrub_secrets(json.dumps(body), cfg))
    return body, truncated


def _skipped(case: dict, ep: Endpoint, error: str) -> dict:
    return {
        "case_id": case["id"], "operation_id": case.get("operation_id") or operation_id(ep), "method": ep.method,
        "path": ep.path, "category": case["category"], "title": case["title"], "auth": case["request"].get("auth"),
        "expected": case["expect"]["status"], "status": None, "statuses": [None], "body": None,
        "body_truncated": False, "response_headers": {}, "timings_ms": [], "outcome": "skipped", "error": error,
    }


def run_case(case: dict, ep: Endpoint, cfg: ApiConfig, client: httpx.Client, budget: list[int],
             ctx: RunContext | None = None) -> dict:
    ctx = ctx or RunContext()
    try:
        spec = build_request(case, ep, cfg, ctx)
    except MissingValue as exc:
        return _skipped(case, ep, f"not sent: no value for '{exc.args[0]}' (the setup case did not produce it)")
    # Keep requests in reserve for the teardowns of what this run has created (and will create).
    reserve = len(ctx.teardowns) + (1 if case.get("teardown") else 0)
    first: httpx.Response | None = None
    # Writes run once (repeating would create duplicates); safe methods repeat for latency.
    n = cfg.samples if ep.method in SAFE_METHODS else 1
    statuses: list[int | None] = []
    timings: list[float] = []
    body, truncated, error, headers = None, False, None, {}
    for i in range(n):
        if budget[0] <= reserve:
            error = "request budget (max_requests) exhausted"
            break
        budget[0] -= 1
        if i:
            time.sleep(1 / cfg.max_rps)
        start = time.perf_counter()
        try:
            try:
                resp = client.request(**spec)
            except _STALE_CONNECTION:
                if ep.method not in SAFE_METHODS or budget[0] <= reserve:
                    raise
                budget[0] -= 1
                start = time.perf_counter()
                resp = client.request(**spec)
        except httpx.HTTPError as exc:
            statuses.append(None)
            error = scrub_secrets(f"{type(exc).__name__}: {exc}", cfg)
            break
        timings.append((time.perf_counter() - start) * 1000)
        statuses.append(resp.status_code)
        if i == 0:
            first = resp
            body, truncated = _capture(resp, cfg)
            headers = redact_headers({"content-type": resp.headers.get("content-type", "")})
    if not statuses:
        statuses = [None]
    expected = case["expect"]["status"]
    if first is not None and 200 <= first.status_code < 300:
        _record_state(case, first, ctx)
    return {
        "case_id": case["id"], "operation_id": case.get("operation_id") or operation_id(ep), "method": ep.method,
        "path": ep.path, "category": case["category"], "title": case["title"], "auth": case["request"].get("auth"),
        "expected": expected, "status": statuses[0], "statuses": statuses, "body": body,
        "body_truncated": truncated, "response_headers": headers, "timings_ms": timings,
        "outcome": refine_outcome(classify_outcome(expected, statuses), case, ep, statuses[0]), "error": error,
    }


def _record_state(case: dict, resp: httpx.Response, ctx: RunContext) -> None:
    """After a successful response: store captured values and queue the case's teardown."""
    captures = case.get("captures") or {}
    if captures:
        try:
            raw = resp.json()
        except ValueError:
            raw = None
        for name, path in captures.items():
            value = extract(raw, path)
            if value is not None:
                ctx.values[name] = value
    td = case.get("teardown")
    if td and names_in(td) <= (set(ctx.values) | {"nonce"}):
        # Snapshot now: a later case may capture a different value under the same name.
        ctx.teardowns.append({"case_id": case["id"], "teardown": td,
                              "path_params": resolve(td.get("path_params", {}), ctx.values, ctx.nonce)})


def _blocked(path: str, cfg: ApiConfig) -> bool:
    return any(path.startswith(b) for b in cfg.blocked_paths)


def _writes_ok(cfg: ApiConfig) -> bool:
    return cfg.allow_writes and cfg.host_allowed()


def run_reset(cfg: ApiConfig, client: httpx.Client, budget: list[int]) -> dict | None:
    """The optional `[reset]` HTTP call. Same gates as any write. Failure is reported, not fatal."""
    if not cfg.reset:
        return None
    out = {"method": cfg.reset["method"], "path": cfg.reset["path"], "status": None, "outcome": "skipped", "error": None}
    if not _writes_ok(cfg):
        out["error"] = "writes are not allowed for this host (allow_writes / allowed_hosts)"
    elif _blocked(cfg.reset["path"], cfg):
        out["error"] = "reset path is in blocked_paths"
    elif budget[0] <= 0:
        out["error"] = "request budget (max_requests) exhausted"
    else:
        budget[0] -= 1
        try:
            resp = client.request(out["method"], cfg.base_url + out["path"], headers=resolve_auth(cfg, "valid"), params=resolve_auth_query(cfg, "valid") or None)
            out["status"] = resp.status_code
            out["outcome"] = "ok" if resp.status_code < 400 else "failed"
        except httpx.HTTPError as exc:
            out["outcome"], out["error"] = "failed", scrub_secrets(f"{type(exc).__name__}: {exc}", cfg)
    return out


def run_teardowns(ctx: RunContext, cfg: ApiConfig, client: httpx.Client, budget: list[int],
                  endpoints: dict[tuple[str, str], Endpoint]) -> list[dict]:
    """Delete what this run created, newest first. Never raises and never affects case outcomes."""
    out = []
    for item in reversed(ctx.teardowns):
        td = item["teardown"]
        rec = {"case_id": item["case_id"], "method": td.get("method"), "path": td.get("path"),
               "status": None, "outcome": "skipped", "error": None}
        out.append(rec)
        ep = endpoints.get((str(td.get("method")), str(td.get("path"))))
        if ep is None:
            rec["error"] = "endpoint not in spec"
        elif ep.method not in SAFE_METHODS and not _writes_ok(cfg):
            rec["error"] = "writes are not allowed for this host"
        elif _blocked(ep.path, cfg):
            rec["error"] = "path is in blocked_paths"
        elif budget[0] <= 0:
            rec["error"] = "request budget (max_requests) exhausted"
        else:
            budget[0] -= 1
            path = ep.path
            for name, value in item["path_params"].items():
                path = path.replace("{" + name + "}", quote(str(value), safe=""))
            headers = resolve_auth(cfg, td.get("auth", "none"))
            params = resolve_auth_query(cfg, td.get("auth", "none")) or None
            try:
                try:
                    resp = client.request(ep.method, cfg.base_url + path, headers=headers, params=params)
                except _STALE_CONNECTION:
                    # A teardown DELETE is idempotent, so (unlike case writes) one retry on a stale pooled
                    # connection is safe: a 404 on the retry is reported as `gone`.
                    if ep.method != "DELETE" or budget[0] <= 0:
                        raise
                    budget[0] -= 1
                    resp = client.request(ep.method, cfg.base_url + path, headers=headers, params=params)
                rec["status"] = resp.status_code
                rec["outcome"] = "ok" if resp.status_code < 300 else ("gone" if resp.status_code == 404 else "failed")
            except httpx.HTTPError as exc:
                rec["outcome"], rec["error"] = "failed", scrub_secrets(f"{type(exc).__name__}: {exc}", cfg)
            except Exception as exc:  # teardown must never break the run
                rec["outcome"], rec["error"] = "failed", scrub_secrets(f"{type(exc).__name__}: {exc}", cfg)
    return out


def run_all(api: str, client: httpx.Client | None = None) -> list[dict]:
    from .login import ensure_login

    own_login = client or httpx.Client(timeout=workspace.load_config(api).timeout_s)
    try:
        ensure_login(api, own_login)
    finally:
        if client is None:
            own_login.close()
    cfg = workspace.load_config(api)
    validation = workspace.read_json(api, "validation.json")
    if not validation:
        raise FileNotFoundError(f"No validation for {api}. Run: ghost validate --api {api}")
    by_op = {operation_id(e): e for e in load_endpoints(api)}
    budget = [cfg.max_requests]
    own_client = client is None
    client = client or httpx.Client(timeout=cfg.timeout_s)
    results: list[dict] = []
    ctx = RunContext()
    # Reads first, then POST, PUT/PATCH, DELETE last, so a delete cannot remove data a later read needs.
    # A case with `setup` first runs the cases it names (once per run), so chains keep working across that order.
    order = {"GET": 0, "HEAD": 0, "OPTIONS": 0, "POST": 1, "PUT": 2, "PATCH": 2, "DELETE": 3}
    files = sorted(load_case_files(api), key=lambda cf: order.get(cf["method"], 2))
    runnable: dict[str, tuple[dict, Endpoint]] = {}  # case id -> (case, endpoint); the first of a duplicate id
    queue: list[tuple[dict, Endpoint]] = []
    for cf in files:
        ep = by_op.get(cf["operation_id"])
        for case in cf["cases"]:
            verdict = validation["cases"].get(case["id"], {}).get("verdict")
            if verdict != "ok" or ep is None:
                continue  # S6 never runs unvalidated cases
            # Defence in depth: re-check the write gates here, not only in validate.
            if ep.method not in SAFE_METHODS and not _writes_ok(cfg):
                continue
            if _blocked(ep.path, cfg):
                continue
            item = ({**case, "operation_id": cf["operation_id"]}, ep)
            runnable.setdefault(case["id"], item)
            queue.append(item)
    done: set[int] = set()

    def execute(item: tuple[dict, Endpoint]) -> None:
        if id(item) in done:
            return
        done.add(id(item))  # before recursing: a setup cycle cannot loop
        case, ep = item
        for sid in case.get("setup") or []:
            if sid in runnable:
                execute(runnable[sid])
        results.append(run_case(case, ep, cfg, client, budget, ctx))

    reset_result, teardown_results = None, []
    try:
        reset_result = run_reset(cfg, client, budget)
        for item in queue:
            execute(item)
    finally:
        try:
            teardown_results = run_teardowns(ctx, cfg, client, budget, {(e.method, e.path): e for e in by_op.values()})
        finally:
            if own_client:
                client.close()
    state = {"reset": reset_result, "teardown": teardown_results}
    workspace.write_json(api, "results/results.json", {"results": results, **state})
    contract = workspace.read_json(api, "contract.json")
    if contract:
        contract.pop("schema_version", None)
        workspace.write_json(api, "contract.json", update_from_results(contract, results))
    return results


def latency_stats(timings: list[float]) -> dict[str, float | int | None]:
    return {
        "n": len(timings),
        "min": min(timings) if timings else None,
        "avg": statistics.fmean(timings) if timings else None,
        "p50": percentile(timings, 50), "p95": percentile(timings, 95), "p99": percentile(timings, 99),
    }
