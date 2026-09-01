from __future__ import annotations

import importlib
import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response
from starlette.background import BackgroundTask

from nam.module import ModuleSpec, discover_module_specs
from nam.project import Project
from nam.route_discovery import discover_routes
from nam.router import Router

APP_MOUNT_PREFIX = "/app"
API_MOUNT_PREFIX = "/api"

PROXY_TIMEOUT_SECONDS = 60.0

# Hop-by-hop headers per RFC 7230 sec. 6.1 - meaningful only for a single
# transport hop, so they must never be copied through a proxy in either
# direction (forwarding them verbatim would e.g. pass the upstream
# module's own connection-management headers through as if they were
# nam's, or send a stale content-length after the body is re-encoded).
_HOP_BY_HOP_HEADERS = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length", "host",
})


@dataclass
class LoadedModules:
    mounted: list[dict] = field(default_factory=list)
    specs: list[ModuleSpec] = field(default_factory=list)


class RouteConflictError(RuntimeError):
    """
    Raised (and left to propagate, crashing startup) when two modules -
    or one module twice - would register the same HTTP method + path
    under the shared, flat /api mount. Nam does NOT inject a module-id
    segment into a module's paths, on purpose: a resource like
    /resource_manager/tokenizers should exist exactly once, declared by
    whichever ONE module actually owns it, and called directly by every
    other module rather than each caller re-declaring its own duplicate
    proxy of the same resource. That only works if a genuine collision -
    two modules independently claiming the same path - is fatal rather
    than silently letting one handler shadow the other.
    """


def _check_no_route_conflicts(existing_owners: dict[tuple[str, str], str], module_id: str, method_paths: list[tuple[str, str]]) -> None:
    for method, path in method_paths:
        key = (method, path)
        owner = existing_owners.get(key)
        if owner is not None:
            raise RouteConflictError(
                f"Route conflict on {method} {API_MOUNT_PREFIX}{path}: "
                f"already registered by module '{owner}', cannot also be registered by module '{module_id}'. "
                f"A path should be declared by exactly one module - have the other module call this "
                f"one over HTTP instead of redeclaring it."
            )
        existing_owners[key] = module_id


def ensure_project_importable(project: Project) -> None:
    """
    Two distinct things need to become importable here:

    1. project.root, so modules can be reached as `modules.<name>.backend.routes`.
    2. project.shared_dir/src, because backend code across modules imports
       shared library code as bare top-level names (`from graph import ...`,
       `import prototype`) rather than as a `shared`-prefixed package - so
       it's shared/src itself that has to be on the path, not shared/.
    """
    for path in (project.root, project.shared_dir / "src"):
        path_str = str(path)
        if path_str not in sys.path:
            sys.path.insert(0, path_str)


def load_global_routes_into_app(app: FastAPI, project: Project) -> None:
    """
    app.py is optional and lives at the project root, sitting alongside
    modules/ and shared/ rather than inside any single module - it's for
    routes that apply across the whole project (e.g. navigation, health
    checks) rather than being scoped to one module's prefix. Its router,
    if present, is mounted unprefixed. Being a root-level *.py file, it's
    already covered by launcher.py's reload_dirs/reload_includes, so it
    gets picked up by the same Python reload as module backend code - no
    separate watcher is needed for it.

    Loaded from its file location directly, rather than via
    importlib.import_module("app"), so it can't collide with an unrelated
    top-level "app" module already on sys.path.
    """
    if not project.global_routes_path.exists():
        return

    try:
        spec = importlib.util.spec_from_file_location("project_app", project.global_routes_path)
        routes_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(routes_module)

        if hasattr(routes_module, "router"):
            app.include_router(routes_module.router, tags=["global"])
            print("Mounted app.py")
    except Exception as e:
        print(f"Error loading app.py: {e}")


def _forward_headers(headers) -> dict:
    return {key: value for key, value in headers.items() if key.lower() not in _HOP_BY_HOP_HEADERS}


