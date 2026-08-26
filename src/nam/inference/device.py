from __future__ import annotations

import logging
from typing import Any, Optional

import torch

from .backend import Backend, CudaBackend, DirectMLBackend

logger = logging.getLogger("nam.inference.device")

_TENSOR_TO_PATCHED = False
_RESOLVED_CUDA_SUBSTITUTE: Optional["torch.device"] = None

CUDA_BACKEND = CudaBackend()
DIRECTML_BACKEND = DirectMLBackend()

_BACKENDS: tuple = (CUDA_BACKEND, DIRECTML_BACKEND)


def backend_for(torch_device: "torch.device") -> Optional[Backend]:
    for backend in _BACKENDS:
        if backend.targets_this_backend(torch_device):
            return backend
    return None


class Device:
    def __init__(self, torch_device: "torch.device", backend: str):
        self.torch_device = torch_device
        self.backend = backend

    def __repr__(self) -> str:
        return f"Device(backend={self.backend!r}, torch_device={self.torch_device})"

    @property
    def is_gpu(self) -> bool:
        return self.backend in ("cuda", "directml")

    @staticmethod
    def auto() -> "Device":
        if CUDA_BACKEND.is_available():
            logger.info("[nam.inference.device] Using CUDA device.")
            return Device(CUDA_BACKEND.resolve_device(), "cuda")

        torch_device = _resolve_cuda_substitute()
        if torch_device.type == "cpu":
            logger.info("[nam.inference.device] No GPU backend available, using CPU.")
            return Device(torch_device, "cpu")

        logger.info("[nam.inference.device] Using torch-directml device.")
        return Device(torch_device, "directml")

    @staticmethod
    def cpu() -> "Device":
        return Device(torch.device("cpu"), "cpu")

    @staticmethod
    def requested(spec: Any) -> "Device":
        """
        Resolve a device the way a user explicitly asked for it via
        `.to(device)`, honoring that request rather than always deferring
        to auto():

        - "cpu": explicit opt-out. Always real CPU, no DirectML
          substitution.
        - "xpu": treated as "give me a GPU", substituted with DirectML
          (the practical non-CUDA GPU path).
        - "cuda": honored if available. If CUDA isn't actually available,
          falls back to auto()'s cascade with a visible warning rather
          than raising.
        - anything else: passed through to torch.device() as-is, not
          eligible for DirectML substitution.
        """
        device_type = _device_type_of(spec)

        if device_type == "cpu":
            return Device(torch.device("cpu"), "cpu")

        if device_type == "xpu":
            torch_device = _resolve_cuda_substitute()
            if torch_device.type != "cpu":
                logger.info("[nam.inference.device] xpu requested, using torch-directml device.")
                return Device(torch_device, "directml")
            logger.warning(
                "[nam.inference.device] xpu requested but no DirectML backend available; falling back to CPU."
            )
            return Device(torch.device("cpu"), "cpu")

        if device_type == "cuda":
            if CUDA_BACKEND.is_available():
                return Device(CUDA_BACKEND.resolve_device(), "cuda")
            logger.warning(
                "[nam.inference.device] cuda requested but not available; falling back to auto()."
            )
            return Device.auto()

        return Device(torch.device(spec), device_type)


def _device_type_of(spec: Any) -> str:
    if isinstance(spec, torch.device):
        return spec.type
    if isinstance(spec, torch.Tensor):
        return spec.device.type
    return torch.device(spec).type


def _directml_device_lock():
    return DIRECTML_BACKEND.lock()


def _targets_directml(target_device: Any) -> bool:
    return DIRECTML_BACKEND.targets_this_backend(target_device)


def invalidate_directml_device() -> None:
    """
    Clears the process-wide resolved DirectML device cache. The next call to
    _resolve_cuda_substitute() will request a fresh device context from the
    backend, which is required to recover from a TDR (device removal).
    """
    global _RESOLVED_CUDA_SUBSTITUTE
    _RESOLVED_CUDA_SUBSTITUTE = None
    DIRECTML_BACKEND.invalidate()
    _resolve_cuda_substitute()


def _resolve_cuda_substitute() -> "torch.device":
    """
    Process-wide answer to "what does 'cuda' actually mean here, given
    this machine doesn't have real CUDA": computed once via the DirectML
    backend, then reused for every unpatched call site (torch.Tensor.to,
    torch.from_numpy(...).to, etc.) so a module that only ever says
    "cuda"/"cpu" and has no DirectML awareness still ends up with every
    tensor and every parameter on the same real device.
    """
    global _RESOLVED_CUDA_SUBSTITUTE
    if _RESOLVED_CUDA_SUBSTITUTE is None:
        _RESOLVED_CUDA_SUBSTITUTE = DIRECTML_BACKEND.resolve_device() if DIRECTML_BACKEND.is_available() else torch.device("cpu")
    return _RESOLVED_CUDA_SUBSTITUTE


def patch_tensor_to() -> None:
    """
    Patches torch.Tensor.to so that any code asking for "cuda" (as a
    string, a torch.device, or a dtype-carrying device kwarg) when CUDA
    isn't actually available gets silently redirected to the same
    resolved substitute device (DirectML or CPU) that Device.auto() and
    Device.requested() already use for modules - so a plain
    tensor.to(device="cuda") call in module code that never mentions
    DirectML still ends up consistent with wherever that module's
    parameters actually live.
    """
    global _TENSOR_TO_PATCHED
    if _TENSOR_TO_PATCHED:
        return
    _TENSOR_TO_PATCHED = True

    original_to = torch.Tensor.to

    def redirected_to(self, *args, **kwargs):
        new_args = list(args)
        if new_args and _is_cuda_request(new_args[0]) and not CUDA_BACKEND.is_available():
            new_args[0] = _resolve_cuda_substitute()
        elif "device" in kwargs and _is_cuda_request(kwargs["device"]) and not CUDA_BACKEND.is_available():
            kwargs["device"] = _resolve_cuda_substitute()

        target_device = new_args[0] if new_args else kwargs.get("device")
        backend = backend_for(target_device) if target_device is not None else None
        if backend is not None:
            remaining_args = new_args[1:]
            return backend.move_tensor(self, target_device, original_to, *remaining_args, **kwargs)

        return original_to(self, *new_args, **kwargs)

    torch.Tensor.to = redirected_to


def _is_cuda_request(spec: Any) -> bool:
    if spec is None:
        return False
    if isinstance(spec, (torch.dtype, int, float, bool)):
        return False
    try:
        return _device_type_of(spec) == "cuda"
    except (TypeError, RuntimeError):
        return False
