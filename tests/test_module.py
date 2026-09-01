from nam.module import ModuleSpec, discover_module_specs


def _make_module(modules_dir, module_id, yaml_text="name: Test\n", with_routes=True):
    module_dir = modules_dir / module_id
    (module_dir / "backend").mkdir(parents=True)
    (module_dir / "module.yaml").write_text(yaml_text)
    if with_routes:
        (module_dir / "backend" / "routes.py").write_text("router = None\n")


def test_discover_module_specs_on_missing_modules_dir_returns_empty(tmp_path):
    assert discover_module_specs(tmp_path / "does_not_exist") == []


def test_discover_module_specs_on_empty_dir_returns_empty(tmp_path):
    modules_dir = tmp_path / "modules"
    modules_dir.mkdir()

    assert discover_module_specs(modules_dir) == []


def test_discover_module_specs_skips_dir_missing_module_yaml(tmp_path):
    modules_dir = tmp_path / "modules"
    module_dir = modules_dir / "broken"
    (module_dir / "backend").mkdir(parents=True)
    (module_dir / "backend" / "routes.py").write_text("router = None\n")

    assert discover_module_specs(modules_dir) == []


def test_discover_module_specs_skips_dir_missing_routes_py(tmp_path):
    modules_dir = tmp_path / "modules"
    module_dir = modules_dir / "broken"
    module_dir.mkdir(parents=True)
    (module_dir / "module.yaml").write_text("name: Broken\n")

    assert discover_module_specs(modules_dir) == []


def test_discover_module_specs_ignores_non_directory_entries(tmp_path):
    modules_dir = tmp_path / "modules"
    modules_dir.mkdir()
    (modules_dir / "stray_file.txt").write_text("not a module")

    assert discover_module_specs(modules_dir) == []


def test_discover_module_specs_defaults_name_to_capitalized_id(tmp_path):
    modules_dir = tmp_path / "modules"
    _make_module(modules_dir, "trainer", yaml_text="type: site\n")

    specs = discover_module_specs(modules_dir)

    assert len(specs) == 1
    assert specs[0].id == "trainer"
    assert specs[0].name == "Trainer"


def test_discover_module_specs_uses_explicit_name_when_given(tmp_path):
    modules_dir = tmp_path / "modules"
    _make_module(modules_dir, "trainer", yaml_text="name: My Trainer\n")

    specs = discover_module_specs(modules_dir)

    assert specs[0].name == "My Trainer"


def test_discover_module_specs_defaults_type_to_site(tmp_path):
    modules_dir = tmp_path / "modules"
    _make_module(modules_dir, "mod", yaml_text="name: Mod\n")

    specs = discover_module_specs(modules_dir)

    assert specs[0].type == "site"


def test_discover_module_specs_with_empty_module_yaml_still_loads_with_defaults(tmp_path):
    modules_dir = tmp_path / "modules"
    _make_module(modules_dir, "mod", yaml_text="")

    specs = discover_module_specs(modules_dir)

    assert len(specs) == 1
    assert specs[0].name == "Mod"
    assert specs[0].type == "site"
    assert specs[0].icon is None


def test_discover_module_specs_orders_services_before_sites_before_tools(tmp_path):
    modules_dir = tmp_path / "modules"
    _make_module(modules_dir, "zz_tool", yaml_text="type: tool\n")
    _make_module(modules_dir, "aa_site", yaml_text="type: site\n")
    _make_module(modules_dir, "mm_service", yaml_text="type: service\n")

    specs = discover_module_specs(modules_dir)

    assert [s.id for s in specs] == ["mm_service", "aa_site", "zz_tool"]


def test_discover_module_specs_unknown_type_sorts_after_tools(tmp_path):
    modules_dir = tmp_path / "modules"
    _make_module(modules_dir, "weird", yaml_text="type: something_else\n")
    _make_module(modules_dir, "a_tool", yaml_text="type: tool\n")

    specs = discover_module_specs(modules_dir)

    assert [s.id for s in specs] == ["a_tool", "weird"]


def test_discover_module_specs_stable_sort_preserves_alphabetical_order_within_same_type(tmp_path):
    modules_dir = tmp_path / "modules"
    _make_module(modules_dir, "b_site", yaml_text="type: site\n")
    _make_module(modules_dir, "a_site", yaml_text="type: site\n")

    specs = discover_module_specs(modules_dir)

    assert [s.id for s in specs] == ["a_site", "b_site"]


def test_module_spec_to_nav_entry_omits_icon_when_absent():
    spec = ModuleSpec(id="m", name="M", type="site", icon=None, path=None)

    entry = spec.to_nav_entry()

    assert entry == {"id": "m", "name": "M", "type": "site"}
    assert "icon" not in entry


def test_module_spec_to_nav_entry_includes_icon_when_present():
    spec = ModuleSpec(id="m", name="M", type="site", icon="cpu", path=None)

    entry = spec.to_nav_entry()

    assert entry["icon"] == "cpu"


def test_module_spec_is_site_true_for_site_type():
    spec = ModuleSpec(id="m", name="M", type="site", icon=None, path=None)
    assert spec.is_site is True


def test_module_spec_is_site_false_for_service_type():
    spec = ModuleSpec(id="m", name="M", type="service", icon=None, path=None)
    assert spec.is_site is False


def test_module_spec_backend_routes_path_and_frontend_dir(tmp_path):
    spec = ModuleSpec(id="m", name="M", type="site", icon=None, path=tmp_path)

    assert spec.backend_routes_path == tmp_path / "backend" / "routes.py"
    assert spec.frontend_dir == tmp_path / "frontend"