def _build_proxy_endpoint(module_id: str, path: str, router: Router):
    """
    Builds the callback nam registers, in place of the module's own
    handler, for one specific /api<path> route on a build that doesn't
    include the module which declared it. Forwards method, query
    string, headers, and body to wherever the module actually lives
    (see Router.get_hostname / builds.yaml sharding) and streams the
    response straight back - so a caller hitting /api<path> gets the
    exact same result whether the owning module is mounted locally or
    sharded off into another process. The full request path (with real
    path-param values already substituted by Starlette's routing, not
    the "{param}" template) is forwarded as-is, since with no injected
    segment the request path onto the upstream module's own mount is a
    direct match - no rewriting needed. Resolved on every call rather
    than once at startup, since a referenced module's address (an env
    var, see Router.reference) could change across a redeploy without
    this process restarting.
    """
    async def proxy_endpoint(request: Request) -> Response:
        base_url = router.get_hostname(module_id)
        target_url = f"{base_url}{request.url.path}"
        body = await request.body()

        client = httpx.AsyncClient(timeout=PROXY_TIMEOUT_SECONDS)
        try:
            upstream_response = await client.request(
                request.method,
                target_url,
                params=request.query_params,
                headers=_forward_headers(request.headers),
                content=body,
            )
        except httpx.HTTPError as exc:
            await client.aclose()
            return Response(
                content=f'{{"detail": "Could not reach module \'{module_id}\': {exc}"}}',
                status_code=502,
                media_type="application/json",
            )

        return Response(
            content=upstream_response.content,
            status_code=upstream_response.status_code,
            headers=_forward_headers(upstream_response.headers),
            media_type=upstream_response.headers.get("content-type"),
            background=BackgroundTask(client.aclose),
        )

    return proxy_endpoint


def _mount_proxy_routes_for_excluded_module(
    app: FastAPI,
    module: ModuleSpec,
    router: Router,
    route_owners: dict[tuple[str, str], str],
) -> None:
    """
    Statically discovers module's route shapes (method + path) straight
    from its routes.py source via AST (see nam.route_discovery) - never
    importing the module, so its backend's actual dependencies never
    need to be installed in this process. Registers one proxy endpoint
    per discovered route, at the SAME bare /api<path> it would get if
    the module were mounted locally, and in the same declaration order
    (so path-shadowing rules, e.g. a literal path registered ahead of a
    {param} one, behave identically whether proxied or local).
    """
    discovered = discover_routes(module.backend_routes_path)
    _check_no_route_conflicts(
        route_owners, module.id, [(route.method, route.path) for route in discovered]
    )

    for route in discovered:
        app.add_api_route(
            f"{API_MOUNT_PREFIX}{route.path}",
            _build_proxy_endpoint(module.id, route.path, router),
            methods=[route.method],
            tags=[module.id],
        )
    print(f"Proxying {len(discovered)} route(s) declared by {module.id} -> {router.get_hostname(module.id)} (Type: {module.type}, Icon: {module.icon})")


def load_modules_into_app(app: FastAPI, project: Project, serve_app_html, included_module_ids: frozenset[str] | None = None) -> LoadedModules:
    """
    included_module_ids, when given, restricts LOCAL MOUNTING to a single
    build's "include" list (see nam.build.load_build) - modules this
    process doesn't own still get their declared /api<path> routes
    registered, just as per-route proxies (see
    _mount_proxy_routes_for_excluded_module) instead of a locally-
    imported router, so every module's declared API exists identically
    no matter which build is running: callers never need to know or
    care whether a given path is served locally or sharded elsewhere.
    None (the default) mounts every discovered module locally, i.e. an
    un-sharded, whole-project launch with no proxies needed.

    Nam mounts each module's router directly onto /api with NO injected
    module-id segment - a module's routes.py is itself responsible for
    the full path it wants to own (see DEV_MANUAL.md section 1), so a
    resource is declared by exactly one module and every other module
    reaches it the same way an external caller would: a plain HTTP call
    to that path. _check_no_route_conflicts is what makes this safe -
    two modules independently declaring the same path is a startup
    error, not a silent shadow.
    """
    ensure_project_importable(project)

    result = LoadedModules()
    route_owners: dict[tuple[str, str], str] = {}
    router: Router = app.state.router

    for module in discover_module_specs(project.modules_dir):
        result.mounted.append(module.to_nav_entry())

        if included_module_ids is not None and module.id not in included_module_ids:
            try:
                _mount_proxy_routes_for_excluded_module(app, module, router, route_owners)
                result.specs.append(module)
            except RouteConflictError:
                raise
            except Exception as e:
                print(f"Error proxying {module.id}: {e}")
            continue

        try:
            app.add_api_route(f"{APP_MOUNT_PREFIX}/{module.id}", serve_app_html, methods=["GET"])
            app.add_api_route(f"{APP_MOUNT_PREFIX}/{module.id}/", serve_app_html, methods=["GET"])

            discovered = discover_routes(module.backend_routes_path)
            _check_no_route_conflicts(
                route_owners, module.id, [(route.method, route.path) for route in discovered]
            )

            import_path = f"modules.{module.id}.backend.routes"
            routes_module = importlib.import_module(import_path)

            if hasattr(routes_module, "router"):
                app.include_router(routes_module.router, prefix=API_MOUNT_PREFIX, tags=[module.id])
                print(f"Mounted {APP_MOUNT_PREFIX}/{module.id} (Type: {module.type}, Icon: {module.icon})")

            result.specs.append(module)
        except RouteConflictError:
            raise
        except Exception as e:
            print(f"Error loading {module.id}: {e}")

    load_global_routes_into_app(app, project)

    return result
