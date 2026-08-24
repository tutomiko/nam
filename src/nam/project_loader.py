from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI

from nam.module import ModuleSpec, discover_module_specs
from nam.project import Project


@dataclass
class LoadedModules:
    mounted: list[dict] = field(default_factory=list)
    nav: list[dict] = field(default_factory=list)
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


def load_modules_into_app(app: FastAPI, project: Project, serve_app_html) -> LoadedModules:
    ensure_project_importable(project)

    result = LoadedModules()

    for module in discover_module_specs(project.modules_dir):
        try:
            nav_entry = module.to_nav_entry()
            result.mounted.append(nav_entry)
            if module.is_site:
                result.nav.append(nav_entry)

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

    return result
