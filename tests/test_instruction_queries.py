"""Indexed query parity, wide keys, cancellation, aggregates and UI ownership."""
import random
import threading
import tracemalloc

import pytest

from alwavelets.decoders import InstructionTrace
from alwavelets.instruction_store import InstructionResults, number_key, key_number
from alwavelets.instruction_ui import run_query
from alwavelets.streaming import OperationCancelled
from tools.stress_instructions import entries


def eager(records):
    trace = InstructionTrace()
    trace.entries = records
    trace.built = True
    return trace


@pytest.fixture
def traces():
    data = list(entries(700))
    for i, e in enumerate(data):
        e['pc'] = [0, 4, 0xffffffff, (1 << 64) - 1, 1 << 100, -4][i % 6]
        e['csr_addr'] = 0x300 if i % 7 == 0 else None
        e['decoded']['is_csr'] = i % 7 == 0
        e['thread_id'] = (1 << 65) + i % 3
    with InstructionResults.collect(data, dict) as store:
        yield eager(data), eager(store)


def test_integer_keys_preserve_order_and_exactness():
    values = [-(1 << 200), -(1 << 64), -256, -1, 0, 1, 255, 256, (1 << 63), (1 << 64) - 1, 1 << 65536]
    assert sorted(map(number_key, values)) == list(map(number_key, values))
    assert [key_number(number_key(v)) for v in values] == values


@pytest.mark.parametrize('filters', [
    {}, {'pc_lo': 0xffffffff, 'pc_hi': (1 << 64) - 1}, {'pc_lo': -10, 'pc_hi': -1},
    {'pc': 1 << 100}, {'hart_id': 3, 'time_lo': 10, 'time_hi': 120},
    {'thread_id': (1 << 65) + 2}, {'mnemonic': 'ADD'}, {'mnemonic_pattern': '^(add|sw)$'},
    {'rd_addr': 7, 'pc_lo': 0, 'pc_hi': 0xffffffff}, {'is_store': True, 'mem_addr_lo': 100, 'mem_addr_hi': 1500},
    {'is_csr': True, 'csr_addr': 0x300}, {'stall': True}, {'hart_id': None}, {'time_lo': 50, 'time_hi': 49}
])
def test_indexed_selections_match_eager_order(traces, filters):
    memory, disk = traces
    expected = list(memory.iter_matching(**filters))
    assert list(disk.iter_matching(**filters)) == expected
    with disk.select(**filters) as selected:
        assert len(selected) == len(expected)
        assert list(selected) == expected
        assert list(selected[::-3]) == expected[::-3]
        if expected:
            assert selected[-1] == expected[-1]


def test_regex_semantics_include_unicode_case_folding():
    data = list(entries(4))
    for e, mnemonic in zip(data, ['k', 'K', '\u212a', 'keep']):
        e['decoded']['mnemonic'] = mnemonic
    with InstructionResults.collect(data, dict) as store:
        assert eager(store).find_mnemonic('^k$') == data[:3]


def test_arbitrary_predicate_paged_results_and_error_cleanup(traces):
    memory, disk = traces
    predicate = lambda e: e['rd_addr'] % 2 == 0
    with disk.select(predicate, time_lo=40, time_hi=90) as selected:
        assert list(selected) == list(memory.iter_matching(predicate, time_lo=40, time_hi=90))
    def broken(e):
        if e['time'] > 20:
            raise ValueError('bad predicate')
        return True
    with pytest.raises(ValueError, match='bad predicate'):
        disk.select(broken)
    assert disk.entries._db.execute("SELECT name FROM sqlite_temp_master WHERE name LIKE 'selection_%'").fetchall() == []


def test_sql_cancel_preserves_previous_selection(traces):
    _, disk = traces
    with disk.select(hart_id=1) as previous:
        class CancelDuringSQLite:
            calls = 0
            def is_set(self):
                self.calls += 1
                return self.calls > 3
        cancel = CancelDuringSQLite()
        with pytest.raises(OperationCancelled):
            disk.select(cancel=cancel)
        assert cancel.calls > 3
        assert len(previous) and previous[0]['hart_id'] == 1
        assert len(disk.entries._db.execute("SELECT name FROM sqlite_temp_master WHERE name LIKE 'selection_%'").fetchall()) == 1


