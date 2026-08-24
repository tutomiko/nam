from __future__ import annotations

import platform
from pathlib import Path


def get_app_data_dir(app_name: str) -> Path:
    home = Path.home()

    if platform.system() == "Windows":
        base_path = home / "AppData" / "Local"
    else:
        base_path = home / ".local" / "share"

    app_data_path = base_path / app_name
    app_data_path.mkdir(parents=True, exist_ok=True)
    return app_data_path
