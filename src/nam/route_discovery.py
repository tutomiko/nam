from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "options", "head"})


@dataclass(frozen=True)
class DiscoveredRoute:
    method: str
    path: str


def _router_decorator_call(decorator: ast.expr, router_name: str) -> ast.Call | None:
    if not isinstance(decorator, ast.Call):
        return None
    func = decorator.func
    if not isinstance(func, ast.Attribute):
        return None
    if func.attr.lower() not in HTTP_METHODS:
        return None
    if not isinstance(func.value, ast.Name) or func.value.id != router_name:
        return None
    return decorator


def _string_literal(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def discover_routes(routes_path: Path, router_name: str = "router") -> list[DiscoveredRoute]:
    """
    Parses routes.py as source text (never imported, never executed) and
    pulls every @router.<method>("/path") decorator off of it, in the
    order they're declared. This is the only way to learn an EXCLUDED
    module's route shapes: importing routes.py would also import
    whatever heavy/GPU-only dependencies that module's backend code
    happens to need, which is exactly what sharding a module out of this
    process is meant to avoid (see builds.yaml / DEV_MANUAL.md section 6).
    Declaration order is preserved because it's meaningful - e.g. a
    module may register "/models/search" before "/models/{model_name}"
    specifically so the literal path wins over the path-param one.
    """
    if not routes_path.exists():
        return []

    source = routes_path.read_text()
    tree = ast.parse(source, filename=str(routes_path))

    routes: list[DiscoveredRoute] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            call = _router_decorator_call(decorator, router_name)
            if call is None:
                continue
            if not call.args:
                continue
            path = _string_literal(call.args[0])
            if path is None:
                continue
            routes.append(DiscoveredRoute(method=call.func.attr.upper(), path=path))

    return routes
