from __future__ import annotations

import argparse
import atexit
import os
import sys

import uvicorn

from nam.build import load_build
from nam.module import discover_module_specs
from nam.module_loader import build_all_once, start_frontend_watchers
from nam.project import load_project

_PROJECT_PATH_ENV_VAR = "NAM_PROJECT_PATH"
_BUILD_NAME_ENV_VAR = "NAM_BUILD_NAME"
_PORT_ENV_VAR = "NAM_PORT"

_watcher_threads = []
_watchers_stop_event = None


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="nam", description="Not Another Monolith")
    parser.add_argument("project_path", help="Path to the project directory containing project.yaml")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=None,
                         help="Overrides the 'port' key in project.yaml (default 8000) if given.")
    parser.add_argument("-build", "--build", dest="build_name", default=None,
                         help="Name of a build in project/builds.yaml. When given, only that "
                              "build's 'include' modules are launched here - modules under "
                              "'reference' are resolved via Router.get_hostname() from the "
                              "referenced environment variable instead of being started locally.")
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
    project = load_project(args.project_path, build_name=args.build_name)
    port = args.port if args.port is not None else project.port
    project.port = port

    build = load_build(project.root, args.build_name) if args.build_name else None
    if build is not None and build.optimization is not None:
        project.optimization = build.optimization

    # uvicorn's reloader runs the app factory in a freshly spawned child
    # process, which does not inherit this process's Python globals - so
    # the project path (and, if given, the build name) is handed down
    # through the environment instead, and _create_app_for_reload()
    # re-resolves the Project/BuildConfig from it.
    os.environ[_PROJECT_PATH_ENV_VAR] = str(project.root)
    os.environ[_PORT_ENV_VAR] = str(port)
    if args.build_name:
        os.environ[_BUILD_NAME_ENV_VAR] = args.build_name
    else:
        os.environ.pop(_BUILD_NAME_ENV_VAR, None)

    modules = discover_module_specs(project.modules_dir)
    if build is not None:
        modules = [module for module in modules if module.id in build.include]

    if project.reload:
        _watcher_threads, _watchers_stop_event = start_frontend_watchers(
            modules=modules,
            bundles_dir=project.bundles_dir,
            shared_dir=project.shared_dir,
            cwd=project.root,
        )
        atexit.register(_cleanup_watchers)
    else:
        build_all_once(
            modules=modules,
            bundles_dir=project.bundles_dir,
            shared_dir=project.shared_dir,
            cwd=project.root,
        )

    uvicorn.run(
        "nam.launcher:_create_app_for_reload",
        host=args.host,
        port=port,
        reload=project.reload,
        factory=True,
        reload_dirs=[str(project.root)],
        reload_includes=["*.py"],
        reload_excludes=["*/frontend/*"],
    )


def _create_app_for_reload():
    from nam.server import create_app
    build_name = os.environ.get(_BUILD_NAME_ENV_VAR) or None
    project = load_project(os.environ[_PROJECT_PATH_ENV_VAR], build_name=build_name)
    project.port = int(os.environ[_PORT_ENV_VAR])
    return create_app(project)


if __name__ == "__main__":
    main(sys.argv[1:])
