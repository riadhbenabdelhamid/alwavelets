"""Bounded traversal helpers shared by exports and decoders."""
import heapq
from contextlib import contextmanager
from contextvars import ContextVar
from itertools import chain

from .core import time_bisect_left, time_bisect_right


class OperationCancelled(ValueError):
    pass


def check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise OperationCancelled('Operation cancelled')


_read_cancel = ContextVar('wavelet_read_cancel', default=None)


@contextmanager
def read_cancellation(cancel):
    """Scope a query's cancellation to this worker, across signal view layers.

    This never sets a trace's load/close event. A context also covers aliases
    and comparison signals backed by a different store, and resets on errors.
    """
    token = _read_cancel.set(cancel)
    try:
        check_cancel(cancel)
        yield
        check_cancel(cancel)
    finally:
        _read_cancel.reset(token)


def check_read_cancel():
    check_cancel(_read_cancel.get())


def iter_window(sig, start, end, *, baseline=True, cancel=None):
    """Stream a closed interval, optionally with its one preceding event.

    Use read-only event iterators instead of changes_in_window(), whose eager
    implementation allocates a list of all decoded values in the interval.
    """
    check_cancel(cancel)
    if end < start:
        return
    if not hasattr(sig, 'iter_changes'):
        times, values = sig.changes_in_window(start, end)
        events = zip(times, values)
    else:
        if baseline:
            before = time_bisect_left(sig._times, start) - 1
            if before >= 0:
                yield sig._times[before], sig.value_str(before)
        events = sig.iter_changes(start, end)
    for i, event in enumerate(events):
        if i % 1024 == 0:
            check_cancel(cancel)
        if baseline or event[0] >= start:
            yield event
    check_cancel(cancel)


def merge_times(streams, cancel=None):
    """Sorted unique timestamps, using one head per input stream."""
    previous = None
    check_cancel(cancel)
    for i, timestamp in enumerate(heapq.merge(*streams)):
        if i % 1024 == 0:
            check_cancel(cancel)
        if timestamp != previous:
            yield timestamp
            previous = timestamp


def signal_times(sig, start, end):
    times = sig._times
    lo, hi = time_bisect_left(times, start), time_bisect_right(times, end)
    if hasattr(times, 'iter_range'):
        yield from times.iter_range(lo, hi)
    else:
        for i in range(lo, hi):
            yield times[i]


def prefer_nonempty(primary, fallback):
    """Peek once, selecting a lazily constructed fallback only if needed."""
    iterator = iter(primary)
    first = next(iterator, None)
    return chain((first,), iterator) if first is not None else fallback()


def iter_segments(sig, start, end, cancel=None):
    """Segments including a held baseline, unknown prefix and delta events."""
    previous_t, previous_v = start, 'x'
    initialized = False
    for t, value in iter_window(sig, start, end, cancel=cancel):
        if t < start:
            previous_v = value
            initialized = True
            continue
        if initialized or t > previous_t:
            yield previous_t, t, previous_v
        previous_t, previous_v = t, value
        initialized = True
    yield previous_t, end, previous_v
