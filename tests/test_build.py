import pytest

from nam.build import load_build


def test_load_build_raises_when_builds_yaml_missing(tmp_path):
    with pytest.raises(ValueError):
        load_build(tmp_path, "webserver")


def test_load_build_raises_when_build_name_not_in_builds_yaml(tmp_path):
    (tmp_path / "builds.yaml").write_text("worker:\n  include:\n    - a\n")

    with pytest.raises(ValueError, match="webserver"):
        load_build(tmp_path, "webserver")


def test_load_build_error_lists_available_build_names(tmp_path):
    (tmp_path / "builds.yaml").write_text("worker:\n  include: [a]\nwebserver:\n  include: [b]\n")

    with pytest.raises(ValueError, match="webserver, worker"):
        load_build(tmp_path, "missing")


def test_load_build_reads_include_and_reference(tmp_path):
    (tmp_path / "builds.yaml").write_text("""
webserver:
  optimization: balance
  include:
    - annotator
    - corpus
  reference:
    classifier: URL_WORKER
""")

    build = load_build(tmp_path, "webserver")

    assert build.name == "webserver"
    assert build.optimization == "balance"
    assert build.include == ["annotator", "corpus"]
    assert build.reference == {"classifier": "URL_WORKER"}


def test_load_build_with_empty_build_entry_uses_empty_defaults(tmp_path):
    (tmp_path / "builds.yaml").write_text("worker:\n")

    build = load_build(tmp_path, "worker")

    assert build.optimization is None
    assert build.include == []
    assert build.reference == {}


def test_load_build_with_missing_include_key_defaults_to_empty_list(tmp_path):
    (tmp_path / "builds.yaml").write_text("worker:\n  optimization: throughput\n")

    build = load_build(tmp_path, "worker")

    assert build.include == []


def test_load_build_with_missing_reference_key_defaults_to_empty_dict(tmp_path):
    (tmp_path / "builds.yaml").write_text("worker:\n  include: [a]\n")

    build = load_build(tmp_path, "worker")

    assert build.reference == {}
