"""Bounded background queries; results are applied by the UI thread only."""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import threading


class BackgroundQueries:
    def __init__(self, limit=128):
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='wavelet-read')
        self.limit = limit
        self.pending, self.ready, self.windows = {}, OrderedDict(), {}
        self.placeholders = {}
        self.cancellations = {}
        self.errors = []

    def request(self, group, window, key, compute, placeholder, *, cancellable=False):
        if self.windows.get(group) != window:
            self.invalidate(group)
            self.windows[group] = window
        token = (group, window, key)
        if token in self.ready:
            self.ready.move_to_end(token)
            return self.ready[token]
        if token not in self.pending and len(self.pending) < self.limit:
            if cancellable:
                cancel = self.cancellations[token] = threading.Event()
                self.pending[token] = self.pool.submit(compute, cancel)
            else:
                self.pending[token] = self.pool.submit(compute)
            self.placeholders[token] = placeholder
        return placeholder

    def invalidate(self, group):
        self.windows.pop(group, None)
        for token in list(self.pending):
            if token[0] == group:
                cancel = self.cancellations.pop(token, None)
                if cancel is not None:
                    cancel.set()
                self.pending.pop(token).cancel()
                self.placeholders.pop(token, None)
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
        for cancel in self.cancellations.values():
            cancel.set()
        for future in self.pending.values():
            future.cancel()
        self.pool.shutdown(wait=True)
        self.pending.clear()
        self.ready.clear()
        self.placeholders.clear()
        self.cancellations.clear()


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
