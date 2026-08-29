import time
import threading
import heapq
import traceback
import atexit


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
