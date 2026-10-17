"""Static scan of a FastAPI source tree for error raise sites, linked to endpoints (S2).

Besides `raise HTTPException(...)` it follows helper/service/dependency code through a project-wide
call graph (see callgraph.py) and understands custom exceptions mapped by exception handlers.
"""

from __future__ import annotations

import ast
import re
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path

from . import workspace
from .callgraph import ProjectGraph, _last_name, _kw, own_nodes
from .discover import Endpoint, load_endpoints, operation_id

_STATUS_CONST = re.compile(r"HTTP_(\d{3})_")
_HTTP_EXC = {"HTTPException", "StarletteHTTPException"}
_RANK = {"direct": 0, "via_call": 1, "via_dependency": 2, "ambiguous": 3}


@dataclass
class RaiseSite:
    file: str
    line: int
    status_code: int | None  # None when it cannot be determined statically
    detail: str | None
    function: str
    route: tuple[str, str] | None  # (METHOD, path as written in the decorator) when raised directly in a route
    via: list[str] = field(default_factory=list)  # call chain from the route, e.g. ["get_item", "crud.get_by_id"]
    confidence: str | None = None  # direct | via_call | via_dependency | ambiguous; None when not linked
    routes: list[dict] = field(default_factory=list)  # every route reaching this site (see _entry)
    exception: str | None = None  # custom exception class name, when not a plain HTTPException
    handler: str | None = None  # file:line of the exception handler that decides the status

    @classmethod
    def from_dict(cls, d: dict) -> "RaiseSite":
        """Tolerant reader: old source_errors.json files lack the newer fields."""
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in d.items() if k in known}
        data["route"] = tuple(data["route"]) if data.get("route") else None
        data.setdefault("detail", None)
        data.setdefault("function", "")
        data["via"] = list(data.get("via") or [])
        data["routes"] = list(data.get("routes") or [])
        return cls(**data)


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


def _status_detail(call: ast.Call, skip: int = 0) -> tuple[ast.expr | None, ast.expr | None]:
    kw = {k.arg: k.value for k in call.keywords}
    args = call.args[skip:]
    return kw.get("status_code") or (args[0] if args else None), kw.get("detail") or (args[1] if len(args) > 1 else None)


# --- exception handlers ------------------------------------------------------

def _keys(g: ProjectGraph, ctx, expr) -> list[tuple]:
    ks = [("cls", t[1]) for t in g.resolve(ctx, expr) if t[0] == "cls"]
    if not ks and _last_name(expr):
        ks = [("ext", _last_name(expr))]
    return ks


def _chain(g: ProjectGraph, key: tuple, seen=frozenset()) -> list[tuple]:
    """The class, then its bases (project classes expanded, others kept by name)."""
    out = [key]
    if key[0] != "cls" or key[1] in seen:
        return out
    c = g.classes[key[1]]
    for b in c.node.bases:
        for k in _keys(g, (c.module, None, None), b):
            out += _chain(g, k, seen | {key[1]})
    return out


def _handler_status(node: ast.AST) -> int | None:
    """The one literal status a handler responds with, else None."""
    if isinstance(node, ast.Lambda):
        nodes: list = [node.body]
    else:
        nodes = [n.value for n in own_nodes(node.body) if isinstance(n, ast.Return) and n.value is not None]
        nodes += [n.exc for n in own_nodes(node.body) if isinstance(n, ast.Raise) and n.exc is not None]
    statuses: set[int | None] = set()
    for call in nodes:
        if not isinstance(call, ast.Call):
            continue
        name = _last_name(call.func)
        if name in _HTTP_EXC:
            statuses.add(_status_of(_status_detail(call)[0]))
        elif name.endswith("Response"):
            kw = _kw(call, "status_code")
            sc = kw if kw is not None else (call.args[1] if len(call.args) > 1 else None)
            statuses.add(200 if sc is None else _status_of(sc))
    return next(iter(statuses)) if len(statuses) == 1 else None


