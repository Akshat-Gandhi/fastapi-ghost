"""Render probe results to the terminal and to report.json."""

from __future__ import annotations

import json
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .probe import ProbeResult

SLOW_MS = 500.0


def _flags(r: ProbeResult) -> list[str]:
    flags = []
    p95 = r.percentile(95)
    if p95 is not None and p95 > SLOW_MS:
        flags.append("SLOW")
    if len(r.codes) > 1:
        flags.append("FLAKY")
    if any(c >= 500 for c in r.codes):
        flags.append("5XX")
    undeclared = [
        c for c in r.observed if str(c) not in r.endpoint.declared_codes
        and "default" not in r.endpoint.declared_codes
    ]
    if undeclared:
        flags.append("UNDECLARED")
    return flags


def to_dict(results: list[ProbeResult]) -> dict:
    out = []
    for r in results:
        ep = r.endpoint
        out.append(
            {
                "method": ep.method,
                "path": ep.path,
                "declared_codes": ep.declared_codes,
                "skipped": r.skipped,
                "error": r.error,
                "observed_codes": {str(k): v for k, v in sorted(r.codes.items())},
                "latency_ms": {
                    "min": r.min_ms,
                    "avg": r.avg_ms,
                    "p95": r.percentile(95),
                    "p99": r.percentile(99),
                },
                "flags": _flags(r),
            }
        )
    return {"endpoints": out}


def print_report(results: list[ProbeResult], console: Console | None = None) -> None:
    console = console or Console()
    table = Table(show_lines=False)
    for col in ("Method", "Path", "Declared", "Observed", "p95", "Flags"):
        table.add_column(col)
    for r in results:
        ep = r.endpoint
        if r.skipped:
            table.add_row(ep.method, ep.path, ",".join(ep.declared_codes), f"[dim]{r.skipped}[/]", "", "")
            continue
        if r.error:
            table.add_row(ep.method, ep.path, ",".join(ep.declared_codes), f"[red]{r.error}[/]", "", "ERROR")
            continue
        p95 = r.percentile(95)
        table.add_row(
            ep.method,
            ep.path,
            ",".join(ep.declared_codes),
            ",".join(str(c) for c in r.observed),
            f"{p95:.0f}ms" if p95 is not None else "",
            " ".join(_flags(r)),
        )
    console.print(table)


def write_json(results: list[ProbeResult], path: Path) -> None:
    path.write_text(json.dumps(to_dict(results), indent=2))


# --- per-API report (S8) -----------------------------------------------------

from . import workspace
from .contract import key_of
from .discover import load_endpoints, operation_id
from .runner import latency_stats


