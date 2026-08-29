import time
import threading
import heapq
import queue
import traceback
import atexit
import asyncio
import inspect

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
    def __init__(self, owner=None, last_frame=None, index=None, userdata=None):
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
    """Represents one pending resolution of a batch as it passes through a
    layer. A task can be resolved two ways:

    - Single-shot (the historical behavior): the handler calls ready(),
      ready(subset), or abort() exactly once before returning. Whatever
      wasn't explicitly named is silently dropped, matching the original
      "return value / one ready() call" contract.
    - Streaming: the handler (typically an async one) calls ready(frame)
      and/or discard(frame) repeatedly, from any thread, as individual
      frames finish - each call reports only the frames it names, and the
      task is fully resolved once every frame in the batch has been
      claimed by some ready() or discard() call.

    The first call of either kind still blocks the layer's return-value
    fallback (_settle_once), matching the pre-existing single-shot
    contract; later streaming calls simply keep draining _remaining.
    """

    def __init__(self, layer, owner, batch):
        self._layer = layer
        self._owner = owner
        self._batch = batch
        self._settle_lock = threading.Lock()
        self._settled = False
        self._remaining = set(batch)

    def _as_frame_list(self, frame_or_batch):
        if isinstance(frame_or_batch, OrchestrationFrame):
            return [frame_or_batch]
        return list(frame_or_batch)

    def _settle_once(self):
        with self._settle_lock:
            if self._settled:
                return False
            self._settled = True
            return True

    def _claim_remaining(self, frames):
        with self._settle_lock:
            claimed = set(frames) & self._remaining
            self._remaining -= claimed
            return claimed

    def ready(self, batch=None):
        if not self._settle_once():
            if batch is None:
                return
            claimed = self._claim_remaining(self._as_frame_list(batch))
            if not claimed:
                return
            self._layer._on_task_ready([f for f in self._batch if f in claimed])
            return

        if batch is None:
            self._remaining.clear()
            self._layer._on_task_ready(self._batch)
            return

        requested_frames = set(self._as_frame_list(batch))
        self._remaining -= requested_frames
        surviving_batch = [f for f in self._batch if f in requested_frames]
        self._layer._on_task_ready(surviving_batch)

    def discard(self, batch=None):
        frames = self._batch if batch is None else self._as_frame_list(batch)

        if not self._settle_once():
            claimed = self._claim_remaining(frames)
            if not claimed:
                return
            self._layer._on_task_discard([f for f in self._batch if f in claimed])
            return

        requested_frames = set(frames)
        self._remaining -= requested_frames
        discarded_batch = [f for f in self._batch if f in requested_frames]
        if discarded_batch:
            self._layer._on_task_discard(discarded_batch)

    def abort(self):
        self.discard(self._batch)


class _LayerEventLoopThread:
    """One dedicated, long-lived event loop for a single coroutine-handler
    OrchestrationLayer, mirroring the _DedicatedScheduler/LoadBalancer
    pattern already used elsewhere in this file: one background thread per
    concern, owned by an object, started once, alive for the process's
    life.

    This exists specifically to avoid asyncio.run()'s per-call loop
    setup/teardown cost. The alternative of caching a loop on
    threading.local() was considered and rejected: Executor's worker
    threads are shared across every LoadBalancerSource in the process
    (every layer of every pipeline, every load-type partition), so a
    thread-local loop would be created lazily by whichever layer happens
    to land on a given thread first, then live forever pinned to that
    thread with no owner and no close() hook - a leaked event loop per
    worker thread, invisible until process shutdown. Scoping the loop to
    the layer instead means exactly one loop per coroutine-handler layer,
    with a clear owner and place to add a shutdown hook, and zero effect
    on sync-handler layers, which never construct one of these.
    """

    def __init__(self):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="OrchestratorLayerEventLoop"
        )
        self._thread.start()

    def run(self, coro):
        # run_coroutine_threadsafe schedules the coroutine on the
        # already-running loop and hands back a concurrent.futures.Future;
        # .result() blocks the calling worker thread until it's done,
        # exactly like asyncio.run() does today - the only change is that
        # the loop itself is created once, up front, instead of per call.
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()


