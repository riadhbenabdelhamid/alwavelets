"""Durable VCD prefixes survive cancellation, crashes and concurrent opens."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

import pytest

from alwavelets import storage
from alwavelets.index_cache import purge_partial_indexes
from alwavelets.parsers import VCDFile
from tools.stress_vcd import create_vcd


@pytest.fixture(autouse=True)
def cache_home(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))


def metadata(path):
    with sqlite3.connect(storage.partial_index_path(path)) as db:
        return json.loads(db.execute('SELECT value FROM meta WHERE key="trace"').fetchone()[0])


def interrupt_after_checkpoint(path, monkeypatch):
    cancel = threading.Event()
    original = storage.PageWriter.checkpoint

    def checkpoint(self, *args):
        original(self, *args)
        if self.changes:
            cancel.set()

    with monkeypatch.context() as patch:
        patch.setattr(storage, 'CHECKPOINT_BYTES', 0)
        patch.setattr(storage.PageWriter, 'checkpoint', checkpoint)
        with pytest.raises(ValueError, match='cancelled'):
            storage.open_trace(path, cancel=cancel, memory_mib=0.1)
    assert not Path(storage.index_path(path)).exists()
    return metadata(path)


def assert_matches(trace, eager):
    assert trace.signal_order == eager.signal_order
    assert trace.max_time == eager.max_time
    count = 0
    for key, expected in eager.signals.items():
        actual = trace.signals[key]
        assert actual.changes == expected.changes
        assert actual.numeric_range_in_window(0, eager.max_time) == expected.numeric_range_in_window(0, eager.max_time)
        count += expected.num_changes()
    assert trace._store.total_changes == count


@pytest.mark.parametrize('mode', ['comment', 'blackout', 'same_tick'])
@pytest.mark.parametrize('disordered', [False, True])
def test_resume_parser_state_aliases_and_summary_groups(tmp_path, monkeypatch, mode, disordered):
    monkeypatch.setattr(storage, 'PAGE_EVENTS', 2)
    original_init = storage.PageWriter.__init__

    def small_groups(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.summary_group_limit = 1

    monkeypatch.setattr(storage.PageWriter, '__init__', small_groups)
    path = tmp_path / 'directives.vcd'
    header = ('$timescale 10 ps $end\n$scope module top $end\n'
              '$var wire 1 ! bit $end\n$var wire 1 ! alias $end\n'
              '$var wire 16 b bus $end\n$var real 64 r real $end\n'
              '$var wire 128 w wide $end\n$upscope $end\n$enddefinitions $end\n')

    def events(ticks):
        return ''.join(f'#{t}\n{t % 2}!\nb{t:016b} b\nr{t / 4 - 2} r\nb{t:0128b} w\n' for t in ticks)

    prefix = header + events(reversed(range(100)) if disordered else range(100))
    padding = 65535 - len(prefix)
    prefix += '$comment ' + ' ' * (padding - len('$comment $end\n')) + '$end\n'
    assert len(prefix) == 65535
    marker = {'comment': '$comment\n', 'blackout': '$dumpoff\n', 'same_tick': '1!\n'}[mode]
    after = {'comment': '#99999\n1!\n$end\n', 'blackout': '#101\n0!\n$dumpon\n', 'same_tick': '0!\n'}[mode]
    path.write_text(prefix + marker + after + events(range(102, 602)))
    eager = VCDFile.parse(path)
    meta = interrupt_after_checkpoint(path, monkeypatch)
    checkpoint = meta['checkpoint']
    assert checkpoint['offset'] == len(prefix + marker)
    assert checkpoint['state'][2] == (mode == 'comment')
    assert checkpoint['state'][1] == (mode != 'blackout')
    assert checkpoint['disordered'] == disordered
    assert checkpoint['monotonic'] != disordered
    assert not meta['complete']

    def forbidden(*args, **kwargs):
        raise AssertionError('resuming must not scan the header again')

    monkeypatch.setattr(VCDFile, 'parse_header', forbidden)
    progress = []
    with storage.open_trace(path, progress_cb=lambda done, total: progress.append(done), memory_mib=0.1) as resumed:
        assert resumed._resume_offset == checkpoint['offset']
        assert progress[0] == checkpoint['offset']
        assert_matches(resumed, eager)
        assert resumed._store.db.execute('SELECT MAX(level) FROM summaries').fetchone()[0] >= 2
        assert resumed._store.peak_cache_bytes <= resumed._store.memory_bytes
    assert not Path(storage.partial_index_path(path)).exists()
    with storage.open_trace(path) as cached:
        assert_matches(cached, eager)


def test_uncheckpointed_published_values_are_replayed_once(tmp_path, monkeypatch):
    path = tmp_path / 'trace.vcd'
    create_vcd(path, 32, 20000, 1, True)
    eager = VCDFile.parse(path)
    original = storage.PageWriter.checkpoint
    cancel = threading.Event()
    first = []
    dirty_counts = []

    def checkpoint(self, *args):
        if not first:
            original(self, *args)
            if self.changes:
                first.append(metadata(path))
        else:
            self.publish(True)
            dirty_counts.append(self.changes)
            cancel.set()

    with monkeypatch.context() as patch:
        patch.setattr(storage, 'CHECKPOINT_BYTES', 0)
        patch.setattr(storage.PageWriter, 'checkpoint', checkpoint)
        with pytest.raises(ValueError, match='cancelled'):
            storage.open_trace(path, cancel=cancel, memory_mib=0.1)
    assert dirty_counts[0] > first[0]['changes']
    assert metadata(path) == first[0]
    with storage.open_trace(path, memory_mib=0.1) as resumed:
        assert_matches(resumed, eager)


@pytest.mark.parametrize('damage', ['append', 'replace_same_stat', 'corrupt', 'version'])
def test_changed_sources_and_invalid_checkpoints_restart(tmp_path, monkeypatch, damage):
    path = tmp_path / 'trace.vcd'
    create_vcd(path, 16, 6000, 1, True)
    interrupt_after_checkpoint(path, monkeypatch)
    if damage == 'append':
        with path.open('a') as output:
            output.write('#99999\n1s0\n')
    elif damage == 'replace_same_stat':
        stat = path.stat()
        replacement = tmp_path / 'replacement.vcd'
        replacement.write_bytes(path.read_bytes().replace(b'0s0\n', b'1s0\n'))
        os.utime(replacement, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        os.replace(replacement, path)
    elif damage == 'corrupt':
        Path(storage.partial_index_path(path)).write_bytes(b'broken checkpoint')
    else:
        meta = metadata(path)
        meta['checkpoint']['version'] += 1
        with sqlite3.connect(storage.partial_index_path(path)) as db:
            db.execute('UPDATE meta SET value=? WHERE key="trace"', (json.dumps(meta),))
    eager = VCDFile.parse(path)
    with storage.open_trace(path) as rebuilt:
        assert rebuilt._resume_offset == 0
        assert_matches(rebuilt, eager)


def test_process_exit_rolls_back_after_checkpoint(tmp_path):
    path = tmp_path / 'crash.vcd'
    create_vcd(path, 32, 12000, 1, True)
    code = '''
import os, sys
from alwavelets import storage
storage.CHECKPOINT_BYTES = 0
original = storage.PageWriter.checkpoint
saved = False
def checkpoint(self, *args):
    global saved
    if saved:
        self.publish(True)
        os._exit(17)
    original(self, *args)
    saved = bool(self.changes)
storage.PageWriter.checkpoint = checkpoint
storage.open_trace(sys.argv[1], memory_mib=0.1)
'''
    result = subprocess.run([sys.executable, '-c', code, str(path)], timeout=20)
    assert result.returncode == 17
    assert not Path(storage.index_path(path)).exists()
    with storage.open_trace(path, memory_mib=0.1) as resumed:
        assert resumed._resume_offset > 65536
        assert_matches(resumed, VCDFile.parse(path))


def test_busy_index_falls_back_and_purge_skips_open_viewer(tmp_path, monkeypatch):
    path = tmp_path / 'trace.vcd'
    create_vcd(path, 16, 6000, 1, True)
    entered, release = threading.Event(), threading.Event()
    original = storage.PageWriter.add_vcd_batch

    def pause(self, *args):
        if threading.current_thread().name == 'wavelet-index' and not entered.is_set():
            entered.set()
            assert release.wait(10)
        original(self, *args)

    monkeypatch.setattr(storage.PageWriter, 'add_vcd_batch', pause)
    first = storage.open_trace(path, background=True)
    try:
        assert entered.wait(5)
        assert purge_partial_indexes() == (0, 0, 1)
        with storage.open_trace(path) as second:
            assert second._store.temporary is not None
            assert second._resume_offset == 0
            assert_matches(second, VCDFile.parse(path))
        assert not Path(storage.index_path(path)).exists()
        first._store.cancel.set()
    finally:
        release.set()
        first.close()
    count, size, skipped = purge_partial_indexes()
    assert count == 1 and size > 0 and skipped == 0
    assert not Path(storage.partial_index_path(path)).exists()
    assert path.exists()


@pytest.mark.parametrize('global_switch', [False, True])
def test_no_cache_ignores_saved_checkpoint_and_removes_temporary_index(tmp_path, monkeypatch, global_switch):
    from alwavelets import cache
    path = tmp_path / 'trace.vcd'
    create_vcd(path, 16, 6000, 1, True)
    before = interrupt_after_checkpoint(path, monkeypatch)
    monkeypatch.setattr(cache, 'ENABLED', not global_switch)
    with storage.open_trace(path, use_cache=global_switch) as trace:
        assert trace._resume_offset == 0
        temporary_path = Path(trace._store.path)
        assert trace._store.temporary is not None
    assert not temporary_path.exists()
    assert metadata(path) == before


def test_final_commit_can_be_recovered_before_rename(tmp_path, monkeypatch):
    path = tmp_path / 'trace.vcd'
    create_vcd(path, 16, 1000, 1, True)
    with monkeypatch.context() as patch:
        def cannot_rename(*args):
            raise OSError('rename unavailable')
        patch.setattr(storage.os, 'replace', cannot_rename)
        with storage.open_trace(path):
            pass
    assert metadata(path)['complete']
    assert not Path(storage.index_path(path)).exists()
    with storage.open_trace(path) as recovered:
        assert_matches(recovered, VCDFile.parse(path))
    assert Path(storage.index_path(path)).exists()
    assert not Path(storage.partial_index_path(path)).exists()


def test_cancelled_final_sort_resumes_from_unsorted_checkpoint(tmp_path, monkeypatch):
    path = tmp_path / 'disordered.vcd'
    path.write_text('$var wire 1 ! bit $end\n$enddefinitions $end\n' +
                    ''.join(f'#{i // 2}\n{i % 2}!\n' for i in reversed(range(12000))))
    original = storage.PageWriter.add
    calls = []

    def cancel_sort(self, *args):
        original(self, *args)
        calls.append(1)
        if len(calls) == 100:
            self.store.cancel.set()

    with monkeypatch.context() as patch:
        patch.setattr(storage.PageWriter, 'add', cancel_sort)
        with pytest.raises(ValueError, match='cancelled'):
            storage.open_trace(path, memory_mib=0.1)
    meta = metadata(path)
    assert meta['checkpoint']['offset'] == path.stat().st_size
    assert meta['checkpoint']['disordered']
    with storage.open_trace(path, memory_mib=0.1) as resumed:
        assert_matches(resumed, VCDFile.parse(path))


def test_close_cancels_background_load_but_keeps_checkpoint(tmp_path, monkeypatch):
    path = tmp_path / 'background.vcd'
    create_vcd(path, 16, 6000, 1, True)
    saved = interrupt_after_checkpoint(path, monkeypatch)
    entered = threading.Event()
    original = storage.PageWriter.add_vcd_batch

    def wait_for_close(self, *args):
        entered.set()
        assert self.store.cancel.wait(5)
        original(self, *args)

    with monkeypatch.context() as patch:
        patch.setattr(storage.PageWriter, 'add_vcd_batch', wait_for_close)
        trace = storage.open_trace(path, background=True)
        try:
            assert entered.wait(5)
            assert trace._resume_offset == saved['checkpoint']['offset']
            assert trace.load_progress > 0
            assert trace.signals['s0'].num_changes() > 0
            assert trace.load_changes == saved['changes']
            with pytest.raises(ValueError, match='still loading'):
                trace.require_loaded()
        finally:
            trace.close()
        assert trace._store.closed and not trace._store.thread.is_alive()
    assert metadata(path) == saved
    with storage.open_trace(path) as resumed:
        assert_matches(resumed, VCDFile.parse(path))


@pytest.mark.parametrize('failure', ['directory', 'index'])
def test_unavailable_checkpoint_cache_uses_temporary_storage(tmp_path, monkeypatch, failure):
    path = tmp_path / 'trace.vcd'
    create_vcd(path, 16, 1000, 1, True)
    if failure == 'directory':
        (tmp_path / 'cache').write_text('not a directory')
    else:
        original = storage.TraceStore.__init__

        def cannot_open_partial(self, path, *args, **kwargs):
            if path.endswith('.partial'):
                raise sqlite3.OperationalError('read-only cache')
            original(self, path, *args, **kwargs)

        monkeypatch.setattr(storage.TraceStore, '__init__', cannot_open_partial)
    with storage.open_trace(path) as trace:
        assert trace._store.temporary is not None
        temporary_path = Path(trace._store.path)
        assert_matches(trace, VCDFile.parse(path))
    assert not temporary_path.exists()


def test_source_change_during_indexing_is_not_published(tmp_path, monkeypatch):
    path = tmp_path / 'changing.vcd'
    create_vcd(path, 16, 6000, 1, True)
    original = storage.PageWriter.checkpoint
    changed = []

    def checkpoint(self, *args):
        original(self, *args)
        if self.changes and not changed:
            changed.append(True)
            with path.open('a') as output:
                output.write('#99999\n1s0\n')

    with monkeypatch.context() as patch:
        patch.setattr(storage, 'CHECKPOINT_BYTES', 0)
        patch.setattr(storage.PageWriter, 'checkpoint', checkpoint)
        with pytest.raises(ValueError, match='Trace changed during indexing'):
            storage.open_trace(path)
    assert not Path(storage.index_path(path)).exists()
    with storage.open_trace(path) as rebuilt:
        assert rebuilt._resume_offset == 0
        assert_matches(rebuilt, VCDFile.parse(path))


def test_purge_cli_only_removes_incomplete_indexes(tmp_path, monkeypatch):
    path = tmp_path / 'trace.vcd'
    create_vcd(path, 16, 6000, 1, True)
    interrupt_after_checkpoint(path, monkeypatch)
    complete = tmp_path / 'complete.vcd'
    create_vcd(complete, 4, 100, 1, True)
    with storage.open_trace(complete):
        pass
    result = subprocess.run([sys.executable, 'alwavelets.py', '--purge-partial-indexes'],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert 'Removed 1 incomplete waveform indexes' in result.stdout
    assert Path(storage.index_path(complete)).exists()
    assert not Path(storage.partial_index_path(path)).exists()
