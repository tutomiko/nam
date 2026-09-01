import threading

import pytest

from nam.concurrency import orchestrator as orchestrator_module
from nam.concurrency.orchestrator import OrchestrationFrame, Task


class RecordingLayer:
    def __init__(self):
        self.on_task_ready_calls = []
        self.on_task_discard_calls = []
        self.push_batch_calls = []

    def _on_task_ready(self, batch):
        self.on_task_ready_calls.append(batch)

    def _on_task_discard(self, batch):
        self.on_task_discard_calls.append(batch)

    def push_batch(self, batch):
        self.push_batch_calls.append(batch)


class FakeScheduler:
    """Records scheduled (delay, callback, args) instead of actually
    waiting, so retry-scheduling tests are deterministic and instant."""

    def __init__(self):
        self.scheduled = []

    def __call__(self, delay, callback, args=()):
        self.scheduled.append((delay, callback, args))

    def fire_all(self):
        pending = self.scheduled
        self.scheduled = []
        for delay, callback, args in pending:
            callback(*args)


def make_frames(count):
    return [OrchestrationFrame(owner=None, last_frame=None, index=i, userdata=i) for i in range(count)]


@pytest.fixture
def fake_scheduler(monkeypatch):
    scheduler = FakeScheduler()
    monkeypatch.setattr(orchestrator_module, "_execute_scheduler", scheduler)
    return scheduler


def test_retry_with_no_batch_schedules_whole_batch(fake_scheduler):
    layer = RecordingLayer()
    frames = make_frames(3)
    task = Task(layer, owner=None, batch=frames)

    task.retry(500)

    assert len(fake_scheduler.scheduled) == 1
    delay, callback, args = fake_scheduler.scheduled[0]
    assert delay == pytest.approx(0.5)
    assert callback == layer.push_batch
    assert args == (frames,)


def test_retry_does_not_forward_downstream_or_discard(fake_scheduler):
    layer = RecordingLayer()
    frames = make_frames(3)
    task = Task(layer, owner=None, batch=frames)

    task.retry(100)

    assert layer.on_task_ready_calls == []
    assert layer.on_task_discard_calls == []


def test_retry_actually_resubmits_into_the_layer_after_firing(fake_scheduler):
    layer = RecordingLayer()
    frames = make_frames(3)
    task = Task(layer, owner=None, batch=frames)

    task.retry(500)
    fake_scheduler.fire_all()

    assert layer.push_batch_calls == [frames]


def test_retry_converts_milliseconds_to_seconds(fake_scheduler):
    layer = RecordingLayer()
    frames = make_frames(1)
    task = Task(layer, owner=None, batch=frames)

    task.retry(1500)

    delay, _, _ = fake_scheduler.scheduled[0]
    assert delay == pytest.approx(1.5)


def test_retry_with_zero_backoff_schedules_immediately(fake_scheduler):
    layer = RecordingLayer()
    frames = make_frames(1)
    task = Task(layer, owner=None, batch=frames)

    task.retry(0)

    delay, _, _ = fake_scheduler.scheduled[0]
    assert delay == 0


def test_retry_with_subset_schedules_only_that_subset(fake_scheduler):
    layer = RecordingLayer()
    frames = make_frames(5)
    task = Task(layer, owner=None, batch=frames)

    subset = [frames[1], frames[3]]
    task.retry(200, subset)

    _, _, args = fake_scheduler.scheduled[0]
    assert args == (subset,)


def test_retry_with_single_frame_schedules_single_element_batch(fake_scheduler):
    layer = RecordingLayer()
    frames = make_frames(3)
    task = Task(layer, owner=None, batch=frames)

    task.retry(200, frames[1])

    _, _, args = fake_scheduler.scheduled[0]
    assert args == ([frames[1]],)


def test_retry_preserves_original_batch_order_regardless_of_subset_order(fake_scheduler):
    layer = RecordingLayer()
    frames = make_frames(5)
    task = Task(layer, owner=None, batch=frames)

    task.retry(200, [frames[4], frames[0], frames[2]])

    _, _, args = fake_scheduler.scheduled[0]
    assert args == ([frames[0], frames[2], frames[4]],)


