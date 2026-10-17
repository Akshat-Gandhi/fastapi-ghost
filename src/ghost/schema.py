"""Helpers for working with OpenAPI/JSON schemas: $ref resolution, examples, invalid mutations."""

from __future__ import annotations

import copy
from typing import Any

_FORMATS = {
    "email": "ghost@example.com", "uuid": "00000000-0000-4000-8000-000000000000",
    "date-time": "2024-01-01T00:00:00Z", "date": "2024-01-01", "uri": "https://example.com",
    "password": "Gh0st-passw0rd!",
}
MAX_DEPTH = 6


def deref(schema: dict, spec: dict, _seen: tuple[str, ...] = ()) -> dict:
    """Resolve a local $ref chain (cycles stop at the ref itself)."""
    while isinstance(schema, dict) and "$ref" in schema:
        ref = schema["$ref"]
        if ref in _seen or not ref.startswith("#/"):
            return {}
        _seen += (ref,)
        node: Any = spec
        for part in ref[2:].split("/"):
            node = node.get(part, {})
        schema = node
    return schema


def with_components(schema: dict, spec: dict) -> dict:
    """A schema jsonschema can validate against: carries #/components so inner $refs resolve."""
    return {**schema, "components": spec.get("components", {})}


def _merge_all_of(schema: dict, spec: dict) -> dict:
    merged: dict = {k: v for k, v in schema.items() if k != "allOf"}
    for part in schema["allOf"]:
        part = deref(part, spec)
        for k, v in part.items():
            if k in ("properties",):
                merged.setdefault(k, {}).update(v)
            elif k == "required":
                merged["required"] = list(dict.fromkeys(merged.get("required", []) + v))
            else:
                merged.setdefault(k, v)
    return merged


def example_from_schema(schema: dict, spec: dict, depth: int = 0) -> Any:
    schema = deref(schema, spec)
    if depth > MAX_DEPTH or not isinstance(schema, dict):
        return None
    if "allOf" in schema:
        schema = _merge_all_of(schema, spec)
    for key in ("example", "default"):
        if key in schema:
            return copy.deepcopy(schema[key])
    if "examples" in schema and isinstance(schema["examples"], list) and schema["examples"]:
        return copy.deepcopy(schema["examples"][0])
    if "enum" in schema:
        return schema["enum"][0]
    for key in ("anyOf", "oneOf"):
        if key in schema:
            options = [o for o in schema[key] if deref(o, spec).get("type") != "null"]
            return example_from_schema(options[0] if options else schema[key][0], spec, depth + 1)
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "null")
    if t == "object" or "properties" in schema:
        return {
            name: example_from_schema(sub, spec, depth + 1)
            for name, sub in schema.get("properties", {}).items()
        }
    if t == "array":
        item = example_from_schema(schema.get("items", {}), spec, depth + 1)
        return [item] if item is not None else []
    if t == "integer":
        return _clamp(schema, 1, int)
    if t == "number":
        return _clamp(schema, 1.0, float)
    if t == "boolean":
        return True
    if t == "string":
        if schema.get("format") in _FORMATS:
            return _FORMATS[schema["format"]]
        text = "ghost"
        if schema.get("pattern"):
            text = _for_pattern(schema["pattern"], text)
        lo, hi = schema.get("minLength"), schema.get("maxLength")
        if lo and len(text) < lo:
            text = (text * (lo // len(text) + 1))[:lo]
        if hi is not None and len(text) > hi:
            text = text[:hi]
        return text
    return None


def _for_pattern(pattern: str, default: str) -> str:
    """A string that matches a `pattern`: the literal inside ^...$ (or its first alternative), else a few guesses."""
    import re

    core = pattern[1:] if pattern.startswith("^") else pattern
    core = core[:-1] if core.endswith("$") else core
    core = core[1:-1] if core.startswith("(") and core.endswith(")") and core.count("(") == 1 else core
    literal = core.split("|")[0]
    guesses = [literal, default, "ghost1", "Ghost123!", "123456", "abc-123", "2020-01-01", "ghost@example.com"]
    for guess in guesses:
        try:
            if re.search(pattern, guess):
                return guess
        except re.error:
            return default
    return default


def _clamp(schema: dict, default, cast):
    value = default
    if "minimum" in schema and value < schema["minimum"]:
        value = schema["minimum"]
    if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
        value = schema["exclusiveMinimum"] + 1
    if "maximum" in schema and value > schema["maximum"]:
        value = schema["maximum"]
    return cast(value)


_WRONG = {"string": 12345, "integer": "not-a-number", "number": "not-a-number", "boolean": "maybe",
          "array": "not-a-list", "object": "not-an-object"}


def mutate_invalid(schema: dict, example: Any, spec: dict) -> list[tuple[str, Any]]:
    """Payloads that break the schema in exactly one way. Callers still verify with jsonschema."""
    schema = deref(schema, spec)
    if "allOf" in schema:
        schema = _merge_all_of(schema, spec)
    out: list[tuple[str, Any]] = []
    if not isinstance(example, dict):
        t = schema.get("type")
        if t in _WRONG:
            out.append((f"wrong type for body (expected {t})", _WRONG[t]))
        return out
    props = schema.get("properties", {})
    for name in schema.get("required", []):
        out.append((f"missing required field '{name}'", {k: v for k, v in example.items() if k != name}))
    for name, sub in props.items():
        sub = deref(sub, spec)
        t = sub.get("type")
        if isinstance(t, list):
            t = next((x for x in t if x != "null"), None)
        if t in _WRONG and "anyOf" not in sub and "oneOf" not in sub:
            out.append((f"wrong type for '{name}' (expected {t})", {**example, name: _WRONG[t]}))
        if t in ("integer", "number") and "minimum" in sub:
            out.append((f"'{name}' below minimum", {**example, name: sub["minimum"] - 1}))
        if t in ("integer", "number") and "maximum" in sub:
            out.append((f"'{name}' above maximum", {**example, name: sub["maximum"] + 1}))
        if t == "string" and sub.get("maxLength") is not None:
            out.append((f"'{name}' exceeds maxLength", {**example, name: "x" * (sub["maxLength"] + 1)}))
        if t == "string" and sub.get("minLength"):
            out.append((f"'{name}' shorter than minLength", {**example, name: ""}))
        if "enum" in sub:
            out.append((f"'{name}' not in enum", {**example, name: "ghost-not-an-option"}))
    return out
