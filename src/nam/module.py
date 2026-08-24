from __future__ import annotations

import yaml
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_MODULE_TYPE = "site"


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

    return specs
