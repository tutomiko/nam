# nam

Not Another Monolith: a modular runtime architecture for AI workloads.

nam is a modular runtime for building and serving AI workloads. A nam
project is a set of self-contained modules, each with its own backend routes
and (optional) frontend bundle, mounted under a single FastAPI server. At the core of
nam is `nam.concurrency`, a batching orchestrator built for running
multi-stage CPU, GPU, disk or network -bound work without every module having to hand-roll
its own thread pool, batching logic, and backpressure.

The contract of nam projects is: the modules must never access each others' files directly, but instead establish a HTTP contract by which they communicate.
This allows each module to remain independent and for a single project to be **sharded** across multiple processes - possibly on different machines - without changing any module code. See [Sharding](#sharding) below.
The use of Python and React also ensures a cloud-friendly, modern approach for the application's design.

nam does not attempt to compete with platforms like Kubernetes, but is instead designed to work alongside them. Additionally, nam is intentionally minimal and lightweight and does not attempt to re-invent any wheels or force you to program against some enormous API - it enforces a maintainable, self-contained architecture and provides module resolution primitives and a unified concurrency model to work with. Whichever framework/platform you choose to run it, and whichever libraries you choose for the communication between modules, this is all up to you, and not a concern of nam.

## Why does it exist?
The purpose of nam is to make complex, modular, distributed web services faster to develop, deploy, and maintain. Its cross-backend inference acceleration and concurrency orchestration aren't only intended to make production services fast, but to also make their development and testing faster.

nam is engineered explicitly for modern AI workloads, where heterogeneous compute, heavy batching, and modular isolation are first-class requirements.

## Features

- **Sustainable Project Layout** - a `project.yaml`, `modules/<id>/` directories (each
  with `module.yaml`, `backend/routes.py`, optional `frontend/`), optional
  `app.py` and `shared/`. Modules own their routes outright under `/api`,
  enforced by an AST-based conflict checker at startup, and are served at
  `/app/<id>` with `GET /` redirecting there automatically. See
  [docs/feature_project_management.md](docs/feature_project_management.md).
- **Sharding** - split a project into multiple `builds.yaml`-defined
  processes with zero module code changes; excluded modules' routes are
  transparently proxied via `Router`. See
  [docs/feature_sharding.md](docs/feature_sharding.md).
- **Module Hotswapping** - Python reload via uvicorn, hash-based
  frontend rebuilds, toggled with `reload` in `project.yaml`. See
  [docs/feature_reload.md](docs/feature_reload.md).
- **Unified Concurrency** - a shared thread pool and load balancer
  (`nam.concurrency`) with pipelines, layers, tasks, and
  `OrchestrationClient` for batched multi-stage work. See
  [docs/feature_unified_concurrency.md](docs/feature_unified_concurrency.md).
- **Inference Acceleration** - transparent device resolution (CUDA, torch-directml,
  CPU) and batch handling for plain PyTorch modules, plus opt-in
  export/compilation to OpenVINO. See
  [docs/feature_inference_acceleration.md](docs/feature_inference_acceleration.md).

## Starting a new project

Copy the `template/` directory as a starting point. It contains a minimal
`project.yaml` and a single working module you can rename and build on.

## Testing

```
pip install -e .
pip install pytest
pytest
```

## Status
Development and testing is actively ongoing. 
The API will remain the same, so all changes will be to internals, not the API surface. 
Safe to integrate in only that regard, but note the warning below.

## Experimental / Partial
This is a partial release of what was internal tooling. 
Most of the unit tests were not included, and most documentation has been stripped 
until investigated thoroughly and adjusted accordingly. 

As this release is missing most of the test suite it should be considered NOT safe 
for production. 

I know there's like, two bugs in there somewhere. Under some rock - or should I say... a monolith?

## Origins / Trivia
This project actually has roots in my [conceptually overlapping experiments in framework design](https://github.com/tutomiko/xboot/tree/main/main/java/com/xahico/boot/publish).

## License

MIT, see [LICENSE](LICENSE).
