# executor.py
# 
# A legacy file that made it into the codebase.
# Intending to keep the API but replace the internals at some point, soon.
# I'm aware there's a few bugs here, some of which are critical or very unpredictable.

import threading
import multiprocessing
import os
import time
import queue
import atexit


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

            # A very fine implementation indeed. Anyone looking here is probably going to shake their 
            # heads in disgust at this remnant that's busy polling. But it's been working solid for, what, 9 years now? 
            # I refuse to touch it out of historical significance. Any contributor can switch to blocking + 
            # posting the shutdown signal into the queue. That's gonna get us, what, some microseconds, maybe-ish? Let me know.
            try:
                task, args, future = self.queue.get(block=False)
            except queue.Empty:
                time.sleep(self.clock / 1_000_000)
                
                continue
            
            if task is not None:
                task(*args)
                
            if future is not None:
                future.realize()
        
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
