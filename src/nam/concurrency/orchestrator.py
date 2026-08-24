import time
import threading
import heapq
import queue
import traceback
import atexit

from nam import concurrency as _concurrency_pkg
from .load_balancer import LoadBalancer, LoadBalancerSource

_DEFAULT_YIELD = 1.0

if _concurrency_pkg.executor is None:
    raise RuntimeError(
        "nam.concurrency.initialize(thread_count) must be called before "
        "nam.concurrency.orchestrator is imported - server.py does this at "
        "startup, before any module that builds an OrchestrationPipeline."
    )

_orchestration_executor = _concurrency_pkg.executor

_LOAD_BALANCER = LoadBalancer(_orchestration_executor)
_LOAD_BALANCER.start()

_LOAD_BALANCER_DISPATCHER = LoadBalancerSource("cpu")

_LOAD_BALANCER.register(_LOAD_BALANCER_DISPATCHER)


class _DedicatedScheduler:
    """
    A single-threaded, non-blocking task scheduler.
    Replaces the need to spawn a new thread for every delayed task.
    """
    def __init__(self):
        self._tasks = []  # Min-heap to keep the soonest task at index 0
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._shutdown = False
        
        # Start the single daemon thread immediately
        self._thread = threading.Thread(target=self._run, daemon=True, name="OrchestratorScheduler")
        self._thread.start()
        atexit.register(self.shutdown)
        
    def schedule(self, delay, callback, args=()):
        run_at = time.monotonic() + delay
        
        with self._condition:
            # Push the task onto the heap sorted by execution time
            # Using id(callback) ensures no comparison errors if times are identical
            heapq.heappush(self._tasks, (run_at, id(callback), callback, args))
            
            # Wake up the background thread in case this new task needs to run 
            # sooner than whatever it is currently sleeping for.
            self._condition.notify()
            
    def shutdown(self):
        with self._condition:
            self._shutdown = True
            self._condition.notify_all()
            
    def _run(self):
        while not self._shutdown:
            task_to_run = None
            
            try:
                with self._condition:
                    if not self._tasks:
                        # Nothing to do, sleep until notified
                        self._condition.wait()
                        if self._shutdown:
                            break
                        continue
                    
                    now = time.monotonic()
                    run_at = self._tasks[0][0]
                    
                    if now >= run_at:
                        # Task is ready! Pop it off the heap.
                        task_to_run = heapq.heappop(self._tasks)
                    else:
                        # Sleep until the next task is ready, OR until a new task wakes us up
                        self._condition.wait(timeout=run_at - now)
                        if self._shutdown:
                            break
                        
                # Execute outside the lock so long-running callbacks don't freeze the scheduler
                if task_to_run:
                    _, _, callback, args = task_to_run
                    try:
                        callback(*args)
                    except Exception as e:
                        # Prevent a bad callback from killing your single scheduler thread
                        print(f"[Scheduler] Uncaught error in callback {callback.__name__}: {e}")
            except Exception:
                # This is the scheduler's own loop machinery (heap/condition
                # handling), as opposed to a callback's exception above. This
                # must never be allowed to escape and kill self._thread -
                # every future scheduled task would silently stop firing for
                # the rest of the process's life with no visible error.
                traceback.print_exc()

# Instantiate the global singleton
_global_scheduler = _DedicatedScheduler()

def _execute_scheduler(yield_after, callback, args=()):
    """
    Drop-in replacement for the original function. 
    Routes all delays through the single dedicated background thread.
    """
    _global_scheduler.schedule(yield_after, callback, args)


class OrchestrationFrame:
    def __init__(self, owner, last_frame, index, userdata):
        self.owner = owner
        self.last_frame = last_frame
        self.index = index
        self.userdata = userdata

    def __str__(self):
        if self.last_frame:
            return "F"+str(self.last_frame.index)+"-"+str(self.index)+""
        return "F"+str(self.index)+""

    def __lt__(self, other):
        return self.index < other.index
        
    def __repr__(self):
        if self.last_frame:
            return "F"+str(self.last_frame.index)+"-"+str(self.index)+""
        return "F"+str(self.index)+""


class Task:
    def __init__(self, layer, owner, batch):
        self._layer = layer
        self._owner = owner
        self._batch = batch
        self._ready_lock = threading.Lock()
        self._readied = False

    def ready(self):
        with self._ready_lock:
            if self._readied:
                return
            self._readied = True

        self._layer._on_task_ready(self._batch)


