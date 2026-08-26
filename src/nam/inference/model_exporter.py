from __future__ import annotations

import copy
import importlib.util
import logging
import pathlib
import queue as _queue
from typing import Any, Callable, Optional

import numpy as np
import torch

from nam import concurrency

logger = logging.getLogger("nam.inference.model_exporter")

_COMPILED_CACHE: dict = {}
_REQUEST_POOL_CACHE: dict = {}


class ExportedModel:
    def __init__(self, pool: "_queue.Queue"):
        self._pool = pool

    def infer(self, batch: np.ndarray) -> list:
        handle = self._pool.get()
        try:
            return handle.infer(batch)
        finally:
            self._pool.put(handle)


def export_pytorch_module_to_onnx(
    module: "torch.nn.Module",
    onnx_path: pathlib.Path,
    dummy_input: "torch.Tensor",
    input_names: list,
    output_names: list,
    dynamic_axes: Optional[dict] = None,
    opset_version: int = 18,
) -> bool:
    cpu_module = copy.deepcopy(module).to("cpu").eval()
    cpu_dummy_input = dummy_input.detach().to("cpu")
    try:
        torch.onnx.export(
            cpu_module,
            cpu_dummy_input,
            str(onnx_path),
            input_names=input_names,
            output_names=output_names,
            opset_version=opset_version,
            dynamic_axes=dynamic_axes or {},
            dynamo=False,
        )
        return onnx_path.exists()
    except Exception as e:
        detail = str(e).strip() or repr(e)
        cause = getattr(e, "__cause__", None)
        if cause is not None:
            cause_detail = str(cause).strip() or repr(cause)
            detail = f"{detail} | caused by: {cause_detail}"
        logger.warning(f"[nam.inference.model_exporter] ONNX export to {onnx_path} failed ({type(e).__name__}): {detail}")
        return False


def export_and_compile(
    onnx_path: pathlib.Path,
    input_name: Any,
    models_dir: pathlib.Path,
    input_shape: Optional[list] = None,
    precision: str = "f16",
) -> Optional[ExportedModel]:
    try:
        pool = _get_or_build_request_pool(
            onnx_path=onnx_path,
            input_name=input_name,
            models_dir=models_dir,
            input_shape=input_shape,
            precision=precision,
        )
        return ExportedModel(pool)
    except Exception as e:
        logger.warning(f"[nam.inference.model_exporter] Failed to compile {onnx_path.name}: {e}")
        return None


def make_pool(base_handle: Any, copy_fn: Optional[Callable] = None) -> "_queue.Queue":
    copy_fn = copy_fn or copy.copy
    pool_size = concurrency.get_parallellism()
    pool: "_queue.Queue" = _queue.Queue()
    pool.put(base_handle)
    for _ in range(max(1, pool_size - 1)):
        pool.put(copy_fn(base_handle))
    return pool


# ---------------------------------------------------------------------------
# Private: the accelerated backend (OpenVINO) lives only below this line.
# ---------------------------------------------------------------------------

class _AcceleratedHandle:
    def __init__(self, compiled: Any, input_name: Any, request: Any):
        self._compiled = compiled
        self._input_name = input_name
        self._request = request

    def infer(self, batch: np.ndarray) -> list:
        req = self._request
        expected_dtype = req.get_input_tensor(0).data.dtype
        if batch.dtype != expected_dtype:
            batch = batch.astype(expected_dtype)

        req.infer({self._input_name: batch})
        return [
            np.array(req.get_output_tensor(i).data, copy=True)
            for i in range(len(self._compiled.outputs))
        ]


def _openvino_installed() -> bool:
    return importlib.util.find_spec("openvino") is not None


def _resolve_accelerator_device(core: Any) -> str:
    available = core.available_devices
    for preferred in ("GPU", "NPU", "CPU"):
        if preferred in available:
            return preferred
    return available[0] if available else "CPU"


def _get_or_build_request_pool(
    onnx_path: pathlib.Path,
    input_name: Any,
    models_dir: pathlib.Path,
    input_shape: Optional[list],
    precision: str,
) -> "_queue.Queue":
    if not _openvino_installed():
        raise RuntimeError("openvino is not installed")

    import openvino as ov

    core = ov.Core()
    core.set_property({"CACHE_DIR": str(models_dir)})

    accel_device = _resolve_accelerator_device(core)

    config = {"PERFORMANCE_HINT": "THROUGHPUT"}
    if precision:
        config["INFERENCE_PRECISION_HINT"] = precision

    config_key = tuple(sorted(config.items()))
    cache_key = (str(onnx_path), accel_device, tuple(input_shape) if input_shape else None, config_key)

    if cache_key not in _COMPILED_CACHE:
        logger.info(f"[nam.inference.model_exporter] Compiling {onnx_path.name} (shape={input_shape}, config={config})...")
        graph = core.read_model(str(onnx_path))
        if input_shape:
            graph.reshape({input_name: input_shape})
        compiled = core.compile_model(graph, accel_device, config=config)
        _COMPILED_CACHE[cache_key] = compiled

        try:
            logger.info(f"[nam.inference.model_exporter] Warming up {onnx_path.name}...")
            warmup_req = compiled.create_infer_request()
            if input_shape:
                expected_dtype = warmup_req.get_input_tensor(0).data.dtype
                dummy_input = np.zeros(input_shape, dtype=expected_dtype)
                warmup_req.infer({input_name: dummy_input})
            logger.info(f"[nam.inference.model_exporter] Warmup for {onnx_path.name} completed.")
        except Exception as warmup_e:
            logger.warning(f"[nam.inference.model_exporter] Warmup for {onnx_path.name} failed: {warmup_e}")
    else:
        logger.info(f"[nam.inference.model_exporter] Loaded {onnx_path.name} from cache.")

    compiled = _COMPILED_CACHE[cache_key]

    if cache_key not in _REQUEST_POOL_CACHE:
        num_requests = concurrency.get_parallellism()
        handle_pool: "_queue.Queue" = _queue.Queue()
        for _ in range(max(1, num_requests)):
            request = compiled.create_infer_request()
            handle_pool.put(_AcceleratedHandle(compiled, input_name, request))
        _REQUEST_POOL_CACHE[cache_key] = handle_pool

    return _REQUEST_POOL_CACHE[cache_key]
