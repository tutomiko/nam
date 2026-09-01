import os

import pytest

from nam.project import Project, _resolve_parallellism, load_project, sync_data_directory


def _write_yaml(path, text):
    path.write_text(text)


def test_load_project_with_no_project_yaml_uses_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr("nam.project.get_app_data_dir", lambda name: tmp_path / "appdata" / name)

    project = load_project(tmp_path)

    assert project.config == {}
    assert project.environment_name == tmp_path.name
    assert project.name == tmp_path.name
    assert project.optimization == "throughput"
    assert project.reload is True
    assert project.port == 8000
    assert project.parallellism is None


def test_load_project_reads_all_keys_from_project_yaml(tmp_path, monkeypatch):
    monkeypatch.setattr("nam.project.get_app_data_dir", lambda name: tmp_path / "appdata" / name)
    _write_yaml(tmp_path / "project.yaml", """
environment: my-env
name: My Cool Project
parallellism: 4
optimization: balance
reload: false
port: 9001
""")

    project = load_project(tmp_path)

    assert project.environment_name == "my-env"
    assert project.name == "My Cool Project"
    assert project.parallellism == 4
    assert project.optimization == "balance"
    assert project.reload is False
    assert project.port == 9001


def test_load_project_name_defaults_to_environment_name_when_name_key_absent(tmp_path, monkeypatch):
    monkeypatch.setattr("nam.project.get_app_data_dir", lambda name: tmp_path / "appdata" / name)
    _write_yaml(tmp_path / "project.yaml", "environment: my-env\n")

    project = load_project(tmp_path)

    assert project.name == "my-env"


def test_load_project_name_defaults_to_folder_name_when_neither_key_present(tmp_path, monkeypatch):
    monkeypatch.setattr("nam.project.get_app_data_dir", lambda name: tmp_path / "appdata" / name)
    _write_yaml(tmp_path / "project.yaml", "optimization: throughput\n")

    project = load_project(tmp_path)

    assert project.name == tmp_path.name
    assert project.environment_name == tmp_path.name


def test_load_project_name_key_present_but_blank_falls_back_to_environment_name(tmp_path, monkeypatch):
    """An empty string is falsy, so `name: ""` should behave like the key
    was never set at all, not like the project is titled ''."""
    monkeypatch.setattr("nam.project.get_app_data_dir", lambda name: tmp_path / "appdata" / name)
    _write_yaml(tmp_path / "project.yaml", "environment: my-env\nname: \"\"\n")

    project = load_project(tmp_path)

    assert project.name == "my-env"


def test_load_project_with_empty_yaml_file_uses_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr("nam.project.get_app_data_dir", lambda name: tmp_path / "appdata" / name)
    _write_yaml(tmp_path / "project.yaml", "")

    project = load_project(tmp_path)

    assert project.config == {}
    assert project.name == tmp_path.name


def test_load_project_sets_assets_dir_alongside_weights_and_bundles(tmp_path, monkeypatch):
    monkeypatch.setattr("nam.project.get_app_data_dir", lambda name: tmp_path / "appdata" / name)

    project = load_project(tmp_path)

    assert project.assets_dir == tmp_path / "assets"
    assert project.weights_dir == tmp_path / "weights"
    assert project.bundles_dir == tmp_path / "bundles"
    # load_project only resolves the path - it must NOT create the
    # directory itself. That's server.py's job at app-creation time, so
    # a project can be introspected (e.g. by a CLI "list builds" command)
    # without side effects on disk.
    assert not project.assets_dir.exists()


def test_load_project_port_can_be_overridden_as_string(tmp_path, monkeypatch):
    monkeypatch.setattr("nam.project.get_app_data_dir", lambda name: tmp_path / "appdata" / name)
    _write_yaml(tmp_path / "project.yaml", "port: \"9090\"\n")

    project = load_project(tmp_path)

    assert project.port == 9090
    assert isinstance(project.port, int)


def test_load_project_raises_on_non_integer_port(tmp_path, monkeypatch):
    monkeypatch.setattr("nam.project.get_app_data_dir", lambda name: tmp_path / "appdata" / name)
    _write_yaml(tmp_path / "project.yaml", "port: not-a-port\n")

    with pytest.raises(ValueError):
        load_project(tmp_path)


