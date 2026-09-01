# Reload and rebuilding

When `reload: true` (the default), nam watches your project for changes:

- Python files are watched by uvicorn's own reloader and restart the server.
- Frontend sources are watched by nam itself. Each module's frontend files
  are hashed, and a rebuild only runs when the hash changes, so idle modules
  don't get rebuilt on every poll.

When `reload: false`, nam builds everything once at startup and does not
watch for changes. Either way, an existing bundle is reused across restarts
if its sources haven't changed since it was last built.
