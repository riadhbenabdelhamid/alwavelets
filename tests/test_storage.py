"""Exercise real page eviction, cache lifecycle, and eager/disk semantic parity."""
from pathlib import Path
import threading

import pytest

from alwavelets.parsers import VCDFile, parse_file
from alwavelets.storage import open_trace, index_path
from alwavelets.core import time_bisect_left, time_bisect_right, VirtualBitSignal
from alwavelets.rendering import draw_single_bit_culled, draw_bus_culled, draw_analog_culled
from alwavelets.core import Radix


def trace(tmp_path, body, declarations='$var wire 1 ! a $end\n'):
    path = tmp_path / 'trace.vcd'
    path.write_text('$scope module top $end\n' + declarations +
                    '$upscope $end\n$enddefinitions $end\n' + body)
    return path


@pytest.mark.parametrize('name', ['fst_ref.vcd', 'fst_ref_4.fst', 'fst_ref_F.fst',
                                  'fst_ref_Z.fst', 'fst_ref_c.fst'])
def test_disk_matches_eager(name):
    path = Path(__file__).parent / 'fixtures' / name
    eager = parse_file(path, storage='memory', use_cache=False)
    with open_trace(path, use_cache=False, memory_mib=0.1) as disk:
        assert disk.signal_order == eager.signal_order
        assert disk.max_time == eager.max_time
        assert disk.timescale == eager.timescale
        for key, expected in eager.signals.items():
            got = disk.signals[key]
            assert got.identity() == expected.identity()
            assert got.changes == expected.changes
            for start, end in [(0, disk.max_time), (17, 900), (50, 50)]:
                assert list(got.changes_in_window(start, end)[1]) == list(expected.changes_in_window(start, end)[1])
                renderer = draw_single_bit_culled if got._scalar else draw_analog_culled if got.analog_mode else draw_bus_culled
                args = (Radix.HEX,) if renderer is draw_bus_culled else ()
                assert renderer(got, start, end, 61, *args) == renderer(expected, start, end, 61, *args)


def test_eviction_duplicates_and_all_signals(tmp_path):
    path = trace(tmp_path, ''.join(f'#{i // 3}\n{i % 2}!\nb{i:032b} "\n' for i in range(22000)),
                 '$var wire 1 ! a $end\n$var wire 32 " b $end\n')
    eager = VCDFile.parse(path)
    with open_trace(path, use_cache=False, memory_mib=0.08) as disk:
        for key in eager.signals:
            expected, got = eager.signals[key], disk.signals[key]
            for t in [0, 1365, 1366, 4000, 7333, 10000, -1, 2000]:
                assert got.value_at(t) == expected.value_at(t)
                assert time_bisect_left(got._times, t) == time_bisect_left(expected._times, t)
                assert time_bisect_right(got._times, t) == time_bisect_right(expected._times, t)
            assert list(got.iter_changes(100, 5000)) == list(expected.iter_changes(100, 5000))
        disk.signal_order = ['!']  # optional visibility filter does not discard other signals
        assert disk.signals['"'].value_at(1234) == eager.signals['"'].value_at(1234)
        assert disk._store.page_reads > 10
        assert disk._store.peak_cache_bytes <= disk._store.memory_bytes
        bit = VirtualBitSignal(disk.signals['"'], 3)
        assert bit.value_at(1333) == VirtualBitSignal(eager.signals['"'], 3).value_at(1333)


