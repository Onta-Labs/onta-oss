"""Deny-by-default: every `/graphs/{tenant}` route authenticates the workspace.

Isolation at the graph layer (ONTA-402b) only matters AFTER the caller is
allowed into that workspace. The membership gate is ``get_tenant`` (and the
write/capability wrappers that call it). A new handler under
``/graphs/{tenant}`` that forgets ``Depends(get_tenant)`` is reachable with
any valid key — including a key that does not grant the path tenant.

This scanner is modelled on ``test_write_capability_convergence.py``: it
walks every route module, not an enumerated list, so a forgotten handler
fails CI instead of review. Planted-violation self-tests prove the scanner
still catches the gap.

Authorized dependencies (all resolve the path tenant against the key's
grant list before the handler runs):

* ``get_tenant``
* ``get_tenant_with_capability`` (calls ``get_tenant``)
* ``require_tenant_write`` (calls ``get_tenant``)
* same-module wrappers that transitively depend on one of those
"""

from __future__ import annotations

import ast
import pathlib

import infona_client

_ROUTES_DIR = pathlib.Path(infona_client.__file__).parent / "api" / "routes"

_AUTH_DEPS = frozenset(
    {
        "get_tenant",
        "get_tenant_with_capability",
        "require_tenant_write",
    }
)

#: Handlers whose path includes ``{tenant}`` but must NOT use get_tenant,
#: each with a written justification. Empty by design — a new exemption
#: needs a real reason, not a missing Depends.
_ALLOWLIST: dict[str, str] = {}


def _depends_targets(*nodes: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in nodes:
        for call in [node, *ast.walk(node)]:
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id in ("Depends", "Security")
                and call.args
            ):
                continue
            target = call.args[0]
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, ast.Attribute):
                names.add(target.attr)
    return names


def _dep_names(fn: ast.AST) -> set[str]:
    args = getattr(fn, "args", None)
    if args is None:
        return set()
    nodes: list[ast.AST] = list(args.defaults) + [d for d in args.kw_defaults if d]
    for arg in list(args.args) + list(args.kwonlyargs) + list(args.posonlyargs):
        if arg.annotation is not None:
            nodes.append(arg.annotation)
    return _depends_targets(*nodes)


def _decorator_dep_names(dec: ast.AST) -> set[str]:
    if not isinstance(dec, ast.Call):
        return set()
    return _depends_targets(
        *(kw.value for kw in dec.keywords if kw.arg == "dependencies")
    )


