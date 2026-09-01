from nam.route_discovery import DiscoveredRoute, discover_routes


def _write_routes(tmp_path, source):
    path = tmp_path / "routes.py"
    path.write_text(source)
    return path


def test_discover_routes_on_missing_file_returns_empty(tmp_path):
    assert discover_routes(tmp_path / "does_not_exist.py") == []


def test_discover_routes_on_empty_file_returns_empty(tmp_path):
    path = _write_routes(tmp_path, "")
    assert discover_routes(path) == []


def test_discover_routes_finds_simple_get(tmp_path):
    path = _write_routes(tmp_path, """
router = None

@router.get("/ping")
def ping():
    return "ok"
""")

    routes = discover_routes(path)

    assert routes == [DiscoveredRoute(method="GET", path="/ping")]


def test_discover_routes_finds_multiple_methods(tmp_path):
    path = _write_routes(tmp_path, """
router = None

@router.get("/models")
def list_models():
    pass

@router.post("/models")
def create_model():
    pass

@router.delete("/models/{model_id}")
def delete_model(model_id: str):
    pass
""")

    routes = discover_routes(path)

    assert routes == [
        DiscoveredRoute(method="GET", path="/models"),
        DiscoveredRoute(method="POST", path="/models"),
        DiscoveredRoute(method="DELETE", path="/models/{model_id}"),
    ]


def test_discover_routes_preserves_declaration_order(tmp_path):
    path = _write_routes(tmp_path, """
router = None

@router.get("/models/search")
def search():
    pass

@router.get("/models/{model_name}")
def get_model(model_name: str):
    pass
""")

    routes = discover_routes(path)

    assert [r.path for r in routes] == ["/models/search", "/models/{model_name}"]


def test_discover_routes_finds_async_handlers(tmp_path):
    path = _write_routes(tmp_path, """
router = None

@router.get("/async-thing")
async def get_thing():
    pass
""")

    routes = discover_routes(path)

    assert routes == [DiscoveredRoute(method="GET", path="/async-thing")]


def test_discover_routes_uppercases_method(tmp_path):
    path = _write_routes(tmp_path, """
router = None

@router.get("/x")
def x():
    pass
""")

    routes = discover_routes(path)

    assert routes[0].method == "GET"


def test_discover_routes_ignores_non_http_method_decorators(tmp_path):
    path = _write_routes(tmp_path, """
router = None

def some_decorator(fn):
    return fn

@some_decorator
@router.get("/real")
def real_route():
    pass
""")

    routes = discover_routes(path)

    assert routes == [DiscoveredRoute(method="GET", path="/real")]


def test_discover_routes_ignores_decorators_on_a_different_object(tmp_path):
    """Only decorators on the actual `router_name` (default "router")
    object should count - a differently-named FastAPI router or
    unrelated decorator with a .get()/.post() method must not be picked
    up as a nam-owned route."""
    path = _write_routes(tmp_path, """
router = None
other_router = None

@other_router.get("/not-mine")
def not_mine():
    pass

@router.get("/mine")
def mine():
    pass
""")

    routes = discover_routes(path)

    assert routes == [DiscoveredRoute(method="GET", path="/mine")]


def test_discover_routes_respects_custom_router_name(tmp_path):
    path = _write_routes(tmp_path, """
api = None

@api.get("/custom")
def custom():
    pass
""")

    routes = discover_routes(path, router_name="api")

    assert routes == [DiscoveredRoute(method="GET", path="/custom")]


def test_discover_routes_ignores_decorator_call_with_no_args(tmp_path):
    path = _write_routes(tmp_path, """
router = None

@router.get()
def broken():
    pass
""")

    routes = discover_routes(path)

    assert routes == []


def test_discover_routes_ignores_non_string_literal_path(tmp_path):
    """A path built from a variable/f-string/constant expression can't be
    statically known, so it must be skipped rather than crash discovery -
    this matters because excluded-module proxying depends on this scan
    succeeding even for routes.py files this process will never import."""
    path = _write_routes(tmp_path, """
router = None
PATH = "/dynamic"

@router.get(PATH)
def dynamic():
    pass
""")

    routes = discover_routes(path)

    assert routes == []


def test_discover_routes_ignores_bare_decorator_without_call(tmp_path):
    path = _write_routes(tmp_path, """
router = None

@router.get
def broken():
    pass
""")

    routes = discover_routes(path)

    assert routes == []


def test_discover_routes_ignores_method_names_not_in_http_methods(tmp_path):
    path = _write_routes(tmp_path, """
router = None

@router.include_router("/not-http")
def weird():
    pass
""")

    routes = discover_routes(path)

    assert routes == []


def test_discover_routes_finds_routes_defined_inside_a_function(tmp_path):
    """ast.walk descends into nested scopes, so a route declared inside a
    conditional or factory function is still discovered - this documents
    that behavior rather than assuming only module-level defs count."""
    path = _write_routes(tmp_path, """
router = None

def register():
    @router.get("/nested")
    def nested():
        pass
""")

    routes = discover_routes(path)

    assert routes == [DiscoveredRoute(method="GET", path="/nested")]
