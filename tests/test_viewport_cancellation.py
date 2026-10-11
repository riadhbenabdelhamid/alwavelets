"""Obsolete viewport work stops without cancelling ingestion or other readers."""
import array
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
import threading
from types import SimpleNamespace
import weakref

import pytest

from alwavelets import rendering, storage
from alwavelets.background import BackgroundQueries
from alwavelets.catalog import CatalogAlias
from alwavelets.core import Radix, TimeScaledSignal, VirtualBitSignal
from alwavelets.streaming import OperationCancelled, check_read_cancel, read_cancellation
from alwavelets.ui_drawing import DrawingMixin
from test_writer_metadata import writer_for


def settle(queries):
    for future in list(queries.pending.values()):
        future.result(timeout=5)
    queries.poll()


def test_obsolete_queue_releases_closures_and_preserves_other_groups():
    entered, release = threading.Event(), threading.Event()
    queries = BackgroundQueries(limit=4)
    calls = []

    def blocked():
        entered.set()
        assert release.wait(5)
        return 'obsolete'

    class Payload:
        pass

    try:
        queries.request('rows', 0, 'a', blocked, None)
        assert entered.wait(5)
        old = next(iter(queries.pending.values()))
        queries.request('values', 0, 'cursor', lambda: 'cursor value', None)
        for window in range(1, 101):
            payload = Payload()
            ref = weakref.ref(payload)
            queries.request('rows', window, 'a', lambda p=payload: p, None)
            queued = queries.pending['rows', window, 'a']
            del payload
            queries.request('rows', window + 1, 'b', lambda w=window: calls.append(w), None)
            assert queued.cancelled()
            assert ref() is None  # No cancelled executor entry retains its render closure.
            assert queries.queued_count == 2
            assert len(queries.pending) == 2
        release.set()
        assert old.result(timeout=5) == 'obsolete'
        settle(queries)
        assert calls == [100]
        assert ('values', 0, 'cursor') in queries.ready
        assert ('rows', 101, 'b') in queries.ready
        assert ('rows', 0, 'a') not in queries.ready
        assert not queries.errors
    finally:
        release.set()
        queries.close()


@pytest.mark.parametrize('action', ['invalidate', 'replace', 'retain', 'close'])
def test_running_job_is_cancelled_and_worker_context_is_reset(action):
    entered = threading.Event()
    queries = BackgroundQueries()

    def running(cancel):
        with read_cancellation(cancel):
            entered.set()
            assert cancel.wait(5)
            check_read_cancel()

    try:
        queries.request('rows', 0, 'old', running, None, cancellable=True, replace='signal')
        future = queries.pending['rows', 0, 'old']
        assert entered.wait(5)
        if action == 'invalidate':
            queries.invalidate('rows')
        elif action == 'replace':
            queries.request('rows', 0, 'new', check_read_cancel, None, replace='signal')
        elif action == 'retain':
            queries.retain('rows', lambda key: key == 'new')
        else:
            queries.close()
        with pytest.raises(OperationCancelled):
            future.result(timeout=5)
        if action != 'close':
            queries.request('rows', 0, 'new', check_read_cancel, None)
            settle(queries)
            assert list(queries.ready) == [('rows', 0, 'new')]
        assert not queries.errors
    finally:
        queries.close()


def test_visible_rows_keep_cached_results_and_comparison_work_separate():
    entered, release = threading.Event(), threading.Event()
    queries = BackgroundQueries()

    def blocked():
        entered.set()
        assert release.wait(5)

    try:
        queries.request('rows', 0, ('bus', 'cached'), lambda: 'cached', None)
        settle(queries)
        queries.request('values', 0, 'block', blocked, None)
        assert entered.wait(5)
        queries.request('rows', 0, ('bus', 'visible'), lambda: 'old', None, replace=(False, 'visible'))
        old = queries.pending['rows', 0, ('bus', 'visible')]
        queries.request('rows', 0, ('dbus', 'visible'), lambda: 'comparison', None, replace=(True, 'visible'))
        queries.request('rows', 0, ('analog', 'visible'), lambda: 'new', None, replace=(False, 'visible'))
        queries.request('rows', 0, ('bus', 'hidden'), lambda: pytest.fail('Hidden work ran'), None)
        queries.retain('rows', lambda key: key[1] == 'visible')
        assert old.cancelled() and queries.queued_count == 2
        release.set()
        settle(queries)
        assert queries.ready['rows', 0, ('dbus', 'visible')] == 'comparison'
        assert queries.ready['rows', 0, ('analog', 'visible')] == 'new'
        assert queries.request('rows', 0, ('bus', 'cached'), lambda: pytest.fail('Cache lost'), None) == 'cached'
        assert not queries.errors
    finally:
        release.set()
        queries.close()


