from __future__ import annotations

import logging
import threading
import weakref
from pathlib import Path
from typing import Any, Optional

import torch
import torch.nn as nn

from nam.project import Project

from .device import Device, patch_tensor_to, backend_for

logger = logging.getLogger("nam.inference.hook")

_HOOK_INSTALLED = False
_ORIGINAL_EVAL = None
_ORIGINAL_CALL = None
_ORIGINAL_TO = None

_STATES: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_STATES_LOCK = threading.Lock()


class _ModuleState:
    def __init__(self):
        self.device_model = None
        self.requested_device = None


def _state_for(module: "nn.Module") -> _ModuleState:
    with _STATES_LOCK:
        state = _STATES.get(module)
        if state is None:
            state = _ModuleState()
            _STATES[module] = state
        return state


def _build_device_model(module: "nn.Module", state: _ModuleState) -> None:
    if state.device_model is None:
        device = state.requested_device if state.requested_device is not None else Device.auto()
        backend = backend_for(device.torch_device)
        module_device = backend.move_module(module, device.torch_device, _ORIGINAL_TO) if backend is not None \
            else _ORIGINAL_TO(module, device.torch_device)
        _ORIGINAL_EVAL(module_device)
        state.device_model = device


def _run_on_device(module: "nn.Module", state: _ModuleState, inputs: tuple, kwargs: dict) -> Any:
    target_device = state.device_model.torch_device
    backend = backend_for(target_device)

    if backend is not None:
        with backend.lock():
            moved = [t.to(target_device) if isinstance(t, torch.Tensor) else t for t in inputs]
            with torch.no_grad():
                try:
                    return _ORIGINAL_CALL(module, *moved, **kwargs)
                except RuntimeError as e:
                    raise backend.wrap_call_error(module, moved, target_device, e) from e

    moved = [t.to(target_device) if isinstance(t, torch.Tensor) else t for t in inputs]
    with torch.no_grad():
        return _ORIGINAL_CALL(module, *moved, **kwargs)


def _patched_to(self, *args, **kwargs):
    device_spec = args[0] if args else kwargs.get("device")
    if device_spec is None:
        return _ORIGINAL_TO(self, *args, **kwargs)

    try:
        resolved = Device.requested(device_spec)
    except (TypeError, RuntimeError):
        return _ORIGINAL_TO(self, *args, **kwargs)

    state = _state_for(self)
    state.requested_device = resolved
    state.device_model = None

    remaining_args = args[1:] if args else ()
    remaining_kwargs = {k: v for k, v in kwargs.items() if k != "device"}
    backend = backend_for(resolved.torch_device)
    if backend is not None:
        return backend.move_module(self, resolved.torch_device, _ORIGINAL_TO, *remaining_args, **remaining_kwargs)
    return _ORIGINAL_TO(self, resolved.torch_device, *remaining_args, **remaining_kwargs)


def _patched_eval(self, *args, **kwargs):
    result = _ORIGINAL_EVAL(self, *args, **kwargs)
    state = _state_for(self)
    _build_device_model(self, state)
    return result


def _patched_call(self, *inputs, **kwargs):
    state = _state_for(self)
    _build_device_model(self, state)
    return _run_on_device(self, state, inputs, kwargs)


def install(project: Project) -> None:
    global _HOOK_INSTALLED, _ORIGINAL_EVAL, _ORIGINAL_CALL, _ORIGINAL_TO

    if _HOOK_INSTALLED:
        return
    _HOOK_INSTALLED = True

    _ORIGINAL_EVAL = nn.Module.eval
    _ORIGINAL_CALL = nn.Module.__call__
    _ORIGINAL_TO = nn.Module.to

    nn.Module.eval = _patched_eval
    nn.Module.__call__ = _patched_call
    nn.Module.to = _patched_to

    patch_tensor_to()

    logger.info("[nam.inference.hook] Installed.")