def test_retry_ignores_foreign_frames_not_in_original_batch(fake_scheduler):
    """A frame that was never part of this task's batch at all (not
    merely already-claimed) is just filtered out silently - it's not a
    "double resolution" bug, there's nothing to warn about."""
    layer = RecordingLayer()
    frames = make_frames(3)
    foreign_frame = OrchestrationFrame(owner=None, last_frame=None, index=99, userdata=99)
    task = Task(layer, owner=None, batch=frames)

    task.retry(200, [frames[0], foreign_frame])

    _, _, args = fake_scheduler.scheduled[0]
    assert args == ([frames[0]],)


def test_retry_with_empty_subset_schedules_nothing(fake_scheduler):
    layer = RecordingLayer()
    frames = make_frames(3)
    task = Task(layer, owner=None, batch=frames)

    task.retry(200, [])

    assert fake_scheduler.scheduled == []


def test_retry_settles_the_task_so_a_later_ready_call_is_ignored(fake_scheduler):
    layer = RecordingLayer()
    frames = make_frames(3)
    task = Task(layer, owner=None, batch=frames)

    task.retry(200)
    task.ready(frames)

    assert layer.on_task_ready_calls == []


def test_ready_settles_the_task_so_a_later_retry_call_is_ignored(fake_scheduler):
    layer = RecordingLayer()
    frames = make_frames(3)
    task = Task(layer, owner=None, batch=frames)

    task.ready(frames)
    task.retry(200)

    assert fake_scheduler.scheduled == []


def test_retry_after_abort_is_noop(fake_scheduler):
    layer = RecordingLayer()
    frames = make_frames(3)
    task = Task(layer, owner=None, batch=frames)

    task.abort()
    task.retry(200)

    assert fake_scheduler.scheduled == []


def test_retry_then_ready_streams_remaining_frames(fake_scheduler):
    """Streaming contract: retry a subset first (settles the task), then
    ready() the rest - the remaining, not-yet-claimed frames should still
    be forwarded downstream normally."""
    layer = RecordingLayer()
    frames = make_frames(3)
    task = Task(layer, owner=None, batch=frames)

    task.retry(200, [frames[0]])
    task.ready([frames[1], frames[2]])

    _, _, args = fake_scheduler.scheduled[0]
    assert args == ([frames[0]],)
    assert layer.on_task_ready_calls == [[frames[1], frames[2]]]


def test_retry_then_discard_streams_remaining_frames(fake_scheduler):
    layer = RecordingLayer()
    frames = make_frames(3)
    task = Task(layer, owner=None, batch=frames)

    task.retry(200, [frames[0]])
    task.discard([frames[1], frames[2]])

    assert layer.on_task_discard_calls == [[frames[1], frames[2]]]


def test_ready_then_retry_streams_remaining_frames_into_scheduler(fake_scheduler):
    layer = RecordingLayer()
    frames = make_frames(3)
    task = Task(layer, owner=None, batch=frames)

    task.ready([frames[0]])
    task.retry(200, [frames[1], frames[2]])

    _, _, args = fake_scheduler.scheduled[0]
    assert args == ([frames[1], frames[2]],)


def test_retry_can_be_called_multiple_times_for_disjoint_subsets(fake_scheduler):
    layer = RecordingLayer()
    frames = make_frames(4)
    task = Task(layer, owner=None, batch=frames)

    task.retry(100, [frames[0]])
    task.retry(200, [frames[1]])
    task.retry(300, [frames[2], frames[3]])

    delays_and_args = [(d, a) for d, _, a in fake_scheduler.scheduled]
    assert delays_and_args == [
        (0.1, ([frames[0]],)),
        (0.2, ([frames[1]],)),
        (0.3, ([frames[2], frames[3]],)),
    ]


def test_retry_warns_and_filters_when_naming_an_already_retried_frame(fake_scheduler, capsys):
    """Second retry() naming an already-claimed frame must warn and must
    not schedule a duplicate re-submission for the same frame."""
    layer = RecordingLayer()
    frames = make_frames(3)
    task = Task(layer, owner=None, batch=frames)

    task.retry(100, [frames[0]])
    task.retry(100, [frames[0]])

    scheduled_args = [args for _, _, args in fake_scheduler.scheduled]
    assert scheduled_args == [([frames[0]],)]
    assert "[Orchestrator]" in capsys.readouterr().out