def test_batch_defers_new_work_and_resumes_after_nested_failure():
    queries = BackgroundQueries()
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def running():
        entered.set()
        assert release.wait(5)
        finished.set()

    try:
        queries.request('rows', 0, 'running', running, None)
        assert entered.wait(5)
        with queries.batch():
            queries.request('rows', 0, 'next', lambda: 'done', None)
            future = queries.pending['rows', 0, 'next']
            with pytest.raises(ValueError), queries.batch():
                raise ValueError('Interrupted drawing pass')
            release.set()
            assert finished.wait(5)
            # Wait for the active drain to finish, proving it did not start the next job.
            runner = queries._runner
            if runner is not None:
                runner.result(timeout=5)
            assert not future.running() and not future.done()
            assert queries.queued_count == 1
        assert future.result(timeout=5) == 'done'
        settle(queries)
        assert not queries.errors
    finally:
        release.set()
        queries.close()


def test_read_scope_is_nested_thread_local_and_checks_final_result():
    outer, inner = threading.Event(), threading.Event()
    with read_cancellation(outer), ThreadPoolExecutor(max_workers=1) as pool:
        with pytest.raises(OperationCancelled), read_cancellation(inner):
            inner.set()
        check_read_cancel()  # Inner cancellation did not poison the enclosing query.
        outer.set()
        pool.submit(check_read_cancel).result(timeout=5)  # Loader / unrelated reader.
        with pytest.raises(OperationCancelled):
            check_read_cancel()
        outer.clear()
    with pytest.raises(OperationCancelled), read_cancellation(inner):
        pytest.fail('A cancelled scope must not start')
    check_read_cancel()


def test_numeric_summary_queries_stop_without_poisoning_cached_reads(tmp_path):
    trace, writer, _ = writer_for(tmp_path, count=1)
    cancel = threading.Event()
    summaries = []
    try:
        writer.add_batch('0', array.array('q', range(4096)), [format(i, '016b') for i in range(4096)])
        writer.publish(True)
        signal = trace.signals['0']
        assert signal.numeric_range_in_window(0, 4095) == (0, 4095)

        def cancel_summary(sql):
            if sql.startswith('SELECT n,vmin,vmax FROM summaries'):
                summaries.append(sql)
                cancel.set()

        trace._store.db.set_trace_callback(cancel_summary)
        with pytest.raises(OperationCancelled), read_cancellation(cancel):
            signal.numeric_range_in_window(0, 4095)
        assert len(summaries) == 1
        trace._store.db.set_trace_callback(None)
        assert signal.numeric_range_in_window(0, 4095) == (0, 4095)
        assert not trace._store.cancel.is_set()
    finally:
        trace.close()