def _collect_handlers(g: ProjectGraph) -> dict[tuple, tuple[int | None, str]]:
    out: dict[tuple, tuple[int | None, str]] = {}
    for ctx, exc_expr, h, line in g.handler_regs:
        node, mod = None, ctx[0]
        if h[0] == "fid":
            node, mod = g.fns[h[1]].node, g.fns[h[1]].module
        elif isinstance(h[1], ast.Lambda):
            node = h[1]
        else:
            fns = [t[1] for t in g.resolve(ctx, h[1]) if t[0] == "fn"]
            if len(fns) == 1:
                node, mod = g.fns[fns[0]].node, g.fns[fns[0]].module
        if node is None:
            continue
        loc = f"{g.mods[mod].file}:{getattr(node, 'lineno', line)}"
        for key in _keys(g, ctx, exc_expr):
            out[key] = (_handler_status(node), loc)
    return out


def _fixed_status(g: ProjectGraph, chain: list[tuple]) -> tuple[bool, int | None, ast.expr | None]:
    """(known, status, detail expr) from the first __init__ / `status_code = N` found along the chain."""
    for k in chain:
        if k[0] != "cls":
            continue
        c = g.classes[k[1]]
        for stmt in c.node.body:
            if isinstance(stmt, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "status_code" for t in stmt.targets):
                return True, _status_of(stmt.value), None
        if "__init__" in c.methods:
            for n in own_nodes(g.fns[c.methods["__init__"][0]].node.body):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "__init__":
                    explicit = isinstance(n.func.value, ast.Name)  # HTTPException.__init__(self, ...)
                    sc, det = _status_detail(n, 1 if explicit else 0)
                    return True, _status_of(sc), det
            return True, None, None
    return False, None, None


def _exception_site(g, handlers, ctx, fexpr, call):
    """(status, detail, handler loc, class name) for a custom exception, or None when it is not an HTTP error."""
    if not isinstance(fexpr, (ast.Name, ast.Attribute)):
        return None
    keys = _keys(g, ctx, fexpr)
    results = []
    for key in keys:
        chain = _chain(g, key)
        hit = next((k for k in chain if k in handlers), None)
        if hit is not None:
            results.append((handlers[hit][0], None, handlers[hit][1]))
            continue
        if any(k[0] == "ext" and k[1] in _HTTP_EXC for k in chain) and key[0] == "cls":
            known, status, det = _fixed_status(g, chain)
            if not known and call is not None:
                sc, det = _status_detail(call)
                status = _status_of(sc)
            elif call is not None and det is None:
                det = _status_detail(call, 0)[1] or (call.args[0] if call.args else None)
            if det is not None and not isinstance(det, ast.Constant) and known:
                det = None  # a parameter of __init__, not a literal
            results.append((status, _detail_of(det), None))
    if not results:
        return None
    statuses = {r[0] for r in results}
    first = results[0]
    return (first[0] if len(statuses) == 1 else None), first[1], first[2], _last_name(fexpr)


# --- scan ---------------------------------------------------------------------

@dataclass
class _Raw:
    site: RaiseSite
    stack: list[str]  # enclosing function ids, innermost last


def _extract(g: ProjectGraph) -> list[_Raw]:
    handlers = _collect_handlers(g)
    raws: list[_Raw] = []

    def visit(body, file: str, ctx, fname: str, stack: list[str]) -> None:
        for n in own_nodes(body):
            if not isinstance(n, ast.Raise) or n.exc is None:
                continue
            call = n.exc if isinstance(n.exc, ast.Call) else None
            fexpr = call.func if call else n.exc
            if call is not None and _last_name(fexpr) in _HTTP_EXC:
                sc, det = _status_detail(call)
                site = RaiseSite(file, n.lineno, _status_of(sc), _detail_of(det), fname, None)
            else:
                info = _exception_site(g, handlers, ctx, fexpr, call)
                if info is None:
                    continue
                site = RaiseSite(file, n.lineno, info[0], info[1], fname, None, exception=info[3], handler=info[2])
            raws.append(_Raw(site, stack))

    for mod in g.mods.values():
        visit(mod.tree.body, mod.file, (mod.name, None, None), "<module>", [])
    for fn in g.fns.values():
        stack, f = [], fn
        while True:
            stack.insert(0, f.fid)
            if not f.parent:
                break
            f = g.fns[f.parent]
        visit(fn.node.body, g.mods[fn.module].file, (fn.module, fn.cls, fn.fid), fn.node.name, stack)
    return raws


