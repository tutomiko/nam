"""
nam.concurrency package

Exposes the Executor/Future primitives and the Orchestrator pipeline
classes. A shared global thread pool (`executor`) is kept initially as `None`
until `nam.concurrency.initialize()` configures and starts it. `initialize()`
must be called (once, by server.py at process startup) before anything
imports nam.concurrency.orchestrator - the orchestrator module wires itself up
against the shared executor at *its own* import time, not lazily, so the
executor has to already exist first.

Usage:
    from nam import concurrency
    concurrency.initialize()  # defaults to one worker per detected core

    from nam.concurrency import Orchestrator, OrchestrationPipeline
    pipeline = OrchestrationPipeline(concurrency.executor)
    ...
"""

import atexit

from .executor import Executor, Future, detect_cpu_count

__all__ = [
    "Executor",
    "Future",
    "Orchestrator",
    "OrchestrationPipeline",
    "OrchestrationLayer",
    "OrchestrationFrame",
    "OrchestrationClient",
    "executor",
    "initialize",
    "get_parallellism",
    "detect_cpu_count",
]

executor = None
executor_threads = 0


def get_parallellism() -> None:
    global executor_threads
    return executor_threads


def initialize(executor_count: int | None = None) -> None:
    global executor
    global executor_threads
    if executor is None:
        if executor_count is None:
            executor_count = detect_cpu_count()
        executor_threads = executor_count
        executor = Executor(executor_count)
        executor.start()

    # Orchestrator wires itself up against nam.concurrency.executor at import
    # time, so it can only be imported for the first time once that
    # executor already exists - hence the import lives here, after the
    # executor is guaranteed to be set up, rather than at module top-level.
    global Orchestrator, OrchestrationPipeline, OrchestrationLayer, OrchestrationFrame, OrchestrationClient
    from .orchestrator import (
        Orchestrator,
        OrchestrationPipeline,
        OrchestrationLayer,
        OrchestrationFrame,
        OrchestrationClient,
    )


def _shutdown_global_executor():
    """Ensure worker threads are signaled to stop when the process exits."""
    if executor is not None:
        executor.shutdown()


atexit.register(_shutdown_global_executor)
