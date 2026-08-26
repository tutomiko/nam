from __future__ import annotations

import contextlib
import importlib.util
import logging
import threading
import time
from typing import Callable, Optional

import torch

from ._base import Backend

logger = logging.getLogger("nam.inference.backend.directml")

_DIRECTML_UNSUPPORTED_DTYPES = {
    torch.float64: torch.float32,
    torch.int64: torch.int32,
    torch.bool: torch.uint8,
}


class DirectMLBackend(Backend):
    """
    torch-directml is not safe for concurrent submission from multiple
    threads, has several unsupported dtypes/layouts, and its errors are
    frequently opaque ("unknown error", "resource deadlock would occur").
    This backend centralizes every DirectML-specific workaround nam
    applies so hook.py and device.py can stay backend-agnostic.
    """

    name = "directml"

    def __init__(self):
        self._device_lock: Optional[threading.RLock] = None
        self._resolved_device: Optional["torch.device"] = None

    def is_available(self) -> bool:
        return self._try_directml() is not None

    def resolve_device(self) -> "torch.device":
        if self._resolved_device is None:
            device = self._try_directml()
            if device is None:
                raise RuntimeError("[nam.inference.backend.directml] torch-directml is not available")
            self._resolved_device = device
        return self._resolved_device

    def invalidate(self) -> None:
        """
        Clears the resolved device cache. The next resolve_device() call
        requests a fresh device context from the backend, required to
        recover from a TDR (device removal).
        """
        self._resolved_device = None
        self.resolve_device()

    def targets_this_backend(self, torch_device: "torch.device") -> bool:
        return getattr(torch_device, "type", None) == "privateuseone"

    def lock(self):
        """
        torch-directml's device context is not safe for concurrent
        submission from multiple threads: nam's background threads (e.g.
        an export/probe thread) and the orchestrator's batch-processing
        threads can both move tensors/modules onto the DirectML device
        at the same moment, and the driver surfaces that race as an
        opaque "resource deadlock would occur" RuntimeError rather than
        serializing internally. Route every DirectML-bound device
        transfer through one process-wide lock so only one thread ever
        talks to the device at a time. Reentrant because nn.Module.to()
        recurses into per-parameter Tensor.to() calls on the same thread.
        """
        if self._device_lock is None:
            self._device_lock = threading.RLock()
        return self._device_lock

    def move_module(self, module: "torch.nn.Module", torch_device: "torch.device", original_to: Callable, *args, **kwargs) -> "torch.nn.Module":
        with self.lock():
            return original_to(module, torch_device, *args, **kwargs)

    def move_tensor(self, tensor: "torch.Tensor", torch_device: "torch.device", original_to: Callable, *args, **kwargs) -> "torch.Tensor":
        with self.lock():
            try:
                return original_to(tensor, torch_device, *args, **kwargs)
            except RuntimeError:
                return self._to_directml_via_cpu_roundtrip(tensor, torch_device, original_to)

    def wrap_call_error(self, module: "torch.nn.Module", inputs: tuple, target_device: "torch.device", error: Exception) -> Exception:
        shapes = [
            f"Tensor(shape={tuple(t.shape)}, dtype={t.dtype})" if isinstance(t, torch.Tensor) else repr(t)
            for t in inputs
        ]
        return RuntimeError(
            f"[nam.inference.backend.directml] DirectML call failed in {module.__class__.__name__}: "
            f"inputs={shapes}. {self.memory_report(target_device)}. Original error: {error}"
        )

    def memory_report(self, target_device: "torch.device") -> str:
        try:
            import torch_directml
            idx = target_device.index if target_device.index is not None else 0
            return f"torch_directml.gpu_memory(device={idx})={torch_directml.gpu_memory(idx)}"
        except Exception as e:
            return f"<gpu_memory unavailable: {e!r}>"

    def patch_process_wide_gaps(self) -> None:
        _patch_directml_gaps()

    # -- internals -----------------------------------------------------

    def _directml_installed(self) -> bool:
        return importlib.util.find_spec("torch_directml") is not None

    def _try_directml(self) -> Optional["torch.device"]:
        if not self._directml_installed():
            return None

        try:
            import torch_directml
        except Exception as e:
            logger.warning(f"[nam.inference.backend.directml] torch-directml is installed but failed to import: {e}")
            return None

        try:
            if not torch_directml.is_available():
                return None
        except Exception:
            pass

        try:
            device = torch_directml.device()
        except Exception as e:
            logger.warning(f"[nam.inference.backend.directml] torch-directml present but device() failed: {e}")
            return None

        self.patch_process_wide_gaps()
        return device

    def _to_directml_via_cpu_roundtrip(self, tensor: "torch.Tensor", target_device: "torch.device", original_to: Callable) -> "torch.Tensor":
        """
        Some tensor dtypes (float64, int64, bool) and non-contiguous
        layouts are not accepted directly by torch-directml's
        copy/allocate path and fail with an opaque "unknown error"
        instead of a clear dtype error. Route around it: bring the
        tensor to CPU and to a DirectML-friendly dtype first, then
        perform the device move as a second, simpler step.
        """
        cpu_tensor = tensor.detach().cpu().contiguous()

        substitute_dtype = _DIRECTML_UNSUPPORTED_DTYPES.get(cpu_tensor.dtype)
        if substitute_dtype is not None:
            logger.warning(
                f"[nam.inference.backend.directml] dtype {cpu_tensor.dtype} unsupported on DirectML; "
                f"using {substitute_dtype} instead for this tensor."
            )
            cpu_tensor = cpu_tensor.to(substitute_dtype)

        try:
            moved = original_to(cpu_tensor, target_device)
        except RuntimeError as first_error:
            moved = None
            for attempt in range(2):
                time.sleep(0.05 * (attempt + 1))
                try:
                    moved = original_to(cpu_tensor, target_device)
                    logger.warning(
                        f"[nam.inference.backend.directml] DirectML copy succeeded on retry {attempt + 1} "
                        f"for Tensor(shape={tuple(cpu_tensor.shape)}, dtype={cpu_tensor.dtype}) "
                        f"after an initial failure - likely a transient driver fault."
                    )
                    break
                except RuntimeError:
                    continue

            if moved is None:
                numel = cpu_tensor.numel()
                nbytes = cpu_tensor.element_size() * numel
                raise RuntimeError(
                    f"[nam.inference.backend.directml] DirectML copy failed even after dtype/contiguity "
                    f"roundtrip and retries: Tensor(shape={tuple(cpu_tensor.shape)}, dtype={cpu_tensor.dtype}, "
                    f"numel={numel}, approx_bytes={nbytes}, requires_grad={tensor.requires_grad}). "
                    f"This is likely a DirectML device-level fault (VRAM exhaustion or driver fault) "
                    f"rather than a dtype/layout issue. Original error: {first_error}"
                ) from first_error

        if tensor.requires_grad:
            moved.requires_grad_(True)
        return moved


