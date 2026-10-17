"""Static scan of a FastAPI source tree for `raise HTTPException(...)` sites (S2)."""

from __future__ import annotations

import ast
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from . import workspace
from .discover import Endpoint, load_endpoints, operation_id

_ROUTE_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
_STATUS_CONST = re.compile(r"HTTP_(\d{3})_")


@dataclass
class RaiseSite:
    file: str
    line: int
    status_code: int | None  # None when it cannot be determined statically
    detail: str | None
    function: str
    route: tuple[str, str] | None  # (METHOD, path as written in the decorator)


def _status_of(node: ast.expr | None) -> int | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if isinstance(node, ast.Attribute):  # status.HTTP_404_NOT_FOUND
        m = _STATUS_CONST.search(node.attr)
        return int(m.group(1)) if m else None
    if isinstance(node, ast.Name):
        m = _STATUS_CONST.search(node.id)
        return int(m.group(1)) if m else None
    return None


def _detail_of(node: ast.expr | None) -> str | None:
    if node is None:
        return None
    if isinstance(node, ast.Constant):
        return str(node.value)
    return ast.unparse(node)  # f-strings, variables: keep the expression, flagged by caller


def _route_of(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, str] | None:
    for dec in fn.decorator_list:
        if (
            isinstance(dec, ast.Call)
            and isinstance(dec.func, ast.Attribute)
            and dec.func.attr in _ROUTE_METHODS
            and dec.args
            and isinstance(dec.args[0], ast.Constant)
            and isinstance(dec.args[0].value, str)
        ):
            return dec.func.attr.upper(), dec.args[0].value
    return None


class _Visitor(ast.NodeVisitor):
    def __init__(self, file: str):
        self.file = file
        self.sites: list[RaiseSite] = []
        self._stack: list[ast.FunctionDef | ast.AsyncFunctionDef] = []

    def _visit_fn(self, node):
        self._stack.append(node)
        self.generic_visit(node)
        self._stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = _visit_fn

    def visit_Raise(self, node: ast.Raise):
        call = node.exc
        if isinstance(call, ast.Call) and getattr(call.func, "id", getattr(call.func, "attr", "")) == "HTTPException":
            kw = {k.arg: k.value for k in call.keywords}
            status = kw.get("status_code") or (call.args[0] if call.args else None)
            detail = kw.get("detail") or (call.args[1] if len(call.args) > 1 else None)
            # Route comes from the outermost decorated function so nested helpers still map.
            route = next((r for fn in self._stack if (r := _route_of(fn))), None)
            self.sites.append(
                RaiseSite(
                    file=self.file, line=node.lineno, status_code=_status_of(status),
                    detail=_detail_of(detail),
                    function=self._stack[-1].name if self._stack else "<module>",
                    route=route,
                )
            )
        self.generic_visit(node)


def scan_source(root: Path) -> list[RaiseSite]:
    sites: list[RaiseSite] = []
    for path in sorted(root.rglob("*.py")):
        if any(part in {".venv", "venv", "node_modules", "__pycache__", "site-packages"} for part in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        v = _Visitor(str(path.relative_to(root)))
        v.visit(tree)
        sites.extend(v.sites)
    return sites


def _norm(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path).rstrip("/") or "/"


def map_sites_to_endpoints(sites: list[RaiseSite], endpoints: list[Endpoint]) -> dict[str, list[RaiseSite]]:
    """Match decorator paths to spec paths by suffix, since routers add prefixes."""
    mapped: dict[str, list[RaiseSite]] = {}
    for site in sites:
        if not site.route:
            continue
        method, rpath = site.route
        for ep in endpoints:
            if ep.method == method and _norm(ep.path).endswith(_norm(rpath)):
                mapped.setdefault(operation_id(ep), []).append(site)
    return mapped


def run_scan(api: str) -> list[RaiseSite]:
    cfg = workspace.load_config(api)
    if not cfg.source_root:
        workspace.write_json(api, "source_errors.json", {"sites": [], "skipped": "no source_root in api.toml"})
        return []
    sites = scan_source(cfg.source_root)
    workspace.write_json(api, "source_errors.json", {"sites": [asdict(s) for s in sites]})
    return sites
