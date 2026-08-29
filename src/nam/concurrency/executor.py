# executor.py
# 
# A legacy file that made it into the codebase.
# Intending to keep the API but replace the internals at some point, soon.

import threading
import os
import time
import queue
import atexit
import traceback


def detect_cpu_count() -> int:
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        return os.cpu_count() or 1


class Future:
    happened = None
    promise = None
    
    def __init__ (self, promise):
        self.happened = False
        self.promise = promise
        
    def realize (self):
        self.happened = True
        
        if self.promise:
            self.promise()

class Executor:
    def __init__ (self, thread_count):
        self.clock = 1
        self.mutex = threading.Lock()
        self.queue = queue.Queue()
        self.shutdown_signaled = False
        self.threadpool = []
        
        for i in range(thread_count):
            thread = threading.Thread(target=self.run, args=(i,), daemon=True)
            
            self.threadpool.append(thread)
        
    def __len__ (self):
        return len(self.threadpool)
        
    def call (self, task, args = (), promise = None):
        future = Future(promise)
        
        self.queue.put((task, args, future))
            
        return future
        
    def callops (self, task, args = ()):
        if self.is_context():
            task(*args)
        else:
            self.call(task, args)
        
    def get_current_tpi (self):
        for thread in self.threadpool:
            if threading.currentThread() is thread:
                return self.threadpool.index(thread)
        
        return -1
        
    def is_context (self):
        for thread in self.threadpool:
            if threading.currentThread() is thread:
                return True
        
        return False
        
    def run (self, tpi):
        while True:
            if self.shutdown_signaled is True:
                break
            
            args = None
            future = None
            task = None

            # Blocks with a timeout instead of busy-polling: idle threads
            # sleep in queue.get() rather than spinning + time.sleep(), and
            # still wake up promptly to notice shutdown_signaled - the
            # timeout just bounds how long that notice can take, it isn't
            # a poll interval for actual work (a put() wakes get() immediately).
            try:
                task, args, future = self.queue.get(timeout=0.1)
            except queue.Empty:
                continue
            
            if task is not None:
                try:
                    task(*args)
                except Exception:
                    # A misbehaving task must never be allowed to kill this
                    # worker thread - that would silently shrink the pool
                    # by one forever, and any tasks already queued behind
                    # it would still be waiting on a thread that no longer
                    # exists to pick them up.
                    traceback.print_exc()
                
            if future is not None:
                # realize() (and therefore the caller's promise) must fire
                # even when the task raised, or callers awaiting future.happened
                # / a promise callback would wait forever for a task that
                # already finished (badly, but finished).
                try:
                    future.realize()
                except Exception:
                    traceback.print_exc()
        
    def set_clock (self, clock_nanos):
        self.clock = clock_nanos
        
    def shutdown (self):
        self.shutdown_signaled = True
        
    def start (self):
        for thread in self.threadpool:
            thread.start()
        atexit.register(self.shutdown)
        
    def wait (self):
        for thread in self.threadpool:
            thread.join()