def test_retry_warns_when_naming_an_already_readied_frame(fake_scheduler, capsys):
    layer = RecordingLayer()
    frames = make_frames(3)
    task = Task(layer, owner=None, batch=frames)

    task.ready([frames[0]])
    task.retry(100, [frames[0], frames[1]])

    _, _, args = fake_scheduler.scheduled[0]
    assert args == ([frames[1]],)
    assert "[Orchestrator]" in capsys.readouterr().out


def test_retry_warns_when_naming_an_already_discarded_frame(fake_scheduler, capsys):
    layer = RecordingLayer()
    frames = make_frames(3)
    task = Task(layer, owner=None, batch=frames)

    task.discard([frames[0]])
    task.retry(100, [frames[0], frames[1]])

    _, _, args = fake_scheduler.scheduled[0]
    assert args == ([frames[1]],)
    assert "[Orchestrator]" in capsys.readouterr().out


def test_retry_with_no_batch_only_retries_unclaimed_frames(fake_scheduler):
    """batch=None means "whatever hasn't been readied/discarded/retried
    yet", not "the whole original batch" - this differs from ready()'s
    and discard()'s batch=None meaning."""
    layer = RecordingLayer()
    frames = make_frames(4)
    task = Task(layer, owner=None, batch=frames)

    task.ready([frames[0]])
    task.discard([frames[1]])
    task.retry(200)

    _, _, args = fake_scheduler.scheduled[0]
    assert args == ([frames[2], frames[3]],)


def test_retry_with_no_batch_and_nothing_unclaimed_schedules_nothing(fake_scheduler):
    layer = RecordingLayer()
    frames = make_frames(2)
    task = Task(layer, owner=None, batch=frames)

    task.ready(frames)
    task.retry(200)

    assert fake_scheduler.scheduled == []


def test_retry_with_no_batch_and_no_prior_claims_retries_everything(fake_scheduler):
    layer = RecordingLayer()
    frames = make_frames(3)
    task = Task(layer, owner=None, batch=frames)

    task.retry(200)

    _, _, args = fake_scheduler.scheduled[0]
    assert sorted(args[0], key=lambda f: f.index) == frames


def test_retry_with_no_batch_never_warns_even_after_partial_claims(fake_scheduler, capsys):
    """Implicit batch=None only ever asks for what's still unclaimed, so
    it should never trigger the already-resolved warning."""
    layer = RecordingLayer()
    frames = make_frames(3)
    task = Task(layer, owner=None, batch=frames)

    task.ready([frames[0]])
    task.retry(200)

    assert "[Orchestrator]" not in capsys.readouterr().out


def test_concurrent_retry_calls_each_frame_scheduled_exactly_once(fake_scheduler):
    layer = RecordingLayer()
    frames = make_frames(20)
    task = Task(layer, owner=None, batch=frames)

    barrier = threading.Barrier(10)

    def worker(i):
        barrier.wait()
        task.retry(50, [frames[i]])

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    scheduled_frames = [f for _, _, args in fake_scheduler.scheduled for f in args[0]]
    assert sorted(scheduled_frames, key=lambda f: f.index) == frames[:10]
    assert len(scheduled_frames) == 10


def test_concurrent_retry_and_abort_never_double_resolves_a_frame(fake_scheduler):
    layer = RecordingLayer()
    frames = make_frames(20)
    task = Task(layer, owner=None, batch=frames)

    barrier = threading.Barrier(2)

    def retry_worker():
        barrier.wait()
        task.retry(50, frames)

    def abort_worker():
        barrier.wait()
        task.abort()

    t1 = threading.Thread(target=retry_worker)
    t2 = threading.Thread(target=abort_worker)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    retried = [f for _, _, args in fake_scheduler.scheduled for f in args[0]]
    discarded = [f for batch in layer.on_task_discard_calls for f in batch]

    assert set(retried).isdisjoint(discarded)
    assert sorted(retried + discarded, key=lambda f: f.index) == frames
