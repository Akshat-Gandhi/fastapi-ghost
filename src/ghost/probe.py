"""Send requests to discovered endpoints and record status codes and timing."""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field

import httpx

from .discover import Endpoint

_SAMPLES = {"integer": "1", "number": "1", "boolean": "true", "string": "ghost"}


@dataclass
class ProbeResult:
    endpoint: Endpoint
    skipped: str | None = None
    codes: dict[int, int] = field(default_factory=dict)  # status -> count
    timings_ms: list[float] = field(default_factory=list)
    error: str | None = None

    @property
    def observed(self) -> list[int]:
        return sorted(self.codes)

    def percentile(self, p: float) -> float | None:
        if not self.timings_ms:
            return None
        data = sorted(self.timings_ms)
        return data[min(len(data) - 1, int(round(p / 100 * (len(data) - 1))))]

    @property
    def avg_ms(self) -> float | None:
        return statistics.fmean(self.timings_ms) if self.timings_ms else None

    @property
    def min_ms(self) -> float | None:
        return min(self.timings_ms) if self.timings_ms else None


def _sample_value(schema: dict) -> str:
    if "example" in schema:
        return str(schema["example"])
    if "enum" in schema:
        return str(schema["enum"][0])
    return _SAMPLES.get(schema.get("type", "string"), "ghost")


def build_url(ep: Endpoint) -> str:
    path = ep.path
    for p in ep.params:
        if p.location == "path":
            path = path.replace("{" + p.name + "}", _sample_value(p.schema))
    return path


def build_query(ep: Endpoint) -> dict[str, str]:
    return {
        p.name: _sample_value(p.schema)
        for p in ep.params
        if p.location == "query" and p.required
    }


def probe_endpoint(
    base_url: str,
    ep: Endpoint,
    client: httpx.Client,
    *,
    allow_writes: bool = False,
    samples: int = 5,
) -> ProbeResult:
    result = ProbeResult(endpoint=ep)
    if ep.method != "GET" and not allow_writes:
        result.skipped = "write method (use --allow-writes)"
        return result

    url = base_url.rstrip("/") + build_url(ep)
    query = build_query(ep)
    for _ in range(samples):
        start = time.perf_counter()
        try:
            resp = client.request(ep.method, url, params=query)
        except httpx.HTTPError as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            break
        result.timings_ms.append((time.perf_counter() - start) * 1000)
        result.codes[resp.status_code] = result.codes.get(resp.status_code, 0) + 1
    return result


def probe_all(
    base_url: str,
    endpoints: list[Endpoint],
    client: httpx.Client,
    *,
    allow_writes: bool = False,
    samples: int = 5,
) -> list[ProbeResult]:
    return [
        probe_endpoint(base_url, ep, client, allow_writes=allow_writes, samples=samples)
        for ep in endpoints
    ]
