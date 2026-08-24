from __future__ import annotations

import argparse
import atexit
import os
import sys

import uvicorn

from nam.module import discover_module_specs
from nam.module_loader import start_frontend_watchers
from nam.project import load_project

_PROJECT_PATH_ENV_VAR = "NAM_PROJECT_PATH"

_watcher_threads = []
_watchers_stop_event = None


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="nam", description="Not Another Monolith")
    parser.add_argument("project_path", help="Path to the project directory containing config.yaml")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    return parser.parse_args(argv)


def _cleanup_watchers():
    if _watchers_stop_event is None:
        return
    print("[Bundler] Shutting down frontend watchers...")
    _watchers_stop_event.set()
    for t in _watcher_threads:
        t.join(timeout=10)


def main(argv=None):
    global _watcher_threads, _watchers_stop_event

    args = _parse_args(argv)
    project = load_project(args.project_path)

    # uvicorn's reloader runs the app factory in a freshly spawned child
    # process, which does not inherit this process's Python globals - so
    # the project path is handed down through the environment instead,
    # and _create_app_for_reload() re-resolves the Project from it.
    os.environ[_PROJECT_PATH_ENV_VAR] = str(project.root)

    modules = discover_module_specs(project.modules_dir)
    _watcher_threads, _watchers_stop_event = start_frontend_watchers(
        modules=modules,
        bundles_dir=project.bundles_dir,
        shared_dir=project.shared_dir,
        cwd=project.root,
    )
    atexit.register(_cleanup_watchers)

    uvicorn.run(
        "nam.launcher:_create_app_for_reload",
        host=args.host,
        port=args.port,
        reload=project.reload,
        factory=True,
        reload_dirs=[str(project.root)],
        reload_includes=["*.py"],
        reload_excludes=["*/frontend/*"],
    )


def _create_app_for_reload():
    from nam.server import create_app
    project = load_project(os.environ[_PROJECT_PATH_ENV_VAR])
    return create_app(project)


if __name__ == "__main__":
    main(sys.argv[1:])