class OrchestrationLayer:
    def __init__(self, pipeline, executor, index, handler, max_batch, min_batch, contiguous, yield_after, shared, role, is_async=False):
        self._pipeline = pipeline
        self._backlog = {}
        self._backlog_lock = threading.Lock()
        self._executor = executor
        self._index = index
        self._handler = handler
        self._next = None
        self._queue = queue.Queue()
        self._queue_max = max_batch
        self._batch_pending_since = None
        self._batch_pending_lock = threading.Lock()
        self._batch_min = min_batch
        self._contiguous = contiguous
        self._yield_after = yield_after
        self._shared = shared
        self._is_async = is_async
        self._lb_source = LoadBalancerSource(role)
        
        _LOAD_BALANCER.register(self._lb_source)

    def _on_task_ready(self, batch):
        if self._next and len(batch) > 0:
            self._next.push_batch(batch)
        
    def __str__(self):
        return "L"+str(self._index)

    def __lt__(self, other):
        return self._index < other._index
        
    def __repr__(self):
        return "L"+str(self._index)
        
    def _aggregate_batch_to_owners(self, batch):
        batch_aggregates = {}
        for f in batch:
            if not f.owner in batch_aggregates:
                batch_aggregates[f.owner] = [f]
            else:
                batch_aggregates[f.owner].append(f)
        return batch_aggregates
        
    def _clear_batch(self, owner):
        with self._backlog_lock:
            if owner in self._backlog:
                del self._backlog[owner]
            
    def _ensure_batch(self, owner, batch, max_acceptable):
        if self._contiguous is not True:
            return min(max_acceptable, len(batch))
        
        with self._backlog_lock:
            backlog = -1
            
            if owner in self._backlog:
                backlog = self._backlog[owner]
            else:
                self._backlog[owner] = backlog
            
            batch_region = 0
            
            for i in range(0, min(max_acceptable, len(batch))):
                f = batch[i]
                
                if f.index == backlog + 1:
                    backlog = f.index
                    batch_region += 1
                else:
                    break
            
            self._backlog[owner] = backlog
            
            return batch_region
        
    def _execute_batch(self, batch):
        batch_proceed = []
        
        if self._shared is True:
            task = Task(self, None, batch)
            try:
                batch_accepted = self._handler(self._pipeline, task, batch)
                
                if self._is_async:
                    return
                
                if batch_accepted:
                    batch_proceed = batch
            except Exception as ex:
                traceback.print_exc()
        else:
            batch_aggregates = self._aggregate_batch_to_owners(batch)
            
            for batch_owner in batch_aggregates.keys():
                batch_aggregate = sorted(batch_aggregates[batch_owner])
                batch_accepted = False
                task = Task(self, batch_owner, batch_aggregate)
                
                try:
                    batch_accepted = self._handler(batch_owner, task, batch_aggregate)
                except Exception as ex:
                    traceback.print_exc()
                
                if self._is_async:
                    continue
                    
                if batch_accepted is None:
                    batch_accepted = True
                
                if batch_accepted:
                    batch_proceed.extend(batch_aggregate)
            
            if self._is_async:
                return
            
        if self._next and len(batch_proceed) > 0:
            self._next.push_batch(batch_proceed)
        
    def pull_batch(self):
        batch = []
        
        try:
            for i in range(0, self._queue_max):
                f = self._queue.get(block=False)
                if f is None:
                    break
                batch.append(f)
        except queue.Empty:
            pass
            
        if len(batch) == 0:
            return
            
        if len(batch) < self._batch_min:
            with self._batch_pending_lock:
                if self._batch_pending_since is None or (not self._yield_after or (time.time() - self._batch_pending_since < self._yield_after)):
                    if self._batch_pending_since is None:
                        self._batch_pending_since = time.time()
                    
                    for f in batch:
                        self._queue.put(f)
                    _execute_scheduler(self._yield_after, _LOAD_BALANCER_DISPATCHER.call, (1, self.pull_batch,))
                    return
                else:
                    self._batch_pending_since = None
        else:
            # ADDED: Clear the timestamp when a healthy batch passes
            with self._batch_pending_lock:
                self._batch_pending_since = None
        
        self._lb_source.call(len(batch), self._execute_batch, (batch,))
        
    def push_batch(self, batch):
        collected_max = self._queue_max - self._queue.qsize()
        collected_max = min(collected_max, len(batch))
        
        batch_aggregates = self._aggregate_batch_to_owners(batch)
        batch_leftovers = []
        
        accepted_batch = []
        remaining_capacity = collected_max
        
        for batch_owner in batch_aggregates.keys():
            batch_aggregate = sorted(batch_aggregates[batch_owner])
            
            # Strictly bound the accepted region by remaining capacity
            batch_region = self._ensure_batch(batch_owner, batch_aggregate, remaining_capacity)
            
            # Correctly separate leftovers without overlapping
            batch_leftovers.extend(batch_aggregate[batch_region:])
            
            for i in range(0, batch_region):
                accepted_batch.append(batch_aggregate[i])
                
            remaining_capacity -= batch_region
                
        accepted_batch = sorted(accepted_batch)
        
        for f in accepted_batch:
            self._queue.put(f)
            
        batch_size = len(accepted_batch)
        
        if batch_size > 0:
            _LOAD_BALANCER_DISPATCHER.call(1, self.pull_batch)
            
        if len(batch_leftovers) > 0:
            # FIX: was `_LOAD_BALANCER_DISPATCHER` (the LoadBalancerSource
            # object itself, which isn't callable) - must be `.call`, the
            # bound method, exactly like every other _execute_scheduler
            # call site in this file. The scheduler invokes this as
            # `callback(*args)`; passing the bare source raised
            # `TypeError: 'LoadBalancerSource' object is not callable`
            # inside the scheduler thread every time a batch overflowed
            # capacity, silently dropping those leftover frames forever
            # (the scheduler's per-callback try/except logs it and moves
            # on, so nothing crashed - the frames just vanished).
            _execute_scheduler(self._yield_after, _LOAD_BALANCER_DISPATCHER.call, (1, self.push_batch, (batch_leftovers,)))
     
     
