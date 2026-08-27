import time

from nam.concurrency import OrchestrationClient

from tests.conftest import wait_until


class _Frame:
    def __init__(self, index, value):
        self.index = index
        self.value = value


class _EchoClient(OrchestrationClient):
    def _create_frame(self, index, userdata):
        return _Frame(index, userdata)


def test_feed_reaches_on_ready_for_frames_that_pass_through(pipeline):
    def layer1_handler(owner, task, batch):
        return None

    pipeline.add_layer(role="cpu", handler=layer1_handler)
    pipeline.finish()

    client = _EchoClient(pipeline, max_window=32)
    ready = []
    client.on_ready(lambda f: ready.append(f.value))
    client.feed([1, 2, 3])

    assert wait_until(lambda: len(ready) == 3)
    assert sorted(ready) == [1, 2, 3]

    client.close()


def test_task_discard_reaches_on_discarded_not_on_ready(pipeline):
    def layer1_handler(owner, task, batch):
        keep = [f for f in batch if f.userdata.value != 99]
        drop = [f for f in batch if f.userdata.value == 99]
        task.ready(keep)
        if drop:
            task.discard(drop)

    pipeline.add_layer(role="cpu", handler=layer1_handler)
    pipeline.finish()

    client = _EchoClient(pipeline, max_window=32)
    ready = []
    discarded = []
    client.on_ready(lambda f: ready.append(f.value))
    client.on_discarded(lambda f: discarded.append(f.value))
    client.feed([1, 99, 2])

    assert wait_until(lambda: len(ready) + len(discarded) == 3)
    assert sorted(ready) == [1, 2]
    assert discarded == [99]

    client.close()


def test_close_tears_down_without_raising(pipeline):
    def layer1_handler(owner, task, batch):
        return None

    pipeline.add_layer(role="cpu", handler=layer1_handler)
    pipeline.finish()

    client = _EchoClient(pipeline, max_window=32)
    client.feed([1])
    time.sleep(0.1)
    client.close()
