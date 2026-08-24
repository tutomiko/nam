from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

from nam.module import ModuleSpec

ENTRY_FILENAMES = ["App.jsx", "app.jsx", "index.jsx", "index.js", "App.js"]


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


def _build_command(entry_point: Path, out_file: Path, shared_dir: Path, watch: bool) -> str:
    watch_flag = "--watch " if watch else ""
    return (
        f"npx --yes esbuild \"{entry_point}\" --bundle --outfile=\"{out_file}\" "
        f"{watch_flag}--loader:.js=jsx --alias:@shared=\"{shared_dir}\""
    )


def run_forced_build(entry_point: Path, out_file: Path, shared_dir: Path, cwd: Path, module_name: str):
    cmd = _build_command(entry_point, out_file, shared_dir, watch=False)
    _log(f"Forcing initial build for '{module_name}'...")
    result = subprocess.run(cmd, shell=True, cwd=cwd, stdout=sys.stdout, stderr=sys.stderr)
    if result.returncode != 0:
        _log(f"WARNING: initial forced build for '{module_name}' FAILED (exit {result.returncode}). "
             f"Bundle may be stale or missing until the next successful rebuild.")
    else:
        _log(f"Initial build for '{module_name}' OK -> {out_file}")


def rebuild_if_stale(entry_point: Path, out_file: Path, shared_dir: Path, cwd: Path, frontend_dir: Path, module_name: str):
    newest_source_mtime = max(
        (p.stat().st_mtime for p in frontend_dir.rglob("*") if p.is_file()),
        default=0,
    )
    bundle_is_stale = not out_file.exists() or out_file.stat().st_mtime < newest_source_mtime
    if not bundle_is_stale:
        return

    cmd = _build_command(entry_point, out_file, shared_dir, watch=False)
    _log(f"Rebuilding stale bundle for '{module_name}'...")
    result = subprocess.run(cmd, shell=True, cwd=cwd, stdout=sys.stdout, stderr=sys.stderr)
    if result.returncode != 0:
        _log(f"WARNING: rebuild for '{module_name}' FAILED (exit {result.returncode}).")


def supervise_watch(entry_point: Path, out_file: Path, shared_dir: Path, cwd: Path, module_name: str, stop_event: threading.Event):
    cmd = _build_command(entry_point, out_file, shared_dir, watch=True)
    backoff = 1
    while not stop_event.is_set():
        _log(f"Starting watch process for '{module_name}'...")
        proc = subprocess.Popen(cmd, shell=True, cwd=cwd, stdout=sys.stdout, stderr=sys.stderr, stdin=subprocess.PIPE)

        while proc.poll() is None:
            if stop_event.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                return
            time.sleep(0.5)

        if stop_event.is_set():
            return

        exit_code = proc.returncode
        _log(f"WARNING: watch process for '{module_name}' exited unexpectedly "
             f"(code {exit_code}). Restarting in {backoff}s...")
        time.sleep(backoff)
        backoff = min(backoff * 2, 30)


def start_frontend_watchers(modules: list[ModuleSpec], bundles_dir: Path, shared_dir: Path, cwd: Path):
    bundles_dir.mkdir(parents=True, exist_ok=True)

    stop_event = threading.Event()
    threads = []

    for module in modules:
        entry_point = find_entry_point(module.frontend_dir)

        if entry_point:
            out_file = bundle_out_path(bundles_dir, module)

            run_forced_build(entry_point, out_file, shared_dir, cwd, module.id)

            t = threading.Thread(
                target=supervise_watch,
                args=(entry_point, out_file, shared_dir, cwd, module.id, stop_event),
                daemon=True,
            )
            t.start()
            threads.append(t)
        elif module.frontend_dir.exists():
            _log(f"WARNING: No entry point found in {module.frontend_dir}. "
                 f"Expected one of: {', '.join(ENTRY_FILENAMES)}")

    return threads, stop_event


def rebuild_stale_bundles(modules: list[ModuleSpec], bundles_dir: Path, shared_dir: Path, cwd: Path):
    for module in modules:
        if not module.frontend_dir.exists():
            continue

        entry_point = find_entry_point(module.frontend_dir)
        if entry_point is None:
            continue

        out_file = bundle_out_path(bundles_dir, module)
        rebuild_if_stale(entry_point, out_file, shared_dir, cwd, module.frontend_dir, module.id)