def test_persistent_reopen_and_invalidation(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    path = trace(tmp_path, '#0\n0!\n#5\n1!\n')
    with open_trace(path) as first:
        assert first.signals['!'].value_at(5) == '1'
    assert Path(index_path(path)).is_file()
    with open_trace(path) as reopened:
        assert reopened._store.page_reads == 0
        assert reopened.signals['!'].value_at(5) == '1'
    with path.open('a') as f:
        f.write('#10\n0!\n')
    with open_trace(path) as updated:
        assert updated.max_time == 10
        assert updated.signals['!'].value_at(10) == '0'


def test_out_of_order_and_directives(tmp_path):
    path = trace(tmp_path, '#10\n1!\n#0\n0!\n$comment\n#500\n1!\n$end\n'
                 '#5\n$dumpoff\n$end\n#6\n1!\n#7\n$dumpon\n1!\n$end\n#10\n0!\n')
    eager = VCDFile.parse(path)
    with open_trace(path, use_cache=False) as disk:
        assert disk.signals['!'].changes == eager.signals['!'].changes
        assert disk.max_time == eager.max_time
        assert disk.load_changes == disk.signals['!'].num_changes()


def test_cancelled_index_is_not_published(tmp_path, monkeypatch):
    from alwavelets import storage
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    path = trace(tmp_path, '#0\n0!\n')
    entered, release = threading.Event(), threading.Event()
    original = storage.PageWriter.add_vcd_batch
    def pause(writer, *args):
        entered.set()
        assert release.wait(5)
        return original(writer, *args)
    monkeypatch.setattr(storage.PageWriter, 'add_vcd_batch', pause)
    disk = open_trace(path, background=True)
    assert entered.wait(5)
    with pytest.raises(ValueError, match='still loading'):
        disk.require_loaded()
    disk._store.cancel.set()
    release.set()
    disk.close()
    assert not Path(index_path(path)).exists()
    assert disk._store.closed


def test_merged_events_keep_identifiers(tmp_path):
    path = trace(tmp_path, '#0\n0!\n1"\n#1\n1!\n#2\n0"\n',
                 '$var wire 1 ! a $end\n$var wire 1 " b $end\n$var wire 1 ! alias $end\n')
    with open_trace(path, use_cache=False) as disk:
        assert list(disk._store.iter_events(['!', '"'])) == [(0, '!', '0'), (0, '"', '1'), (1, '!', '1'), (2, '"', '0')]
        assert list(disk.signals[disk.signal_order[-1]].iter_changes()) == [(0, '0'), (1, '1')]


def test_streaming_search_measure_and_derived(tmp_path):
    from alwavelets.expr import find_all_expression, find_expression, make_derived_signal, ExprError
    from alwavelets.analysis import measure_signal
    path = trace(tmp_path, ''.join(f'#{i}\n{i % 2}!\n' for i in range(12000)))
    eager = VCDFile.parse(path)
    with open_trace(path, use_cache=False, memory_mib=0.08) as disk:
        assert find_all_expression(disk, 'a == 1', limit=3) == [1, 3, 5]
        reports = []
        assert find_expression(disk, 'a == 3', progress=lambda t, end: reports.append((t, end))) is None
        assert reports[-1] == (disk.max_time, disk.max_time)
        cancel = threading.Event()
        cancel.set()
        with pytest.raises(ExprError, match='cancelled'):
            find_expression(disk, 'a', cancel=cancel)
        assert measure_signal(disk.signals['!'], 1, 11000) == measure_signal(eager.signals['!'], 1, 11000)
        derived = make_derived_signal(disk, 'inverse', '!a')
        assert derived.changes == make_derived_signal(eager, 'inverse', '!a').changes
        assert derived.store.cache_bytes <= derived.store.memory_bytes
    assert derived.store.closed


def test_disk_ingestion_does_not_intern_values(tmp_path, monkeypatch):
    import sys
    path = trace(tmp_path, '#0\nr1.234 !\n#5\nr2.345 !\n', '$var real 64 ! a $end\n')
    def forbidden(value):
        raise AssertionError('disk pages must not intern value strings')
    monkeypatch.setattr(sys, 'intern', forbidden)
    with open_trace(path, use_cache=False) as disk:
        assert disk.signals['!'].value_at(5) == '2.345'


def test_background_queries_discard_obsolete_results():
    from alwavelets.background import BackgroundQueries
    entered, release = threading.Event(), threading.Event()
    def old_query():
        entered.set()
        assert release.wait(5)
        return 'old'
    worker = BackgroundQueries(limit=2)
    try:
        assert worker.request('rows', 1, 'a', old_query, 'pending') == 'pending'
        assert entered.wait(5)
        assert worker.request('rows', 2, 'a', lambda: 'new', 'pending') == 'pending'
        release.set()
        for future in list(worker.pending.values()):
            future.result(timeout=5)
        assert worker.poll()
        assert worker.request('rows', 2, 'a', lambda: 'wrong', 'pending') == 'new'
        assert all(token[1] == 2 for token in worker.ready)
    finally:
        release.set()
        worker.close()


def test_summary_hierarchy_preserves_extrema_without_scanning_values(tmp_path):
    path = trace(tmp_path, ''.join(f'#{i}\nr{9000 if i == 20001 else i % 3} !\n'
                                  for i in range(24000)), '$var real 64 ! a $end\n')
    eager = VCDFile.parse(path)
    with open_trace(path, use_cache=False, memory_mib=0.08) as disk:
        sig = disk.signals['!']
        assert disk._store.db.execute('SELECT MAX(level) FROM summaries').fetchone()[0] >= 1
        for start, end in [(0, 24000), (17000, 21000), (20002, 21000), (24001, 25000)]:
            assert sig.numeric_range_in_window(start, end) == eager.signals['!'].numeric_range_in_window(start, end)
        assert disk._store.page_reads < 20


def test_auto_storage_override_and_corrupt_cache(tmp_path, monkeypatch):
    from alwavelets import storage
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    monkeypatch.setattr(storage, 'AUTO_THRESHOLD', 1)
    path = trace(tmp_path, '#0\n0!\n#1\n1!\n')
    target = Path(index_path(path))
    target.parent.mkdir(parents=True)
    target.write_bytes(b'broken index')
    with parse_file(path) as disk:
        assert disk.signals['!'].value_at(1) == '1'
        assert disk._store
    with parse_file(path, storage='memory', use_cache=False) as memory:
        assert not hasattr(memory, '_store')


def test_ui_rows_are_computed_on_worker(tmp_path):
    from types import SimpleNamespace
    from alwavelets.ui_drawing import DrawingMixin
    path = trace(tmp_path, '#0\n0!\n#1\n1!\n')
    with open_trace(path, use_cache=False) as disk:
        viewer = SimpleNamespace(time_start=0, time_end=2, wave_width=20, line_thickness=1,
                                  _row_cache_window=None, _row_cache={}, vcd=disk)
        sig = disk.signals['!']
        try:
            # The curses bus drawing path calls labels.items() even while
            # the background result is pending.
            bus_pending = DrawingMixin._render_row(viewer, 'bus', sig, '!', Radix.HEX)
            assert hasattr(bus_pending[2], 'items')
            DrawingMixin._render_row(viewer, 'bit', sig, '!')
            for future in list(viewer._queries.pending.values()):
                future.result(timeout=5)
            viewer._queries.poll()
            assert DrawingMixin._render_row(viewer, 'bit', sig, '!') == draw_single_bit_culled(sig, 0, 2, 20)
        finally:
            if hasattr(viewer, '_queries'):
                viewer._queries.close()


def test_fractional_time_queries(tmp_path):
    from fractions import Fraction
    from alwavelets.core import TimeScaledSignal
    from alwavelets.analysis import compare_signal_ranges
    path = trace(tmp_path, '#0\n0!\n#1\n1!\n#2\n0!\n')
    with open_trace(path, use_cache=False) as disk:
        sig = disk.signals['!']
        assert sig.value_at(Fraction(3, 2)) == '1'
        assert time_bisect_left(sig._times, Fraction(3, 2)) == 2
        assert time_bisect_right(sig._times, Fraction(2)) == 3
        assert sig.value_at(10 ** 30) == '0'
        assert compare_signal_ranges(sig, TimeScaledSignal(sig, Fraction(1, 2)), 0, 2) == [(Fraction(1, 2), 2)]


def test_disk_png_preserves_external_rendering_fixes(tmp_path):
    from alwavelets.graphics import render_waveform_png
    path = trace(tmp_path, '#0\n0!\n#500\n1!\n#501\n0!\n#700\nx!\n#701\nz!\n#702\n0!\n#1000\n0!\n')
    eager = VCDFile.parse(path)
    with open_trace(path, use_cache=False) as disk:
        assert render_waveform_png(disk, ['top.a'], width=80) == render_waveform_png(eager, ['top.a'], width=80)


@pytest.mark.parametrize('disordered', [False, True])
def test_summary_groups_spill_without_losing_extrema(tmp_path, monkeypatch, disordered):
    from alwavelets import storage
    original = storage.PageWriter.__init__
    def small_groups(writer, *args):
        original(writer, *args)
        writer.summary_group_limit = 2
    monkeypatch.setattr(storage.PageWriter, '__init__', small_groups)
    monkeypatch.setattr(storage, 'PAGE_EVENTS', 2)
    declarations = ''.join('$var wire 16 %s s%d $end\n' % (key, i) for i, key in enumerate('!"#$'))
    ticks = list(range(100))
    if disordered:
        ticks.reverse()
    body = ''.join('#%d\n%s' % (t, ''.join('b%s %s\n' % (format(t + i, '016b'), key)
                                           for i, key in enumerate('!"#$'))) for t in ticks)
    path = trace(tmp_path, body, declarations)
    eager = VCDFile.parse(path)
    with open_trace(path, use_cache=False) as disk:
        assert disk._store.db.execute('SELECT MAX(level) FROM summaries').fetchone()[0] >= 1
        for key, expected in eager.signals.items():
            assert disk.signals[key].changes == expected.changes
            for start, end in [(0, 99), (5, 77), (3, 3)]:
                assert disk.signals[key].numeric_range_in_window(start, end) == expected.numeric_range_in_window(start, end)