def _router_meta(tree: ast.AST) -> dict[str, tuple[str, set[str]]]:
    """router var → (prefix, router-wide dep names)."""
    out: dict[str, tuple[str, set[str]]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if not isinstance(value, ast.Call):
            continue
        func = value.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name != "APIRouter":
            continue
        prefix = ""
        for kw in value.keywords:
            if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                prefix = str(kw.value.value or "")
        deps = _depends_targets(
            *(kw.value for kw in value.keywords if kw.arg == "dependencies")
        )
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                out[target.id] = (prefix, deps)
    return out


def _decorator_path(dec: ast.AST) -> str | None:
    """First positional string arg of ``@router.get("/x")`` etc."""
    if not isinstance(dec, ast.Call) or not dec.args:
        return None
    arg0 = dec.args[0]
    if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
        return arg0.value
    return None


def _is_route_decorator(dec: ast.AST) -> bool:
    f = dec.func if isinstance(dec, ast.Call) else dec
    if not isinstance(f, ast.Attribute):
        return False
    return f.attr in {
        "get",
        "post",
        "put",
        "patch",
        "delete",
        "head",
        "options",
        "api_route",
        "websocket",
    }


def _module_functions(tree: ast.AST) -> dict[str, ast.AST]:
    return {
        n.name: n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _enforces_tenant_auth(
    fn: ast.AST,
    functions: dict[str, ast.AST],
    extra_deps: set[str] | None = None,
) -> bool:
    seen: set[str] = set()
    frontier = list(_dep_names(fn) | (extra_deps or set()))
    while frontier:
        dep = frontier.pop()
        if dep in _AUTH_DEPS:
            return True
        if dep in seen:
            continue
        seen.add(dep)
        inner = functions.get(dep)
        if inner is not None:
            frontier.extend(_dep_names(inner))
    return False


def _tenant_handlers(tree: ast.AST) -> list[tuple[ast.AST, set[str]]]:
    """Decorator-style handlers whose full path includes ``{tenant}``."""
    routers = _router_meta(tree)
    out: list[tuple[ast.AST, set[str]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        extra: set[str] = set()
        tenant_scoped = False
        for dec in node.decorator_list:
            if not _is_route_decorator(dec):
                continue
            path = _decorator_path(dec) or ""
            extra |= _decorator_dep_names(dec)
            f = dec.func if isinstance(dec, ast.Call) else dec
            owner = getattr(f, "value", None)
            prefix, router_deps = ("", set())
            if isinstance(owner, ast.Name) and owner.id in routers:
                prefix, router_deps = routers[owner.id]
                extra |= router_deps
            full = f"{prefix}{path}"
            if "{tenant}" in full:
                tenant_scoped = True
        if tenant_scoped:
            out.append((node, extra))
    return out


def _import_aliases(tree: ast.AST) -> dict[str, tuple[str, str]]:
    """local name → (imported module file stem, original name)."""
    out: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        stem = node.module.rsplit(".", 1)[-1]
        for alias in node.names:
            local = alias.asname or alias.name
            out[local] = (stem, alias.name)
    return out


def _rebind_calls(tree: ast.AST) -> list[tuple[str, str, set[str]]]:
    """``name = router.get(path)(handler)`` → (handler_name, full_path, extra)."""
    routers = _router_meta(tree)
    out: list[tuple[str, str, set[str]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        inner = node.value.func
        if not isinstance(inner, ast.Call) or not _is_route_decorator(inner):
            continue
        args = node.value.args
        if not args or not isinstance(args[0], ast.Name):
            continue
        path = _decorator_path(inner) or ""
        extra = _decorator_dep_names(inner)
        f = inner.func if isinstance(inner, ast.Call) else inner
        owner = getattr(f, "value", None)
        prefix, router_deps = ("", set())
        if isinstance(owner, ast.Name) and owner.id in routers:
            prefix, router_deps = routers[owner.id]
            extra |= router_deps
        full = f"{prefix}{path}"
        if "{tenant}" not in full:
            continue
        out.append((args[0].id, full, extra))
    return out


def _scan_routes(root: pathlib.Path) -> list[str]:
    trees: dict[str, ast.AST] = {}
    for path in sorted(root.rglob("*.py")):
        if path.name.startswith("_") and path.name != "__init__.py":
            continue
        trees[path.stem] = ast.parse(path.read_text())

    unguarded: list[str] = []
    for stem, tree in trees.items():
        functions = _module_functions(tree)
        aliases = _import_aliases(tree)
        for fn, extra in _tenant_handlers(tree):
            if not _enforces_tenant_auth(fn, functions, extra):
                unguarded.append(f"{stem}.py::{fn.name}")
        for handler_name, _full, extra in _rebind_calls(tree):
            fn = functions.get(handler_name)
            fn_functions = functions
            label = f"{stem}.py::{handler_name}"
            if fn is None and handler_name in aliases:
                mod_stem, orig = aliases[handler_name]
                other = trees.get(mod_stem)
                if other is None:
                    unguarded.append(label)
                    continue
                fn_functions = _module_functions(other)
                fn = fn_functions.get(orig)
                label = f"{mod_stem}.py::{orig}"
            if fn is None:
                unguarded.append(label)
                continue
            if not _enforces_tenant_auth(fn, fn_functions, extra):
                unguarded.append(label)
    return unguarded


def test_every_tenant_scoped_route_depends_on_get_tenant():
    offenders = [k for k in _scan_routes(_ROUTES_DIR) if k not in _ALLOWLIST]
    assert not offenders, (
        "These /graphs/{tenant} handlers do not Depends(get_tenant) (or a "
        "wrapper). A valid key for a DIFFERENT workspace can call them. Add "
        "`Depends(get_tenant)` / `require_tenant_write` / "
        "`get_tenant_with_capability`, or a justified _ALLOWLIST entry: "
        f"{offenders}"
    )


def test_allowlist_entries_are_live():
    unguarded = set(_scan_routes(_ROUTES_DIR))
    stale = sorted(k for k in _ALLOWLIST if k not in unguarded)
    assert not stale, f"stale _ALLOWLIST entries: {stale}"


def test_scanner_catches_planted_unguarded_tenant_route(tmp_path):
    planted = tmp_path / "planted.py"
    planted.write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter(prefix='/graphs/{tenant}')\n"
        "@router.get('/leak')\n"
        "async def leak():\n"
        "    return {}\n"
        "@router.post('/leak2')\n"
        "async def leak2():\n"
        "    return {}\n"
    )
    found = _scan_routes(tmp_path)
    assert "planted.py::leak" in found
    assert "planted.py::leak2" in found


def test_scanner_catches_planted_rebind_without_get_tenant(tmp_path):
    """knowledge_graphs.py registers ``router.get("")(_list_kgs)``. A new
    unguarded re-bind must fail the same way a forgotten decorator would."""
    (tmp_path / "sib.py").write_text(
        "from fastapi import APIRouter\n"
        "async def leak_impl():\n"
        "    return {}\n"
    )
    (tmp_path / "planted.py").write_text(
        "from fastapi import APIRouter\n"
        "from sib import leak_impl as _leak\n"
        "router = APIRouter(prefix='/graphs/{tenant}/kgs')\n"
        "leak = router.get('')(_leak)\n"
        "async def local_ok():\n"
        "    return {}\n"
        "still_leak = router.post('/x')(local_ok)\n"
    )
    found = _scan_routes(tmp_path)
    assert "sib.py::leak_impl" in found
    assert "planted.py::local_ok" in found


def test_scanner_accepts_get_tenant_and_wrappers(tmp_path):
    planted = tmp_path / "ok.py"
    planted.write_text(
        "from fastapi import APIRouter, Depends\n"
        "from infona_client.auth.api_keys import TenantContext, get_tenant\n"
        "from infona_client.auth.access import (\n"
        "    get_tenant_with_capability, require_tenant_write,\n"
        ")\n"
        "router = APIRouter(prefix='/graphs/{tenant}')\n"
        "@router.get('/a')\n"
        "async def a(t: TenantContext = Depends(get_tenant)):\n"
        "    return {}\n"
        "@router.get('/b')\n"
        "async def b(t: TenantContext = Depends(get_tenant_with_capability)):\n"
        "    return {}\n"
        "@router.post('/c')\n"
        "async def c(t: TenantContext = Depends(require_tenant_write)):\n"
        "    return {}\n"
        "async def wrapped(t: TenantContext = Depends(get_tenant)):\n"
        "    return t\n"
        "@router.get('/d')\n"
        "async def d(t: TenantContext = Depends(wrapped)):\n"
        "    return {}\n"
        "gated = APIRouter(prefix='/graphs/{tenant}', "
        "dependencies=[Depends(get_tenant)])\n"
        "@gated.get('/e')\n"
        "async def e():\n"
        "    return {}\n"
    )
    assert _scan_routes(tmp_path) == []


def test_scanner_ignores_non_tenant_paths(tmp_path):
    planted = tmp_path / "health.py"
    planted.write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "@router.get('/health')\n"
        "async def health():\n"
        "    return {}\n"
        "@router.get('/v1/me/tenants')\n"
        "async def list_tenants():\n"
        "    return []\n"
    )
    assert _scan_routes(tmp_path) == []
