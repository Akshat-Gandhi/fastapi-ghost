"""Evidence bundles for failing results (S7 input). The failure-explainer agent reads these."""

from __future__ import annotations

from . import workspace
from .contract import key_of
from .discover import load_endpoints, operation_id

FAILING = {"mismatch", "5xx", "flaky", "error"}


def make_bundles(api: str) -> list[str]:
    results = workspace.read_json(api, "results/results.json", {"results": []})["results"]
    contract = workspace.read_json(api, "contract.json", {})
    by_op = {operation_id(e): e for e in load_endpoints(api)}
    written = []
    for r in results:
        if r["outcome"] not in FAILING:
            continue
        ep = by_op.get(r["operation_id"])
        entry = contract.get(key_of(ep), {}) if ep else {}
        bundle = {
            "case_id": r["case_id"], "category": r["category"], "title": r["title"],
            "request": {"method": r["method"], "path": r["path"], "auth_mode": r["auth"], **(r.get("request_sent") or {})},
            "base_url": workspace.load_config(api).base_url, "at": r.get("at"),
            "expected_status": r["expected"], "observed_statuses": r["statuses"],
            "response_body": r["body"], "response_body_truncated": r["body_truncated"],
            "timings_ms": r["timings_ms"], "error": r["error"], "outcome": r["outcome"],
            "contract_expected": entry.get("expected", []),
            "candidate_raise_sites": [e["raise_site"] for e in entry.get("expected", []) if e.get("raise_site")],
        }
        name = f"results/bundles/{r['case_id']}.json"
        workspace.write_json(api, name, bundle)
        written.append(name)
    return written
