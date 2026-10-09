"""All-result navigation, bounded background pages and atomic streaming export."""
import csv
import curses
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from alwavelets.background import BackgroundQueries
from alwavelets.decoders import InstructionTrace
from alwavelets.exporting import write_instruction_csv
from alwavelets.instruction_store import InstructionResults
from alwavelets.instruction_ui import run_query
from alwavelets.parsers import VCDFile
from alwavelets.streaming import OperationCancelled
from alwavelets.ui_debug import DebugActionsMixin
from tools.stress_instructions import entries


class Viewer(DebugActionsMixin):
    height, width = 30, 140
    time_start, time_end, cursor_time, wave_width = 0, 100000, 0, 100

    def _show_message(self, message):
        self.message = message

    def _ensure_cursor_visible(self):
        self.jumped = self.cursor_time


def settle(viewer):
    viewer.poll_instruction_results()
    for future in list(viewer._queries.pending.values()):
        future.result(timeout=5)
    viewer._queries.poll()
    viewer.poll_instruction_results()


@pytest.fixture
def viewer():
    with VCDFile() as vcd:
        viewer = Viewer()
        viewer.vcd = vcd
        viewer.instr_trace = InstructionTrace()
        viewer.instr_trace.entries = InstructionResults.collect(entries(10000), dict, owner=vcd)
        viewer.instr_trace.built = True
        viewer.markers = []
        viewer._instruction_query_result = run_query(viewer.instr_trace, 'time 0 10000', 0, 10000, 0, 100)
        viewer._queries = BackgroundQueries()
        viewer._reset_instruction_browser()
        try:
            yield viewer
        finally:
            job = getattr(viewer, '_analysis_job', None)
            if job:
                job.close()
            viewer._queries.close()
            viewer._clear_decode_results(all_results=True)


