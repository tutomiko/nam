from __future__ import annotations

import yaml
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class BuildConfig:
    name: str
    optimization: str | None
    include: list[str] = field(default_factory=list)
    reference: dict[str, str] = field(default_factory=dict)


def _load_builds_config(root: Path) -> dict:
    builds_path = root / "builds.yaml"
    if not builds_path.exists():
        return {}
    with open(builds_path, "r") as f:
        return yaml.safe_load(f) or {}


def load_build(root: Path, build_name: str) -> BuildConfig:
    """
    A project's builds.yaml shards a single nam project into multiple
    deployable processes ("builds") - e.g. a webserver build, a worker
    build, a trainer build - each launched from the same project
    directory via `nam <project_dir> -build <name>`. A build's "include"
    list is the only set of modules this process actually starts;
    everything else the modules under "include" import module-to-module
    (e.g. corpus_client hitting the corpus module) is instead resolved
    at runtime through Router.get_hostname(), using the "reference" map
    below to say which environment variable holds each such module's
    address when it isn't running in this same process.
    """
    builds = _load_builds_config(root)

    if build_name not in builds:
        available = ", ".join(sorted(builds)) or "(none defined)"
        raise ValueError(f"No build '{build_name}' in {root / 'builds.yaml'}. Available builds: {available}")

    raw = builds[build_name] or {}

    return BuildConfig(
        name=build_name,
        optimization=raw.get("optimization"),
        include=list(raw.get("include") or []),
        reference=dict(raw.get("reference") or {}),
    )
