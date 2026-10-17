"""Project-wide static call graph and FastAPI route discovery, built from the AST only.

Nothing is guessed: a call whose target cannot be resolved statically produces no edge, and a
name that resolves to several candidates produces one edge per candidate, flagged ambiguous.
"""

from __future__ import annotations

import ast
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

MAX_DEPTH = 6
SKIP_PARTS = {".venv", "venv", "node_modules", "__pycache__", "site-packages"}
ROUTE_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
FUNC_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)

Ctx = tuple  # (module name, enclosing class id | None, enclosing function id | None)
Target = tuple  # ("fn", fid) | ("cls", cid) | ("mod", name) | ("var", mod, name) | ("alias", mod, name)


@dataclass
class Fn:
    fid: str
    module: str
    qual: str
    node: ast.AST
    cls: str | None
    parent: str | None
    nested: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class Cls:
    cid: str
    module: str
    qual: str
    node: ast.ClassDef
    methods: dict[str, list[str]] = field(default_factory=dict)
    attr_types: dict[str, ast.expr] = field(default_factory=dict)


@dataclass
class Mod:
    name: str
    file: str
    tree: ast.Module
    is_pkg: bool
    defs: dict[str, list[Target]] = field(default_factory=dict)
    imports: dict[str, tuple] = field(default_factory=dict)  # local -> ("mod", name) | ("sym", mod, name)
    consts: dict[str, str] = field(default_factory=dict)
    instances: dict[str, ast.expr] = field(default_factory=dict)  # module-level `x = Cls(...)` -> Cls expr
    aliases: dict[str, ast.expr] = field(default_factory=dict)  # `X = Annotated[T, Depends(f)]`


@dataclass
class Container:  # an APIRouter or FastAPI instance
    key: tuple
    kind: str
    ctx: Ctx
    prefix: ast.expr | None
    deps: list  # [(ctx, callable expr)]


@dataclass
class Include:
    ctx: Ctx
    parent: ast.expr
    child: ast.expr
    prefix: ast.expr | None
    deps: list
    parent_key: tuple | None = None
    child_key: tuple | None = None


@dataclass
class RouteReg:
    ctx: Ctx
    methods: list[str]
    path: str
    handler: object  # fid (str) or an expression resolved at link time
    base: ast.expr | None
    deps: list
    line: int


@dataclass
class Route:
    method: str
    written: str
    full: str
    exact: bool
    handler: str
    deps: list


@dataclass
class Edge:
    target: str
    label: str
    kind: str  # call | dep
    amb: bool


def _last_name(expr: ast.AST) -> str:
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return expr.attr
    return ""


def _dep_arg(call: ast.AST) -> ast.expr | None:
    """`Depends(x)` / `Security(x, ...)` -> x."""
    if isinstance(call, ast.Call) and _last_name(call.func) in {"Depends", "Security"}:
        if call.args:
            return call.args[0]
        for kw in call.keywords:
            if kw.arg == "dependency":
                return kw.value
    return None


def _dep_list(expr: ast.expr | None) -> list[ast.expr]:
    if isinstance(expr, (ast.List, ast.Tuple)):
        return [a for e in expr.elts if (a := _dep_arg(e)) is not None]
    return []


def own_nodes(body):
    """Nodes of a body without descending into nested functions or classes."""
    stack = [n for n in body if not isinstance(n, FUNC_NODES + (ast.ClassDef,))]
    while stack:
        n = stack.pop()
        yield n
        for c in ast.iter_child_nodes(n):
            if not isinstance(c, FUNC_NODES + (ast.ClassDef,)):
                stack.append(c)


def join_path(a: str, b: str) -> str:
    out = a.rstrip("/") + (("/" + b.lstrip("/")) if b else "")
    if out and not out.startswith("/"):
        out = "/" + out
    return out or "/"


