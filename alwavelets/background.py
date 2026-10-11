"""Bounded background queries; results are applied by the UI thread only."""
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
import threading


class BackgroundQueries:
    def __init__(self, limit=128):
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='wavelet-read')
        self.limit = limit
        self.pending, self.ready, self.windows = {}, OrderedDict(), {}
        self.placeholders = {}
        self.cancellations = {}
        self.replacements = {}
        self.errors = []
        # Keep jobs here, rather than filling the executor's unbounded queue
        # with cancelled work items that retain obsolete render closures.
        self._queued = OrderedDict()
        self._lock = threading.Lock()
        self._runner = self._active_cancel = None
        self._batch_depth = 0
        self._closed = False

    @property
    def queued_count(self):
        with self._lock:
            return len(self._queued)

    @contextmanager
    def batch(self):
        """Defer new jobs until the UI has submitted a complete drawing pass.

        An already running job can finish, but no next job starts mid-pass
        and takes the storage lock ahead of the remaining metadata reads.
        """
        with self._lock:
            self._batch_depth += 1
        try:
            yield
        finally:
            with self._lock:
                self._batch_depth -= 1
                self._start_locked()

    def _start_locked(self):
        if not self._closed and not self._batch_depth and self._queued and self._runner is None:
            self._runner = self.pool.submit(self._drain)

    def _drain(self):
        while True:
            with self._lock:
                if not self._queued or self._batch_depth:
                    self._runner = None
                    return
                _, (future, compute, cancel) = self._queued.popitem(last=False)
                self._active_cancel = cancel
            if future.set_running_or_notify_cancel():
                try:
                    future.set_result(compute(cancel) if cancel is not None else compute())
                except BaseException as exc:
                    future.set_exception(exc)
            with self._lock:
                self._active_cancel = None

    def request(self, group, window, key, compute, placeholder, *, cancellable=False, replace=None):
        if self._closed:
            raise RuntimeError('Background queries are closed')
        if self.windows.get(group) != window:
            self.invalidate(group)
            self.windows[group] = window
        token = (group, window, key)
        if replace is not None:
            for old in list(self.pending):
                if old != token and old[0] == group and self.replacements.get(old) == replace:
                    self._discard(old)
        if token in self.ready:
            self.ready.move_to_end(token)
            return self.ready[token]
        if token not in self.pending and len(self.pending) < self.limit:
            cancel = threading.Event() if cancellable else None
            if cancel is not None:
                self.cancellations[token] = cancel
            future = self.pending[token] = Future()
            self.replacements[token] = replace
            self.placeholders[token] = placeholder
            with self._lock:
                self._queued[token] = (future, compute, cancel)
                self._start_locked()
        return placeholder

    def _discard(self, token):
        cancel = self.cancellations.pop(token, None)
        if cancel is not None:
            cancel.set()
        with self._lock:
            self._queued.pop(token, None)
        self.pending.pop(token).cancel()
        self.placeholders.pop(token, None)
        self.replacements.pop(token, None)

    def retain(self, group, keep):
        """Cancel pending work outside the visible rows, keeping cached results."""
        for token in list(self.pending):
            if token[0] == group and not keep(token[2]):
                self._discard(token)

    def invalidate(self, group):
        self.windows.pop(group, None)
        for token in list(self.pending):
            if token[0] == group:
                self._discard(token)
        for token in list(self.ready):
            if token[0] == group:
                del self.ready[token]

    def poll(self):
        changed = False
        for token, future in list(self.pending.items()):
            if not future.done():
                continue
            del self.pending[token]
            self.cancellations.pop(token, None)
            self.replacements.pop(token, None)
            placeholder = self.placeholders.pop(token, None)
            if self.windows.get(token[0]) != token[1]:
                continue
            try:
                self.ready[token] = future.result()
            except Exception as exc:
                self.errors.append(str(exc))
                self.ready[token] = placeholder
            while len(self.ready) > self.limit * 2:
                self.ready.popitem(last=False)
            changed = True
        return changed

    def close(self):
        self._closed = True
        for cancel in self.cancellations.values():
            cancel.set()
        with self._lock:
            if self._active_cancel is not None:
                self._active_cancel.set()
            self._queued.clear()
        for future in self.pending.values():
            future.cancel()
        self.pool.shutdown(wait=True)
        self.pending.clear()
        self.ready.clear()
        self.placeholders.clear()
        self.cancellations.clear()
        self.replacements.clear()


class AnalysisJob:
    def __init__(self, compute, complete, discard=None):
        self.cancel = threading.Event()
        self.progress = None
        self.complete = complete
        self.discard = discard
        self.result_claimed = False
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='wavelet-analysis')
        self.future = self.pool.submit(compute, self.cancel, self.update_progress)

    def update_progress(self, current, total):
        self.progress = current, total

    def close(self):
        self.cancel.set()
        self.future.cancel()
        self.pool.shutdown(wait=True)
        if self.discard is not None and not self.result_claimed and not self.future.cancelled():
            self.result_claimed = True
            if self.future.exception() is None:
                self.discard(self.future.result())