@pytest.mark.parametrize('view', ['signal', 'alias', 'bit', 'scaled'])
def test_cancelled_samples_stop_mid_read_and_leave_trace_writable(tmp_path, monkeypatch, view):
    monkeypatch.setattr(storage, 'SHORT_EVENTS', 0)
    trace, writer, _ = writer_for(tmp_path, count=1)
    try:
        writer.add_batch('0', array.array('q', range(64)), [format(i, '016b') for i in range(64)])
        writer.publish(True)
        signal = trace.signals['0']
        if view == 'alias':
            signal = CatalogAlias('alias', '0', 16, 'wire', '', signal)
        elif view == 'bit':
            signal = VirtualBitSignal(signal, 0)
        elif view == 'scaled':
            signal = TimeScaledSignal(signal, Fraction(2, 3))
        cancel, calls = threading.Event(), []
        original = storage.check_read_cancel

        def cancel_after_reads():
            calls.append(1)
            if len(calls) == 8:
                cancel.set()
            original()

        monkeypatch.setattr(storage, 'check_read_cancel', cancel_after_reads)
        draw = rendering.draw_single_bit_culled if view == 'bit' else rendering.draw_bus_culled
        extra = () if view == 'bit' else (Radix.HEX,)
        with pytest.raises(OperationCancelled), read_cancellation(cancel):
            draw(signal, 0, 63, 8, *extra)
        assert len(calls) == 8
        assert not trace._store.cancel.is_set()
        writer.add('0', 64, '1' * 16)
        writer.publish(True)
        assert trace.signals['0'].value_at(64) == '1' * 16
        assert draw(signal, 0, 63, 8, *extra)
    finally:
        trace.close()


@pytest.mark.parametrize('kind,extra', [('bit', None), ('sub', None), ('bus', Radix.HEX),
                                      ('analog', None), ('abraille', 3), ('amulti', 3), ('dbus', Radix.HEX)])
def test_new_row_cancels_before_waiting_for_catalog_lock(tmp_path, monkeypatch, kind, extra):
    trace, writer, _ = writer_for(tmp_path, count=1, width=1 if kind in ('bit', 'sub') else 16)
    comparison = None
    queries = BackgroundQueries()
    entered, release = threading.Event(), threading.Event()
    try:
        for i in range(100):
            writer.add('0', i, str(i % 2))
        writer.publish(True)
        signal = trace.signals['0']
        if kind == 'dbus':
            directory = tmp_path / 'comparison'
            directory.mkdir()
            comparison, other_writer, _ = writer_for(directory, count=1)
            other_writer.add_batch('0', array.array('q', range(100)), [format(i, '016b') for i in range(100)])
            other_writer.publish(True)
            signal = TimeScaledSignal(comparison.signals['0'], Fraction(2, 3))
        viewer = SimpleNamespace(time_start=0, time_end=80, wave_width=16, line_thickness=1,
                                 _row_cache_window=None, _row_cache={}, vcd=trace, _queries=queries)
        original = storage.check_read_cancel

        def hold_old_window():
            if threading.current_thread().name.startswith('wavelet-read') and not entered.is_set():
                with (comparison or trace)._store.lock:
                    entered.set()
                    # A broken implementation reaches the timeout without hanging the test.
                    while not release.wait(0.01):
                        original()
            original()

        monkeypatch.setattr(storage, 'check_read_cancel', hold_old_window)
        DrawingMixin._render_row(viewer, kind, signal, '0', extra)
        future = next(iter(queries.pending.values()))
        assert entered.wait(5)
        viewer.time_start, viewer.time_end = 10, 90
        with ThreadPoolExecutor(max_workers=1) as ui:
            submission = ui.submit(DrawingMixin._render_row, viewer, kind, signal, '0', extra)
            try:
                submission.result(timeout=5)
            finally:
                release.set()
        with pytest.raises(OperationCancelled):
            future.result(timeout=5)
        settle(queries)
        result = DrawingMixin._render_row(viewer, kind, signal, '0', extra)
        assert result and not queries.errors
        assert all(token[1][1][:2] == (10, 90) for token in queries.ready)
        assert not trace._store.cancel.is_set()
        assert comparison is None or not comparison._store.cancel.is_set()
        # Compare full glyphs, attributes, raw labels and analog limits with an uncancelled job.
        extras = viewer._row_extras[kind, '0']
        queries.invalidate('rows')
        DrawingMixin._render_row(viewer, kind, signal, '0', extra)
        settle(queries)
        assert DrawingMixin._render_row(viewer, kind, signal, '0', extra) == result
        assert viewer._row_extras[kind, '0'] == extras
    finally:
        release.set()
        queries.close()
        if comparison is not None:
            comparison.close()
        trace.close()
