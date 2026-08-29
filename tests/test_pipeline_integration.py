import asyncio
import threading
import time

from tests.conftest import wait_until


def test_sync_handler_implicit_none_return_passes_everything(pipeline, make_orchestrator):
    received = []

    def layer1_handler(owner, task, batch):
        return None

    def layer2_handler(owner, task, batch):
        received.extend(f.userdata for f in batch)

    pipeline.add_layer(role="cpu", handler=layer1_handler)
    pipeline.add_layer(role="cpu", handler=layer2_handler)
    pipeline.finish()

    orch = make_orchestrator()
    orch.feed(list(range(6)))

    assert wait_until(lambda: len(received) == 6)
    assert sorted(received) == [0, 1, 2, 3, 4, 5]


def test_sync_handler_returning_subset_discards_the_rest(pipeline, make_orchestrator):
    received = []

    def layer1_handler(owner, task, batch):
        return [f for f in batch if f.userdata % 2 == 0]

    def layer2_handler(owner, task, batch):
        received.extend(f.userdata for f in batch)

    pipeline.add_layer(role="cpu", handler=layer1_handler)
    pipeline.add_layer(role="cpu", handler=layer2_handler)
    pipeline.finish()

    orch = make_orchestrator()
    orch.feed(list(range(10)))

    assert wait_until(lambda: len(received) >= 5)
    time.sleep(0.3)
    assert sorted(received) == [0, 2, 4, 6, 8]


def test_on_ready_receives_batches_for_frames_that_reach_finish(pipeline, make_orchestrator):
    def layer1_handler(owner, task, batch):
        return None

    pipeline.add_layer(role="cpu", handler=layer1_handler)
    pipeline.finish()

    orch = make_orchestrator()
    ready_batches = []
    orch.on_ready(lambda batch: ready_batches.append(batch))
    orch.feed(list(range(6)))

    assert wait_until(lambda: sum(len(b) for b in ready_batches) == 6)
    seen = sorted(f.index for b in ready_batches for f in b)
    assert seen == [0, 1, 2, 3, 4, 5]
    assert all(isinstance(b, list) for b in ready_batches)


def test_on_discard_receives_frames_dropped_via_task_discard(pipeline, make_orchestrator):
    def layer1_handler(owner, task, batch):
        keep = [f for f in batch if f.userdata < 3]
        drop = [f for f in batch if f.userdata >= 3]
        task.ready(keep)
        if drop:
            task.discard(drop)

    pipeline.add_layer(role="cpu", handler=layer1_handler)
    pipeline.finish()

    orch = make_orchestrator()
    ready_batches = []
    discard_batches = []
    orch.on_ready(lambda batch: ready_batches.append(batch))
    orch.on_discard(lambda batch: discard_batches.append(batch))
    orch.feed(list(range(6)))

    assert wait_until(
        lambda: sum(len(b) for b in ready_batches) + sum(len(b) for b in discard_batches) == 6
    )
    readied = sorted(f.index for b in ready_batches for f in b)
    discarded = sorted(f.userdata for b in discard_batches for f in b)
    assert readied == [0, 1, 2]
    assert discarded == [3, 4, 5]
    assert all(isinstance(b, list) for b in discard_batches)


def test_on_discard_receives_frames_dropped_via_task_abort(pipeline, make_orchestrator):
    def layer1_handler(owner, task, batch):
        task.abort()

    pipeline.add_layer(role="cpu", handler=layer1_handler)
    pipeline.finish()

    orch = make_orchestrator()
    discard_batches = []
    orch.on_discard(lambda batch: discard_batches.append(batch))
    orch.feed(list(range(4)))

    assert wait_until(lambda: sum(len(b) for b in discard_batches) == 4)
    assert sorted(f.userdata for b in discard_batches for f in b) == [0, 1, 2, 3]


def test_sync_handler_returning_empty_list_discards_everything(pipeline, make_orchestrator):
    received = []

    def layer1_handler(owner, task, batch):
        return []

    def layer2_handler(owner, task, batch):
        received.extend(f.userdata for f in batch)

    pipeline.add_layer(role="cpu", handler=layer1_handler)
    pipeline.add_layer(role="cpu", handler=layer2_handler)
    pipeline.finish()

    orch = make_orchestrator()
    orch.feed(list(range(5)))

    time.sleep(0.5)
    assert received == []