def test_load_project_with_malformed_yaml_raises(tmp_path, monkeypatch):
    monkeypatch.setattr("nam.project.get_app_data_dir", lambda name: tmp_path / "appdata" / name)
    _write_yaml(tmp_path / "project.yaml", "environment: [unterminated\n")

    with pytest.raises(Exception):
        load_project(tmp_path)


def test_load_project_resolves_relative_project_path_to_absolute(tmp_path, monkeypatch):
    monkeypatch.setattr("nam.project.get_app_data_dir", lambda name: tmp_path / "appdata" / name)
    monkeypatch.chdir(tmp_path)

    project = load_project(".")

    assert project.root.is_absolute()
    assert project.root == tmp_path.resolve()


def test_resolve_parallellism_none_means_auto():
    assert _resolve_parallellism(None) is None


def test_resolve_parallellism_auto_string_means_auto():
    assert _resolve_parallellism("auto") is None


def test_resolve_parallellism_explicit_int_is_kept():
    assert _resolve_parallellism(8) == 8


def test_resolve_parallellism_numeric_string_is_coerced_to_int():
    assert _resolve_parallellism("4") == 4


def test_resolve_parallellism_non_numeric_string_raises():
    with pytest.raises(ValueError):
        _resolve_parallellism("throughput")


def test_activate_project_chdirs_to_app_data_dir(tmp_path, monkeypatch):
    from nam.project import activate_project

    app_data_dir = tmp_path / "appdata"
    app_data_dir.mkdir()
    project = Project(
        root=tmp_path,
        config={},
        environment_name="env",
        name="env",
        parallellism=None,
        optimization="throughput",
        reload=True,
        port=8000,
        app_data_dir=app_data_dir,
        modules_dir=tmp_path / "modules",
        shared_dir=tmp_path / "shared",
        weights_dir=tmp_path / "weights",
        bundles_dir=tmp_path / "bundles",
        assets_dir=tmp_path / "assets",
        data_dir=tmp_path / "data",
        env_data_dir=app_data_dir / "data",
    )
    original_cwd = os.getcwd()

    try:
        activate_project(project)
        assert os.getcwd() == str(app_data_dir.resolve())
    finally:
        os.chdir(original_cwd)


def test_sync_data_directory_copies_new_files(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    (src / "a.txt").write_text("hello")

    sync_data_directory(src, dst)

    assert (dst / "a.txt").read_text() == "hello"


def test_sync_data_directory_removes_files_deleted_from_source(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (dst / "stale.txt").write_text("old")

    sync_data_directory(src, dst)

    assert not (dst / "stale.txt").exists()


def test_sync_data_directory_overwrites_changed_files(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "a.txt").write_text("new content")
    (dst / "a.txt").write_text("old content")

    sync_data_directory(src, dst)

    assert (dst / "a.txt").read_text() == "new content"


def test_sync_data_directory_leaves_unchanged_files_alone(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "a.txt").write_text("same")
    (dst / "a.txt").write_text("same")
    dst_file = dst / "a.txt"
    original_mtime = dst_file.stat().st_mtime

    sync_data_directory(src, dst)

    assert dst_file.stat().st_mtime == original_mtime


def test_sync_data_directory_replaces_file_with_dir_when_source_became_a_dir(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "item").mkdir()
    (src / "item" / "nested.txt").write_text("x")
    (dst / "item").write_text("was a file")

    sync_data_directory(src, dst)

    assert (dst / "item").is_dir()
    assert (dst / "item" / "nested.txt").read_text() == "x"


def test_sync_data_directory_replaces_dir_with_file_when_source_became_a_file(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "item").write_text("now a file")
    (dst / "item").mkdir()
    (dst / "item" / "nested.txt").write_text("stale")

    sync_data_directory(src, dst)

    assert (dst / "item").is_file()
    assert (dst / "item").read_text() == "now a file"


def test_sync_data_directory_recurses_into_nested_dirs(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    (src / "sub").mkdir(parents=True)
    dst.mkdir()
    (src / "sub" / "deep.txt").write_text("deep")

    sync_data_directory(src, dst)

    assert (dst / "sub" / "deep.txt").read_text() == "deep"
