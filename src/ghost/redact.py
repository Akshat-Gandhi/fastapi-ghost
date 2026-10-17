"""Keep secrets out of stored artifacts."""

from __future__ import annotations

from typing import Any

SENSITIVE_KEYS = {"password", "passwd", "secret", "token", "access_token", "refresh_token", "api_key", "apikey",
                  "authorization", "cookie", "set-cookie", "client_secret", "private_key"}
SENSITIVE_HEADERS = {"authorization", "cookie", "set-cookie", "x-api-key", "proxy-authorization"}


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: ("[REDACTED]" if k.lower() in SENSITIVE_HEADERS else v) for k, v in headers.items()}


def redact_body(body: Any, extra_keys: set[str] | None = None) -> Any:
    keys = SENSITIVE_KEYS | {k.lower() for k in (extra_keys or set())}
    if isinstance(body, dict):
        return {k: ("[REDACTED]" if k.lower() in keys else redact_body(v, extra_keys)) for k, v in body.items()}
    if isinstance(body, list):
        return [redact_body(v, extra_keys) for v in body]
    return body
