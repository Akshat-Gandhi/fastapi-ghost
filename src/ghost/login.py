"""Sign in once per run with the `[login]` section of api.toml and keep the token in memory only."""

from __future__ import annotations

import os

import httpx

from . import workspace


def _dig(data: object, dotted: str):
    for part in dotted.split("."):
        if isinstance(data, dict) and part in data:
            data = data[part]
        elif isinstance(data, list) and part.isdigit() and int(part) < len(data):
            data = data[int(part)]
        else:
            return None
    return data


def ensure_login(api: str, client: httpx.Client) -> bool:
    """Returns True when a token is available. No-op without [login] or when already signed in this process."""
    if api in workspace._SESSION_AUTH:
        return True
    cfg = workspace.load_config(api)
    spec = cfg.login
    if not spec:
        return False
    payload = {}
    for field, env in spec["fields"].items():
        value = os.environ.get(env)
        if value is None:
            raise ValueError(f"[login] needs the environment variable {env} (for field '{field}'), but it is not set.")
        node = payload  # "user.email" builds {"user": {"email": ...}} for APIs that wrap their bodies
        *parents, leaf = field.split(".")
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = value
    kwargs = {"json": payload} if spec["as"] == "json" else {"data": payload}
    resp = client.request(spec["method"], f"{cfg.base_url}{spec['path']}", **kwargs)
    if resp.status_code >= 400:
        raise ValueError(
            f"[login] {spec['method']} {spec['path']} returned {resp.status_code}. "
            "Check the credentials in the environment variables named in api.toml."
        )
    try:
        token = _dig(resp.json(), spec["token_path"])
    except ValueError:
        token = None
    if not isinstance(token, str) or not token:
        raise ValueError(
            f"[login] the reply from {spec['path']} has no text value at '{spec['token_path']}'. "
            "Set token_path to where the token is in the JSON reply."
        )
    workspace._SESSION_AUTH[api] = {spec["header"]: f"{spec['prefix']}{token}"}
    return True
