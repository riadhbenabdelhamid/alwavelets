"""Paged decoder records, bounded memory, and viewer ownership/cancellation."""
from pathlib import Path
import threading
import tracemalloc
from types import SimpleNamespace

import pytest

from alwavelets.background import AnalysisJob
from alwavelets.decoders import DecodedFrame, BusTransaction, SPIDecoder
from alwavelets.parsers import VCDFile
from alwavelets.results import PagedResults, close_results
from alwavelets.streaming import OperationCancelled
from alwavelets.ui_debug import DebugActionsMixin
from alwavelets.core import VCDSignal


def fields(item):
    return tuple(getattr(item, name) for name in item.__slots__)


def test_frames_retain_deltas_overlap_slices_and_large_times():
    t = 2**60
    frames = [DecodedFrame(t, t + 100, 'long', 'x'),
              DecodedFrame(t + 5, t + 5, 'S'), DecodedFrame(t + 5, t + 8, 'byte'),
              DecodedFrame(t + 5, t + 5, 'P'), DecodedFrame(t + 10, t + 20, 'last')]
    with PagedResults.collect(iter(frames), DecodedFrame) as results:
        assert len(results) == len(frames)
        assert [fields(x) for x in results] == [fields(x) for x in frames]
        assert fields(results[-1]) == fields(frames[-1])
        assert [fields(x) for x in results[::-2]] == [fields(x) for x in frames[::-2]]
        assert [x.label for x in results.iter_window(t + 5, t + 5)] == ['long', 'S', 'byte', 'P']
        assert [x.label for x in results.iter_window(t + 50, t + 60)] == ['long']
        results[0].label = 'changed copy'
        assert results[0].label == 'long'
        with pytest.raises(IndexError):
            results[len(results)]
    assert not Path(results.path).exists()


def test_bus_payloads_and_instruction_records_are_exact():
    txn = BusTransaction('AXI', 'read', 5)
    txn.data, txn.id, txn.t_end = [0, (1 << 65536) - 1], (1 << 64) - 1, 10
    txn.details = {'error': None, 'beats': ['x', 'z']}
    with PagedResults.collect(iter([txn]), BusTransaction) as results:
        assert fields(results[0]) == fields(txn)
    entries = [{'time': i, 'pc': 4 * i, 'decoded': {'asm': 'nop'}, 'rd_data': 1 << 16000} for i in range(3)]
    with PagedResults.collect(iter(entries), dict) as results:
        assert list(results) == entries
        assert list(results.iter_window(1, 1)) == entries[1:2]


def test_collect_memory_is_independent_of_frame_count():
    tracemalloc.start()
    try:
        frames = (DecodedFrame(i, i + 1, 'label-%d' % i) for i in range(30000))
        with PagedResults.collect(frames, DecodedFrame) as results:
            _, peak = tracemalloc.get_traced_memory()
            assert len(results) == 30000 and results[-1].label == 'label-29999'
            assert peak < 1024 * 1024  # Python memory; SQLite separately caps its cache
    finally:
        tracemalloc.stop()


def test_failure_and_cancel_remove_files_and_close_source(tmp_path, monkeypatch):
    import alwavelets.results as module
    monkeypatch.setattr(module.tempfile, 'tempdir', str(tmp_path))
    owner, cancel, closed = VCDFile(), threading.Event(), []
    def frames():
        try:
            for i in range(10000):
                yield DecodedFrame(i, i + 1, 'x')
        finally:
            closed.append(True)
    with pytest.raises(OperationCancelled):
        PagedResults.collect(frames(), DecodedFrame, owner=owner, cancel=cancel,
                             progress=lambda *_: cancel.set())
    assert closed == [True] and owner._result_stores == set()
    assert list(tmp_path.iterdir()) == []
    def broken():
        yield DecodedFrame(0, 1, 'ok')
        raise ValueError('decoder failed')
    with pytest.raises(ValueError, match='decoder failed'):
        PagedResults.collect(broken(), DecodedFrame, owner=owner)
    assert owner._result_stores == set() and list(tmp_path.iterdir()) == []


def test_trace_close_releases_results_and_active_readers():
    owner = VCDFile()
    results = PagedResults.collect((DecodedFrame(i, i + 1, 'a') for i in range(100)), DecodedFrame, owner=owner)
    reader = iter(results)
    assert next(reader).t_start == 0
    owner.close()
    assert list(reader) == []
    assert not Path(results.path).exists()
    owner.close()


def test_completed_but_unclaimed_job_disposes_result():
    job = AnalysisJob(lambda cancel, progress: PagedResults.collect([DecodedFrame(0, 1, 'a')], DecodedFrame),
                      lambda result: None, discard=close_results)
    result = job.future.result(timeout=5)
    assert Path(result.path).exists()
    job.close()  # also covers cancellation/reload between completion and polling
    assert not Path(result.path).exists()


class Viewer(DebugActionsMixin):
    def __init__(self):
        self.vcd = VCDFile()
        self.decode_overlays = {}
        self.bus_transactions = []
        self.markers = []
        self.messages = []
        self.time_start, self.time_end = 0, 100

    def _show_message(self, message):
        self.messages.append(message)

    def finish(self):
        job = self._analysis_job
        result = job.future.result(timeout=5)
        job.complete(result)
        job.result_claimed = True
        job.close()
        self._analysis_job = None
        return result


