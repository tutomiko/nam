import pytest

from nam.build import BuildConfig
from nam.router import DEFAULT_ROUTER_HOST, Router, build_router


def test_get_hostname_returns_own_address_for_included_module():
    router = Router(own_host="127.0.0.1", own_port=8000, included_modules=frozenset({"trainer"}), reference={})

    assert router.get_hostname("trainer") == "http://127.0.0.1:8000"


def test_get_hostname_reads_referenced_module_from_env_var(monkeypatch):
    monkeypatch.setenv("URL_WORKER", "10.0.0.5:9000")
    router = Router(own_host="127.0.0.1", own_port=8000, included_modules=frozenset(), reference={"trainer": "URL_WORKER"})

    assert router.get_hostname("trainer") == "http://10.0.0.5:9000"


def test_get_hostname_preserves_scheme_already_in_env_var(monkeypatch):
    monkeypatch.setenv("URL_WORKER", "https://10.0.0.5:9000")
    router = Router(own_host="127.0.0.1", own_port=8000, included_modules=frozenset(), reference={"trainer": "URL_WORKER"})

    assert router.get_hostname("trainer") == "https://10.0.0.5:9000"


def test_get_hostname_raises_when_referenced_env_var_is_unset():
    router = Router(own_host="127.0.0.1", own_port=8000, included_modules=frozenset(), reference={"trainer": "URL_MISSING"})

    with pytest.raises(RuntimeError):
        router.get_hostname("trainer")


def test_get_hostname_raises_when_referenced_env_var_is_empty_string(monkeypatch):
    monkeypatch.setenv("URL_WORKER", "")
    router = Router(own_host="127.0.0.1", own_port=8000, included_modules=frozenset(), reference={"trainer": "URL_WORKER"})

    with pytest.raises(RuntimeError):
        router.get_hostname("trainer")


def test_get_hostname_raises_for_module_neither_included_nor_referenced():
    router = Router(own_host="127.0.0.1", own_port=8000, included_modules=frozenset({"trainer"}), reference={})

    with pytest.raises(ValueError):
        router.get_hostname("unknown_module")


def test_get_hostname_included_takes_priority_over_reference():
    """A module listed in both include and reference (a builds.yaml
    authoring mistake) should resolve locally rather than silently
    reaching out over the network - included wins."""
    router = Router(
        own_host="127.0.0.1", own_port=8000,
        included_modules=frozenset({"trainer"}),
        reference={"trainer": "URL_WORKER"},
    )

    assert router.get_hostname("trainer") == "http://127.0.0.1:8000"


def test_build_router_rewrites_wildcard_host_to_loopback():
    build = BuildConfig(name="b", optimization=None, include=["a"], reference={})

    router = build_router(build, host="0.0.0.0", port=8000)

    assert router.own_host == DEFAULT_ROUTER_HOST


def test_build_router_rewrites_empty_host_to_loopback():
    build = BuildConfig(name="b", optimization=None, include=["a"], reference={})

    router = build_router(build, host="", port=8000)

    assert router.own_host == DEFAULT_ROUTER_HOST


def test_build_router_preserves_explicit_host():
    build = BuildConfig(name="b", optimization=None, include=["a"], reference={})

    router = build_router(build, host="192.168.1.5", port=8000)

    assert router.own_host == "192.168.1.5"


def test_build_router_converts_include_list_to_frozenset():
    build = BuildConfig(name="b", optimization=None, include=["a", "b", "a"], reference={})

    router = build_router(build, host="127.0.0.1", port=8000)

    assert router.included_modules == frozenset({"a", "b"})


def test_get_active_router_raises_before_any_router_is_set():
    import nam.router as router_module
    router_module._active_router = None

    with pytest.raises(RuntimeError):
        router_module.get_active_router()


def test_set_active_router_then_get_active_router_round_trips():
    import nam.router as router_module

    router = Router(own_host="127.0.0.1", own_port=8000, included_modules=frozenset(), reference={})
    router_module.set_active_router(router)

    assert router_module.get_active_router() is router

    router_module._active_router = None
