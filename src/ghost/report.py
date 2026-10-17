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
