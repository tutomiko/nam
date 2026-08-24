import threading

from nam.concurrency.orchestrator import OrchestrationFrame, Task


class RecordingLayer:
    def __init__(self):
        self.on_task_ready_calls = []
        self._lock = threading.Lock()

    def _on_task_ready(self, batch):
        with self._lock:
            self.on_task_ready_calls.append(batch)


def make_frames(count):
    return [OrchestrationFrame(owner=None, last_frame=None, index=i, userdata=i) for i in range(count)]


def test_concurrent_ready_calls_only_one_settles():
    layer = RecordingLayer()
    frames = make_frames(20)
    task = Task(layer, owner=None, batch=frames)

    barrier = threading.Barrier(10)

    def worker(i):
        barrier.wait()
        task.ready([frames[i]])

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(layer.on_task_ready_calls) == 1


def test_concurrent_ready_and_abort_only_one_settles():
    layer = RecordingLayer()
    frames = make_frames(20)
    task = Task(layer, owner=None, batch=frames)

    barrier = threading.Barrier(2)
    results = {}

    def ready_worker():
        barrier.wait()
        results["ready_ran"] = True
        task.ready(frames)

    def abort_worker():
        barrier.wait()
        results["abort_ran"] = True
        task.abort()

    t1 = threading.Thread(target=ready_worker)
    t2 = threading.Thread(target=abort_worker)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(layer.on_task_ready_calls) <= 1
