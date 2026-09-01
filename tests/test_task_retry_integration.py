import time

from tests.conftest import wait_until


def test_retry_replays_frame_through_same_layer_after_backoff(pipeline, make_orchestrator):
    """A handler that fails the first time it sees a frame retries it;
    the second time it sees the same frame it lets it through. The
    frame must actually come back to layer1's handler, not just vanish
    or skip straight to layer2."""
    attempts = {}
    layer1_calls = []

    def layer1_handler(owner, task, batch):
        for f in batch:
            layer1_calls.append(f.userdata)
            attempts[f.userdata] = attempts.get(f.userdata, 0) + 1
            if attempts[f.userdata] == 1:
                task.retry(50, f)
            else:
                task.ready(f)

    received = []

    def layer2_handler(owner, task, batch):
        received.extend(f.userdata for f in batch)

    pipeline.add_layer(role="cpu", handler=layer1_handler, asynchronous=True)
    pipeline.add_layer(role="cpu", handler=layer2_handler)
    pipeline.finish()

    orch = make_orchestrator()
    orch.feed([1, 2, 3])

    assert wait_until(lambda: len(received) == 3, timeout=3.0)
    assert sorted(received) == [1, 2, 3]
    assert layer1_calls.count(1) == 2
    assert layer1_calls.count(2) == 2
    assert layer1_calls.count(3) == 2


def test_retry_does_not_replay_before_backoff_elapses(pipeline, make_orchestrator):
    attempts = []

    def layer1_handler(owner, task, batch):
        for f in batch:
            attempts.append((f.userdata, time.monotonic()))
            if len(attempts) <= len(batch):
                task.retry(300, f)
            else:
                task.ready(f)

    received = []

    def layer2_handler(owner, task, batch):
        received.extend(f.userdata for f in batch)

    pipeline.add_layer(role="cpu", handler=layer1_handler, asynchronous=True)
    pipeline.add_layer(role="cpu", handler=layer2_handler)
    pipeline.finish()

    orch = make_orchestrator()
    start = time.monotonic()
    orch.feed([1])

    assert wait_until(lambda: len(received) == 1, timeout=3.0)
    elapsed = time.monotonic() - start
    assert elapsed >= 0.3


def test_retry_whole_batch_with_no_batch_arg_replays_all_frames(pipeline, make_orchestrator):
    call_batches = []

    def layer1_handler(owner, task, batch):
        call_batches.append(sorted(f.userdata for f in batch))
        if len(call_batches) == 1:
            task.retry(50)
        else:
            return batch

    received = []

    def layer2_handler(owner, task, batch):
        received.extend(f.userdata for f in batch)

    pipeline.add_layer(role="cpu", handler=layer1_handler, min_batch=3, max_batch=3, yield_after=0.2)
    pipeline.add_layer(role="cpu", handler=layer2_handler)
    pipeline.finish()

    orch = make_orchestrator()
    orch.feed([1, 2, 3])

    assert wait_until(lambda: len(received) == 3, timeout=3.0)
    assert sorted(received) == [1, 2, 3]
    assert len(call_batches) == 2
    assert call_batches[0] == [1, 2, 3]


def test_retried_frame_that_never_succeeds_never_reaches_next_layer(pipeline, make_orchestrator):
    """No built-in retry cap: a handler that always retries just keeps
    retrying, and the frame never reaches downstream layers or
    on_discarded. This pins the "no automatic give-up" behavior
    documented for retry()."""
    call_count = {"n": 0}

    def layer1_handler(owner, task, batch):
        call_count["n"] += 1
        task.retry(20)

    received = []
    discarded = []

    def layer2_handler(owner, task, batch):
        received.extend(f.userdata for f in batch)

    pipeline.add_layer(role="cpu", handler=layer1_handler, asynchronous=True)
    pipeline.add_layer(role="cpu", handler=layer2_handler)
    pipeline.finish()

    orch = make_orchestrator()
    orch.on_discard(lambda batch: discarded.extend(f.userdata for f in batch))
    orch.feed([42])

    assert wait_until(lambda: call_count["n"] >= 3, timeout=3.0)
    assert received == []
    assert discarded == []


def test_retry_partial_subset_lets_rest_proceed_immediately(pipeline, make_orchestrator):
    """retry() on only some frames of a batch, combined with ready() on
    the rest in the same handler call: the non-retried frames must not
    wait for the retried ones' backoff."""
    def layer1_handler(owner, task, batch):
        odd = [f for f in batch if f.userdata % 2 == 1]
        even = [f for f in batch if f.userdata % 2 == 0]
        if odd:
            task.retry(500, odd)
        task.ready(even)

    received = []

    def layer2_handler(owner, task, batch):
        received.extend(f.userdata for f in batch)

    pipeline.add_layer(role="cpu", handler=layer1_handler, asynchronous=True)
    pipeline.add_layer(role="cpu", handler=layer2_handler)
    pipeline.finish()

    orch = make_orchestrator()
    orch.feed([1, 2, 3, 4])

    assert wait_until(lambda: 2 in received and 4 in received, timeout=1.0)
    assert 1 not in received
    assert 3 not in received


def test_implicit_retry_batch_only_covers_frames_not_already_readied(pipeline, make_orchestrator):
    """task.retry(backoff) with no batch arg, called after a partial
    ready(), must only re-submit the frames that weren't already
    readied - not the whole original batch."""
    layer1_calls = []

    def layer1_handler(owner, task, batch):
        layer1_calls.append(sorted(f.userdata for f in batch))
        even = [f for f in batch if f.userdata % 2 == 0]
        task.ready(even)
        task.retry(50)

    received = []

    def layer2_handler(owner, task, batch):
        received.extend(f.userdata for f in batch)

    pipeline.add_layer(role="cpu", handler=layer1_handler, min_batch=4, max_batch=4, yield_after=0.2)
    pipeline.add_layer(role="cpu", handler=layer2_handler)
    pipeline.finish()

    orch = make_orchestrator()
    orch.feed([1, 2, 3, 4])

    assert wait_until(lambda: sorted(received) == [2, 4], timeout=1.0)
    assert wait_until(lambda: len(layer1_calls) >= 2, timeout=1.0)
    time.sleep(0.2)
    assert sorted(received) == [2, 4]
    assert layer1_calls[0] == [1, 2, 3, 4]
    assert sorted(layer1_calls[1]) == [1, 3]