def test_selection_and_statistics_do_not_deserialize_history(monkeypatch):
    with InstructionResults.collect(entries(30000), dict) as store:
        def forbidden(_):
            raise AssertionError('index operation decoded an instruction payload')
        monkeypatch.setattr(store, '_restore', forbidden)
        trace = eager(store)
        tracemalloc.start()
        try:
            with trace.select() as selected:
                assert len(selected) == 30000
            assert sum(s['count'] for s in trace.fairness_analysis().values()) == 30000
            assert len(trace.all_hart_ipc()) == 8
            assert tracemalloc.get_traced_memory()[1] < 1024 * 1024
        finally:
            tracemalloc.stop()


def test_aggregates_match_independent_reference(traces):
    memory, disk = traces
    data = memory.entries
    expected = {}
    for h in range(8):
        times = [e['time'] for e in data if e['hart_id'] == h]
        gaps = [b - a for a, b in zip(times, times[1:])]
        avg = sum(gaps) / len(gaps)
        expected[h] = {'count': len(times), 'avg_gap': avg, 'max_gap': max(gaps),
                       'max_gap_time': times[gaps.index(max(gaps))], 'starved': max(gaps) > 3 * avg}
    for trace in (memory, disk):
        assert trace.fairness_analysis() == expected
        for h in range(8):
            times = [e['time'] for e in data if e['hart_id'] == h and 10 <= e['time'] <= 100]
            assert trace.hart_ipc(h, 10, 100) == len(times) / (times[-1] - times[0])
        heatmap = trace.scheduling_heatmap(10, 100, 11)
        assert sum(map(sum, heatmap['heatmap'])) == sum(10 <= e['time'] <= 100 for e in data)
    assert memory.summary() == disk.summary()


def test_gap_statistics_keep_same_tick_and_first_maximum():
    data = list(entries(7))
    for e, t in zip(data, [0, 0, 2, 4, 6, 30, 30]):
        e['time'], e['hart_id'] = t, 0
    with InstructionResults.collect(data, dict) as store:
        trace = eager(store)
        assert trace.fairness_analysis()[0] == {'count': 7, 'avg_gap': 5, 'max_gap': 24, 'max_gap_time': 6, 'starved': True}
        assert trace.find_starvation_events() == [{'hart_id': 0, 'time': 6, 'gap': 24, 'avg_gap': 5, 'ratio': 4.8}]


def test_register_snapshots_and_divergence(traces):
    memory, disk = traces
    for h in range(8):
        expected = [0] + [None] * 31
        for e in memory.entries:
            if e['hart_id'] == h and e['time'] <= 99:
                expected[e['rd_addr']] = e['rd_data']
        assert disk.register_snapshot(h, 99) == expected
    assert disk.register_panel_state(99) == memory.register_panel_state(99)
    for a, b in [(0, 0), (0, 1), (1, 30)]:
        assert disk.thread_divergence(a, b) == memory.thread_divergence(a, b)


def test_contention_summary_matches_legacy_hazards():
    rng = random.Random(42)
    data = list(entries(300))
    for e in data:
        e['mem_addr'] = rng.randrange(3)
        e['hart_id'] = rng.randrange(3)
        e['decoded']['is_store'] = bool(rng.randrange(2))
    legacy = eager(data).shared_resource_contention()
    with InstructionResults.collect(data, dict) as store:
        overview = eager(store).contention_overview()
        assert overview['count'] == len(legacy)
        by_addr = {c['addr']: c for c in legacy}
        for c in overview['items']:
            expected = by_addr[c['addr']]
            for field in ('n_harts', 'harts', 'has_waw', 'has_raw', 'has_war'):
                assert c[field] == expected[field]
            assert c['access_count'] == len(expected['accesses'])