def _patch_sdp_backends() -> None:
    """
    Force PyTorch to disable memory-efficient and flash attention backends globally,
    preventing DirectML's C++ kernel dispatcher from calling buggy MemEffAttention paths.
    """
    with contextlib.suppress(Exception):
        torch.backends.cuda.enable_mem_efficient_sdp(False)
        torch.backends.cuda.enable_flash_sdp(False)
        torch.backends.cuda.enable_math_sdp(True)


def patch_sdpa() -> None:
    """
    torch-directml has no fused scaled_dot_product_attention kernel; the
    call reaches DirectML's op dispatcher anyway and fails with an opaque
    "unknown error" or MemEffAttention crash. Replace SDPA with the standard
    unfused softmax(QK^T/sqrt(d) + mask)V computation, with device alignment
    and CPU fallbacks for heavy allocations.
    """
    import math
    import torch.nn.functional as F

    original_sdpa = F.scaled_dot_product_attention

    def manual_sdpa(query, key, value, attn_mask=None, dropout_p=0.0, is_causal=False, scale=None):
        target_device = query.device
        key = _align_to_device(key, target_device)
        value = _align_to_device(value, target_device)
        if attn_mask is not None:
            attn_mask = _align_to_device(attn_mask, target_device)

        head_dim = query.shape[-1]
        softmax_scale = scale if scale is not None else 1.0 / math.sqrt(head_dim)

        scores = torch.matmul(query, key.transpose(-2, -1)) * softmax_scale

        if is_causal:
            Tq, Tk = query.shape[-2], key.shape[-2]
            causal_mask = torch.triu(torch.ones(Tq, Tk, dtype=torch.uint8, device=target_device), diagonal=1)
            scores = scores.masked_fill(causal_mask != 0, float("-inf"))

        if attn_mask is not None:
            if attn_mask.dtype == torch.bool:
                scores = scores.masked_fill(~attn_mask, float("-inf"))
            else:
                scores = scores + attn_mask

        probs = F.softmax(scores, dim=-1)
        if dropout_p > 0.0:
            probs = F.dropout(probs, p=dropout_p)

        return torch.matmul(probs, value)

    def guarded_sdpa(query, key, value, attn_mask=None, dropout_p=0.0, is_causal=False, scale=None):
        on_directml = any(
            isinstance(t, torch.Tensor) and t.device.type == "privateuseone" for t in (query, key, value)
        )
        if not on_directml:
            return original_sdpa(
                query, key, value, attn_mask=attn_mask, dropout_p=dropout_p, is_causal=is_causal, scale=scale
            )
        try:
            return manual_sdpa(query, key, value, attn_mask=attn_mask, dropout_p=dropout_p, is_causal=is_causal, scale=scale)
        except RuntimeError as e:
            logger.warning(
                f"[nam.inference.backend.directml] DirectML SDPA computation failed ({e}). "
                f"Falling back to CPU for this attention layer."
            )
            target_dev = query.device if isinstance(query, torch.Tensor) else torch.device("cpu")
            q_cpu = query.cpu() if isinstance(query, torch.Tensor) else query
            k_cpu = key.cpu() if isinstance(key, torch.Tensor) else key
            v_cpu = value.cpu() if isinstance(value, torch.Tensor) else value
            mask_cpu = attn_mask.cpu() if isinstance(attn_mask, torch.Tensor) else attn_mask

            res_cpu = manual_sdpa(q_cpu, k_cpu, v_cpu, attn_mask=mask_cpu, dropout_p=dropout_p, is_causal=is_causal, scale=scale)
            return res_cpu.to(target_dev)

    F.scaled_dot_product_attention = guarded_sdpa
    torch.nn.functional.scaled_dot_product_attention = guarded_sdpa


