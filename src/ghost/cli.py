from __future__ import annotations

from pathlib import Path

import httpx
import typer

from . import workspace
from .discover import discover as discover_endpoints, snapshot_spec
from .probe import probe_all
from .report import print_report, write_json

app = typer.Typer(help="Report on how a running FastAPI server behaves.", no_args_is_help=True)


@app.callback()
def main() -> None:
    """fastapi-ghost"""


@app.command()
def run(
    port: int = typer.Option(8000, help="Port of the running FastAPI server."),
    host: str = typer.Option("127.0.0.1", help="Host of the running server."),
    allow_writes: bool = typer.Option(False, help="Also probe POST/PUT/PATCH/DELETE."),
    samples: int = typer.Option(5, min=1, help="Requests per endpoint, for latency stats."),
    json_out: Path = typer.Option(None, "--json", help="Write report.json to this path."),
) -> None:
    """Discover endpoints from /openapi.json, probe them, and print a report."""
    base_url = f"http://{host}:{port}"
    with httpx.Client(timeout=10.0) as client:
        try:
            endpoints = discover_endpoints(base_url, client)
        except httpx.HTTPError as exc:
            typer.secho(f"Could not read {base_url}/openapi.json: {exc}", fg="red", err=True)
            raise typer.Exit(2)
        results = probe_all(base_url, endpoints, client, allow_writes=allow_writes, samples=samples)
    print_report(results)
    if json_out:
        write_json(results, json_out)
        typer.echo(f"Wrote {json_out}")
    # Ghost reports; it never fails the caller's build on findings.


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Interface to bind."),
    port: int = typer.Option(8080, help="Port for the Ghost web app."),
) -> None:
    """Run the web app locally (no Docker). Set GHOST_DATABASE_URL for Postgres; defaults to SQLite."""
    try:
        import uvicorn
        from .web.app import app_factory
    except ImportError:
        typer.secho("Web app needs extras: pip install 'fastapi-ghost[web]'", fg="red", err=True)
        raise typer.Exit(2)
    uvicorn.run(app_factory, factory=True, host=host, port=port)


def _guard(fn):
    """Per-API commands report problems and exit 2 only for config/tool errors, never for findings."""
    try:
        return fn()
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(2)
    except httpx.HTTPError as exc:
        typer.secho(f"Cannot reach the API: {exc}", fg="red", err=True)
        raise typer.Exit(2)


@app.command()
def init(
    api: str = typer.Argument(..., help="Short name for this API, e.g. todo."),
    url: str = typer.Option(..., help="Base URL, e.g. http://localhost:8000"),
    source: str = typer.Option(None, help="Path to the API's source code (enables the error-site scan)."),
    writes: bool = typer.Option(False, "--writes", help="Allow POST/PUT/PATCH/DELETE."),
    force: bool = typer.Option(False, help="Overwrite an existing api.toml."),
) -> None:
    """S0: create .ghost/<api>/api.toml."""
    path = _guard(lambda: workspace.write_default_config(api, url, source, writes, force))
    typer.echo(f"Wrote {path}")


@app.command()
def discover(api: str = typer.Option(..., help="API name from `ghost init`.")) -> None:
    """S1: snapshot /openapi.json and parse endpoints."""
    def go():
        cfg = workspace.load_config(api)
        with httpx.Client(timeout=cfg.timeout_s) as client:
            return snapshot_spec(api, client)
    eps = _guard(go)
    typer.echo(f"Discovered {len(eps)} endpoints -> {workspace.api_dir(api)}/endpoints.json")


@app.command()
def scan(api: str = typer.Option(..., help="API name from `ghost init`.")) -> None:
    """S2: scan source for `raise HTTPException` sites."""
    from .scan import run_scan
    sites = _guard(lambda: run_scan(api))
    typer.echo(f"Found {len(sites)} raise sites -> {workspace.api_dir(api)}/source_errors.json")


@app.command()
def contract(api: str = typer.Option(..., help="API name from `ghost init`.")) -> None:
    """S3: build contract.json and drift.json from the spec and source scan."""
    from .contract import run_contract
    contract_, drift = _guard(lambda: run_contract(api))
    breaking = sum(1 for d in drift if d["severity"] == "breaking")
    typer.echo(f"Contract for {len(contract_)} endpoints; drift: {len(drift)} changes ({breaking} breaking)")


@app.command()
def gen(api: str = typer.Option(..., help="API name from `ghost init`.")) -> None:
    """S4: generate baseline test cases into cases/."""
    from .gen import run_gen
    total = _guard(lambda: run_gen(api))
    typer.echo(f"Generated {total} baseline cases -> {workspace.api_dir(api)}/cases/")


@app.command()
def validate(api: str = typer.Option(..., help="API name from `ghost init`.")) -> None:
    """S5: statically validate cases; only verdict `ok` cases will run."""
    from .validate import run_validate
    result = _guard(lambda: run_validate(api))
    typer.echo(f"Validation: {result['summary']}")
