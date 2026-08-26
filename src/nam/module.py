from __future__ import annotations

import yaml
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_MODULE_TYPE = "site"

# Modules start in this order: services first (so sites/tools that depend
# on a running service - e.g. a DB connection or background worker opened
# at import time - never race against it), then sites, then tools.
# Anything with a type not listed here starts last, after tools.
STARTUP_ORDER = ("service", "site", "tool")


@dataclass
class ModuleSpec:
    id: str
    name: str
    type: str
    icon: Optional[str]
    path: Path

    @property
    def backend_routes_path(self) -> Path:
        return self.path / "backend" / "routes.py"

    @property
    def frontend_dir(self) -> Path:
        return self.path / "frontend"

    @property
    def is_site(self) -> bool:
        return self.type == "site"

    def to_nav_entry(self) -> dict:
        entry = {"id": self.id, "name": self.name, "type": self.type}
        if self.icon:
            entry["icon"] = self.icon
        return entry


def _startup_priority(module_type: str) -> int:
    try:
        return STARTUP_ORDER.index(module_type)
    except ValueError:
        return len(STARTUP_ORDER)


def discover_module_specs(modules_dir: Path) -> list[ModuleSpec]:
    if not modules_dir.exists():
        return []

    specs = []
    for module_name in sorted(p.name for p in modules_dir.iterdir() if p.is_dir()):
        module_path = modules_dir / module_name
        yaml_path = module_path / "module.yaml"
        routes_path = module_path / "backend" / "routes.py"

        if not (yaml_path.exists() and routes_path.exists()):
            continue

        with open(yaml_path, "r") as f:
            meta = yaml.safe_load(f) or {}

        specs.append(
            ModuleSpec(
                id=module_name,
                name=meta.get("name", module_name.capitalize()),
                type=meta.get("type", DEFAULT_MODULE_TYPE),
                icon=meta.get("icon"),
                path=module_path,
            )
        )

    # Stable sort: within the same type, modules keep the alphabetical
    # order they were discovered in above.
    specs.sort(key=lambda spec: _startup_priority(spec.type))
    return specs