def test_last_match_navigation_loads_one_page_on_worker(viewer, monkeypatch):
    matches = viewer._instruction_matches()
    original = matches.read_page
    requested = []
    main = threading.get_ident()
    def read_page(start, size, cancel):
        assert threading.get_ident() != main
        requested.append((start, size))
        return original(start, size, cancel)
    monkeypatch.setattr(matches, 'read_page', read_page)
    viewer._cmd_instruction_results('goto 10000')
    settle(viewer)
    assert viewer.cursor_time == matches[-1]['time']
    assert requested == [(9999 // 11 * 11, 11)]
    viewer.instruction_results_handle_key(ord('p'))
    settle(viewer)
    assert viewer._instruction_result_index == 9998
    assert viewer.cursor_time == matches[-2]['time']
    assert len(requested) == 2
    viewer.instruction_results_handle_key(ord('p'))
    settle(viewer)
    assert len(requested) == 2  # previous match within the same page
    viewer.instruction_results_handle_key(10)
    assert not viewer.show_instruction_results


def test_page_boundaries_jump_validation_and_resize(viewer):
    viewer._cmd_instruction_results()
    settle(viewer)
    viewer.instruction_results_handle_key(curses.KEY_NPAGE)
    settle(viewer)
    assert viewer._instruction_result_index == 11
    viewer._cmd_instruction_results('goto 0')
    assert viewer._instruction_result_index == 11 and 'between' in viewer.message
    viewer._cmd_instruction_results('goto 10001')
    assert viewer._instruction_result_index == 11
    viewer.height = 50
    settle(viewer)
    assert viewer._instruction_page_size() == 21
    viewer.instruction_results_handle_key(curses.KEY_END)
    settle(viewer)
    assert viewer._instruction_result_index == 9999
    viewer.instruction_results_handle_key(curses.KEY_HOME)
    settle(viewer)
    assert viewer._instruction_result_index == 0


def test_query_summary_keeps_selection_and_new_query_resets_browser(viewer):
    original = viewer._instruction_matches()
    viewer._cmd_instruction_results('goto 300')
    settle(viewer)
    def query(text):
        viewer._run_instruction_query(text)
        job = viewer._analysis_job
        result = job.future.result(timeout=5)
        job.complete(result)
        job.result_claimed = True
        job.close()
        viewer._analysis_job = None
    query('summary')
    assert viewer._instruction_matches() is original
    query('hart 3')
    assert original.closed and not viewer.show_instruction_results
    assert viewer._instruction_result_index == 0
    viewer._cmd_instruction_results()
    settle(viewer)
    assert viewer._instruction_matches()[0]['hart_id'] == 3


def test_late_page_cannot_jump_after_reset(viewer):
    viewer._cmd_instruction_results('goto 9999')
    viewer.poll_instruction_results()
    viewer._reset_instruction_browser()
    viewer._queries.poll()
    viewer.poll_instruction_results()
    assert viewer.cursor_time == 0 and not viewer.show_instruction_results


def test_browser_render_and_clear(viewer):
    lines = []
    viewer.stdscr = SimpleNamespace(addstr=lambda row, col, text, *args: lines.append(text))
    viewer._cmd_instruction_results('goto 5000')
    settle(viewer)
    viewer.draw_instruction_results()
    assert any('5,000 / 10,000' in line for line in lines)
    assert len(lines) == viewer._instruction_page_size() + 3
    viewer._clear_decode_results(all_results=True)
    assert viewer._instruction_matches() is None and not viewer.show_instruction_results


def test_streaming_export_includes_all_matches(viewer, tmp_path):
    destination = tmp_path / 'all matches.csv'
    viewer._cmd_instruction_results('export ' + str(destination))
    job = viewer._analysis_job
    count = job.future.result(timeout=10)
    job.complete(count)
    job.close()
    viewer._analysis_job = None
    with destination.open() as f:
        rows = list(csv.DictReader(f))
    assert count == len(rows) == 10000
    assert int(rows[-1]['rd_data']) == 9999
    assert int(rows[-1]['pc']) >= 1 << 63
    assert rows[0]['time'] == rows[1]['time']
    assert len(viewer._instruction_matches()) == 10000


def test_cancelled_export_preserves_existing_destination(viewer, tmp_path):
    destination = tmp_path / 'saved.csv'
    destination.write_text('previous output')
    cancel = threading.Event()
    def progress(done, total):
        cancel.set()
    with pytest.raises(OperationCancelled):
        write_instruction_csv(viewer._instruction_matches(), destination, cancel=cancel, progress=progress)
    assert destination.read_text() == 'previous output'
    assert not list(tmp_path.glob('.alwavelets-export-*'))
    assert viewer._instruction_matches()[-1]['rd_data'] == 9999


def test_export_guards_source_and_active_jobs(viewer, tmp_path):
    source = tmp_path / 'source.vcd'
    source.write_text('trace')
    viewer.vcd._filename = str(source)
    viewer._export_instruction_results(str(source))
    assert 'different' in viewer.message and source.read_text() == 'trace'
    viewer._export_instruction_results(viewer._instruction_matches().source.path)
    assert 'different' in viewer.message
    viewer._analysis_job = SimpleNamespace(close=lambda: None)
    viewer._export_instruction_results(str(tmp_path / 'out.csv'))
    assert 'already running' in viewer.message
    viewer._analysis_job = None


def test_export_memory_does_not_scale_with_matches(viewer, tmp_path):
    import tracemalloc
    tracemalloc.start()
    try:
        assert write_instruction_csv(viewer._instruction_matches(), tmp_path / 'out.csv') == 10000
        assert tracemalloc.get_traced_memory()[1] < 1024 * 1024
    finally:
        tracemalloc.stop()


def test_small_and_empty_pages(viewer):
    matches = viewer._instruction_matches()
    assert len(matches.read_page(9999, 20)) == 1
    assert matches.read_page(10000) == []
    with pytest.raises(ValueError):
        matches.read_page(0, 100000)
    path = Path(matches.source.path)
    assert path.exists()


def test_enter_waits_for_selected_row_before_closing(viewer, monkeypatch):
    ready = threading.Event()
    original = viewer._instruction_matches().read_page
    def delayed(*args):
        assert ready.wait(5)
        return original(*args)
    monkeypatch.setattr(viewer._instruction_matches(), 'read_page', delayed)
    viewer._cmd_instruction_results('goto 9000')
    viewer.instruction_results_handle_key(10)
    assert viewer.show_instruction_results
    ready.set()
    settle(viewer)
    assert not viewer.show_instruction_results
    assert viewer.cursor_time == viewer._instruction_matches()[8999]['time']
