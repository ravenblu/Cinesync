"""Cancellation of only the subprocesses owned by a packaging job."""
import threading
from contextlib import contextmanager

_local = threading.local()


class Cancelled(RuntimeError):
    pass


class Control:
    def __init__(self):
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.processes = set()

    def check(self):
        if self.cancelled.is_set():
            raise Cancelled('Export cancelled')

    def cancel(self):
        self.cancelled.set()
        with self.lock:
            for process in self.processes:
                if process.poll() is None:
                    process.kill()

    @contextmanager
    def watch(self, process):
        with self.lock:
            self.processes.add(process)
            if self.cancelled.is_set() and process.poll() is None:
                process.kill()
        try:
            yield process
        finally:
            with self.lock:
                self.processes.discard(process)
        self.check()


@contextmanager
def active(control):
    previous = getattr(_local, 'control', None)
    _local.control = control
    try:
        yield
    finally:
        _local.control = previous


def current():
    return getattr(_local, 'control', None) or Control()
