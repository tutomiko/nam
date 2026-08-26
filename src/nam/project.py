from __future__ import annotations

import hashlib
import os
import shutil
import yaml
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from nam.environment import get_app_data_dir

DEFAULT_OPTIMIZATION = "throughput"
DEFAULT_PORT = 8000
PARALLELLISM_AUTO = "auto"


@dataclass
class Project:
    root: Path
    config: dict
    environment_name: str
    parallellism: Optional[int]
    optimization: str
    reload: bool
    port: int
    app_data_dir: Path
    modules_dir: Path
    shared_dir: Path
    weights_dir: Path
    bundles_dir: Path
    data_dir: Path
    env_data_dir: Path
    build_name: Optional[str] = None

    @property
    def config_path(self) -> Path:
        return self.root / "project.yaml"

    @property
    def global_routes_path(self) -> Path:
        return self.root / "app.py"

    @property
    def builds_path(self) -> Path:
        return self.root / "builds.yaml"


def _load_config(root: Path) -> dict:
    config_path = root / "project.yaml"
    if not config_path.exists():
        return {}
    with open(config_path, "r") as f:
        return yaml.safe_load(f) or {}


def _resolve_parallellism(raw) -> Optional[int]:
    """None means "let nam.concurrency.detect_cpu_count() decide" - the
    config's own 'auto' value, and also the default when the key is
    absent entirely. Any other value is taken as an explicit worker count."""
    if raw is None or raw == PARALLELLISM_AUTO:
        return None
    return int(raw)


def _file_hash(filepath: Path) -> str:
    hasher = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def sync_data_directory(src: Path, dst: Path):
    if not dst.exists():
        dst.mkdir(parents=True, exist_ok=True)

    for dst_item in dst.iterdir():
        src_item = src / dst_item.name

        if not src_item.exists():
            if dst_item.is_dir():
                shutil.rmtree(dst_item)
            else:
                dst_item.unlink()
        elif dst_item.is_dir() and src_item.is_dir():
            sync_data_directory(src_item, dst_item)
        elif dst_item.is_file() and src_item.is_dir():
            dst_item.unlink()
        elif dst_item.is_dir() and src_item.is_file():
            shutil.rmtree(dst_item)

    for src_item in src.iterdir():
        dst_item = dst / src_item.name

        if not dst_item.exists():
            if src_item.is_dir():
                shutil.copytree(src_item, dst_item)
            else:
                shutil.copy2(src_item, dst_item)
        elif src_item.is_file() and dst_item.is_file():
            if _file_hash(src_item) != _file_hash(dst_item):
                shutil.copy2(src_item, dst_item)


def load_project(project_path: str | Path, build_name: Optional[str] = None) -> Project:
    root = Path(project_path).resolve()
    config = _load_config(root)
    environment_name = config.get("environment") or root.name
    app_data_dir = get_app_data_dir(environment_name)

    data_dir = root / "data"
    env_data_dir = app_data_dir / "data"

    if data_dir.exists():
        sync_data_directory(data_dir, env_data_dir)
    else:
        env_data_dir.mkdir(parents=True, exist_ok=True)

    return Project(
        root=root,
        config=config,
        environment_name=environment_name,
        parallellism=_resolve_parallellism(config.get("parallellism")),
        optimization=config.get("optimization", DEFAULT_OPTIMIZATION),
        reload=bool(config.get("reload", True)),
        port=int(config.get("port", DEFAULT_PORT)),
        app_data_dir=app_data_dir,
        modules_dir=root / "modules",
        shared_dir=root / "shared",
        weights_dir=root / "weights",
        bundles_dir=root / "bundles",
        data_dir=data_dir,
        env_data_dir=env_data_dir,
        build_name=build_name,
    )


def activate_project(project: Project) -> None:
    """
    cwd is set to project.app_data_dir (the OS-level env root, e.g.
    ~/.local/share/LAMEStudio) - matching the original app's behavior of
    `os.chdir(ENV_DIR)`, not its data subfolder. project/data/ is synced
    into app_data_dir/data (see load_project's sync_data_directory call),
    so a module opening a relative path like "data/foo.txt" resolves it
    from there correctly. Everything else modules need (shared/src for
    imports, modules/ itself) is reached via sys.path in
    ensure_project_importable, which is absolute-path based and so is
    unaffected by cwd.
    """
    os.chdir(project.app_data_dir)
