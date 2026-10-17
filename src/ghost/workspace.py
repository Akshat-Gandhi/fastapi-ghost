"""Per-API artifact directory (.ghost/<api>/) and its api.toml config."""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

SCHEMA_VERSION = 1
INVALID_TOKEN = "ghost-invalid-token"


def root() -> Path:
    return Path(os.environ.get("GHOST_HOME", ".ghost"))


def api_dir(api: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", api):
        raise ValueError(f"Invalid api slug: {api!r}")
    return root() / api


def read_json(api: str, name: str, default: Any = None) -> Any:
    path = api_dir(api) / name
    return json.loads(path.read_text()) if path.exists() else default


def write_json(api: str, name: str, data: Any) -> Path:
    path = api_dir(api) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, dict):
        data = {"schema_version": SCHEMA_VERSION, **data}
    path.write_text(json.dumps(data, indent=2, sort_keys=False))
    return path


@dataclass
class ApiConfig:
    base_url: str
    source_root: Path | None = None
    allow_writes: bool = False
    samples: int = 5
    timeout_s: float = 10.0
    slow_ms: float = 500.0
    max_rps: float = 20.0
    max_requests: int = 2000
    auth: dict[str, str] = field(default_factory=dict)  # header name -> ENV VAR NAME
    allowed_hosts: list[str] = field(default_factory=list)
    blocked_paths: list[str] = field(default_factory=list)

    def host_allowed(self) -> bool:
        """Writes only run against local hosts or hosts explicitly allowed."""
        from urllib.parse import urlsplit

        host = urlsplit(self.base_url).hostname or ""
        return host in {"localhost", "127.0.0.1", "::1", "host.docker.internal"} or host in self.allowed_hosts


def load_config(api: str) -> ApiConfig:
    path = api_dir(api) / "api.toml"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found. Run: ghost init {api} --url http://localhost:8000")
    data = tomllib.loads(path.read_text())
    src = data.get("source_root")
    return ApiConfig(
        base_url=data["base_url"].rstrip("/"),
        source_root=Path(src) if src else None,
        allow_writes=bool(data.get("allow_writes", False)),
        samples=int(data.get("samples", 5)),
        timeout_s=float(data.get("timeout_s", 10.0)),
        slow_ms=float(data.get("slow_ms", 500.0)),
        max_rps=float(data.get("max_rps", 20.0)),
        max_requests=int(data.get("max_requests", 2000)),
        auth=dict(data.get("auth", {})),
        allowed_hosts=list(data.get("allowed_hosts", [])),
        blocked_paths=list(data.get("blocked_paths", [])),
    )


def write_default_config(api: str, url: str, source: str | None, writes: bool, force: bool = False) -> Path:
    path = api_dir(api) / "api.toml"
    if path.exists() and not force:
        raise FileExistsError(f"{path} already exists (use --force to overwrite)")
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f'base_url = "{url.rstrip("/")}"',
        f'source_root = "{source}"' if source else '# source_root = "./path/to/your/app"',
        f"allow_writes = {str(writes).lower()}",
        "samples = 5",
        "slow_ms = 500",
        "max_rps = 20",
        "# Hosts (besides localhost) where writes are allowed:",
        "allowed_hosts = []",
        "# Paths never called, e.g. destructive admin routes:",
        "blocked_paths = []",
        "",
        "# Auth: header name = NAME OF AN ENVIRONMENT VARIABLE holding the value. Never put tokens here.",
        "[auth]",
        '# Authorization = "MY_API_TOKEN_HEADER_VALUE"',
        "",
    ]
    path.write_text("\n".join(lines))
    return path


def resolve_auth(cfg: ApiConfig, mode: str) -> dict[str, str]:
    """Headers for an auth mode: none | valid | bad."""
    if mode == "none":
        return {}
    if mode == "bad":
        return {name: INVALID_TOKEN for name in cfg.auth}
    if mode == "valid":
        headers = {}
        for name, env in cfg.auth.items():
            value = os.environ.get(env)
            if value:
                headers[name] = value
        return headers
    raise ValueError(f"Unknown auth mode {mode!r}")


def secret_values(cfg: ApiConfig) -> list[str]:
    return [v for env in cfg.auth.values() if (v := os.environ.get(env))]


def scrub_secrets(text: str, cfg: ApiConfig) -> str:
    for secret in secret_values(cfg):
        text = text.replace(secret, "[REDACTED]")
    return text