def test_viewer_protocol_replacement_clear_and_cancellation():
    viewer = Viewer()
    clock, data = VCDSignal('clk', '!'), VCDSignal('data', '"')
    for i in range(100):
        clock.add_change(i, str(i % 2))
    data.add_change(0, '1')
    viewer.vcd.signals = {'!': clock, '"': data}
    decoder = SPIDecoder(clock, data)
    viewer._decode_protocol(decoder, data, 'SPI')
    first = viewer.finish()
    assert isinstance(viewer.decode_overlays['"'], PagedResults)
    assert [fields(x) for x in first] == [fields(x) for x in decoder.decode(0, 100)]
    viewer._decode_protocol(decoder, data, 'SPI')
    second = viewer.finish()
    assert not Path(first.path).exists() and Path(second.path).exists()
    viewer._decode_protocol(decoder, data, 'SPI')
    pending = viewer._analysis_job.future.result(timeout=5)
    viewer._clear_decode_results()
    assert viewer._analysis_job is None and not viewer.decode_overlays
    assert not Path(pending.path).exists() and not Path(second.path).exists()
    viewer.vcd.close()


def test_viewer_bus_results_keep_all_transactions_with_50_markers():
    viewer = Viewer()
    def transactions(**kwargs):
        for i in range(10000):
            txn = BusTransaction('AXI4', 'write', i)
            txn.addr = i
            yield txn
    viewer._decode_bus(SimpleNamespace(iter_decode=transactions), 'AXI')
    results = viewer.finish()
    assert len(results) == 10000 and results[-1].addr == 9999
    assert len(viewer.markers) == 50
    viewer._clear_decode_results(all_results=True)
    assert not Path(results.path).exists() and not viewer.bus_transactions


def test_overlay_matches_paint_order_with_bounded_output():
    frames = [DecodedFrame(0, 50, 'first'), DecodedFrame(10, 50, 'second'),
              DecodedFrame(49, 50, 'narrow'), DecodedFrame(80, 99, 'last')]
    expected = {}
    for frame in frames:
        left, right = frame.t_start * 20 // 100, frame.t_end * 20 // 100
        if right > left:
            expected.update(enumerate(frame.label[:right-left].center(right-left), left))
    with PagedResults.collect(frames, DecodedFrame) as results:
        painted = {col + i: char for col, text in results.overlay(0, 100, 20) for i, char in enumerate(text)}
        assert painted == expected


def test_viewer_reads_paged_overlays_on_worker(monkeypatch):
    from alwavelets.ui import Alwavelets
    import curses
    monkeypatch.setattr(curses, 'color_pair', lambda _: 0)
    main_thread = threading.get_ident()
    calls = []
    with PagedResults.collect([DecodedFrame(0, 100, 'A')], DecodedFrame) as results:
        original = results.overlay
        def overlay(*args, **kwargs):
            assert threading.get_ident() != main_thread
            return original(*args, **kwargs)
        monkeypatch.setattr(results, 'overlay', overlay)
        viewer = SimpleNamespace(vcd=VCDFile(), decode_overlays={'!': results}, time_start=0, time_end=100,
                                  wave_width=20, name_width=10, C_BUSLBL=1,
                                  stdscr=SimpleNamespace(addstr=lambda *args: calls.append(args)))
        try:
            Alwavelets.draw_decode_overlay(viewer, 3, '!')
            for future in viewer._queries.pending.values():
                future.result(timeout=5)
            viewer._queries.poll()
            Alwavelets.draw_decode_overlay(viewer, 3, '!')
            assert calls[-1][:3] == (3, 11, 'A'.center(20))
        finally:
            viewer._queries.close()


def test_obsolete_overlay_read_is_cancelled():
    from alwavelets.background import BackgroundQueries
    started, stopped = threading.Event(), threading.Event()
    def slow(cancel):
        started.set()
        assert cancel.wait(5)
        stopped.set()
        return 'obsolete'
    queries = BackgroundQueries()
    try:
        queries.request('decodes', 1, 'a', slow, '', cancellable=True)
        assert started.wait(5)
        queries.request('decodes', 2, 'a', lambda: 'current', '')
        assert stopped.wait(5)
        for future in queries.pending.values():
            future.result(timeout=5)
        queries.poll()
        assert queries.request('decodes', 2, 'a', lambda: None, '') == 'current'
    finally:
        queries.close()


def test_viewer_instruction_build_uses_paged_entries(monkeypatch):
    from alwavelets.decoders import InstructionTrace, RV32Decoder
    from alwavelets.persistence import SignalTagStore
    viewer = Viewer()
    viewer.instr_trace, viewer.isa_decoder = InstructionTrace(), RV32Decoder()
    viewer.signal_tags = SignalTagStore()
    viewer.signal_tags.set_tag('isa.instr.writeback', 'instr')
    seen = []
    def entries(trace, vcd, tags, decoder, **kwargs):
        seen.append(tags)
        yield from ({'time': i, 'pc': i * 4, 'decoded': {'mnemonic': 'nop'}} for i in range(1000))
    monkeypatch.setattr(InstructionTrace, 'iter_entries', entries)
    viewer._build_trace()
    result = viewer.finish()
    assert viewer.instr_trace.built and viewer.instr_trace.entries is result
    assert seen[0] is not viewer.signal_tags
    assert viewer.instr_trace.entries[-1]['pc'] == 3996
    assert viewer.instr_trace.summary().startswith('1000 instructions')
    viewer._clear_decode_results(all_results=True)
    assert not viewer.instr_trace.built and not Path(result.path).exists()
