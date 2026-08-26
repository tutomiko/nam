from __future__ import annotations

import torch

from ._base import Backend


class CudaBackend(Backend):
    """
    torch already knows how to do everything CUDA-specific natively -
    device placement, streams, memory management - so this backend has
    nothing to add beyond identifying itself and answering availability.
    No lock override: CUDA's own stream/context handling is safe for
    concurrent submission from multiple threads without nam adding one.
    """

    name = "cuda"

    def is_available(self) -> bool:
        return torch.cuda.is_available()

    def resolve_device(self) -> "torch.device":
        return torch.device("cuda")

    def targets_this_backend(self, torch_device: "torch.device") -> bool:
        return getattr(torch_device, "type", None) == "cuda"

    def memory_report(self, target_device: "torch.device") -> str:
        try:
            idx = target_device.index if target_device.index is not None else 0
            allocated = torch.cuda.memory_allocated(idx)
            reserved = torch.cuda.memory_reserved(idx)
            return f"torch.cuda.memory_allocated(device={idx})={allocated}, memory_reserved={reserved}"
        except Exception as e:
            return f"<memory report unavailable: {e!r}>"
