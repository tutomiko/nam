from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse
from starlette.middleware.gzip import GZipMiddleware

from nam import concurrency
from nam import module_loader
from nam.module import discover_module_specs
from nam.project import Project, activate_project
from nam.project_loader import load_modules_into_app

APP_HTML_PATH = Path(__file__).parent / "app.html"

# Compresses response bodies over GZIP_MIN_SIZE_BYTES - critical for routes
# that serve large nested JSON payloads (e.g. embeddings grids, depth
# maps), where GZip's text-compression ratio on repetitive float-string
# JSON is typically 5-10x, trading a small amount of CPU time on both ends
# for a proportionally much larger cut in wire transfer time.
GZIP_MIN_SIZE_BYTES = 500


def _serve_app_html():
    return FileResponse(str(APP_HTML_PATH))


def create_app(project: Project) -> FastAPI:
    activate_project(project)

    concurrency.initialize(project.parallellism)

    project.weights_dir.mkdir(parents=True, exist_ok=True)
    project.bundles_dir.mkdir(parents=True, exist_ok=True)

    module_loader.rebuild_stale_bundles(
        modules=discover_module_specs(project.modules_dir),
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

    loaded = load_modules_into_app(app, project, _serve_app_html)
    app.state.mounted_modules = loaded.mounted
    app.state.nav_modules = loaded.nav
    app.state.project = project

    @app.get("/")
    def read_root(request: Request):
        sites = request.app.state.nav_modules
        if sites:
            return RedirectResponse(url=f"/{sites[0]['id']}")
        return FileResponse(str(APP_HTML_PATH))

    @app.get("/api/navigation")
    def get_navigation(request: Request):
        return request.app.state.nav_modules

    return app