def test_task_ready_with_explicit_batch_overrides_return_value(pipeline, make_orchestrator):
    received = []

    def layer1_handler(owner, task, batch):
        task.ready([f for f in batch if f.userdata % 3 == 0])
        return batch

    def layer2_handler(owner, task, batch):
        received.extend(f.userdata for f in batch)

    pipeline.add_layer(role="cpu", handler=layer1_handler)
    pipeline.add_layer(role="cpu", handler=layer2_handler)
    pipeline.finish()

    orch = make_orchestrator()
    orch.feed(list(range(10)))

    assert wait_until(lambda: len(received) >= 4)
    time.sleep(0.3)
    assert sorted(received) == [0, 3, 6, 9]


def test_task_abort_discards_entire_batch(pipeline, make_orchestrator):
    received = []

    def layer1_handler(owner, task, batch):
        task.abort()
        return batch

    def layer2_handler(owner, task, batch):
        received.extend(f.userdata for f in batch)

    pipeline.add_layer(role="cpu", handler=layer1_handler)
    pipeline.add_layer(role="cpu", handler=layer2_handler)
    pipeline.finish()

    orch = make_orchestrator()
    orch.feed(list(range(5)))

    time.sleep(0.5)
    assert received == []


def test_async_handler_deferred_ready_with_subset(pipeline, make_orchestrator):
    received = []

    def layer1_handler(owner, task, batch):
        def deferred():
            time.sleep(0.1)
            task.ready([f for f in batch if f.userdata >= 5])

        threading.Thread(target=deferred, daemon=True).start()

    def layer2_handler(owner, task, batch):
        received.extend(f.userdata for f in batch)

    pipeline.add_layer(role="cpu", handler=layer1_handler, asynchronous=True)
    pipeline.add_layer(role="cpu", handler=layer2_handler)
    pipeline.finish()

    orch = make_orchestrator()
    orch.feed(list(range(10)))

    assert wait_until(lambda: len(received) >= 5, timeout=3.0)
    time.sleep(0.3)
    assert sorted(received) == [5, 6, 7, 8, 9]


def test_async_handler_deferred_abort_discards_everything(pipeline, make_orchestrator):
    received = []

    def layer1_handler(owner, task, batch):
        def deferred():
            time.sleep(0.1)
            task.abort()

        threading.Thread(target=deferred, daemon=True).start()

    def layer2_handler(owner, task, batch):
        received.extend(f.userdata for f in batch)

    pipeline.add_layer(role="cpu", handler=layer1_handler, asynchronous=True)
    pipeline.add_layer(role="cpu", handler=layer2_handler)
    pipeline.finish()

    orch = make_orchestrator()
    orch.feed(list(range(5)))

    time.sleep(1.0)
    assert received == []


def test_async_handler_never_calling_ready_never_forwards(pipeline, make_orchestrator):
    received = []

    def layer1_handler(owner, task, batch):
        pass

    def layer2_handler(owner, task, batch):
        received.extend(f.userdata for f in batch)

    pipeline.add_layer(role="cpu", handler=layer1_handler, asynchronous=True)
    pipeline.add_layer(role="cpu", handler=layer2_handler)
    pipeline.finish()

    orch = make_orchestrator()
    orch.feed(list(range(5)))

    time.sleep(0.5)
    assert received == []


def test_shared_layer_return_subset_discards_rest(pipeline, make_orchestrator):
    received = []

    def shared_handler(orchestrator, task, batch):
        return [f for f in batch if f.userdata % 2 == 1]

    def layer2_handler(owner, task, batch):
        received.extend(f.userdata for f in batch)

    pipeline.add_layer(role="cpu", handler=shared_handler, shared=True)
    pipeline.add_layer(role="cpu", handler=layer2_handler)
    pipeline.finish()

    orch = make_orchestrator()
    orch.feed(list(range(8)))

    assert wait_until(lambda: len(received) >= 4)
    time.sleep(0.3)
    assert sorted(received) == [1, 3, 5, 7]


def test_shared_layer_task_abort_discards_everything(pipeline, make_orchestrator):
    received = []

    def shared_handler(orchestrator, task, batch):
        task.abort()

    def layer2_handler(owner, task, batch):
        received.extend(f.userdata for f in batch)

    pipeline.add_layer(role="cpu", handler=shared_handler, shared=True)
    pipeline.add_layer(role="cpu", handler=layer2_handler)
    pipeline.finish()

    orch = make_orchestrator()
    orch.feed(list(range(5)))

    time.sleep(0.8)
    assert received == []


