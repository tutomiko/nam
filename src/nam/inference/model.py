from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import torch

from .device import Device


@dataclass
class Model:
    module: "torch.nn.Module"
    device: Device

    def __post_init__(self):
        self.module = self.module.to(self.device.torch_device)
        self.module.eval()

    def infer(self, *inputs: "torch.Tensor", **kwargs) -> Any:
        moved = [
            t.to(self.device.torch_device) if isinstance(t, torch.Tensor) else t
            for t in inputs
        ]
        with torch.no_grad():
            return self.module(*moved, **kwargs)

    def to_device(self, tensor: "torch.Tensor") -> "torch.Tensor":
        return tensor.to(self.device.torch_device)
