# nam

Not Another Monolith.

nam is a modular platform for building and serving AI workloads. Instead of one
big app, a nam project is a set of self-contained modules, each with its own
backend routes and frontend bundle, mounted under a single FastAPI server.

## Why

AI workloads tend to accumulate a pile of loosely related tools: an annotator,
a converter, a classifier, a data browser. nam gives each of these its own
folder with its own backend and frontend, discovered and mounted
automatically, so they can be developed and deployed as one server without
becoming one codebase.

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

`nam.concurrency` exposes a shared thread pool and an `Orchestrator` pipeline
for building multi-stage, batched processing chains, useful for modules doing
GPU or CPU-bound inference work.

## Starting a new project

Copy the `template/` directory as a starting point. It contains a minimal
`project.yaml` and a single working module you can rename and build on.

## License

MIT, see [LICENSE](LICENSE).
