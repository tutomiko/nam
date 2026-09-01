import pytest

from nam.project_loader import RouteConflictError, _check_no_route_conflicts


def test_no_conflict_between_distinct_paths():
    owners = {}
    _check_no_route_conflicts(owners, "module_a", [("GET", "/ping")])
    _check_no_route_conflicts(owners, "module_b", [("GET", "/pong")])

    assert owners == {("GET", "/ping"): "module_a", ("GET", "/pong"): "module_b"}


def test_no_conflict_for_same_path_different_methods():
    owners = {}
    _check_no_route_conflicts(owners, "module_a", [("GET", "/models"), ("POST", "/models")])

    assert owners[("GET", "/models")] == "module_a"
    assert owners[("POST", "/models")] == "module_a"


def test_conflict_raised_for_exact_same_method_and_path_across_two_modules():
    owners = {}
    _check_no_route_conflicts(owners, "module_a", [("GET", "/tokenizers")])

    with pytest.raises(RouteConflictError):
        _check_no_route_conflicts(owners, "module_b", [("GET", "/tokenizers")])


def test_conflict_error_message_names_both_modules():
    owners = {}
    _check_no_route_conflicts(owners, "resource_manager", [("GET", "/tokenizers")])

    with pytest.raises(RouteConflictError) as excinfo:
        _check_no_route_conflicts(owners, "trainer", [("GET", "/tokenizers")])

    message = str(excinfo.value)
    assert "resource_manager" in message
    assert "trainer" in message


def test_conflict_raised_when_a_single_module_redeclares_its_own_path_twice():
    """Two @router.get("/x") on the same routes.py is also a conflict -
    not just two different modules colliding."""
    owners = {}

    with pytest.raises(RouteConflictError):
        _check_no_route_conflicts(owners, "module_a", [("GET", "/x"), ("GET", "/x")])


def test_conflict_leaves_first_owner_registered_after_raising():
    """A caught conflict shouldn't clobber the existing owner - if
    startup continues (e.g. under a try/except at a higher layer) the
    original registration must still be intact."""
    owners = {}
    _check_no_route_conflicts(owners, "module_a", [("GET", "/x")])

    try:
        _check_no_route_conflicts(owners, "module_b", [("GET", "/x")])
    except RouteConflictError:
        pass

    assert owners[("GET", "/x")] == "module_a"


def test_no_conflict_for_same_literal_path_segment_different_param_names():
    """KNOWN GAP: the checker keys on the raw (method, path) string, so
    /models/{model_name}/weights and /models/{model_id}/weights are
    treated as different paths even though FastAPI/Starlette route on
    segment SHAPE, not param name, and would actually collide at
    runtime (whichever registers first silently shadows the other).
    This test documents current behavior, not desired behavior - it
    should start failing (and get flipped to pytest.raises) once the
    checker is updated to normalize {param} segments before comparing."""
    owners = {}
    _check_no_route_conflicts(owners, "modeler", [("GET", "/models/{model_name}/weights")])
    _check_no_route_conflicts(owners, "trainer", [("GET", "/models/{model_id}/weights")])

    assert owners[("GET", "/models/{model_name}/weights")] == "modeler"
    assert owners[("GET", "/models/{model_id}/weights")] == "trainer"


def test_no_conflict_between_literal_path_and_similarly_shaped_param_path():
    """/models/search (literal) vs /models/{model_name} (2-segment param)
    are genuinely different route shapes even under proper normalization,
    so this one should never conflict, unlike the case above."""
    owners = {}
    _check_no_route_conflicts(owners, "modeler", [("GET", "/models/search")])
    _check_no_route_conflicts(owners, "modeler", [("GET", "/models/{model_name}")])

    assert len(owners) == 2