class ProjectGraph:
    def __init__(self) -> None:
        self.mods: dict[str, Mod] = {}
        self.fns: dict[str, Fn] = {}
        self.classes: dict[str, Cls] = {}
        self.containers: dict[tuple, Container] = {}
        self.includes: list[Include] = []
        self.route_regs: list[RouteReg] = []
        self.handler_regs: list[tuple] = []  # (ctx, exc expr, handler expr, line)
        self._mod_cache: dict[str, list[str]] = {}
        self._edges: dict[str, list[Edge]] = {}
        self.routes: list[Route] = []
        self.unparsed: list[str] = []  # files this Python could not read (e.g. newer syntax)

    # --- building ---------------------------------------------------------
    @classmethod
    def build(cls, root: Path) -> "ProjectGraph":
        g = cls()
        for path in sorted(root.rglob("*.py")):
            if any(p in SKIP_PARTS for p in path.parts):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError, ValueError):
                g.unparsed.append(str(path.relative_to(root)))
                continue
            rel = path.relative_to(root)
            parts = list(rel.with_suffix("").parts)
            is_pkg = parts[-1] == "__init__"
            if is_pkg:
                parts = parts[:-1]
            name = ".".join(parts) or "__root__"
            g.mods[name] = Mod(name, str(rel), tree, is_pkg)
        for mod in g.mods.values():
            _Collector(g, mod).visit(mod.tree)
        g._link()
        return g

    def _link(self) -> None:
        for inc in self.includes:
            inc.parent_key = self.resolve_container(inc.ctx, inc.parent)
            inc.child_key = self.resolve_container(inc.ctx, inc.child)
        for reg in self.route_regs:
            key = self.resolve_container(reg.ctx, reg.base) if reg.base is not None else None
            variants = self.container_variants(key, frozenset()) if key else [("", False, [])]
            if isinstance(reg.handler, str):
                handlers = [reg.handler]
            else:
                handlers = _uniq([t[1] for t in self.resolve(reg.ctx, reg.handler) if t[0] == "fn"])
            for prefix, exact, cdeps in variants:
                for h in handlers:
                    for m in reg.methods:
                        self.routes.append(Route(m, reg.path, join_path(prefix, reg.path), exact, h,
                                                 cdeps + reg.deps))

    # --- modules and symbols ---------------------------------------------
    def find_modules(self, name: str) -> list[str]:
        if not name:
            return []
        if name in self._mod_cache:
            return self._mod_cache[name]
        if name in self.mods:
            out = [name]
        else:
            out = [m for m in self.mods if m != "__root__" and (name.endswith("." + m) or m.endswith("." + name))]
        self._mod_cache[name] = out
        return out

    def lookup(self, mod: str, name: str, depth: int = 0) -> list[Target]:
        m = self.mods.get(mod)
        if m is None or depth > 6:
            return []
        if name in m.defs:
            return list(m.defs[name])
        imp = m.imports.get(name)
        if imp is None:
            return []
        if imp[0] == "mod":
            return [("mod", x) for x in self.find_modules(imp[1])]
        _, src, sym = imp
        res: list[Target] = []
        for sm in self.find_modules(src):
            res += self.lookup(sm, sym, depth + 1)
        if not res:
            res = [("mod", x) for x in self.find_modules(f"{src}.{sym}" if src else sym)]
        return _uniq(res)

    def _scope_lookup(self, fid: str | None, name: str) -> list[Target]:
        while fid:
            fn = self.fns[fid]
            if name in fn.nested:
                return [("fn", x) for x in fn.nested[name]]
            fid = fn.parent
        return []

    def class_member(self, cid: str, name: str, seen: frozenset = frozenset()) -> list[str]:
        c = self.classes.get(cid)
        if c is None or cid in seen:
            return []
        if name in c.methods:
            return list(c.methods[name])
        out: list[str] = []
        for b in c.node.bases:
            for t in self.resolve((c.module, None, None), b):
                if t[0] == "cls":
                    out += self.class_member(t[1], name, seen | {cid})
        return _uniq(out)

    def resolve(self, ctx: Ctx, expr: ast.AST | None) -> list[Target]:
        mod, cid, fid = ctx
        if isinstance(expr, ast.Name):
            return self._scope_lookup(fid, expr.id) or self.lookup(mod, expr.id)
        if not isinstance(expr, ast.Attribute):
            return []
        base, attr = expr.value, expr.attr
        out: list[Target] = []
        if isinstance(base, ast.Name) and base.id in {"self", "cls"} and cid:
            out += [("fn", f) for f in self.class_member(cid, attr)]
        else:
            for t in self.resolve(ctx, base):
                if t[0] == "mod":
                    found = self.lookup(t[1], attr) or [("mod", x) for x in self.find_modules(f"{t[1]}.{attr}")]
                    out += found
                elif t[0] == "cls":
                    out += [("fn", f) for f in self.class_member(t[1], attr)]
        for c in self.instance_classes(ctx, base):
            out += [("fn", f) for f in self.class_member(c, attr)]
        if not out:  # `import a.b.c` then `a.b.c.f`: resolve the dotted prefix as a module
            dotted = _dotted(base)
            if dotted:
                root, _, rest = dotted.partition(".")
                imp = self.mods[mod].imports.get(root) if mod in self.mods else None
                if imp and imp[0] == "mod" and rest:
                    for m in self.find_modules(imp[1] + "." + rest):
                        out += self.lookup(m, attr) or [("mod", x) for x in self.find_modules(f"{m}.{attr}")]
        return _uniq(out)

    def instance_classes(self, ctx: Ctx, expr: ast.AST) -> list[str]:
        """Classes `expr` is an instance of, when statically obvious (`x = Cls()` / `self.a = Cls()`)."""
        mod, cid, _ = ctx
        out: list[str] = []
        if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name) and expr.value.id == "self" and cid:
            c = self.classes.get(cid)
            if c and expr.attr in c.attr_types:
                out += [t[1] for t in self.resolve((c.module, cid, None), c.attr_types[expr.attr]) if t[0] == "cls"]
        elif isinstance(expr, (ast.Name, ast.Attribute)):
            for t in self.resolve(ctx, expr):
                if t[0] == "var":
                    inst = self.mods[t[1]].instances.get(t[2])
                    if inst is not None:
                        out += [x[1] for x in self.resolve((t[1], None, None), inst) if x[0] == "cls"]
        return _uniq(out)

    # --- containers (APIRouter / FastAPI) --------------------------------
    def resolve_container(self, ctx: Ctx, expr: ast.AST | None) -> tuple | None:
        mod, cid, _ = ctx
        if isinstance(expr, ast.Attribute) and expr.attr == "router":  # app.router
            inner = self.resolve_container(ctx, expr.value)
            if inner and self.containers[inner].kind == "FastAPI":
                return inner
        if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name) and expr.value.id == "self" and cid:
            key = (self.classes[cid].module, f"{self.classes[cid].qual}.{expr.attr}")
            if key in self.containers:
                return key
        if isinstance(expr, (ast.Name, ast.Attribute)):
            for t in self.resolve(ctx, expr):
                if t[0] == "var" and (t[1], t[2]) in self.containers:
                    return (t[1], t[2])
            if isinstance(expr, ast.Attribute):
                for c in self.instance_classes(ctx, expr.value):
                    key = (self.classes[c].module, f"{self.classes[c].qual}.{expr.attr}")
                    if key in self.containers:
                        return key
        if isinstance(expr, ast.Name) and (mod, expr.id) in self.containers:
            return (mod, expr.id)
        return None

    def prefix_of(self, expr: ast.expr | None, ctx: Ctx) -> tuple[str, bool]:
        if expr is None:
            return "", True
        s = self._const_str(expr, ctx[0])
        return (s, True) if s is not None else ("", False)

    def _const_str(self, e: ast.AST, mod: str) -> str | None:
        if isinstance(e, ast.Constant) and isinstance(e.value, str):
            return e.value
        if isinstance(e, ast.Name) and mod in self.mods:
            return self.mods[mod].consts.get(e.id)
        if isinstance(e, ast.BinOp) and isinstance(e.op, ast.Add):
            a, b = self._const_str(e.left, mod), self._const_str(e.right, mod)
            return a + b if a is not None and b is not None else None
        return None

    def container_variants(self, key: tuple, seen: frozenset) -> list[tuple[str, bool, list]]:
        """All (full prefix, exact?, dependencies) a container is reachable under."""
        c = self.containers[key]
        own, own_exact = self.prefix_of(c.prefix, c.ctx)
        incs = [i for i in self.includes if i.child_key == key]
        if not incs:
            return [(own, own_exact and c.kind == "FastAPI", list(c.deps))]
        out: list[tuple[str, bool, list]] = []
        for inc in incs:
            ipre, iexact = self.prefix_of(inc.prefix, inc.ctx)
            if inc.parent_key is None:
                out.append((join_path(ipre, own) if (ipre or own) else "", False, inc.deps + c.deps))
                continue
            if inc.parent_key in seen or inc.parent_key == key:
                continue
            for pp, pe, pd in self.container_variants(inc.parent_key, seen | {key}):
                full = join_path(join_path(pp, ipre), own) if (pp or ipre or own) else ""
                out.append((full, pe and iexact and own_exact, pd + inc.deps + c.deps))
        return out or [(own, False, list(c.deps))]

    # --- edges ------------------------------------------------------------
    def annotation_deps(self, ctx: Ctx, ann: ast.AST | None, depth: int = 0) -> list[tuple]:
        out: list[tuple] = []
        if ann is None or depth > 4:
            return out
        if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
            try:
                ann = ast.parse(ann.value, mode="eval").body
            except SyntaxError:
                return out
        if isinstance(ann, ast.Call):
            a = _dep_arg(ann)
            return [(ctx, a)] if a is not None else out
        if isinstance(ann, ast.Subscript):
            out += self.annotation_deps(ctx, ann.value, depth)
            sl = ann.slice
            for e in (sl.elts if isinstance(sl, ast.Tuple) else [sl]):
                out += self.annotation_deps(ctx, e, depth)
        elif isinstance(ann, ast.BinOp):
            out += self.annotation_deps(ctx, ann.left, depth) + self.annotation_deps(ctx, ann.right, depth)
        elif isinstance(ann, (ast.Name, ast.Attribute)):
            for t in self.resolve(ctx, ann):
                if t[0] == "alias":
                    out += self.annotation_deps((t[1], None, None), self.mods[t[1]].aliases[t[2]], depth + 1)
        return out

    def param_deps(self, fid: str) -> list[tuple]:
        fn = self.fns[fid]
        a = fn.node.args
        ctx = (fn.module, fn.cls, fn.parent)
        out: list[tuple] = []
        for d in list(a.defaults) + [d for d in a.kw_defaults if d is not None]:
            arg = _dep_arg(d)
            if arg is not None:
                out.append((ctx, arg))
        for p in a.posonlyargs + a.args + a.kwonlyargs:
            out += self.annotation_deps(ctx, p.annotation)
        return out

    def dep_targets(self, ctx: Ctx, expr: ast.AST) -> list[tuple[str, bool]]:
        fids: list[str] = []
        for t in self.resolve(ctx, expr):
            if t[0] == "fn":
                fids.append(t[1])
            elif t[0] == "cls":
                fids += self.class_member(t[1], "__init__") + self.class_member(t[1], "__call__")
        fids = list(dict.fromkeys(fids))
        return [(f, len(fids) > 1) for f in fids]

    def _locals(self, fn: Fn) -> set[str]:
        a = fn.node.args
        names = {p.arg for p in a.posonlyargs + a.args + a.kwonlyargs}
        names |= {x.arg for x in (a.vararg, a.kwarg) if x}
        for n in own_nodes(fn.node.body):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                names.add(n.id)
            elif isinstance(n, ast.ExceptHandler) and n.name:
                names.add(n.name)
        return names

    def edges(self, fid: str) -> list[Edge]:
        if fid in self._edges:
            return self._edges[fid]
        fn = self.fns[fid]
        out: list[Edge] = []
        for ctx, expr in self.param_deps(fid):
            for target, amb in self.dep_targets(ctx, expr):
                out.append(Edge(target, ast.unparse(expr), "dep", amb))
        local = self._locals(fn)
        ctx = (fn.module, fn.cls, fid)
        for node in sorted((n for n in own_nodes(fn.node.body) if isinstance(n, ast.Call)),
                           key=lambda n: (n.lineno, n.col_offset)):
            root = node.func
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name) and root.id in local and root.id not in {"self", "cls"}:
                continue  # a parameter / local variable: its target is dynamic
            fids: list[str] = []
            for t in self.resolve(ctx, node.func):
                if t[0] == "fn":
                    fids.append(t[1])
                elif t[0] == "cls":
                    fids += self.class_member(t[1], "__init__")
            fids = list(dict.fromkeys(fids))
            for f in fids:
                out.append(Edge(f, ast.unparse(node.func), "call", len(fids) > 1))
        self._edges[fid] = out
        return out

    def reach(self, handler: str, extra_deps: list) -> dict[str, tuple[list[str], bool, bool]]:
        """fid -> (via labels, reached through a dependency, ambiguous), shortest path first."""
        res: dict[str, tuple[list[str], bool, bool]] = {handler: ([], False, False)}
        queue: deque[tuple[str, int]] = deque()
        for ctx, expr in extra_deps:
            for target, amb in self.dep_targets(ctx, expr):
                if target not in res:
                    res[target] = ([ast.unparse(expr)], True, amb)
                    queue.append((target, 1))
        queue.appendleft((handler, 0))
        while queue:
            node, depth = queue.popleft()
            if depth >= MAX_DEPTH:
                continue
            via, dep, amb = res[node]
            for e in self.edges(node):
                if e.target not in res:
                    res[e.target] = (via + [e.label], dep or e.kind == "dep", amb or e.amb)
                    queue.append((e.target, depth + 1))
        return res