class OrchestrationPipeline:
    def __init__(self, executor):
        self._chain_head = None
        self._chain_tail = None
        self._chain_size = 0
        self._executor = executor
        
    def add_layer(self, role, handler, contiguous=False, min_batch=1, max_batch=256, yield_after=_DEFAULT_YIELD, shared=False, async_=False):
        layer = OrchestrationLayer(self, self._executor, self._chain_size, handler, max_batch, min_batch, contiguous, yield_after, shared, role, is_async=async_)
        
        if self._chain_head is None:
            self._chain_head = layer
            self._chain_tail = layer
        else:
            self._chain_tail._next = layer
            self._chain_tail = layer
            
        self._chain_size += 1
        
    def feed(self, batch):
        layer = self._chain_head
        
        layer.push_batch(batch)
        
    def finish(self):
        def _handler(orchestrator, task, batch):
            try:
                for f in batch:
                    if not f.last_frame:
                        continue
                    f.last_frame.userdata = None
                    f.last_frame = None
            finally:
                orchestrator._tear_window(batch)
            
        self.add_layer(handler=_handler, role='cpu', max_batch=256, contiguous=True)
        
    def limit(self):
        layer = self._chain_head
        
        return layer._queue_max - layer._queue.qsize()
        
    def teardown(self, owner):
        chain_link = self._chain_head
        
        while chain_link:
            chain_link._clear_batch(owner)
            
            chain_link = chain_link._next
        
        
class Orchestrator:
    def __init__(self, pipeline, max_window):
        self._closed = False
        self._pipeline = pipeline
        self._max_window = max_window
        self._queue_interim = queue.Queue()
        self._window_lock = threading.Lock()
        self._window_size = 0
        self._window_ends = None
        self._window_logt = 0
        self.userdata = None
        self.wrapper = None
        self.window_listener = None
        
    def close(self):
        try:
            self._pipeline.teardown(self)
        finally:
            self._closed = True
            
    def _create_frame(self, last_frame, index, userdata):
        if self.wrapper:
            userdata = self.wrapper(index, userdata)
            
        return OrchestrationFrame(self, last_frame, index, userdata)
        
    def _pull_window(self):
        collected_max = 0
        collected_max = self._pipeline.limit()
        
        if collected_max == 0:
            _execute_scheduler(_DEFAULT_YIELD, _LOAD_BALANCER_DISPATCHER.call, (1, self._pull_window,))
            return
        
        fp = None
        
        with self._window_lock:
            collected_max = min(collected_max, self._max_window - self._window_size)
            fp = self._window_ends
            
            if collected_max > 0:
                self._window_size += collected_max
            
        if collected_max <= 0:
            _execute_scheduler(_DEFAULT_YIELD, _LOAD_BALANCER_DISPATCHER.call, (1, self._pull_window,))
            return
            
        collected = []
        
        with self._window_lock:
            try:
                for offset in range(0, collected_max):
                    index = offset + self._window_logt
                    d = self._queue_interim.get(block=False)
                    if d is None:
                        break
                    f = self._create_frame(fp, index, d)
                    fp = f
                    collected.append(f)
            except queue.Empty:
                pass
            
            # MOVED: Always increments, even if the queue empties early
            self._window_logt += len(collected)
        
        with self._window_lock:
            self._window_ends = fp
            
            if len(collected) < collected_max:
                self._window_size -= collected_max - len(collected)
                
        if len(collected) > 0:
            self._pipeline.feed(collected)
            
            # Keep the pump running to clear the interim queue.
            # If the window is full on the next run, it will automatically 
            # fall back to your _execute_scheduler polling.
            _LOAD_BALANCER_DISPATCHER.call(1, self._pull_window)   
            
    def _tear_window(self, batch):
        free_room = 0
        
        with self._window_lock:
            self._window_size -= len(batch)
            
            free_room = self._window_size
            
        if self.window_listener is not None:
            try:
                self.window_listener(free_room)
            finally:
                pass
            
    def feed(self, batch):
        for i in range(0, len(batch)):
            self._queue_interim.put(batch[i])
            
        _LOAD_BALANCER_DISPATCHER.call(1, self._pull_window)