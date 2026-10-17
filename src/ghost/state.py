"""Making repeat runs against a stateful server quiet.

* Placeholders: `{{nonce}}` / `{{nonce:N}}` (a short per-run random string) and `{{name}}` (a value captured
  from an earlier response) can appear in any string of a case request (path params, query, headers, body).
* Unique values: string fields that are likely unique (title, name, email, ...) get a nonce in happy bodies.
* Chains: a POST that creates a resource plus the item path `/things/{id}` gives setup/captures/teardown.
"""

from __future__ import annotations

import copy
import re
import secrets
from dataclasses import dataclass
from typing import Any

from .discover import Endpoint
from .schema import _merge_all_of, deref

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)(?::(\d+))?\s*\}\}")
WHOLE = re.compile(r"^\{\{\s*([A-Za-z_][A-Za-z0-9_]*)(?::(\d+))?\s*\}\}$")
CAPTURE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
NONCE_LEN = 6
BUILTIN = {"nonce"}
UNIQUE_NAMES = ("title", "name", "email", "username", "slug", "code")


class MissingValue(KeyError):
    """A placeholder names a captured value that is not available."""


def new_nonce() -> str:
    return secrets.token_hex(NONCE_LEN // 2)


def names_in(value: Any) -> set[str]:
    """All placeholder names used in the strings of `value` (recursively)."""
    if isinstance(value, str):
        return {m.group(1) for m in PLACEHOLDER.finditer(value)}
    if isinstance(value, dict):
        return set().union(*(names_in(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(names_in(v) for v in value)) if value else set()
    return set()


def resolve(value: Any, values: dict[str, Any], nonce: str) -> Any:
    """Substitute placeholders. A string that is exactly one placeholder keeps the captured value's type."""
    if isinstance(value, dict):
        return {k: resolve(v, values, nonce) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, values, nonce) for v in value]
    if not isinstance(value, str) or "{{" not in value:
        return value

    def lookup(name: str, width: str | None) -> Any:
        if name == "nonce":
            return nonce[: int(width)] if width else nonce
        if name not in values:
            raise MissingValue(name)
        return values[name]

    whole = WHOLE.match(value)
    if whole:
        return lookup(whole.group(1), whole.group(2))
    return PLACEHOLDER.sub(lambda m: str(lookup(m.group(1), m.group(2))), value)


def sample_nonce_only(value: Any) -> tuple[Any, bool]:
    """Resolve `{{nonce}}` with a stand-in so the result can be schema-checked.
    Returns (value, has_other_placeholders)."""
    others = names_in(value) - BUILTIN
    if others:
        return value, True
    return resolve(value, {}, "0" * NONCE_LEN), False


def extract(body: Any, path: str) -> Any:
    """Read a dotted path (`id`, `data.id`, `items.0.id`) from a JSON body. Only scalars are captured."""
    node = body
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return None
    return node if isinstance(node, (str, int)) and not isinstance(node, bool) else None


# --- unique values -----------------------------------------------------------

def _is_unique_name(name: str) -> bool:
    n = name.lower()
    return n in UNIQUE_NAMES or any(n.endswith("_" + u) for u in UNIQUE_NAMES)


def _string_option(sub: dict, spec: dict) -> dict | None:
    sub = deref(sub, spec)
    if "allOf" in sub:
        sub = _merge_all_of(sub, spec)
    for key in ("anyOf", "oneOf"):
        if key in sub:
            opts = [deref(o, spec) for o in sub[key]]
            strs = [o for o in opts if o.get("type") == "string"]
            return strs[0] if strs else None
    t = sub.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), None)
    return sub if t == "string" else None


