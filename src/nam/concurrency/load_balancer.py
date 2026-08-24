import threading
import time
import queue
import atexit


_LOAD_TYPE_CPU = "cpu"
_LOAD_TYPE_DISK = "disk"
_LOAD_TYPE_GPU = "gpu"
_LOAD_TYPE_NETWORK = "network"

# How fast the per-source EMA adapts to new samples. Higher = more reactive
# to recent runs, lower = smoother/more historical. 0.2 is a reasonable
# default: ~5 samples to mostly wash out an old estimate, but no single
# sample can swing it wildly (unlike a plain running mean, which either
# weights everything equally forever or, if naively recomputed as
# `(old + new) / 2`, effectively *overweights* the most recent sample).
_EMA_ALPHA = 0.2

# Every this many seconds, a task's effective scheduling weight is reduced
# by _AGING_DECAY, in effect increasing its priority. This guarantees a
# task can only be starved a bounded number of ticks before it becomes the
# cheapest thing to schedule, regardless of what else shows up later.
_AGING_INTERVAL_SEC = 0.5
_AGING_DECAY = 0.85

# Minimum per-unit weight assumed for a source with no history yet, so a
# brand-new source doesn't get treated as "free" (weight 0) and flood a
# partition before any timing data exists.
_DEFAULT_UNIT_WEIGHT_NS = 1_000_000  # 1ms/unit


class LoadBalancerPartition:
    def __init__(self, load_type):
        self.load_type = load_type
        self._queue = queue.Queue()
        self._congestion = 0.0
        self._lock = threading.Lock()

    def add_congestion(self, weight):
        with self._lock:
            self._congestion += weight

    def get_congestion(self):
        with self._lock:
            return self._congestion

    def pull(self):
        try:
            return self._queue.get(block=False)
        except queue.Empty:
            return None

    def peek_size(self):
        return self._queue.qsize()

    def post(self, task):
        self._queue.put(task)

    def reduce_congestion(self, weight):
        with self._lock:
            self._congestion -= weight


class LoadBalancerTask:
    def __init__(self, source, batch_size, function, args):
        self.source = source
        self.batch_size = batch_size
        self.function = function
        self.args = args
        self.enqueued_at = time.monotonic()
        # Weight is snapshotted once, at construction time, rather than
        # recomputed live from source.get_weight() on every call. The
        # source's EMA weight is mutated by _update_weight() as soon as
        # this very task finishes (before congestion is reduced), so a
        # live-computed get_weight() would return a DIFFERENT number for
        # the add_congestion() at dispatch vs. the reduce_congestion() at
        # completion -- the two would never exactly cancel out, and
        # congestion would drift (up or down) with every completed task.
        self._weight_snapshot = source.get_weight() * batch_size

    def get_weight(self):
        return self._weight_snapshot

    def get_expected_duration_ns(self):
        return self._weight_snapshot

    def get_wait_seconds(self):
        return time.monotonic() - self.enqueued_at

    def get_effective_priority_weight(self):
        """
        The value used to pick which task to run next: lower = runs sooner.

        Starts at the task's expected cost (bigger/slower tasks are
        naturally deprioritized versus small quick ones, so the balancer
        favors throughput) but decays the longer the task waits, so an
        old task eventually becomes cheaper than any freshly-arrived task,
        no matter how large that new task's queue/batch is. This bounds
        worst-case wait time instead of allowing indefinite starvation.
        """
        base = max(self.get_expected_duration_ns(), 1.0)
        ticks_waited = self.get_wait_seconds() / _AGING_INTERVAL_SEC
        decay = _AGING_DECAY ** ticks_waited
        return base * decay


class LoadBalancerSourceRecord:
    def __init__(self, source):
        self.source = source
        self.tasks_completed = 0
        self.total_duration_ns = 0

    def record_completion(self, batch_size, duration_ns):
        self.tasks_completed += 1
        self.total_duration_ns += duration_ns


