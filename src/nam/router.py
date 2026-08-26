from __future__ import annotations

import os
from dataclasses import dataclass

from nam.build import BuildConfig

DEFAULT_SCHEME = "http"
DEFAULT_ROUTER_HOST = "127.0.0.1"


@dataclass
class Router:
    """
    nam's "sharding" answer to "where do I find module X": a build's
    modules aren't necessarily all in one process, so nothing gets to
    hardcode another module's URL. A module id is either in this
    build's own "include" list, meaning it's mounted right here and
    resolves to this process's own host:port, or it's in "reference",
    meaning some OTHER process owns it and its address instead comes
    from the named environment variable (e.g. reference: {corpus:
    URL_WEBSERVER} means os.environ["URL_WEBSERVER"] holds corpus's
    address). A module id absent from both is a configuration error -
    surfaced immediately by get_hostname() rather than deferred into a
    confusing connection failure later.
    """

    own_host: str
    own_port: int
    included_modules: frozenset[str]
    reference: dict[str, str]
    scheme: str = DEFAULT_SCHEME

    def get_hostname(self, module_id: str) -> str:
        if module_id in self.included_modules:
            return f"{self.scheme}://{self.own_host}:{self.own_port}"

        if module_id in self.reference:
            env_var = self.reference[module_id]
            address = os.environ.get(env_var)
            if not address:
                raise RuntimeError(
                    f"Module '{module_id}' is referenced via env var '{env_var}', "
                    f"but that environment variable is not set."
                )
            return address if "://" in address else f"{self.scheme}://{address}"

        raise ValueError(
            f"Module '{module_id}' is neither included in this build nor listed "
            f"under its 'reference' section - nam has no way to locate it."
        )


def build_router(build: BuildConfig, host: str, port: int) -> Router:
    router_host = DEFAULT_ROUTER_HOST if host in ("0.0.0.0", "") else host
    return Router(
        own_host=router_host,
        own_port=port,
        included_modules=frozenset(build.include),
        reference=build.reference,
    )


_active_router: Router | None = None


def set_active_router(router: Router) -> None:
    """Called once by create_app() so module code running outside any
    request (background jobs, boot-time requeues, module-level import
    side effects) can still reach this process's own Router via
    get_active_router() below, the same way in-request code reaches it
    off app.state.router."""
    global _active_router
    _active_router = router


def get_active_router() -> Router:
    if _active_router is None:
        raise RuntimeError(
            "No active Router - set_active_router() must run (via create_app()) "
            "before any code calls get_active_router()."
        )
    return _active_router