def patch_dropout() -> None:
    """
    torch-directml's native dropout kernel is flaky - same class of
    intermittent, unexplained failures as SDPA and raw tensor.to(). Swap
    in the standard inverted-dropout formula (rand_like, compare, mul,
    div - all ops DirectML handles reliably) whenever the input tensor is
    on the DirectML device, for both the functional call and nn.Dropout's
    module form. CPU/CUDA calls pass through to the native kernel
    untouched.
    """
    import torch.nn.functional as F

    original_functional_dropout = F.dropout

    def manual_dropout(x: "torch.Tensor", p: float = 0.5) -> "torch.Tensor":
        if p <= 0.0:
            return x
        keep_prob = 1.0 - p
        mask = (torch.rand_like(x) < keep_prob).to(x.dtype)
        return x * mask / keep_prob

    def guarded_functional_dropout(input, p=0.5, training=True, inplace=False):
        if not training or p == 0.0:
            return input
        if isinstance(input, torch.Tensor) and input.device.type == "privateuseone":
            return manual_dropout(input, p)
        return original_functional_dropout(input, p=p, training=training, inplace=inplace)

    F.dropout = guarded_functional_dropout
    torch.nn.functional.dropout = guarded_functional_dropout

    original_module_forward = torch.nn.Dropout.forward

    def guarded_module_forward(self, input: "torch.Tensor") -> "torch.Tensor":
        if not self.training or self.p == 0.0:
            return input
        if isinstance(input, torch.Tensor) and input.device.type == "privateuseone":
            return manual_dropout(input, self.p)
        return original_module_forward(self, input)

    torch.nn.Dropout.forward = guarded_module_forward


_DIRECTML_PATCHED = False


def _patch_directml_gaps() -> None:
    global _DIRECTML_PATCHED
    if _DIRECTML_PATCHED:
        return
    _DIRECTML_PATCHED = True

    _patch_sdp_backends()
    _patch_pin_memory()
    _patch_missing_aten_ops()
    _patch_autocast()
    patch_tensor_cat()
    patch_tensor_arithmetic()
    patch_sdpa()
    patch_dropout()


def _patch_pin_memory() -> None:
    original_pin_memory = torch.Tensor.pin_memory

    def safe_pin_memory(self, *args, **kwargs):
        if self.device.type == "privateuseone":
            return self
        return original_pin_memory(self, *args, **kwargs)

    torch.Tensor.pin_memory = safe_pin_memory

    original_is_pinned = torch.Tensor.is_pinned

    def safe_is_pinned(self, *args, **kwargs):
        if self.device.type == "privateuseone":
            return False
        return original_is_pinned(self, *args, **kwargs)

    torch.Tensor.is_pinned = safe_is_pinned