def _uniq(items: list) -> list:
    return list(dict.fromkeys(items))


def _dotted(e: ast.AST) -> str | None:
    if isinstance(e, ast.Name):
        return e.id
    if isinstance(e, ast.Attribute):
        d = _dotted(e.value)
        return f"{d}.{e.attr}" if d else None
    return None


class _Collector(ast.NodeVisitor):
    """First pass over one module: definitions, imports, containers, route registrations."""

    def __init__(self, g: ProjectGraph, mod: Mod):
        self.g, self.mod = g, mod
        self.q: list[str] = []
        self.cls: list[str] = []  # enclosing class ids
        self.fn: list[str] = []  # enclosing function ids
        self.scope: list[str] = []  # "class" | "fn"

    @property
    def ctx(self) -> Ctx:
        return (self.mod.name, self.cls[-1] if self.cls else None, self.fn[-1] if self.fn else None)

    def _add_def(self, name: str, target: Target) -> None:
        self.mod.defs.setdefault(name, []).append(target)

    # imports
    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            if a.asname:
                self.mod.imports[a.asname] = ("mod", a.name)
            else:
                self.mod.imports[a.name.split(".")[0]] = ("mod", a.name.split(".")[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = node.module or ""
        if node.level:
            pkg = self.mod.name if self.mod.is_pkg else self.mod.name.rpartition(".")[0]
            parts = pkg.split(".") if pkg and pkg != "__root__" else []
            parts = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
            base = ".".join(parts + ([node.module] if node.module else []))
        for a in node.names:
            if a.name != "*":
                self.mod.imports[a.asname or a.name] = ("sym", base, a.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qual = ".".join(self.q + [node.name])
        cid = f"{self.mod.name}:{qual}"
        self.g.classes[cid] = Cls(cid, self.mod.name, qual, node)
        if not self.q:
            self._add_def(node.name, ("cls", cid))
        for d in node.decorator_list:
            self.visit(d)
        self.q.append(node.name)
        self.cls.append(cid)
        self.scope.append("class")
        for stmt in node.body:
            self.visit(stmt)
        self.scope.pop()
        self.cls.pop()
        self.q.pop()

    def _visit_fn(self, node) -> None:
        qual = ".".join(self.q + [node.name])
        fid = f"{self.mod.name}:{qual}"
        if fid in self.g.fns:  # redefinition (if/else branches): keep both, disambiguate
            fid = f"{fid}@{node.lineno}"
        parent = self.fn[-1] if self.fn else None
        fn = Fn(fid, self.mod.name, qual, node, self.cls[-1] if self.cls else None, parent)
        self.g.fns[fid] = fn
        if self.scope and self.scope[-1] == "class":
            self.g.classes[self.cls[-1]].methods.setdefault(node.name, []).append(fid)
        elif parent:
            self.g.fns[parent].nested.setdefault(node.name, []).append(fid)
        elif not self.q:
            self._add_def(node.name, ("fn", fid))
        ctx = self.ctx
        for dec in node.decorator_list:
            self._decorator(dec, fid, ctx)
            self.visit(dec)
        self.q.append(node.name)
        self.fn.append(fid)
        self.scope.append("fn")
        for stmt in node.body:
            self.visit(stmt)
        self.scope.pop()
        self.fn.pop()
        self.q.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = _visit_fn

    def _decorator(self, dec: ast.AST, fid: str, ctx: Ctx) -> None:
        if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
            return
        attr = dec.func.attr
        if attr == "exception_handler" and dec.args:
            self.g.handler_regs.append((ctx, dec.args[0], ("fid", fid), dec.lineno))
            return
        if attr in ROUTE_METHODS or attr == "api_route":
            path = self._route_path(dec)
            if path is None:
                return
            if attr == "api_route":
                methods = _str_list(_kw(dec, "methods")) or ["GET"]
            else:
                methods = [attr.upper()]
            deps = [(ctx, a) for a in _dep_list(_kw(dec, "dependencies"))]
            self.g.route_regs.append(RouteReg(ctx, methods, path, fid, dec.func.value, deps, dec.lineno))

    def _route_path(self, call: ast.Call) -> str | None:
        e = call.args[0] if call.args else _kw(call, "path")
        return self.g._const_str(e, self.mod.name) if e is not None else None

    # assignments
    def _assign(self, targets: list[ast.expr], value: ast.expr | None) -> None:
        if value is None:
            return
        for t in targets:
            key = None
            if isinstance(t, ast.Name):
                key = t.id
            elif isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self" and self.cls:
                key = f"{self.g.classes[self.cls[-1]].qual}.{t.attr}"
            if key is None:
                continue
            top = isinstance(t, ast.Name) and not self.q
            if isinstance(value, ast.Call):
                kind = _last_name(value.func)
                if kind in {"APIRouter", "FastAPI"}:
                    deps = [(self.ctx, a) for a in _dep_list(_kw(value, "dependencies"))]
                    self.g.containers[(self.mod.name, key)] = Container(
                        (self.mod.name, key), kind, self.ctx, _kw(value, "prefix"), deps)
                    if kind == "FastAPI":
                        for k in value.keywords:
                            if k.arg == "exception_handlers" and isinstance(k.value, ast.Dict):
                                for ek, hv in zip(k.value.keys, k.value.values):
                                    if ek is not None:
                                        self.g.handler_regs.append((self.ctx, ek, ("expr", hv), value.lineno))
                if isinstance(t, ast.Name) and top:
                    self.mod.instances[t.id] = value.func
                if isinstance(t, ast.Attribute) and self.cls:
                    self.g.classes[self.cls[-1]].attr_types[t.attr] = value.func
            if top:
                self._add_def(t.id, ("var", self.mod.name, t.id))
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    self.mod.consts[t.id] = value.value
                elif self.g._const_str(value, self.mod.name) is not None:
                    self.mod.consts[t.id] = self.g._const_str(value, self.mod.name)  # type: ignore[assignment]
                if any(_dep_arg(n) is not None for n in ast.walk(value)):
                    self.mod.aliases[t.id] = value
                    self._add_def(t.id, ("alias", self.mod.name, t.id))

    def visit_Assign(self, node: ast.Assign) -> None:
        self._assign(node.targets, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._assign([node.target], node.value)
        self.generic_visit(node)

    def visit_TypeAlias(self, node) -> None:  # `type X = Annotated[...]` (3.12+)
        if not self.q and any(_dep_arg(n) is not None for n in ast.walk(node.value)):
            self.mod.aliases[node.name.id] = node.value
            self._add_def(node.name.id, ("alias", self.mod.name, node.name.id))

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute):
            attr, ctx = node.func.attr, self.ctx
            if attr == "include_router" and node.args:
                deps = [(ctx, a) for a in _dep_list(_kw(node, "dependencies"))]
                self.g.includes.append(Include(ctx, node.func.value, node.args[0], _kw(node, "prefix"), deps))
            elif attr == "add_api_route":
                path_e = node.args[0] if node.args else _kw(node, "path")
                end_e = node.args[1] if len(node.args) > 1 else _kw(node, "endpoint")
                path = self.g._const_str(path_e, self.mod.name) if path_e is not None else None
                if path is not None and end_e is not None:
                    methods = _str_list(_kw(node, "methods")) or ["GET"]
                    deps = [(ctx, a) for a in _dep_list(_kw(node, "dependencies"))]
                    self.g.route_regs.append(RouteReg(ctx, methods, path, end_e, node.func.value, deps, node.lineno))
            elif attr == "add_exception_handler" and len(node.args) >= 2:
                self.g.handler_regs.append((ctx, node.args[0], ("expr", node.args[1]), node.lineno))
        self.generic_visit(node)


def _kw(call: ast.Call, name: str) -> ast.expr | None:
    for k in call.keywords:
        if k.arg == name:
            return k.value
    return None


def _str_list(e: ast.expr | None) -> list[str]:
    if isinstance(e, (ast.List, ast.Tuple, ast.Set)):
        return [x.value.upper() for x in e.elts if isinstance(x, ast.Constant) and isinstance(x.value, str)]
    return []