class OrchestrationLayer:
    def __init__(self, pipeline, executor, index, handler, max_batch, min_batch, contiguous, yield_after, shared, role, is_async=False):
        self._pipeline = pipeline
        self._backlog = {}
        self._backlog_lock = threading.Lock()
        self._executor = executor
        self._index = index
        self._handler = handler
        self._handler_is_coroutine = inspect.iscoroutinefunction(handler)
        self._handler_event_loop = _LayerEventLoopThread() if self._handler_is_coroutine else None
        self._next = None
        self._queue = queue.Queue()
        self._queue_max = max_batch
        self._batch_pending_since = None
        self._batch_pending_lock = threading.Lock()
        self._batch_min = min_batch
        self._queue_capacity_lock = threading.Lock()
        self._contiguous = contiguous
        self._yield_after = yield_after
        self._shared = shared
        self._is_async = is_async
        self._lb_source = LoadBalancerSource(role)
        
        _LOAD_BALANCER.register(self._lb_source)

    def _invoke_handler(self, *args):
        result = self._handler(*args)
        if self._handler_is_coroutine:
            # Runs on this layer's own dedicated event loop instead of
            # spinning one up per call. Still blocks the calling worker
            # thread for the coroutine's whole lifetime, exactly like a
            # synchronous handler already blocks it; `await` points inside
            # the handler still yield control within that loop, and any
            # task.ready()/discard() calls made from inside the coroutine
            # behave exactly as the streaming contract already documents
            # for asynchronous=True.
            return self._handler_event_loop.run(result)
        return result

    def _on_task_ready(self, batch):
        if self._next and len(batch) > 0:
            self._next.push_batch(batch)

    def _on_task_discard(self, batch):
        if len(batch) == 0:
            return

        by_orchestrator = {}
        for f in batch:
            by_orchestrator.setdefault(f.owner, []).append(f)

        for orchestrator, frames in by_orchestrator.items():
            orchestrator._handle_discarded(frames)

        self._advance_downstream_backlogs(batch)

    def _advance_downstream_backlogs(self, batch):
        """A discarded frame never reaches downstream layers, so any
        downstream layer with contiguous=True would otherwise wait
        forever for that frame's index before releasing anything after
        it. Discarding still has to advance those layers' backlogs by
        the discarded indices, exactly as if the frame had passed
        through, or every later frame from the same owner stalls
        permanently once a single frame ahead of it is dropped."""
        by_owner = {}
        for f in batch:
            by_owner.setdefault(f.owner, []).append(f)

        layer = self._next
        while layer is not None:
            if layer._contiguous is True:
                for owner, frames in by_owner.items():
                    layer._advance_backlog(owner, frames)
            layer = layer._next

    def _advance_backlog(self, owner, frames):
        with self._backlog_lock:
            backlog = self._backlog.get(owner, -1)
            for f in sorted(frames):
                if f.index == backlog + 1:
                    backlog = f.index
            self._backlog[owner] = backlog
        
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
        
    def _resolve_sync_handler_result(self, task, source_batch, handler_result):
        if not task._settle_once():
            return []

        if handler_result is None:
            return source_batch

        surviving_frames = set(handler_result)
        return [f for f in source_batch if f in surviving_frames]

    def _execute_batch(self, batch):
        batch_proceed = []
        
        if self._shared is True:
            task = Task(self, None, batch)
            try:
                handler_result = self._invoke_handler(self._pipeline, task, batch)
                
                if self._is_async:
                    return
                
                batch_proceed = self._resolve_sync_handler_result(task, batch, handler_result)
            except Exception as ex:
                traceback.print_exc()
                task.abort()
        else:
            batch_aggregates = self._aggregate_batch_to_owners(batch)
            
            for batch_owner in batch_aggregates.keys():
                batch_aggregate = sorted(batch_aggregates[batch_owner])
                task = Task(self, batch_owner, batch_aggregate)
                
                try:
                    handler_result = self._invoke_handler(batch_owner, task, batch_aggregate)
                except Exception as ex:
                    traceback.print_exc()
                    task.abort()
                    continue
                
                if self._is_async:
                    continue
                    
                batch_proceed.extend(self._resolve_sync_handler_result(task, batch_aggregate, handler_result))
            
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
        batch_aggregates = self._aggregate_batch_to_owners(batch)
        batch_leftovers = []
        accepted_batch = []
        
        # The capacity check (qsize()) and the reservation it justifies
        # (deciding + enqueuing how many frames get in) have to be one
        # atomic step. Otherwise two concurrent push_batch calls (routine,
        # since multiple upstream layer threads can target this layer at
        # once) can both read the same low occupancy and both proceed to
        # fill up to their own collected_max, jointly overshooting
        # _queue_max - defeating this layer's backpressure entirely.
        with self._queue_capacity_lock:
            collected_max = self._queue_max - self._queue.qsize()
            collected_max = min(collected_max, len(batch))
            
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
        
    def add_layer(self, role, handler, contiguous=False, min_batch=1, max_batch=256, yield_after=_DEFAULT_YIELD, shared=False, asynchronous=False):
        layer = OrchestrationLayer(self, self._executor, self._chain_size, handler, max_batch, min_batch, contiguous, yield_after, shared, role, is_async=asynchronous)
        
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
                orchestrator._handle_ready(batch)
            finally:
                for f in batch:
                    if not f.last_frame:
                        continue
                    f.last_frame.userdata = None
                    f.last_frame = None
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
        
        
class OrchestrationClient:
    """Base class for a single caller's use of one shared, module-level
    OrchestrationPipeline (see DINOv2TiledImageProcessor and
    ImageProcessor for the pattern this replaces - both hand-rolled this
    exact Orchestrator setup/on_ready/on_discard/feed/close boilerplate
    around their own frame type before this existed).

    Subclasses provide _create_frame(index, userdata) to box each fed
    item into their own frame type, and call on_ready()/on_discarded()
    to register listeners for, respectively, frames that made it all the
    way through the pipeline and frames that got dropped anywhere along
    the way via task.discard()/abort(). Both listeners receive one
    subclass-boxed frame at a time (whatever _create_frame returned for
    it), not the underlying OrchestrationFrame and not a batch.

    Callers that need to record caller-side context per fed item (e.g.
    which outer frame a fed item belongs to) can use on_dispatch(callback)
    instead. callback(first_frame, userdata) fires once per frame, right
    when it's created, with the exact item that was fed for it - the same
    userdata _create_frame would have received. Its return value becomes
    the frame's userdata that the pipeline stage sees. This lets the
    caller build its own mapping (e.g. first_frame -> outer_frame) in the
    callback's closure, and unwrap/replace userdata down to just the
    payload the pipeline stage needs, without a second feed() channel.
    """

    def __init__(self, pipeline, max_window):
        self.orchestrator = Orchestrator(pipeline, max_window)
        self.orchestrator.userdata = self
        self.orchestrator.wrapper = self._wrap_frame
        self.orchestrator.on_ready(self._on_ready)
        self.orchestrator.on_discard(self._on_discarded)
        self._ready_callback = None
        self._discard_callback = None
        self._dispatch_callback = None

    def _create_frame(self, index, userdata):
        raise NotImplementedError

    def on_ready(self, callback):
        self._ready_callback = callback

    def on_discarded(self, callback):
        self._discard_callback = callback

    def on_dispatch(self, callback):
        """callback(first_frame, userdata) is invoked right as each frame
        is created, before the pipeline stage ever sees it, with the same
        userdata that was fed for it. Its return value replaces the
        frame's userdata."""
        self._dispatch_callback = callback

    def _wrap_frame(self, index, userdata):
        frame = self._create_frame(index, userdata)
        if self._dispatch_callback is not None:
            frame.userdata = self._dispatch_callback(frame, userdata)
        return frame

    def _on_ready(self, batch):
        if self._ready_callback is None:
            return
        for f in batch:
            self._ready_callback(f.userdata if type(f) is OrchestrationFrame else f)

    def _on_discarded(self, batch):
        if self._discard_callback is None:
            return
        for f in batch:
            self._discard_callback(f.userdata if type(f) is OrchestrationFrame else f)

    def feed(self, items):
        self.orchestrator.feed(items)

    def close(self):
        self.orchestrator.close()




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
        self._ready_callback = None
        self._discard_callback = None

    def on_ready(self, callback):
        self._ready_callback = callback

    def on_discard(self, callback):
        self._discard_callback = callback

    def _handle_ready(self, batch):
        if self._ready_callback is not None and len(batch) > 0:
            self._ready_callback(list(batch))

    def _handle_discarded(self, batch):
        try:
            if self._discard_callback is not None and len(batch) > 0:
                self._discard_callback(list(batch))
        finally:
            self._tear_window(batch)

    def close(self):
        try:
            self._pipeline.teardown(self)
        finally:
            self._closed = True
            
    def _create_frame(self, last_frame, index, userdata):
        if self.wrapper:
            wrapped = self.wrapper(index, userdata)
            if isinstance(wrapped, OrchestrationFrame):
                wrapped.owner = self
                wrapped.last_frame = last_frame
                wrapped.index = index
                return wrapped
            userdata = wrapped

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
            fp = self._window_ends
            
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
            
            # Always increments, even if the queue empties early
            self._window_logt += len(collected)
            
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