def unique_string(sub: dict, spec: dict) -> str | None:
    """A template like `ghost-{{nonce}}` that still satisfies the string schema, or None if unsafe to change."""
    s = _string_option(sub, spec)
    if s is None or any(k in s for k in ("enum", "const", "pattern")):
        return None
    fmt = s.get("format")
    if fmt not in (None, "", "email"):
        return None
    suffix = "@example.com" if fmt == "email" else ""
    lo, hi = s.get("minLength"), s.get("maxLength")
    prefix, width = "ghost-", NONCE_LEN
    if hi is not None:
        room = hi - len(suffix)
        if room < 1:
            return None
        if room < len(prefix) + width:
            width = min(NONCE_LEN, room)
            prefix = prefix[: room - width]
    length = len(prefix) + width + len(suffix)
    if lo and length < lo:
        prefix += "x" * (lo - length)
        length = lo
    if hi is not None and length > hi:
        return None
    tag = "{{nonce}}" if width == NONCE_LEN else "{{nonce:%d}}" % width
    return prefix + tag + suffix


def uniquify(example: Any, schema: dict | None, spec: dict, depth: int = 0) -> Any:
    """Copy of `example` where likely-unique string fields carry a nonce template."""
    if schema is None or depth > 4:
        return copy.deepcopy(example)
    schema = deref(schema, spec)
    if "allOf" in schema:
        schema = _merge_all_of(schema, spec)
    if isinstance(example, dict):
        out = {}
        for name, val in example.items():
            sub = schema.get("properties", {}).get(name)
            if sub is None:
                out[name] = copy.deepcopy(val)
            elif isinstance(val, str) and _is_unique_name(name):
                out[name] = unique_string(sub, spec) or val
            else:
                out[name] = uniquify(val, sub, spec, depth + 1)
        return out
    if isinstance(example, list):
        item = deref(schema.get("items", {}), spec)
        return [uniquify(v, item, spec, depth + 1) for v in example]
    return copy.deepcopy(example)


# --- chains ------------------------------------------------------------------

@dataclass
class Chain:
    create_ep: Endpoint
    create_case_id: str
    capture: str
    capture_path: str
    item_path: str
    param: str
    delete_ep: Endpoint | None


def _success_props(ep: Endpoint, spec: dict) -> dict:
    responses = spec.get("paths", {}).get(ep.path, {}).get(ep.method.lower(), {}).get("responses", {})
    for code in sorted(responses):
        if code.isdigit() and 200 <= int(code) < 300:
            resp = deref(responses[code], spec)
            schema = resp.get("content", {}).get("application/json", {}).get("schema")
            if not schema:
                return {}
            schema = deref(schema, spec)
            if "allOf" in schema:
                schema = _merge_all_of(schema, spec)
            return schema.get("properties", {}) if schema.get("type", "object") == "object" else {}
    return {}


def _id_field(props: dict, param: str, spec: dict) -> str | None:
    """Dotted path of the new record's id in the create response: `id`, or `article.slug` for APIs that wrap
    the record in a single envelope key."""
    wanted = (param, "id", "uuid", "pk", "slug")
    direct = next((c for c in wanted if c in props), None)
    if direct:
        return direct
    wrappers = [k for k, v in props.items() if deref(v, spec).get("type") == "object" or "properties" in deref(v, spec)]
    if len(wrappers) == 1:
        inner = deref(props[wrappers[0]], spec)
        if "allOf" in inner:
            inner = _merge_all_of(inner, spec)
        found = next((c for c in wanted if c in inner.get("properties", {})), None)
        return f"{wrappers[0]}.{found}" if found else None
    return None


def find_chains(endpoints: list[Endpoint], spec: dict, op_id) -> dict[str, Chain]:
    """item path -> Chain, for every `POST /things` (no params) that creates something addressable at
    `/things/{param}`. The id must be visible in the documented success response, otherwise nothing is chained."""
    chains: dict[str, Chain] = {}
    for post in endpoints:
        if post.method != "POST" or "{" in post.path:
            continue
        item_re = re.compile("^" + re.escape(post.path.rstrip("/")) + r"/\{([^/{}]+)\}$")
        for other in endpoints:
            m = item_re.match(other.path)
            if not m or other.path in chains:
                continue
            param = m.group(1)
            props = _success_props(post, spec)
            field = _id_field(props, param, spec)
            if field is None:
                continue
            delete = next((e for e in endpoints if e.method == "DELETE" and e.path == other.path), None)
            chains[other.path] = Chain(post, f"{op_id(post)}.happy.1", param, field, other.path, param, delete)
    return chains