def build_report(api: str) -> dict:
    cfg = workspace.load_config(api)
    contract = workspace.read_json(api, "contract.json", {})
    results = workspace.read_json(api, "results/results.json", {"results": []})["results"]
    drift = workspace.read_json(api, "drift.json", {"items": []})["items"]
    explanations = workspace.read_json(api, "explanations.json", {"explanations": {}}).get("explanations", {})
    drifted = {d["where"] for d in drift}
    endpoints = []
    for ep in load_endpoints(api):
        key = key_of(ep)
        entry = contract.get(key, {})
        mine = [r for r in results if r["operation_id"] == operation_id(ep)]
        expected = entry.get("expected", [])
        declared = sorted({e["status"] for e in expected})
        observed = sorted({s for r in mine for s in r["statuses"] if s is not None})
        timings = [t for r in mine if r["category"] == "happy" for t in r["timings_ms"]]
        lat = latency_stats(timings)
        flags: list[str] = []
        if lat["n"] >= 5 and lat["p95"] is not None and lat["p95"] > cfg.slow_ms:
            flags.append("SLOW")
        if any(r["outcome"] == "flaky" for r in mine):
            flags.append("FLAKY")
        if any(s >= 500 for s in observed):
            flags.append("5XX")
        if any(s not in declared for s in observed):
            flags.append("UNDECLARED")
        if key in drifted:
            flags.append("DRIFT")
        if any(r["outcome"] in ("mismatch", "error") for r in mine):
            flags.append("CASE_FAILED")
        raise_sites = {e["status"]: e["raise_site"] for e in expected if e.get("raise_site")}
        error_responses = []
        for r in mine:
            if r["status"] is not None and r["status"] >= 400:
                error_responses.append({
                    "case_id": r["case_id"], "status": r["status"], "body": r["body"],
                    "internal_raise_site": raise_sites.get(r["status"]),
                })
        endpoints.append({
            "method": ep.method, "path": ep.path, "operation_id": operation_id(ep),
            "declared_codes": declared, "observed_codes": observed, "flags": flags, "latency_ms": lat,
            "cases": [{k: r[k] for k in ("case_id", "category", "title", "expected", "status", "outcome")} for r in mine],
            "error_responses": error_responses,
            "explanations": {r["case_id"]: explanations[r["case_id"]] for r in mine if r["case_id"] in explanations},
        })
    outcomes: dict[str, int] = {}
    for r in results:
        outcomes[r["outcome"]] = outcomes.get(r["outcome"], 0) + 1
    return {
        "api": api, "base_url": cfg.base_url, "summary": {
            "endpoints": len(endpoints), "cases_run": len(results), "outcomes": outcomes,
            "with_findings": sum(1 for e in endpoints if e["flags"]), "drift_items": len(drift),
        },
        "drift": drift, "endpoints": endpoints,
    }


def render_markdown(rep: dict) -> str:
    s = rep["summary"]
    lines = [
        f"# API report: {rep['api']}", "", f"Server: `{rep['base_url']}`  ",
        "This report is informational and never blocks a build.", "",
        f"- Endpoints: {s['endpoints']}  - Cases run: {s['cases_run']}  - Endpoints with findings: {s['with_findings']}",
        f"- Outcomes: {s['outcomes']}  - Spec drift items: {s['drift_items']}", "",
        "| Endpoint | Declared | Observed | p95 | Flags |", "|---|---|---|---|---|",
    ]
    for e in rep["endpoints"]:
        p95 = e["latency_ms"]["p95"]
        lines.append(
            f"| `{e['method']} {e['path']}` | {','.join(map(str, e['declared_codes']))} | "
            f"{','.join(map(str, e['observed_codes']))} | {f'{p95:.0f}ms' if p95 is not None else '-'} | {' '.join(e['flags'])} |"
        )
    if rep["drift"]:
        lines += ["", "## Spec drift", ""]
        lines += [f"- **{d['severity']}** `{d['where']}`: {d['detail']}" for d in rep["drift"]]
    for e in rep["endpoints"]:
        lines += ["", f"## `{e['method']} {e['path']}`", ""]
        for c in e["cases"]:
            lines.append(f"- [{c['outcome']}] {c['category']}: {c['title']} (expected {c['expected']}, got {c['status']})")
        if e["error_responses"]:
            lines += ["", "What the client sees on errors:", ""]
            for er in e["error_responses"]:
                site = f" (raised at `{er['internal_raise_site']}`)" if er["internal_raise_site"] else ""
                lines.append(f"- `{er['status']}`{site}: `{json.dumps(er['body'])[:300]}`")
        for cid, ex in e["explanations"].items():
            lines.append(f"- Explanation for `{cid}`: {ex}")
    return "\n".join(lines) + "\n"


def write_report(api: str) -> dict:
    import shutil
    from datetime import datetime, timezone

    rep = build_report(api)
    workspace.write_json(api, "report.json", rep)
    md = workspace.api_dir(api) / "report.md"
    md.write_text(render_markdown(rep))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive = workspace.api_dir(api) / "runs" / stamp
    archive.mkdir(parents=True, exist_ok=True)
    for name in ("report.json", "report.md", "results/results.json"):
        src = workspace.api_dir(api) / name
        if src.exists():
            shutil.copy(src, archive / src.name)
    return rep