def _entry(route, via: list[str], confidence: str) -> dict:
    return {"method": route.method, "path": route.written, "full_path": route.full, "exact": route.exact,
            "via": via, "confidence": confidence}


def scan_source(root: Path) -> list[RaiseSite]:
    g = ProjectGraph.build(root)
    raws = _extract(g)
    by_inner: dict[str, list[_Raw]] = {}
    by_stack: dict[str, list[_Raw]] = {}
    for r in raws:
        if r.stack:
            by_inner.setdefault(r.stack[-1], []).append(r)
        for f in r.stack:
            by_stack.setdefault(f, []).append(r)
    for route in g.routes:
        direct = by_stack.get(route.handler, [])
        for r in direct:
            r.site.routes.append(_entry(route, [], "direct"))
        direct_ids = {id(r) for r in direct}
        for fid, (via, dep, amb) in g.reach(route.handler, route.deps).items():
            if fid == route.handler:
                continue
            conf = "ambiguous" if amb else "via_dependency" if dep else "via_call"
            for r in by_inner.get(fid, []):
                if id(r) not in direct_ids:
                    r.site.routes.append(_entry(route, via, conf))
    sites = []
    for r in raws:
        s = r.site
        uniq = {(e["method"], e["path"], e["full_path"], tuple(e["via"]), e["confidence"]): e for e in s.routes}
        s.routes = sorted(uniq.values(), key=lambda e: _RANK[e["confidence"]])
        if s.routes:
            s.via, s.confidence = list(s.routes[0]["via"]), s.routes[0]["confidence"]
            d = next((e for e in s.routes if e["confidence"] == "direct"), None)
            s.route = (d["method"], d["path"]) if d else None
        sites.append(s)
    sites.sort(key=lambda s: (s.file, s.line))
    return sites


def _norm(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path).rstrip("/") or "/"


def _match(entry: dict, endpoints: list[Endpoint]) -> list[Endpoint]:
    """Spec endpoints for a route: full-path equality first, then suffix matching as the fallback."""
    cands = [ep for ep in endpoints if ep.method == entry["method"]]
    full = entry.get("full_path")
    if full:
        if entry.get("exact"):
            hit = [ep for ep in cands if _norm(ep.path) == _norm(full)]
            if hit:
                return hit
        hit = [ep for ep in cands if _norm(ep.path).endswith(_norm(full))]
        if hit:
            return hit
    return [ep for ep in cands if _norm(ep.path).endswith(_norm(entry["path"]))]


def map_sites_to_endpoints(sites: list[RaiseSite], endpoints: list[Endpoint]) -> dict[str, list[RaiseSite]]:
    """Attach each site to every endpoint whose route reaches it; the copies carry that route's via/confidence."""
    best: dict[tuple, tuple[int, RaiseSite]] = {}
    for site in sites:
        entries = site.routes or (
            [{"method": site.route[0], "path": site.route[1], "full_path": None, "exact": False,
              "via": [], "confidence": "direct"}] if site.route else [])
        for e in entries:
            for ep in _match(e, endpoints):
                key = (operation_id(ep), site.file, site.line)
                rank = _RANK.get(e["confidence"], 3)
                if key not in best or rank < best[key][0]:
                    best[key] = (rank, replace(site, route=(e["method"], e["path"]), via=list(e["via"]),
                                               confidence=e["confidence"]))
    mapped: dict[str, list[RaiseSite]] = {}
    for (op, _f, _l), (rank, inst) in sorted(best.items(), key=lambda kv: (kv[1][0], kv[0][1], kv[0][2])):
        mapped.setdefault(op, []).append(inst)
    return mapped


def run_scan(api: str) -> list[RaiseSite]:
    cfg = workspace.load_config(api)
    if not cfg.source_root:
        workspace.write_json(api, "source_errors.json", {"sites": [], "skipped": "no source_root in api.toml"})
        return []
    sites = scan_source(cfg.source_root)
    workspace.write_json(api, "source_errors.json", {"sites": [asdict(s) for s in sites]})
    return sites
