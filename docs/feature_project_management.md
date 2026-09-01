# Project layout

A nam project is a directory that looks like this:

```
my-project/
  project.yaml
  builds.yaml          (optional)
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

or, to launch a single shard of the project instead of the whole thing:

```
nam my-project -build worker
```

## project.yaml

All keys are optional.

```yaml
environment: my-project   # namespaces this project's app data dir, defaults to the folder name
parallellism: auto        # worker count for the concurrency executor, or an integer
optimization: throughput  # passed through to your own module code
reload: true               # gates both the Python reloader and the frontend watcher
port: 8000                 # can be overridden with --port
```

## Modules

Each module lives in `modules/<id>/` and needs a `module.yaml`:

```yaml
name: My Module
type: site
icon: layout-template
```

and a `backend/routes.py` exposing a FastAPI `router`. The module's page is
served at `/app/<id>`, and its `router` is mounted onto `/api` directly -
nam does NOT inject a `/<id>` segment into a module's routes. A path is
declared by exactly one module, and it owns the full path itself: a route
declared as `@router.get("/<id>/ping")` in `<id>`'s `routes.py` is reached
at `/api/<id>/ping` because `<id>` put it there, not because nam added it.
This means a resource never needs to be re-declared by every module that
wants it - whichever module owns `/tokenizers` declares
`@router.get("/resource_manager/tokenizers")` once, and every other module
(and the frontend) just calls that same path directly, rather than each
caller growing its own duplicate proxy of someone else's resource.

nam parses each module's `routes.py` statically (an AST scan, never an
import) to know its exact route shapes, and uses that to check at startup
that no two modules - or one module twice - declare the same method+path;
a conflict raises and the process refuses to start. This is the mechanism
that keeps "declared by exactly one module" actually true: a genuine
collision between two modules is a hard error, not a silently shadowed
route.

Every module's declared paths exist under `/api` in EVERY build, even one
that doesn't mount that module locally (see
[docs/feature_sharding.md](feature_sharding.md)): for a module excluded
from this build, nam registers a proxy for each of its routes instead of
a live handler, forwarding the request to wherever that module actually
lives via `Router.get_hostname`. Callers - a module's own frontend, or
another module's backend - always just call `/api/<owning module>/...`
and never need to know or care whether that module is running in this
process or a different one.

If a `frontend/` directory exists with an entry point (`App.jsx`,
`app.jsx`, `index.jsx`, `index.js`, or `App.js`), it gets bundled with
esbuild and served at `/bundles/<id>.js`.

## app.py

An optional `app.py` at the project root can define a `router` for routes
that apply to the whole project rather than one module, such as a
navigation endpoint or a custom root redirect. It's mounted unprefixed.

## shared/

`shared/src` is put on the Python path, so backend code across modules can
import shared library code directly. `shared/com` is aliased to `@shared` in
the frontend bundler, for shared React components. 

Ideally, shared code and components should be things specific to none of the modules, 
and contain only things that might warrant a library/libraries of their own.
Otherwise, you might risk turning the shared region into a monolith itself.