def _patch_missing_aten_ops() -> None:
    _cpu_fallback_wrap(torch.Tensor, "__mod__")
    _cpu_fallback_wrap(torch, "linalg", "svd")
    _cpu_fallback_wrap(torch, "linalg", "eigh")


def _cpu_fallback_wrap(namespace, attr_name: str, sub_attr: Optional[str] = None) -> None:
    import functools

    target_obj = namespace if sub_attr is None else getattr(namespace, attr_name)
    target_name = sub_attr or attr_name
    original_fn = getattr(target_obj, target_name)

    @functools.wraps(original_fn)
    def wrapped(*args, **kwargs):
        try:
            return original_fn(*args, **kwargs)
        except NotImplementedError:
            return _run_on_cpu_and_restore(original_fn, args, kwargs)

    try:
        setattr(target_obj, target_name, wrapped)
    except (AttributeError, TypeError):
        logger.debug(f"[nam.inference.backend.directml] Could not patch {attr_name}.{sub_attr}; skipping.")


def _run_on_cpu_and_restore(fn: Callable, args: tuple, kwargs: dict):
    orig_device = None
    cpu_args = []
    for a in args:
        if isinstance(a, torch.Tensor) and a.device.type == "privateuseone":
            orig_device = a.device
            cpu_args.append(a.cpu())
        else:
            cpu_args.append(a)

    result = fn(*cpu_args, **kwargs)

    if orig_device is None:
        return result
    if isinstance(result, torch.Tensor):
        return result.to(orig_device)
    if isinstance(result, tuple):
        return tuple(r.to(orig_device) if isinstance(r, torch.Tensor) else r for r in result)
    return result


def _patch_autocast() -> None:
    original_autocast = torch.autocast

    class _SafeAutocast:
        def __init__(self, device_type, *args, **kwargs):
            if device_type == "privateuseone":
                self._cm = contextlib.nullcontext()
            else:
                self._cm = original_autocast(device_type, *args, **kwargs)

        def __enter__(self):
            return self._cm.__enter__()

        def __exit__(self, *exc):
            return self._cm.__exit__(*exc)

    torch.autocast = _SafeAutocast


def _privateuseone_device_among(candidates: tuple) -> Optional["torch.device"]:
    for c in candidates:
        if isinstance(c, torch.Tensor) and c.device.type == "privateuseone":
            return c.device
    return None


def _align_to_device(value, target_device: "torch.device"):
    if isinstance(value, torch.Tensor) and value.device != target_device:
        return value.to(target_device)
    return value


def patch_tensor_cat() -> None:
    """
    Guards torch.cat against silently mixing a DirectML ("privateuseone")
    tensor with a CPU/CUDA tensor.
    """
    original_cat = torch.cat

    def guarded_cat(tensors, *args, **kwargs):
        target_device = _privateuseone_device_among(tuple(tensors))
        if target_device is None:
            return original_cat(tensors, *args, **kwargs)
        aligned = [_align_to_device(t, target_device) for t in tensors]
        return original_cat(aligned, *args, **kwargs)

    torch.cat = guarded_cat


def patch_tensor_arithmetic() -> None:
    """
    Guards elementwise binary ops (+, -, *, /, matmul) against DirectML device mismatches.
    """
    binary_ops = ("__add__", "__radd__", "__sub__", "__rsub__", "__mul__", "__rmul__", "__truediv__", "__matmul__")

    for op_name in binary_ops:
        original_op = getattr(torch.Tensor, op_name)

        def make_guarded(original_op, op_name):
            def guarded_op(self, other):
                target_device = _privateuseone_device_among((self, other))
                if target_device is None:
                    return original_op(self, other)

                aligned_self = _align_to_device(self, target_device)
                aligned_other = _align_to_device(other, target_device)
                try:
                    return original_op(aligned_self, aligned_other)
                except RuntimeError as e:
                    other_desc = (
                        f"Tensor(shape={tuple(aligned_other.shape)}, dtype={aligned_other.dtype})"
                        if isinstance(aligned_other, torch.Tensor)
                        else repr(aligned_other)
                    )
                    raise RuntimeError(
                        f"[nam.inference.backend.directml] DirectML op {op_name} failed: "
                        f"self=Tensor(shape={tuple(aligned_self.shape)}, dtype={aligned_self.dtype}), "
                        f"other={other_desc}. Original error: {e}"
                    ) from e

            return guarded_op

        setattr(torch.Tensor, op_name, make_guarded(original_op, op_name))
