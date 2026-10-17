"""Execute a project's discover -> probe -> report cycle and store the result."""

from __future__ import annotations

import os
from urllib.parse import urlsplit, urlunsplit

import httpx
from sqlalchemy.orm import sessionmaker

from ..discover import discover
from ..probe import probe_all
from ..report import to_dict
from .db import Project, Run, utcnow
from .security import decrypt_headers

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}


def resolve_url(base_url: str) -> str:
    """Inside Docker, 'localhost' is the container; point it at the host machine."""
    if os.environ.get("GHOST_IN_DOCKER") != "1":
        return base_url
    parts = urlsplit(base_url)
    if parts.hostname in _LOCAL_HOSTS:
        netloc = "host.docker.internal" + (f":{parts.port}" if parts.port else "")
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return base_url


def execute_run(factory: sessionmaker, run_id: int) -> None:
    with factory() as db:
        run = db.get(Run, run_id)
        project = db.get(Project, run.project_id)
        try:
            base_url = resolve_url(project.base_url)
            headers = decrypt_headers(project.headers_enc)
            with httpx.Client(timeout=15.0, headers=headers) as client:
                endpoints = discover(base_url, client)
                results = probe_all(
                    base_url, endpoints, client,
                    allow_writes=project.allow_writes, samples=project.samples,
                )
            run.results = to_dict(results)
            run.status = "done"
        except Exception as exc:  # report the failure on the run instead of losing it
            run.status = "failed"
            run.error = f"{type(exc).__name__}: {exc}"
        run.finished_at = utcnow()
        db.commit()
