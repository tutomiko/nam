from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse
from starlette.middleware.gzip import GZipMiddleware

from nam import concurrency
from nam import module_loader
from nam.build import BuildConfig, load_build
from nam.inference import hook as inference_hook
from nam.module import discover_module_specs
from nam.project import Project, activate_project
from nam.project_loader import APP_MOUNT_PREFIX, load_modules_into_app
from nam.router import Router, build_router, set_active_router

APP_HTML_PATH = Path(__file__).parent / "app.html"

# Compresses response bodies over GZIP_MIN_SIZE_BYTES - critical for routes
# that serve large nested JSON payloads (e.g. embeddings grids, depth
# maps), where GZip's text-compression ratio on repetitive float-string
# JSON is typically 5-10x, trading a small amount of CPU time on both ends
# for a proportionally much larger cut in wire transfer time.
GZIP_MIN_SIZE_BYTES = 500


def _serve_app_html():
    return FileResponse(str(APP_HTML_PATH))


def _resolve_router(project: Project) -> Router:
    """
    Every build gets a Router, even an un-sharded, no -build-flag launch:
    in that case every discovered module is implicitly "included" (the
    whole project runs in this one process, same as before builds.yaml
    existed), so Router.get_hostname() still answers correctly for
    module-to-module calls without those calls needing to know whether
    they're running inside a shard or a monolith.
    """
    if project.build_name is not None:
        build = load_build(project.root, project.build_name)
    else:
        all_module_ids = [spec.id for spec in discover_module_specs(project.modules_dir)]
        build = BuildConfig(name="__monolith__", optimization=None, include=all_module_ids, reference={})

    return build_router(build, host="0.0.0.0", port=project.port)


def create_app(project: Project) -> FastAPI:
    activate_project(project)

    concurrency.initialize(project.parallellism)

    project.weights_dir.mkdir(parents=True, exist_ok=True)
    project.bundles_dir.mkdir(parents=True, exist_ok=True)

    inference_hook.install(project)

    router = _resolve_router(project)
    set_active_router(router)

    active_modules = discover_module_specs(project.modules_dir)
    if project.build_name is not None:
        active_modules = [spec for spec in active_modules if spec.id in router.included_modules]

    module_loader.rebuild_stale_bundles(
        modules=active_modules,
        bundles_dir=project.bundles_dir,
        shared_dir=project.shared_dir,
        cwd=project.root,
    )

    app = FastAPI(title="nam")
    app.add_middleware(GZipMiddleware, minimum_size=GZIP_MIN_SIZE_BYTES)

    @app.middleware("http")
    async def no_cache_bundles(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/bundles/"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        elif request.url.path.startswith("/weights/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    app.mount("/weights", StaticFiles(directory=str(project.weights_dir)), name="weights")
    app.mount("/bundles", StaticFiles(directory=str(project.bundles_dir)), name="bundles")

    app.state.app_html_path = APP_HTML_PATH
    app.state.router = router

    included_module_ids = router.included_modules if project.build_name is not None else None
    loaded = load_modules_into_app(app, project, _serve_app_html, included_module_ids=included_module_ids)
    app.state.mounted_modules = loaded.mounted
    app.state.project = project

    @app.get("/")
    def read_root():
        """
        Bare "/" has no module in the path, so app.html's own script
        (which only loads a bundle when the URL is already /app/<id>)
        would otherwise render nothing. Redirect to the first mounted
        "site" module's page instead (a "service" module like a worker
        has no frontend to land on, even though it's still mounted) -
        the first mounted module overall only if no site is mounted, so
        landing on the raw address:port still goes somewhere rather than
        a blank shell. Server-side, not client-side, so it works before
        any JS runs and shows up correctly in server logs / curl -I.
        """
        reachable_modules = [
            m for m in loaded.mounted
            if project.build_name is None or m["id"] in included_module_ids
        ]
        landing_module = next(
            (m for m in reachable_modules if m["type"] == "site"),
            reachable_modules[0] if reachable_modules else None,
        )
        if landing_module is not None:
            return RedirectResponse(url=f"{APP_MOUNT_PREFIX}/{landing_module['id']}")
        return FileResponse(str(APP_HTML_PATH))

    return app