def test_discarded_frames_are_not_requeued_on_next_feed(pipeline, make_orchestrator):
    received = []

    def layer1_handler(owner, task, batch):
        return [f for f in batch if f.userdata % 2 == 0]

    def layer2_handler(owner, task, batch):
        received.extend(f.userdata for f in batch)

    pipeline.add_layer(role="cpu", handler=layer1_handler)
    pipeline.add_layer(role="cpu", handler=layer2_handler)
    pipeline.finish()

    orch = make_orchestrator()
    orch.feed([0, 1, 2, 3])

    assert wait_until(lambda: len(received) >= 2)
    time.sleep(0.3)
    assert sorted(received) == [0, 2]

    orch.feed([10, 11, 12, 13])

    assert wait_until(lambda: len(received) >= 4)
    time.sleep(0.3)
    assert sorted(received) == [0, 2, 10, 12]


def test_discard_still_works_with_min_batch_accumulation(pipeline, make_orchestrator):
    received = []

    def layer1_handler(owner, task, batch):
        return [f for f in batch if f.userdata % 2 == 0]

    def layer2_handler(owner, task, batch):
        received.extend(f.userdata for f in batch)

    pipeline.add_layer(role="cpu", handler=layer1_handler, min_batch=3, yield_after=0.3)
    pipeline.add_layer(role="cpu", handler=layer2_handler)
    pipeline.finish()

    orch = make_orchestrator()
    orch.feed([0, 1])
    time.sleep(0.1)
    orch.feed([2, 3])

    assert wait_until(lambda: len(received) >= 2, timeout=3.0)
    time.sleep(0.3)
    assert sorted(received) == [0, 2]


def test_discarding_a_frame_stalls_a_downstream_contiguous_layer(pipeline, make_orchestrator):
    received = []

    def layer1_handler(owner, task, batch):
        return [f for f in batch if f.userdata != 2]

    def layer2_handler(owner, task, batch):
        received.extend(f.userdata for f in batch)

    pipeline.add_layer(role="cpu", handler=layer1_handler, contiguous=True)
    pipeline.add_layer(role="cpu", handler=layer2_handler, contiguous=True)
    pipeline.finish()

    orch = make_orchestrator()
    orch.feed(list(range(6)))

    assert wait_until(lambda: len(received) >= 2, timeout=1.5)
    time.sleep(0.3)
    assert sorted(received) == [0, 1]


def test_coroutine_handler_auto_detected_and_forwards_result(pipeline, make_orchestrator):
    received = []

    async def layer1_handler(owner, task, batch):
        await asyncio.sleep(0.01)
        return [f for f in batch if f.userdata % 2 == 0]

    def layer2_handler(owner, task, batch):
        received.extend(f.userdata for f in batch)

    pipeline.add_layer(role="cpu", handler=layer1_handler)
    pipeline.add_layer(role="cpu", handler=layer2_handler)
    pipeline.finish()

    orch = make_orchestrator()
    orch.feed(list(range(10)))

    assert wait_until(lambda: len(received) >= 5, timeout=3.0)
    time.sleep(0.3)
    assert sorted(received) == [0, 2, 4, 6, 8]


def test_coroutine_handler_exception_aborts_task_without_orphaning_frames(pipeline, make_orchestrator):
    received = []
    discarded = []

    async def layer1_handler(owner, task, batch):
        await asyncio.sleep(0.01)
        raise RuntimeError("deliberate coroutine handler failure")

    def layer2_handler(owner, task, batch):
        received.extend(f.userdata for f in batch)

    pipeline.add_layer(role="cpu", handler=layer1_handler)
    pipeline.add_layer(role="cpu", handler=layer2_handler)
    pipeline.finish()

    orch = make_orchestrator()
    orch.on_discard(lambda batch: discarded.extend(f.userdata for f in batch))
    orch.feed(list(range(4)))

    assert wait_until(lambda: len(discarded) == 4, timeout=2.0)
    time.sleep(0.2)
    assert received == []
    assert sorted(discarded) == [0, 1, 2, 3]


def test_coroutine_handler_reuses_one_dedicated_loop_across_batches(pipeline, make_orchestrator):
    """Guards the fix for asyncio.run()'s per-call loop setup/teardown
    cost: a coroutine-handler layer must run every batch on the same
    long-lived event loop, not spin up a fresh one each time."""
    seen_loop_ids = []

    async def layer1_handler(owner, task, batch):
        seen_loop_ids.append(id(asyncio.get_running_loop()))
        return None

    pipeline.add_layer(role="cpu", handler=layer1_handler)
    pipeline.finish()

    orch = make_orchestrator()
    for i in range(5):
        orch.feed([i])
        time.sleep(0.05)

    assert wait_until(lambda: len(seen_loop_ids) == 5, timeout=3.0)
    assert len(set(seen_loop_ids)) == 1
