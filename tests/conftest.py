import time

import pytest

from nam import concurrency

concurrency.initialize(4)

from nam.concurrency import Orchestrator, OrchestrationPipeline  # noqa: E402


@pytest.fixture
def pipeline():
    return OrchestrationPipeline(concurrency.executor)


@pytest.fixture
def make_orchestrator(pipeline):
    created = []

    def _make(max_window=256):
        orch = Orchestrator(pipeline, max_window)
        created.append(orch)
        return orch

    yield _make

    for orch in created:
        orch.close()


def wait_until(predicate, timeout=2.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()
