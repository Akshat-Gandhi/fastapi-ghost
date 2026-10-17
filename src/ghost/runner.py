"""Execute validated cases against the live API and record outcomes (S6)."""

from __future__ import annotations

import json
import statistics
import time
from typing import Any
from urllib.parse import quote

import httpx

from . import workspace
from .contract import update_from_results
from .discover import Endpoint, load_endpoints, operation_id
from .gen import SAFE_METHODS, load_case_files
from .redact import redact_body, redact_headers
from .workspace import ApiConfig, resolve_auth, scrub_secrets

MAX_BODY_CHARS = 2000
# A pooled keep-alive connection can be closed by the server just as we reuse it. That is not an API
# failure, so safe requests get one retry on a fresh connection. Writes are never retried.
_STALE_CONNECTION = (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError, httpx.ConnectError)


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    data = sorted(values)
    return data[min(len(data) - 1, int(round(p / 100 * (len(data) - 1))))]


def build_request(case: dict, ep: Endpoint, cfg: ApiConfig) -> dict:
    req = case["request"]
    path = ep.path
    for name, value in req.get("path_params", {}).items():
        path = path.replace("{" + name + "}", quote(str(value), safe=""))
    headers = {**req.get("headers", {}), **resolve_auth(cfg, req.get("auth", "none"))}
    kwargs: dict[str, Any] = {"params": req.get("query") or None, "headers": headers}
    if req.get("body") is not None:
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


def run_case(case: dict, ep: Endpoint, cfg: ApiConfig, client: httpx.Client, budget: list[int]) -> dict:
    spec = build_request(case, ep, cfg)
    # Writes run once (repeating would create duplicates); safe methods repeat for latency.
    n = cfg.samples if ep.method in SAFE_METHODS else 1
    statuses: list[int | None] = []
    timings: list[float] = []
    body, truncated, error, headers = None, False, None, {}
    for i in range(n):
        if budget[0] <= 0:
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
                if ep.method not in SAFE_METHODS or budget[0] <= 0:
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
            body, truncated = _capture(resp, cfg)
            headers = redact_headers({"content-type": resp.headers.get("content-type", "")})
    if not statuses:
        statuses = [None]
    expected = case["expect"]["status"]
    return {
        "case_id": case["id"], "operation_id": case.get("operation_id") or operation_id(ep), "method": ep.method,
        "path": ep.path, "category": case["category"], "title": case["title"], "auth": case["request"].get("auth"),
        "expected": expected, "status": statuses[0], "statuses": statuses, "body": body,
        "body_truncated": truncated, "response_headers": headers, "timings_ms": timings,
        "outcome": classify_outcome(expected, statuses), "error": error,
    }


def run_all(api: str, client: httpx.Client | None = None) -> list[dict]:
    cfg = workspace.load_config(api)
    validation = workspace.read_json(api, "validation.json")
    if not validation:
        raise FileNotFoundError(f"No validation for {api}. Run: ghost validate --api {api}")
    by_op = {operation_id(e): e for e in load_endpoints(api)}
    budget = [cfg.max_requests]
    own_client = client is None
    client = client or httpx.Client(timeout=cfg.timeout_s)
    results: list[dict] = []
    # Reads first, then POST, PUT/PATCH, DELETE last, so a delete cannot remove data a later read needs.
    order = {"GET": 0, "HEAD": 0, "OPTIONS": 0, "POST": 1, "PUT": 2, "PATCH": 2, "DELETE": 3}
    files = sorted(load_case_files(api), key=lambda cf: order.get(cf["method"], 2))
    try:
        for cf in files:
            ep = by_op.get(cf["operation_id"])
            for case in cf["cases"]:
                verdict = validation["cases"].get(case["id"], {}).get("verdict")
                if verdict != "ok" or ep is None:
                    continue  # S6 never runs unvalidated cases
                # Defence in depth: re-check the write gates here, not only in validate.
                if ep.method not in SAFE_METHODS and (not cfg.allow_writes or not cfg.host_allowed()):
                    continue
                case = {**case, "operation_id": cf["operation_id"]}
                results.append(run_case(case, ep, cfg, client, budget))
    finally:
        if own_client:
            client.close()
    workspace.write_json(api, "results/results.json", {"results": results})
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
