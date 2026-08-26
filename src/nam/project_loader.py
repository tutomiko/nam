from __future__ import annotations

import importlib
import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI

from nam.module import ModuleSpec, discover_module_specs
from nam.project import Project


@dataclass
class LoadedModules:
    mounted: list[dict] = field(default_factory=list)
    specs: list[ModuleSpec] = field(default_factory=list)


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


def load_modules_into_app(app: FastAPI, project: Project, serve_app_html, included_module_ids: frozenset[str] | None = None) -> LoadedModules:
    """
    included_module_ids, when given, restricts mounting to a single
    build's "include" list (see nam.build.load_build) - modules this
    process doesn't own are reached at runtime via Router.get_hostname()
    instead of being mounted here. None (the default) mounts every
    discovered module, i.e. an un-sharded, whole-project launch.
    """
    ensure_project_importable(project)

    result = LoadedModules()

    for module in discover_module_specs(project.modules_dir):
        if included_module_ids is not None and module.id not in included_module_ids:
            continue
        try:
            result.mounted.append(module.to_nav_entry())

            app.add_api_route(f"/{module.id}", serve_app_html, methods=["GET"])
            app.add_api_route(f"/{module.id}/", serve_app_html, methods=["GET"])

            import_path = f"modules.{module.id}.backend.routes"
            routes_module = importlib.import_module(import_path)

            if hasattr(routes_module, "router"):
                app.include_router(routes_module.router, prefix=f"/{module.id}", tags=[module.id])
                print(f"Mounted /{module.id} (Type: {module.type}, Icon: {module.icon})")

            result.specs.append(module)
        except Exception as e:
            print(f"Error loading {module.id}: {e}")

    load_global_routes_into_app(app, project)

    return result
