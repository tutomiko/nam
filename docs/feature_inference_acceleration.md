# Inference

`nam.inference` lets module code write plain PyTorch and transparently get
device resolution and batch handling underneath - without calling any
nam-specific API for the basic case. Accelerated export/compilation is
available too, as an opt-in call (see below) rather than something that
happens automatically.

```python
import torch.nn as nn

class MyModel(nn.Module):
    ...

model = MyModel()
model.eval()
output = model(some_tensor)
```

That's it. `nam.inference.hook` patches `nn.Module.eval()`/`__call__()` at
server startup (see `create_app` in `server.py`), so every module's models
route through nam's inference stack automatically.

Do note that the accelerated inference applies to individual blocks as well, 
so methods like DINOv2's `forward_features()` still get GPU acceleration 
despite not being direct calls to  `nn.Module.eval()`/`__call__()`, 
since the blocks themselves are invoked via `nn.Module.__call__()`. 

Tested and confirmed working on:
- DINOv2/DINOv3
- Mask2Former
- DepthAnythingV2/DepthAnythingV3
- PWCNet
- MiDAS
- SAM2

_WARNING_:
If you're running on a machine with an integrated GPU, there's no 
separate VRAM - there's just the shared system RAM. 
This means that if your PyTorch inference is a massive batch 
dumped to CPU/CUDA, and the inference gets accelerated by DirectML, 
you might encounter an OOM. This is not an implementation fault, 
this means that you didn't batch properly given the resources you have. 
If this is happening and you're unwilling to separate it into 
proper, digestible batches, then gracefully request CPU. 
Integrated GPUs have this feature/issue (they're quite suboptimal for running heavy, batched AI models) 
and it's something nam shouldn't, and thus won't, touch. 

## Device resolution (`nam.inference.device`)

Models run through `Device.auto()`, which resolves the best available torch
backend in priority order: CUDA, then torch-directml, then CPU.
torch-directml is detected at runtime (`importlib.util.find_spec`) and is
never required in `requirements.txt` - most machines have CUDA and
shouldn't need it. When torch-directml is in use, `nam.inference.device`
also patches known operator gaps (pin_memory, a handful of missing ATen
ops, autocast) once, so the rest of the codebase can treat a DirectML
device like any other torch device.

## Accelerated export (`nam.inference.model_exporter`)

`nam.inference.model_exporter` is an opt-in helper for module authors who
want a model compiled rather than run eagerly: `export_pytorch_module_to_onnx`
takes a plain PyTorch module to ONNX, and `export_and_compile` compiles that
ONNX graph into a pooled, accelerated OpenVINO model. OpenVINO follows the
same "check if installed, don't require it" pattern as torch-directml -
it's not in `requirements.txt`. Nothing calls this automatically; it's not
gated by any project.yaml key - a module's own code decides when (or
whether) to export and switch a given model over to the compiled path.

Pool sizes for compiled models are always set to
`nam.concurrency.get_parallellism()` - the same worker count as the shared
executor - rather than a caller-supplied number, since nam is meant to run
entirely on those pools.

## Batch padding (`nam.inference.padding`)

Compiled/exported models have a fixed batch size, set from whatever batch
size a model's first real call happened to use - not from config, since a
project with several different models (each with very different memory
footprints) shouldn't have to commit them all to the same batch size.

Later calls with a different batch size are transparently split against
that fixed size: full chunks run as-is, and any remainder is zero-padded
up to the fixed size, run, then trimmed back down before being handed
back. A fixed batch size of 4 called with 5 inputs runs as a chunk of 4
plus a chunk of 1 padded to 4, discarding the 3 padding rows from that
second chunk's output.

The reason that the batch size is detected from the first invocation is simple: if you're running inference on a nam project, it expects you to be doing so within an OrchestrationLayer. These have min_batch and max_batch (size) you can adjust. Running the inference on a layer like this ensures that you're utilizing the nam infrastructure effectively, while also keeping the mental model clear: this layer executes batch of the size the layer expects.
