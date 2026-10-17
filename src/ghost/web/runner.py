"""Execute a project's discover -> probe -> report cycle and store the result."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
from sqlalchemy.orm import sessionmaker

from .. import workspace
from ..pipeline import run_pipeline
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


def resolve_source(path: str) -> Path | None:
    """Inside Docker, source folders live under the read-only /sources mount; elsewhere any local path works."""
    if not path:
        return None
    root = Path(os.environ.get("GHOST_SOURCES_DIR", "/sources")) if os.environ.get("GHOST_IN_DOCKER") == "1" else None
    candidate = (root / path.lstrip("/")).resolve() if root else Path(path).expanduser().resolve()
    if root and root.resolve() not in candidate.parents and candidate != root.resolve():
        return None  # no escaping the mount with ../
    return candidate if candidate.is_dir() else None


def execute_run(factory: sessionmaker, run_id: int) -> None:
    with factory() as db:
        run = db.get(Run, run_id)
        project = db.get(Project, run.project_id)
        slug = f"project-{project.id}"  # one persistent workspace per project: contract + drift survive runs
        cfg = None
        try:
            base_url = resolve_url(project.base_url)
            host = urlsplit(base_url).hostname or ""
            cfg = workspace.ApiConfig(
                base_url=base_url.rstrip("/"),
                allow_writes=project.allow_writes,
                samples=project.samples,
                source_root=resolve_source(project.source_path),
                auth_values=decrypt_headers(project.headers_enc),
                # Ticking "allow writes" in the UI is the explicit consent for this host.
                allowed_hosts=[host] if project.allow_writes else [],
            )
            workspace.set_override(slug, cfg)
            with httpx.Client(timeout=cfg.timeout_s) as client:
                run.results = {**run_pipeline(slug, client), "api": project.name}
            run.status = "done"
        except Exception as exc:  # report the failure on the run instead of losing it
            run.status = "failed"
            message = f"{type(exc).__name__}: {exc}"
            run.error = workspace.scrub_secrets(message, cfg) if cfg else message
        finally:
            workspace.clear_override(slug)
        run.finished_at = utcnow()
        db.commit()
