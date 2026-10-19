"""Bounded display I/O worker; unrelated to coordinator task scheduling."""
from collections import deque
from concurrent.futures import Future
import threading

class BoundedReadPool:
    def __init__(self, capacity=8):
        self.capacity=capacity;self.jobs=deque();self.condition=threading.Condition()
        self.closed=False;self.peak_queued=0
        self.thread=threading.Thread(target=self._run,name='studio-history',daemon=True)
        self.thread.start()

    def submit(self, action):
        with self.condition:
            if self.closed:raise RuntimeError('只读 worker 已关闭')
            # Cancelling a Future must also release its queue storage. A plain
            # ThreadPoolExecutor retains cancelled work in an unbounded queue.
            self.jobs=deque(job for job in self.jobs if not job[0].cancelled())
            if len(self.jobs)>=self.capacity:raise RuntimeError('只读队列已满')
            future=Future();self.jobs.append((future,action))
            self.peak_queued=max(self.peak_queued,len(self.jobs))
            self.condition.notify()
            return future

    def _run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda:self.closed or self.jobs)
                if not self.jobs and self.closed:return
                future,action=self.jobs.popleft()
            if not future.set_running_or_notify_cancel():continue
            try:future.set_result(action())
            except BaseException as error:future.set_exception(error)

    def shutdown(self, wait=False, cancel_futures=True):
        with self.condition:
            self.closed=True
            if cancel_futures:
                for future,_ in self.jobs:future.cancel()
                self.jobs.clear()
            self.condition.notify_all()
        if wait and threading.current_thread()!=self.thread:self.thread.join()
