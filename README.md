# nam

Not Another Monolith: a modular platform architecture for AI workloads.

nam is a modular platform for building and serving AI workloads. A nam
project is a set of self-contained modules, each with its own backend routes
and (optional) frontend bundle, mounted under a single FastAPI server. At the core of
nam is `nam.concurrency`, a batching orchestrator built for running
multi-stage CPU, GPU, disk or network -bound work without every module having to hand-roll
its own thread pool, batching logic, and backpressure.

The contract of nam projects is: the modules must never access each others' files directly, but instead establish a HTTP contract by which they communicate. 
This allows each module to remain independent and for projects to be sharded into separate projects without breaking the application. 
The use of Python and React also ensures a cloud-friendly, modern approach for the application's design.

## Project layout

A nam project is a directory that looks like this:

```
my-project/
  project.yaml
  app.py              (optional)
  modules/
    my_module/
      module.yaml
      backend/
        routes.py
      frontend/
        App.jsx
  shared/
    src/              (importable by all backends)
    com/              (importable by all frontends as @shared)
```

Run it with:

```
nam my-project
```

### project.yaml

All keys are optional.

```yaml
environment: my-project   # namespaces this project's app data dir, defaults to the folder name
parallellism: auto        # worker count for the concurrency executor, or an integer
optimization: throughput  # passed through to your own module code
reload: true               # gates both the Python reloader and the frontend watcher
port: 8000                 # can be overridden with --port
```

### Modules

Each module lives in `modules/<id>/` and needs a `module.yaml`:

```yaml
name: My Module
type: site       # "site" modules appear in navigation, other types are backend-only
icon: layout-template
```

and a `backend/routes.py` exposing a FastAPI `router`. nam mounts it at
`/<id>`. If a `frontend/` directory exists with an entry point
(`App.jsx`, `app.jsx`, `index.jsx`, `index.js`, or `App.js`), it gets bundled
with esbuild and served at `/bundles/<id>.js`.

### app.py

An optional `app.py` at the project root can define a `router` for routes
that apply to the whole project rather than one module, such as a
navigation endpoint or a custom root redirect. It's mounted unprefixed.

### shared/

`shared/src` is put on the Python path, so backend code across modules can
import shared library code directly. `shared/com` is aliased to `@shared` in
the frontend bundler, for shared React components.

## Reload and rebuilding

When `reload: true` (the default), nam watches your project for changes:

- Python files are watched by uvicorn's own reloader and restart the server.
- Frontend sources are watched by nam itself. Each module's frontend files
  are hashed, and a rebuild only runs when the hash changes, so idle modules
  don't get rebuilt on every poll.

When `reload: false`, nam builds everything once at startup and does not
watch for changes. Either way, an existing bundle is reused across restarts
if its sources haven't changed since it was last built.

## Concurrency

`nam.concurrency` is initialized once, at server startup, before any module
code imports the orchestrator:

```python
from nam import concurrency
concurrency.initialize()  # defaults to one worker per detected core
```

This sets up a shared thread pool (`concurrency.executor`) and a global
load balancer that every `OrchestrationPipeline` schedules work through.
Modules don't create their own thread pools; they build a pipeline against
the one nam already started.

### Pipelines and layers

An `OrchestrationPipeline` is a chain of `OrchestrationLayer`s. Each layer
has a handler, and frames flow from one layer to the next in batches:

```python
from nam.concurrency import OrchestrationPipeline, Orchestrator

pipeline = OrchestrationPipeline(concurrency.executor)

def preprocess(owner, task, batch):
    return [f for f in batch if f.userdata is not None]

def run_model(owner, task, batch):
    for f in batch:
        f.userdata = model.predict(f.userdata)

pipeline.add_layer(role="cpu", handler=preprocess)
pipeline.add_layer(role="gpu", handler=run_model)
pipeline.finish()
```

`add_layer` takes:

- `role`: which load balancer partition this layer's work counts against
  (`cpu`, `gpu`, `disk`, or `network`). The load balancer tracks a moving
  average of how long each layer's work takes and schedules accordingly, so
  a slow GPU layer doesn't starve a fast CPU layer sharing the same executor.
- `handler(owner, task, batch)`: called with the batch of frames belonging
  to a single owner. `owner` is whichever value you fed in as an
  `Orchestrator`, `task` is a `Task` for signalling completion, and `batch`
  is the list of `OrchestrationFrame`s to process.
- `contiguous`: if `True`, a frame only leaves this layer once every frame
  before it (by index, for the same owner) has already left. Useful for
  layers where output order must match input order.
- `min_batch` / `max_batch`: batch size bounds. A layer won't call its
  handler until at least `min_batch` frames are queued, or `yield_after`
  seconds have passed, whichever comes first.
- `shared`: if `True`, the handler is called once per batch across all
  owners, instead of once per owner.
- `asynchronous`: if `True`, the layer doesn't wait for the handler to
  return before moving on. The handler is responsible for calling
  `task.ready(...)` or `task.abort()` itself, whenever it's actually done
  (which can be from another thread, later).

### Handler return values

For a non-asynchronous layer, what the handler returns decides what
proceeds to the next layer:

- Returning `None` (including simply not returning anything) passes the
  whole batch through unchanged.
- Returning a list of frames passes through only those frames. Anything in
  the original batch that isn't in the returned list is discarded: it
  won't reach the next layer, and it won't be requeued.

```python
def filter_bad_frames(owner, task, batch):
    return [f for f in batch if f.userdata.is_valid]
```

### Task.ready() and Task.abort()

Every handler call gets a `Task`. Calling `task.ready(batch)` explicitly
does the same job as a return value, but works for asynchronous layers too,
and can be called from a different thread than the one that ran the
handler:

```python
def dispatch_to_worker(owner, task, batch):
    def on_worker_done(results):
        survivors = [f for f, r in zip(batch, results) if r is not None]
        task.ready(survivors)

    worker_pool.submit(batch, callback=on_worker_done)

pipeline.add_layer(role="gpu", handler=dispatch_to_worker, asynchronous=True)
```

`task.ready()` with no argument passes the whole original batch through,
same as returning `None`. `task.abort()` discards the whole batch, the same
as `task.ready([])`.

A task only settles once. The first call to `ready()` or `abort()` wins;
any later call (whether it's another `ready()`, an `abort()`, or a handler
return value) is ignored. This makes it safe to call `task.ready()` from a
callback that might fire more than once.

### Orchestrator

`Orchestrator` sits in front of a pipeline and manages a bounded window of
in-flight work:

```python
orch = Orchestrator(pipeline, max_window=256)
orch.feed([item_a, item_b, item_c])
```

Each item you feed becomes an `OrchestrationFrame` and enters the pipeline
at the first layer. `max_window` caps how many frames can be in flight at
once; once frames finish (reach the end of the pipeline, added with
`pipeline.finish()`), room frees up and more queued items are pulled in.
Set `orch.window_listener` to get called with the remaining free capacity
whenever frames finish.

Call `orch.close()` when you're done with it, to clear any per-owner
backlog state held by `contiguous` layers.

## Starting a new project

Copy the `template/` directory as a starting point. It contains a minimal
`project.yaml` and a single working module you can rename and build on.

## Testing

```
pip install -e .
pip install pytest
pytest
```

## License

MIT, see [LICENSE](LICENSE).
