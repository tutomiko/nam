from __future__ import annotations

import contextlib
from typing import Any, Callable, Optional

import torch


class Backend:
    """
    Generic per-accelerator wrapper. A backend answers three questions for
    nam's device-routing hook: what torch.device does this backend run on,
    how do tensors/modules get moved onto it safely, and how should a call
    into that device be serialized and error-reported.

    Concrete backends (DirectMLBackend, CudaBackend, ...) override the
    pieces that actually differ; everything else here is a safe no-op
    default so a backend with no special quirks (e.g. plain CUDA) can be
    a thin subclass.
    """

    name: str = "base"

    def is_available(self) -> bool:
        raise NotImplementedError

    def resolve_device(self) -> "torch.device":
        raise NotImplementedError

    def targets_this_backend(self, torch_device: "torch.device") -> bool:
        raise NotImplementedError

    @contextlib.contextmanager
    def lock(self):
        """
        Serialization scope around a device-bound operation. Backends that
        aren't safe for concurrent submission from multiple threads (e.g.
        DirectML) override this with a real lock; backends that are fine
        with concurrent access (e.g. CUDA, which has its own stream
        semantics) can leave this as a no-op.
        """
        yield

    def move_module(self, module: "torch.nn.Module", torch_device: "torch.device", original_to: Callable, *args, **kwargs) -> "torch.nn.Module":
        return original_to(module, torch_device, *args, **kwargs)

    def move_tensor(self, tensor: "torch.Tensor", torch_device: "torch.device", original_to: Callable, *args, **kwargs) -> "torch.Tensor":
        return original_to(tensor, torch_device, *args, **kwargs)

    def wrap_call_error(self, module: "torch.nn.Module", inputs: tuple, target_device: "torch.device", error: Exception) -> Exception:
        """
        Given an error raised while running `module` on `target_device`
        with `inputs`, return the exception nam's hook should raise
        instead. Default: pass the original error through unchanged.
        """
        return error

    def memory_report(self, target_device: "torch.device") -> str:
        return "<memory report unavailable for this backend>"

    def patch_process_wide_gaps(self) -> None:
        """
        One-time, process-wide monkeypatches this backend needs to paper
        over kernel/op gaps in its underlying driver (e.g. DirectML's
        missing SDPA kernel). Called once, guarded by the backend
        registry - not per-call. Default: nothing to patch.
        """
        return None