class LoadBalancer:
    def __init__(self, executor):
        self._executor = executor
        self._partition_cpu = LoadBalancerPartition(_LOAD_TYPE_CPU)
        self._partition_disk = LoadBalancerPartition(_LOAD_TYPE_DISK)
        self._partition_gpu = LoadBalancerPartition(_LOAD_TYPE_GPU)
        self._partition_network = LoadBalancerPartition(_LOAD_TYPE_NETWORK)
        self._partitions = [
            self._partition_cpu,
            self._partition_disk,
            self._partition_gpu,
            self._partition_network,
        ]
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._sources = {}
        self._sources_lock = threading.Lock()
        self._wake = threading.Event()
        self._shutdown = False

    def _dispatch_task(self, partition, task):
        # Congestion must be added the moment we *decide* to dispatch this
        # task, not when a worker thread actually gets around to running
        # it. self._executor.call() only enqueues the stub - there can be
        # a real gap (executor's own queue backlog, thread scheduling,
        # etc.) between "dispatched" and "running". If we waited to add
        # congestion until inside the stub, the partition would look
        # falsely idle during that gap and the next scheduler tick could
        # pile even more work onto a partition that's already fully
        # loaded, just not yet executing.
        partition.add_congestion(task.get_weight())

        def _task_stub():
            task_disp = time.time_ns()

            try:
                task.function(*task.args)
            finally:
                duration_ns = time.time_ns() - task_disp
                task.source._update_weight(task.batch_size, duration_ns)

                with self._sources_lock:
                    record = self._sources.get(task.source)
                if record is not None:
                    record.record_completion(task.batch_size, duration_ns)

                partition.reduce_congestion(task.get_weight())

                # Something finished -> capacity freed up, worth
                # re-checking partitions immediately rather than waiting
                # for the next poll interval.
                self._wake.set()

        self._executor.call(_task_stub)

    def _get_partition_for_type(self, load_type):
        if load_type == _LOAD_TYPE_CPU:
            return self._partition_cpu
        if load_type == _LOAD_TYPE_DISK:
            return self._partition_disk
        if load_type == _LOAD_TYPE_GPU:
            return self._partition_gpu
        if load_type == _LOAD_TYPE_NETWORK:
            return self._partition_network
        return None

    def _drain_partition_in_priority_order(self, partition):
        """
        Pop every task currently sitting in the partition's queue and
        return ALL of them, sorted by effective priority weight (lowest
        first - i.e. cheapest-adjusted-for-age runs first).

        Every tick should dispatch as much eligible work as exists per
        partition, not just one task: dispatching a single task per tick
        caps a partition's throughput at "1 task per wake cycle" even
        when the executor has plenty of idle worker capacity, which is
        especially visible (and especially bad) when only one load type
        is in use - other partitions can no longer mask the stall by
        happening to generate extra wake-ups.

        Sorting here (rather than picking one best-of) is still what
        provides the anti-starvation behavior: an old, aged task sorts
        ahead of a freshly-arrived big-batch task within the same
        partition, so it can't be starved out - but nothing stops the
        rest of the queue from being dispatched in the same tick too.
        """
        pending = []
        while True:
            task = partition.pull()
            if task is None:
                break
            pending.append(task)

        pending.sort(key=lambda t: t.get_effective_priority_weight())
        return pending

    def _run(self):
        while not self._shutdown:
            self._wake.wait(timeout=_AGING_INTERVAL_SEC)
            self._wake.clear()

            tasks = []

            for partition in self._partitions:
                for task in self._drain_partition_in_priority_order(partition):
                    tasks.append((partition, task))

            if not tasks:
                continue

            for partition, task in tasks:
                self._dispatch_task(partition, task)

    def register(self, source):
        with self._sources_lock:
            self._sources[source] = LoadBalancerSourceRecord(source)
        source._partition = self._get_partition_for_type(source._load_type)
        source._balancer = self

    def start(self):
        self._thread.start()
        atexit.register(self.shutdown)

    def shutdown(self):
        self._shutdown = True
        self._wake.set()

    def notify(self):
        """Allow sources / external callers to wake the scheduler early."""
        self._wake.set()


class LoadBalancerSource:
    def __init__(self, load_type):
        self._load_type = load_type
        self._partition = None
        self._balancer = None
        # Per-unit-of-batch-size EMA of duration in nanoseconds.
        self._unit_weight = float(_DEFAULT_UNIT_WEIGHT_NS)
        self._weight_lock = threading.Lock()

    def get_weight(self):
        """Expected nanoseconds per unit of batch size."""
        with self._weight_lock:
            return self._unit_weight

    def _update_weight(self, batch_size, duration_ns):
        if batch_size <= 0:
            return

        sample_unit_weight = duration_ns / batch_size

        with self._weight_lock:
            self._unit_weight = (
                (1.0 - _EMA_ALPHA) * self._unit_weight
                + _EMA_ALPHA * sample_unit_weight
            )

    def call(self, batch_size, task, args=()):
        if self._partition is None:
            raise RuntimeError(
                "LoadBalancerSource must be registered with a LoadBalancer "
                "before calling call()"
            )
        self._partition.post(LoadBalancerTask(self, batch_size, task, args))
        if self._balancer is not None:
            self._balancer.notify()