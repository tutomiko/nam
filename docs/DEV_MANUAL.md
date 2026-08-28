# DEVELOPMENT_MANUAL.md

NAM PROJECT DEVELOPMENT MANUAL
(How to build modules/projects on top of the "nam" framework. This is a
usage manual, not a nam-internals doc. 

SOURCE: https://github.com/tutomiko/nam

Please refer to the examples/ files for reference implementations. 
These help a lot more than this document alone.

_______________________
0. IMPORTANT
_______________________
NAM IS ASYNC/CONCURRENT-FIRST ARCHITECTURE BUILT AROUND A SHARED THREAD POOL
AND A PIPELINE ORCHESTRATOR. DO NOT ROLL YOUR OWN THREADING.

- Never do: threading.Thread(...), multiprocessing, asyncio.run in a
  background thread, manual locks/mutexes around request-handling logic,
  hand-rolled worker pools, or global mutable state guarded by your own lock.
- Instead: push work through nam's OrchestrationPipeline /
  OrchestrationClient (see section 3). It already gives you: a shared
  process-wide thread pool, batching, backpressure, per-frame success/
  failure signaling, and ordering guarantees — for free, and consistently
  across every module in the project.
- If you find yourself reaching for `threading.Lock`, `queue.Queue` +
  a worker loop, or a background daemon thread inside a module: STOP.
  That is very likely a pipeline you should be building with
  OrchestrationPipeline instead. Locks in the pipeline path are exactly
  the kind of thing that silently kills throughput or deadlocks under
  load — the whole point of nam's concurrency model is that you don't
  need them.
- FastAPI route handlers can be `def` or `async def` as normal. The
  forbidden thing is spinning up your own concurrency primitives to do
  background/parallel work — that's the orchestrator's job.
- Exception: truly one-off fire-and-forget background chores unrelated to
  request throughput (e.g. a fresh `_DedicatedScheduler`-style delayed
  callback) still shouldn't be hand-rolled either — there is no public
  "delay a callback" API exposed to project code, so if you need that,
  ask before building it rather than reaching for threading.Timer.

_______________________
1. WHAT NAM IS, FROM A PROJECT'S POV
_______________________
nam ("Not Another Monolith") is a framework that runs a FastAPI app made
of independently-developed "modules." Each module contributes:
  - a backend (FastAPI router, mounted at /<module_id>/...)
  - an optional frontend (a React app, auto-bundled with esbuild)
A project is a directory of modules + shared code + config, launched with:
    nam <project_dir> [--host H] [--port P] [-build <name>]

You develop MODULES inside a PROJECT.

IMPORTANT: there is NO module base class to subclass, no lifecycle hooks
to override, no `class MyModule(nam.Module)` anywhere. A "module" is
purely structural — nam discovers it by directory shape, not by type:
  - a directory under modules/
  - containing module.yaml (plain data)
  - containing backend/routes.py that defines a top-level `router`
    (a plain fastapi.APIRouter — nothing nam-specific about it)
  - optionally a frontend/ entry file
That's the entire contract.

_______________________
2. PROJECT LAYOUT (what to create / where things live)
_______________________
<project_root>/
  project.yaml          project-level config (see section 2a)
  builds.yaml            OPTIONAL - splits project into multiple deployable
                          processes ("builds"). Absent = single monolith
                          process running every module.
  app.py                 OPTIONAL - project-wide routes not owned by any
                          single module (must define a top-level `router`
                          FastAPI APIRouter). Mounted UNPREFIXED at "/".
  data/                  OPTIONAL - files here get synced (one-way,
                          hash-diffed) into the app's persistent data dir
                          at startup. Use for seed/static data shipped
                          with the project.
  shared/
    src/                 Code here is importable as bare top-level modules
                          from ANY module's backend (e.g. shared/src/foo.py
                          -> `import foo` works everywhere). This is where
                          cross-module shared logic/types/clients live.
                          NOTE: shared/src is on sys.path, not shared/ -
                          don't `import shared.foo`, just `import foo`.
  weights/                Served statically at /weights/... (cached
                          aggressively - immutable, put versioned/hashed
                          filenames here for model weights, static assets).
  bundles/                Auto-generated frontend build output. Don't hand-
                          edit; served at /bundles/... with no-cache headers.
  modules/
    <module_id>/
      module.yaml         REQUIRED. Module metadata (section 2b).
      backend/
        routes.py         REQUIRED. Must define a top-level `router`
                           (FastAPI APIRouter). Mounted at /<module_id>/*.
      frontend/            OPTIONAL. React app. Entry point auto-detected:
                           App.jsx, app.jsx, index.jsx, index.js, or App.js
                           (first match wins). Auto-bundled with esbuild on
                           change (dev) or once at startup (prod). Can
                           `import '@shared/...'` to reach shared/ (frontend
                           shared code, separate from shared/src backend code).
                           If your module has a frontend, it's served at
                           /<module_id> and should mount into `#root`.

<module_id> IS the URL prefix and the Python import segment
(`modules.<module_id>.backend.routes`) — keep it a valid identifier
(lowercase, underscores, no spaces/dashes-as-python-issue).

2a. project.yaml keys (all optional, shown with defaults):
    environment: <string>       # data dir / process identity name;
                                 # defaults to the project folder's name.
                                 # Distinct environment name = separate
                                 # persistent data dir.
    parallellism: auto          # "auto" (= cpu count) or an integer =
                                 # size of the shared thread pool AND the
                                 # size of inference request pools
                                 # (see section 5). This is nam's ONE
                                 # concurrency knob for the whole process.
    optimization: throughput    # free-form string, read by your own code
                                 # via project.optimization if you want to
                                 # branch behavior on it. nam itself doesn't
                                 # interpret it except a build can override it.
    reload: true                # dev hot-reload (uvicorn --reload +
                                 # frontend rebuild-on-change watcher).
                                 # Set false for production.
    port: 8000

2b. module.yaml keys:
    name: <display name>        # defaults to module_id.capitalize()
    type: site | tool | service # startup order: service, then site, then
                                 # tool, then anything else/unlisted last.
                                 # Use "service" for modules that open a
                                 # DB connection / background worker at
                                 # import time that other modules depend on
                                 # being already up.
    icon: <string>               # optional, passed through to nav entry

A module is only "discovered" (and thus mounted) if BOTH module.yaml AND
backend/routes.py exist. No routes.py = silently skipped.

_______________________
3. THE ORCHESTRATOR — THE CORE CONCURRENCY PRIMITIVE
_______________________
Import: `from nam.concurrency import OrchestrationPipeline, OrchestrationClient, Orchestrator`
(also available: Executor, Future, detect_cpu_count, initialize,
get_parallellism — but initialize() is called once by nam's own server
startup; you never call it yourself in module code.)

MENTAL MODEL:
An OrchestrationPipeline is a chain of LAYERS. You feed batches of
arbitrary items in one end; each layer's handler runs on the shared
thread pool, decides which items survive to the next layer (or get
dropped), and the survivors flow to the next layer automatically. This
replaces manually spawning threads/workers per request, manually
batching, and manually building drop/retry logic.

WHEN TO USE IT: any time a module needs to do throughput-sensitive work
across many requests/items concurrently — batched inference, batched
I/O, any multi-step "pipeline" of processing stages. This is what nam
gives you INSTEAD of rolling your own thread pool or async task queue.

--- Building a pipeline (once, at module import/setup time — NOT per request) ---

    from nam.concurrency import OrchestrationPipeline
    from nam import concurrency

    pipeline = OrchestrationPipeline(concurrency.executor)

    def stage_one(owner, task, batch):
        # `batch` = list of OrchestrationFrame-like objects for ONE owner
        # (unless shared=True, see below). `owner` = the Orchestrator
        # instance that fed these frames (or None if shared=True).
        # Do your work. Then either:
        #   - return a list/set of the frames that should proceed (None
        #     is shorthand for "everything proceeds")
        #   - or call task.ready(subset) / task.discard(subset) /
        #     task.abort() explicitly, any time, even from another
        #     thread (needed for async handlers, see below)
        results = do_the_work([f.userdata for f in batch])
        return batch  # or a filtered subset, or None

    pipeline.add_layer(role="cpu", handler=stage_one)
    # ... add more layers as needed, each processes what the previous
    # layer let through ...
    pipeline.finish()   # REQUIRED - terminates the chain and fires
                         # on_ready()/on_discard() callbacks for whatever
                         # survives/drops.

add_layer(role, handler, contiguous=False, min_batch=1, max_batch=256,
          yield_after=1.0, shared=False, asynchronous=False):
    role          free-form label used by nam's internal load balancer.
                  Use "cpu" unless you have a specific reason not to.
    handler       (owner, task, batch) -> optional list/None (see above).
                  Signature is (orchestrator, task, batch) when shared=True.
    max_batch     upper bound on items handled per call.
    min_batch     handler waits (up to yield_after seconds) to accumulate
                  at least this many items before running, to batch more
                  efficiently. Use this for anything where batching
                  matters (e.g. GPU inference).
    yield_after   seconds to wait for min_batch before running anyway
                  with whatever's accumulated.
    contiguous    if True, this layer only releases frames from a given
                  owner IN ORDER (index 0, then 1, then 2...). Use when
                  downstream needs strict ordering per caller. A dropped
                  frame still "counts" toward the sequence so later
                  frames aren't stuck waiting forever on it.
    shared        if True, ALL owners' items are merged into one batch
                  for this handler (handler signature becomes
                  (orchestrator, task, batch) — no per-owner split).
                  Use for e.g. a shared batched-inference layer serving
                  many callers at once.
    asynchronous  if True, the handler is expected to call task.ready()/
                  task.discard()/task.abort() LATER (from any thread,
                  any number of times, covering different subsets of the
                  batch each time) rather than returning synchronously.
                  Use when a stage's work itself is async/callback-driven
                  (e.g. waiting on an external service). If the handler
                  never calls ready/discard for some frames, those frames
                  never proceed — they just don't move forward (no crash,
                  no leak of the process, but they also never resolve
                  unless you tear things down).

--- Feeding items and getting results back: OrchestrationClient ---
This is the pattern for "one caller's use of one pipeline" — build a
small subclass per distinct item type you feed through a pipeline:

    from nam.concurrency import OrchestrationClient

    class MyClient(OrchestrationClient):
        def _create_frame(self, index, userdata):
            # box the raw item you fed in into whatever shape your
            # handlers expect to read as `f.userdata`
            return MyFrame(index, userdata)

    client = MyClient(pipeline, max_window=32)   # max_window = how many
                                                  # in-flight items this
                                                  # client allows at once
                                                  # (backpressure control)
    client.on_ready(lambda userdata: ...)         # called per item that
                                                   # made it through every
                                                   # layer
    client.on_discarded(lambda userdata: ...)     # called per item
                                                   # dropped anywhere
                                                   # along the way
    client.feed([item1, item2, item3])
    ...
    client.close()   # tears down this client's in-flight state in the
                      # pipeline. Call when the client is no longer needed
                      # (e.g. request/connection ended) — don't just let
                      # it get garbage collected while frames are in flight.

Lower-level (no subclassing) equivalent is `Orchestrator` directly
(what OrchestrationClient wraps): `Orchestrator(pipeline, max_window)`,
then `.on_ready(cb)` / `.on_discard(cb)` take BATCHES of raw
OrchestrationFrame objects (not unwrapped userdata) and `.feed(items)` /
`.close()`. Prefer OrchestrationClient unless you specifically need raw
frame batches.

RULES OF THUMB:
- Build the OrchestrationPipeline ONCE per logical pipeline (e.g. at
  module import time / lazily on first use), never per-request. It's a
  static chain of stages, not a per-call thing.
- One Orchestrator/OrchestrationClient per logical "caller" or
  "connection" (e.g. one per websocket session, one per request-context
  that streams multiple items) is fine and expected — they're cheap and
  independent; multiple clients safely share the same pipeline.
- A handler function must not block for a long time holding shared
  resources — if you need to wait on something external, use
  asynchronous=True and resolve the task later instead of blocking the
  worker thread.
- Never call task.ready()/discard() more than the contract allows: a
  sync (non-async) handler resolves via ITS RETURN VALUE (or an
  explicit task.ready/discard/abort call before returning) exactly
  once. An async handler may call ready/discard multiple times as long
  as each call names disjoint frames from the batch, from any thread,
  until every frame is claimed.

_______________________
4. MODULE-TO-MODULE COMMUNICATION — USE THE ROUTER, NEVER HARDCODE URLS
_______________________
Modules may run in the SAME process or a DIFFERENT process (see builds.yaml,
section 6) depending on deployment. Never hardcode another module's host/
port or assume it's local.

    from nam.router import get_active_router

    router = get_active_router()
    base_url = router.get_hostname("other_module_id")   # -> "http://host:port"
    # then make an HTTP call to f"{base_url}/other_module_id/api/..."

This works identically whether "other_module_id" is mounted in this same
process or lives elsewhere — resolution is transparent. Raises clearly if
the target module isn't reachable from this build's config (see
builds.yaml's include/reference — that's what defines reachability).

Inside a request handler, the router is also on `request.app.state.router`
if you already have the request object; get_active_router() is for code
that runs outside a request (module-import-time setup, background jobs).

_______________________
5. ML / INFERENCE MODULES (torch)
_______________________
nam globally patches torch so device placement "just works" without you
manually branching on cuda/directml/cpu availability:

- Write normal PyTorch: `model.to("cuda")`, `model.eval()`, then call
  `model(some_tensor)` as usual (or plain `tensor.to("cuda")`).
- nam intercepts these calls process-wide: "cuda" transparently resolves
  to whatever's actually available (real CUDA, DirectML, or CPU
  fallback), moves tensors/modules to the right real device, and
  serializes calls to non-thread-safe GPU backends (e.g. DirectML)
  under an internal lock automatically. You do NOT need to detect the
  device yourself or add your own locking around inference calls.
- "cpu" always means real CPU (no substitution). "xpu" is treated as
  "give me a GPU."
- This patch is installed once automatically by nam at process startup —
  you don't call anything to enable it, it's just how `nn.Module`/`Tensor`
  behave for the whole process once nam is running.

Higher-level helpers, from `nam.inference`:
    Model(module, device)        wraps an nn.Module: .infer(*tensors) runs
                                  under no_grad with inputs auto-moved to
                                  its device. Device comes from
                                  nam.inference.Device.auto()/.cpu()/etc.
    export_pytorch_module_to_onnx(module, onnx_path, dummy_input, ...)
                                  export a torch module to ONNX (runs on
                                  CPU internally regardless of the module's
                                  actual device).
    export_and_compile(onnx_path, input_name, models_dir, input_shape,
                        precision)
                                  compile an ONNX model via OpenVINO
                                  (optional dependency) into an
                                  ExportedModel with a pooled .infer(batch)
                                  — pool size = project.yaml's
                                  parallellism, so it matches your
                                  concurrency budget automatically. Returns
                                  None (not an exception) if OpenVINO isn't
                                  installed or compile fails — check for
                                  None before using it.
    make_pool(base_handle, copy_fn=copy.copy)
                                  generic helper to build a
                                  queue.Queue-based round-robin pool of
                                  `concurrency.get_parallellism()` copies
                                  of some handle — use if you need pooling
                                  for something that isn't the ONNX path
                                  above (e.g. pooling your own client
                                  objects across the orchestrator's worker
                                  count).
    BatchPlan / plan_for / run_padded
                                  helpers for splitting a numpy array into
                                  fixed-size batches (padding the last
                                  chunk with zeros) and reassembling
                                  outputs — for backends that require a
                                  fixed batch size (e.g. compiled/exported
                                  models). run_padded(infer_fn, batch_size,
                                  array) is the one-call version.

Do NOT hand-roll: device detection, GPU locks, or your own inference
thread pools. Feed inference work through an OrchestrationPipeline layer
if you need concurrency/batching across many callers (min_batch/
max_batch on that layer IS your batching strategy — don't build a
separate manual batcher).

_______________________
6. builds.yaml — SHARDING A PROJECT ACROSS PROCESSES (optional, project-level)
_______________________
Only needed if you're deploying a project as multiple separate processes
(e.g. a web-facing build + a heavy-inference worker build). Format:

    <build_name>:
      optimization: <string>        # optional, overrides project.yaml's
      include: [<module_id>, ...]   # modules THIS process actually mounts
      reference:                    # modules NOT in this process — maps
        <module_id>: ENV_VAR_NAME   # module_id -> env var holding its
                                     # "host:port" (or full URL) address

Launch a specific build with: `nam <project_dir> -build <build_name>`
Launch without -build = single monolith process, every discovered module
included, no reference entries needed (nothing to reference).

A module id absent from BOTH include and reference in the active build's
config is a hard configuration error the first time something tries to
resolve it via the router — fix builds.yaml, don't work around it in code.

_______________________
7. DO / DON'T CHEAT SHEET
_______________________
DO:
  - Put per-module backend logic in modules/<id>/backend/routes.py with a
    top-level `router`.
  - Put cross-module shared backend code in shared/src/ and import it as
    a bare top-level module.
  - Use OrchestrationPipeline/OrchestrationClient for any concurrent /
    batched / multi-stage processing.
  - Use get_active_router().get_hostname(module_id) for any module-to-
    module call.
  - Write plain torch code (`.to("cuda")`, `.eval()`) and trust nam's
    device patching.
  - Set module.yaml `type: service` for modules other modules depend on
    being up first.
  - Read project.optimization / project.parallellism from config if your
    module wants to branch behavior on them — they're already resolved
    for you, don't reimplement config parsing.

DON'T:
  - Don't spawn your own threads/processes/locks for request-handling or
    throughput-sensitive work — use the orchestrator.
  - Don't hardcode another module's host/port/URL.
  - Don't manually detect or lock around torch device placement.
  - Don't build an OrchestrationPipeline per-request — build it once,
    feed it many times.
  - Don't `import shared.foo` — it's `import foo` (shared/src is what's
    on sys.path).
  - Don't hand-edit anything under bundles/ — it's generated.
  - Don't assume a monolith deployment — always resolve other modules
    through the router so the module still works when split across builds.
  - Don't skip module.yaml or backend/routes.py — a module missing either
    is silently NOT mounted (no error, just absent).

_______________________
8. QUICK-REFERENCE: MINIMAL NEW MODULE
_______________________
modules/mymod/module.yaml:
    name: My Module
    type: site
    icon: box

modules/mymod/backend/routes.py:
    from fastapi import APIRouter
    router = APIRouter()

    @router.get("/api/ping")
    def ping():
        return {"ok": True}

(optional) modules/mymod/frontend/App.jsx:
    import React from 'react';
    import { createRoot } from 'react-dom/client';
    const App = () => <div>hello</div>;
    createRoot(document.getElementById('root')).render(<App />);

That's it — nam auto-discovers, mounts at /mymod/*, and bundles the
frontend if present. No registration step anywhere else needed.
