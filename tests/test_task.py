from nam.concurrency.orchestrator import OrchestrationFrame, Task


class RecordingLayer:
    def __init__(self):
        self.on_task_ready_calls = []
        self.on_task_discard_calls = []

    def _on_task_ready(self, batch):
        self.on_task_ready_calls.append(batch)

    def _on_task_discard(self, batch):
        self.on_task_discard_calls.append(batch)


def make_frames(count):
    return [OrchestrationFrame(owner=None, last_frame=None, index=i, userdata=i) for i in range(count)]


def test_ready_with_no_batch_forwards_everything():
    layer = RecordingLayer()
    frames = make_frames(3)
    task = Task(layer, owner=None, batch=frames)

    task.ready()

    assert layer.on_task_ready_calls == [frames]


def test_ready_with_subset_forwards_only_that_subset():
    layer = RecordingLayer()
    frames = make_frames(5)
    task = Task(layer, owner=None, batch=frames)

    kept = [frames[1], frames[3]]
    task.ready(kept)

    assert layer.on_task_ready_calls == [kept]


def test_ready_with_empty_batch_discards_everything():
    layer = RecordingLayer()
    frames = make_frames(4)
    task = Task(layer, owner=None, batch=frames)

    task.ready([])

    assert layer.on_task_ready_calls == [[]]


def test_ready_preserves_original_batch_order_regardless_of_subset_order():
    layer = RecordingLayer()
    frames = make_frames(5)
    task = Task(layer, owner=None, batch=frames)

    task.ready([frames[4], frames[0], frames[2]])

    assert layer.on_task_ready_calls == [[frames[0], frames[2], frames[4]]]


def test_ready_ignores_frames_not_in_original_batch():
    layer = RecordingLayer()
    frames = make_frames(3)
    foreign_frame = OrchestrationFrame(owner=None, last_frame=None, index=99, userdata=99)
    task = Task(layer, owner=None, batch=frames)

    task.ready([frames[0], foreign_frame])

    assert layer.on_task_ready_calls == [[frames[0]]]


def test_ready_streams_only_not_yet_resolved_frames_on_later_calls():
    layer = RecordingLayer()
    frames = make_frames(3)
    task = Task(layer, owner=None, batch=frames)

    task.ready([frames[0]])
    task.ready(frames)
    task.ready()

    assert layer.on_task_ready_calls == [[frames[0]], [frames[1], frames[2]]]


def test_ready_with_single_frame_forwards_single_element_batch():
    layer = RecordingLayer()
    frames = make_frames(3)
    task = Task(layer, owner=None, batch=frames)

    task.ready(frames[1])

    assert layer.on_task_ready_calls == [[frames[1]]]


def test_abort_discards_everything_and_never_calls_on_task_ready():
    layer = RecordingLayer()
    frames = make_frames(3)
    task = Task(layer, owner=None, batch=frames)

    task.abort()

    assert layer.on_task_ready_calls == []


def test_abort_after_ready_is_noop():
    layer = RecordingLayer()
    frames = make_frames(3)
    task = Task(layer, owner=None, batch=frames)

    task.ready(frames)
    task.abort()

    assert layer.on_task_ready_calls == [frames]


def test_ready_after_abort_is_noop():
    layer = RecordingLayer()
    frames = make_frames(3)
    task = Task(layer, owner=None, batch=frames)

    task.abort()
    task.ready(frames)

    assert layer.on_task_ready_calls == []


def test_settle_once_reports_first_caller_wins():
    layer = RecordingLayer()
    frames = make_frames(1)
    task = Task(layer, owner=None, batch=frames)

    assert task._settle_once() is True
    assert task._settle_once() is False
    assert task._settle_once() is False