def test_viewer_queries_and_cancellation_keep_previous_results(traces):
    from alwavelets.ui_debug import DebugActionsMixin
    from alwavelets.parsers import VCDFile
    _, trace = traces
    class Viewer(DebugActionsMixin):
        def _show_message(self, text):
            self.message = text
        def _ensure_cursor_visible(self):
            pass
    viewer = Viewer()
    viewer.instr_trace, viewer.vcd = trace, VCDFile()
    viewer.time_start, viewer.time_end, viewer.cursor_time, viewer.wave_width = 0, 100, 0, 80
    viewer.markers = []
    viewer._run_instruction_query('time 0 100')
    job = viewer._analysis_job
    result = job.future.result(timeout=5)
    job.complete(result)
    job.result_claimed = True
    job.close()
    viewer._analysis_job = None
    assert len(result.matches) == 202 and len(viewer.markers) == 100
    viewer._run_instruction_query('hart 3')
    job = viewer._analysis_job
    cancelled_result = job.future.result(timeout=5)
    job.close()
    viewer._analysis_job = None
    assert cancelled_result.matches.closed and viewer._instruction_query_result is result
    assert result.matches[-1]['time'] == 100
    viewer._clear_decode_results(all_results=True)
    assert result.matches.closed


@pytest.mark.parametrize('query', ['summary', 'ipc', 'fairness', 'diverge 0 1', 'contention', 'heatmap', 'regs 3', 'harts',
                                 'pc 0 4', 'hart 2', 'threadid 36893488147419103233', 'mn= add', 'lambda e: e["stall"]'])
def test_worker_commands_leave_bounded_ui_payloads(traces, query):
    _, trace = traces
    result = run_query(trace, query, 0, 300, 90, 80)
    try:
        assert result.message and len(result.markers) <= 100
    finally:
        result.close()


def test_eager_no_match_scan_can_be_cancelled():
    cancel = threading.Event()
    def records():
        for i, entry in enumerate(entries(10000)):
            if i == 4096:
                cancel.set()
            yield entry
    trace = eager(records())
    with pytest.raises(OperationCancelled):
        list(trace.iter_matching(hart_id=50, cancel=cancel))


def test_library_disk_build_cancel_and_replacement(monkeypatch):
    from alwavelets.parsers import VCDFile
    from alwavelets.persistence import SignalTagStore
    with VCDFile() as owner:
        trace, tags = InstructionTrace(), SignalTagStore()
        monkeypatch.setattr(trace, 'iter_entries', lambda *args, **kwargs: entries(100))
        assert trace.build(owner, tags, storage='disk') == 100
        original = trace.entries
        cancel = threading.Event()
        cancel.set()
        with pytest.raises(OperationCancelled):
            trace.build(owner, tags, storage='disk', cancel=cancel)
        assert trace.entries is original and trace.built
        assert trace.build(owner, tags) == 100  # default list-returning compatibility
        assert isinstance(trace.entries, list) and original._closed.is_set()


def test_register_panel_runs_on_worker(traces, monkeypatch):
    import curses
    from types import SimpleNamespace
    from alwavelets.ui_drawing import DrawingMixin
    _, trace = traces
    main = threading.get_ident()
    original = trace.register_panel_state
    calls = []
    def compute(*args, **kwargs):
        assert threading.get_ident() != main
        return original(*args, **kwargs)
    monkeypatch.setattr(trace, 'register_panel_state', compute)
    monkeypatch.setattr(curses, 'color_pair', lambda _: 0)
    viewer = SimpleNamespace(instr_trace=trace, cursor_time=120, height=30, width=140,
                              C_HEADER=1, C_HIGH=2, C_MENUSEP=3, C_VAL=4, C_MENUKEY=5,
                              stdscr=SimpleNamespace(addstr=lambda *args: calls.append(args), addch=lambda *args: None))
    try:
        DrawingMixin.draw_register_panel(viewer)
        for future in viewer._queries.pending.values():
            future.result(timeout=5)
        viewer._queries.poll()
        DrawingMixin.draw_register_panel(viewer)
        assert any('PC=0x' in args[2] for args in calls)
    finally:
        viewer._queries.close()


def test_closing_index_invalidates_selections(traces):
    _, trace = traces
    selection = trace.select()
    trace.entries.close()
    with pytest.raises(ValueError, match='closed'):
        selection[0]
    selection.close()
