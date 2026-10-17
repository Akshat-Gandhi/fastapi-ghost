"""The deterministic S1..S8 pipeline as a function (used by `ghost pipeline` and the web app)."""

from __future__ import annotations

import httpx

from . import workspace
from .contract import run_contract
from .discover import snapshot_spec
from .explain import make_bundles
from .gen import run_gen
from .report import write_report
from .runner import run_all
from .scan import run_scan
from .validate import run_validate


def run_pipeline(api: str, client: httpx.Client | None = None) -> dict:
    cfg = workspace.load_config(api)
    own = client is None
    client = client or httpx.Client(timeout=cfg.timeout_s, limits=workspace.CLIENT_LIMITS)
    try:
        snapshot_spec(api, client)
        run_scan(api)
        run_contract(api)
        run_gen(api)
        run_validate(api)
        run_all(api, client)
        make_bundles(api)
        return write_report(api)
    finally:
        if own:
            client.close()
