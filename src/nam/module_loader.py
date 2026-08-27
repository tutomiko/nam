from __future__ import annotations

import hashlib
import subprocess
import sys
import threading
import time
from pathlib import Path

from nam.module import ModuleSpec

ENTRY_FILENAMES = ["App.jsx", "app.jsx", "index.jsx", "index.js", "App.js"]
POLL_INTERVAL_SECONDS = 0.75


def _log(msg: str):
    ts = time.strftime("%H:%M:%S")
    print(f"[Bundler {ts}] {msg}", flush=True)


def find_entry_point(frontend_dir: Path) -> Path | None:
    for filename in ENTRY_FILENAMES:
        candidate = frontend_dir / filename
        if candidate.exists():
            return candidate
    return None


def bundle_out_path(bundles_dir: Path, module: ModuleSpec) -> Path:
    return bundles_dir / f"{module.id}.js"


def bundle_hash_path(bundles_dir: Path, module: ModuleSpec) -> Path:
    return bundles_dir / f"{module.id}.hash"


def _build_command(entry_point: Path, out_file: Path, shared_dir: Path) -> str:
    return (
        f"npx --yes esbuild \"{entry_point}\" --bundle --outfile=\"{out_file}\" "
        f"--loader:.js=jsx --alias:@shared=\"{shared_dir}\""
    )


HASH_EXCLUDED_DIR_NAMES = {"node_modules", ".git", "__pycache__", ".cache"}
FRONTEND_SOURCE_EXTENSIONS = {".js", ".jsx", ".ts", ".tsx", ".css", ".html", ".json"}


def _hash_dir_into(digest, root_dir: Path, label: str):
    for source_file in sorted(p for p in root_dir.rglob("*") if p.is_file()):
        relative_parts = source_file.relative_to(root_dir).parts
        if HASH_EXCLUDED_DIR_NAMES.intersection(relative_parts):
            continue
        if source_file.suffix not in FRONTEND_SOURCE_EXTENSIONS:
            continue
        digest.update(label.encode())
        digest.update(str(source_file.relative_to(root_dir)).encode())
        digest.update(source_file.read_bytes())


def hash_frontend_sources(frontend_dir: Path, shared_dir: Path | None = None) -> str:
    digest = hashlib.sha256()
    _hash_dir_into(digest, frontend_dir, "frontend")
    if shared_dir is not None and shared_dir.exists():
        _hash_dir_into(digest, shared_dir, "shared")
    return digest.hexdigest()


def _read_recorded_hash(hash_file: Path) -> str | None:
    if not hash_file.exists():
        return None
    return hash_file.read_text().strip()


def _write_recorded_hash(hash_file: Path, source_hash: str):
    hash_file.write_text(source_hash)


def run_build(entry_point: Path, out_file: Path, shared_dir: Path, cwd: Path, module_name: str) -> bool:
    cmd = _build_command(entry_point, out_file, shared_dir)
    result = subprocess.run(cmd, shell=True, cwd=cwd, stdout=sys.stdout, stderr=sys.stderr)
    build_ok = result.returncode == 0
    if not build_ok:
        _log(f"WARNING: build for '{module_name}' FAILED (exit {result.returncode}). "
             f"Bundle may be stale or missing until the next successful rebuild.")
    return build_ok


def build_if_changed(entry_point: Path, out_file: Path, hash_file: Path, shared_dir: Path, cwd: Path, frontend_dir: Path, module_name: str) -> str:
    current_hash = hash_frontend_sources(frontend_dir, shared_dir)
    recorded_hash = _read_recorded_hash(hash_file)

    if out_file.exists() and current_hash == recorded_hash:
        return current_hash

    _log(f"Building '{module_name}' (sources changed)...")
    if run_build(entry_point, out_file, shared_dir, cwd, module_name):
        _log(f"Build for '{module_name}' OK -> {out_file}")
        _write_recorded_hash(hash_file, current_hash)

    return current_hash


class _WatchedModule:
    def __init__(self, module: ModuleSpec, entry_point: Path, out_file: Path, hash_file: Path, source_hash: str):
        self.module = module
        self.entry_point = entry_point
        self.out_file = out_file
        self.hash_file = hash_file
        self.source_hash = source_hash


def supervise_watch(watched_modules: list[_WatchedModule], shared_dir: Path, cwd: Path, stop_event: threading.Event):
    while not stop_event.wait(POLL_INTERVAL_SECONDS):
        for watched in watched_modules:
            current_hash = hash_frontend_sources(watched.module.frontend_dir, shared_dir)
            if current_hash == watched.source_hash:
                continue

            _log(f"Change detected for '{watched.module.id}', rebuilding...")
            if run_build(watched.entry_point, watched.out_file, shared_dir, cwd, watched.module.id):
                _log(f"Rebuild for '{watched.module.id}' OK -> {watched.out_file}")
                _write_recorded_hash(watched.hash_file, current_hash)
                watched.source_hash = current_hash


def start_frontend_watchers(modules: list[ModuleSpec], bundles_dir: Path, shared_dir: Path, cwd: Path):
    bundles_dir.mkdir(parents=True, exist_ok=True)

    watched_modules = []

    for module in modules:
        entry_point = find_entry_point(module.frontend_dir)

        if entry_point:
            out_file = bundle_out_path(bundles_dir, module)
            hash_file = bundle_hash_path(bundles_dir, module)
            source_hash = build_if_changed(entry_point, out_file, hash_file, shared_dir, cwd, module.frontend_dir, module.id)
            watched_modules.append(_WatchedModule(module, entry_point, out_file, hash_file, source_hash))
        elif module.frontend_dir.exists():
            _log(f"WARNING: No entry point found in {module.frontend_dir}. "
                 f"Expected one of: {', '.join(ENTRY_FILENAMES)}")

    stop_event = threading.Event()
    t = threading.Thread(
        target=supervise_watch,
        args=(watched_modules, shared_dir, cwd, stop_event),
        daemon=True,
    )
    t.start()

    return [t], stop_event


def build_all_once(modules: list[ModuleSpec], bundles_dir: Path, shared_dir: Path, cwd: Path):
    bundles_dir.mkdir(parents=True, exist_ok=True)

    for module in modules:
        entry_point = find_entry_point(module.frontend_dir)

        if entry_point:
            out_file = bundle_out_path(bundles_dir, module)
            hash_file = bundle_hash_path(bundles_dir, module)
            build_if_changed(entry_point, out_file, hash_file, shared_dir, cwd, module.frontend_dir, module.id)
        elif module.frontend_dir.exists():
            _log(f"WARNING: No entry point found in {module.frontend_dir}. "
                 f"Expected one of: {', '.join(ENTRY_FILENAMES)}")


def rebuild_stale_bundles(modules: list[ModuleSpec], bundles_dir: Path, shared_dir: Path, cwd: Path):
    for module in modules:
        if not module.frontend_dir.exists():
            continue

        entry_point = find_entry_point(module.frontend_dir)
        if entry_point is None:
            continue

        out_file = bundle_out_path(bundles_dir, module)
        hash_file = bundle_hash_path(bundles_dir, module)
        build_if_changed(entry_point, out_file, hash_file, shared_dir, cwd, module.frontend_dir, module.id)
