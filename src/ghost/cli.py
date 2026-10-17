from __future__ import annotations

from pathlib import Path

import httpx
import typer

from .discover import discover
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
            endpoints = discover(base_url, client)
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
