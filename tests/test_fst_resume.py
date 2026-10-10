"""Durable FST decoder positions cover chains, frames, blackouts and final sorting."""
import json
import os
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import threading

import pytest

from alwavelets import cache, index_cache, storage
from alwavelets.fst import FSTReader
from alwavelets.index_cache import WaveformCache
from test_fst_batches import write_trace
from tools.stress_fst import create_fst, varint, wrap_fst

FIXTURES = Path(__file__).parent / 'fixtures'


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    monkeypatch.delenv('ALWAVELETS_CACHE_DIR', raising=False)
    monkeypatch.setattr(cache, 'DIRECTORY', None)
    monkeypatch.setattr(index_cache, 'QUOTA_BYTES', None)


def metadata(source):
    with sqlite3.connect(storage.partial_index_path(source)) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        return json.loads(db.execute('SELECT value FROM meta WHERE key="trace"').fetchone()[0])


def assert_matches(actual, expected):
    assert list(actual.signal_order) == list(expected.signal_order)
    assert actual.max_time == expected.max_time
    for key, sig in expected.signals.items():
        got = actual.signals[key]
        assert (got.full_name, got.size, got.signal_type, got.declared_range) == (sig.full_name, sig.size, sig.signal_type, sig.declared_range)
        assert got.changes == sig.changes, key
        assert got.numeric_range_in_window(0, actual.max_time) == sig.numeric_range_in_window(0, expected.max_time)


def interrupt(source, monkeypatch, predicate):
    cancel = threading.Event()
    original = storage.PageWriter.checkpoint_fst
    reached = []

    def checkpoint(self, *args):
        original(self, *args)
        if predicate(args[-1], self):
            reached.append(args[-1])
            cancel.set()

    with monkeypatch.context() as patch:
        patch.setattr(storage, 'CHECKPOINT_BYTES', 0)
        patch.setattr(storage.PageWriter, 'checkpoint_fst', checkpoint)
        with pytest.raises(ValueError, match='cancelled'):
            storage.open_trace(source, memory_mib=0.1, cancel=cancel)
    assert reached and not Path(storage.index_path(source)).exists()
    return metadata(source)


@pytest.mark.parametrize('variant', ['4', 'F', 'Z', 'c'])
@pytest.mark.parametrize('phase', ['values', 'blackouts', 'done'])
def test_codec_alias_and_blackout_parity_after_resume(tmp_path, monkeypatch, variant, phase):
    source = tmp_path / 'trace.fst'
    source.write_bytes((FIXTURES / f'fst_ref_{variant}.fst').read_bytes())
    eager = FSTReader(source).parse()
    monkeypatch.setattr(storage, 'PAGE_EVENTS', 32)
    saved = interrupt(source, monkeypatch, lambda state, writer: state['phase'] == phase and writer.changes > 0)
    assert saved['checkpoint']['format'] == 'fst'

    def no_hierarchy(*args):
        raise AssertionError('resume must reuse the saved declarations')

    monkeypatch.setattr(FSTReader, '_parse_hierarchy', no_hierarchy)
    progress = []
    with storage.open_trace(source, memory_mib=0.1, progress_cb=lambda done, total: progress.append((done, total))) as resumed:
        assert resumed._resume_offset == saved['checkpoint']['offset']
        assert progress[0] == (saved['checkpoint']['offset'], source.stat().st_size)
        assert_matches(resumed, eager)
        assert resumed._store.peak_cache_bytes <= resumed._store.memory_bytes
    assert not Path(storage.partial_index_path(source)).exists()
    with storage.open_trace(source) as cached:
        assert_matches(cached, eager)


@pytest.mark.parametrize('width,real', [(1, False), (7, False), (64, False), (257, False), (64, True)])
def test_resume_inside_second_block_preserves_same_tick_order(tmp_path, monkeypatch, width, real):
    events = [(3 + i // 3, str((i % 311) / 8) if real else
               ('xz'[i % 2] * width if i % 19 == 0 else format(i % (1 << width), f'0{width}b')))
              for i in range(11000)]
    source = tmp_path / 'blocks.fst'
    write_trace(source, width, events, real)
    eager = FSTReader(source).parse()
    saved = interrupt(source, monkeypatch,
                      lambda state, writer: state['phase'] == 'values' and state['block'] == 2 and state['position'] > 0)
    visited = []
    original = FSTReader._parse_vc_block

    def block(self, *args):
        visited.append(self._block_ordinal)
        return original(self, *args)

    monkeypatch.setattr(FSTReader, '_parse_vc_block', block)
    with storage.open_trace(source, memory_mib=0.1) as resumed:
        assert_matches(resumed, eager)
        assert resumed.load_changes == len(events) + 1
    assert visited == [saved['checkpoint']['state']['block']]


@pytest.mark.parametrize('wrapped', [False, True])
def test_large_initial_frame_resumes_after_committed_handles(tmp_path, monkeypatch, wrapped):
    source = tmp_path / 'frame.fst'
    create_fst(source, signals=2100)
    if wrapped:
        wrapped_source = tmp_path / 'wrapped.fst'
        wrap_fst(source, wrapped_source)
        source = wrapped_source
    saved = interrupt(source, monkeypatch,
                      lambda state, writer: state['phase'] == 'frame' and state['handle'] > 1)
    visited = []
    original = FSTReader._vc_header

    def header(self, *args):
        visited.append(1)
        return original(self, *args)

    monkeypatch.setattr(FSTReader, '_vc_header', header)
    with storage.open_trace(source, memory_mib=0.1) as resumed:
        assert len(resumed.signals) == 2100
        assert resumed.load_changes == 2100
        for sid in ('1', str(saved['checkpoint']['state']['handle']), '2100'):
            assert resumed.signals[sid].changes == [(0, '0')]
    assert visited == [1]


def test_repeated_crash_replays_only_uncommitted_values(tmp_path):
    source = tmp_path / 'crash.fst'
    create_fst(source, events=30000)
    code = '''
import os, sys
from alwavelets import storage
storage.CHECKPOINT_BYTES = 0
storage.PAGE_EVENTS = 512
original = storage.PageWriter.checkpoint_fst
saved = False
def checkpoint(self, *args):
    global saved
    if saved:
        self.publish(True)
        os._exit(17)
    original(self, *args)
    saved = bool(self.changes)
storage.PageWriter.checkpoint_fst = checkpoint
storage.open_trace(sys.argv[1], memory_mib=0.1)
'''
    offsets = []
    for _ in range(2):
        result = subprocess.run([sys.executable, '-c', code, str(source)], timeout=15)
        assert result.returncode == 17
        offsets.append(metadata(source)['checkpoint']['state']['position'])
    assert offsets[1] > offsets[0] > 0
    with storage.open_trace(source, memory_mib=0.1) as resumed:
        assert_matches(resumed, FSTReader(source).parse())


def test_sqlite_full_preserves_fst_checkpoint_and_readable_values(tmp_path, monkeypatch):
    source = tmp_path / 'full.fst'
    create_fst(source, events=50000)
    original = storage.PageWriter.checkpoint_fst
    saved = []

    def limit(self, *args):
        original(self, *args)
        if self.changes and not saved:
            saved.append((metadata(source), self.vcd.signals['1'].changes))
            count = self.store.db.execute('PRAGMA page_count').fetchone()[0]
            self.store.db.execute(f'PRAGMA max_page_count={count}')

    with monkeypatch.context() as patch:
        patch.setattr(index_cache, 'QUOTA_BYTES', 1)
        patch.setattr(storage, 'CHECKPOINT_BYTES', 0)
        patch.setattr(storage.PageWriter, 'checkpoint_fst', limit)
        trace = storage.open_trace(source, background=True, memory_mib=0.1)
        try:
            trace._store.thread.join(15)
            assert not trace.is_loading
            assert 'Not enough disk space' in trace._load_error
            assert 'FST checkpoint is saved' in trace._load_error
            assert trace.signals['1'].changes == saved[0][1]
            assert trace.load_changes == saved[0][0]['changes']
        finally:
            trace.close()
        assert metadata(source) == saved[0][0]
    with storage.open_trace(source, memory_mib=0.1) as resumed:
        assert_matches(resumed, FSTReader(source).parse())


def test_busy_partial_falls_back_and_close_leaves_resumable_checkpoint(tmp_path, monkeypatch):
    source = tmp_path / 'busy.fst'
    create_fst(source, events=30000)
    entered, release = threading.Event(), threading.Event()
    original = storage.PageWriter.checkpoint_fst

    def checkpoint(self, *args):
        original(self, *args)
        if self.changes and threading.current_thread().name == 'wavelet-index':
            entered.set()
            assert release.wait(10)

    with monkeypatch.context() as patch:
        patch.setattr(storage, 'CHECKPOINT_BYTES', 0)
        patch.setattr(storage.PageWriter, 'checkpoint_fst', checkpoint)
        trace = storage.open_trace(source, background=True, memory_mib=0.1)
        try:
            assert entered.wait(5)
            assert WaveformCache().purge() == (0, 0, 1)
            with storage.open_trace(source) as other:
                assert other._store.temporary is not None
                assert other.signals['1'].num_changes() == 30000
            trace._store.cancel.set()
        finally:
            release.set()
            trace.close()
    assert metadata(source)['changes'] > 0
    with storage.open_trace(source) as resumed:
        assert_matches(resumed, FSTReader(source).parse())


@pytest.mark.parametrize('damage', ['version', 'state', 'format', 'changed', 'replace_same_stat'])
def test_invalid_or_stale_checkpoint_restarts(tmp_path, monkeypatch, damage):
    source = tmp_path / 'invalid.fst'
    create_fst(source, events=20000)
    saved = interrupt(source, monkeypatch, lambda state, writer: bool(writer.changes))
    if damage == 'changed':
        create_fst(source, events=21000)
    elif damage == 'replace_same_stat':
        stat = source.stat()
        other = tmp_path / 'replacement.fst'
        other.write_bytes(source.read_bytes())
        os.utime(other, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        os.replace(other, source)
    else:
        if damage == 'version':
            saved['checkpoint']['version'] += 1
        elif damage == 'format':
            saved['checkpoint']['format'] = 'unknown'
        else:
            saved['checkpoint']['state']['position'] = -1
        with sqlite3.connect(storage.partial_index_path(source)) as db:
            db.execute('UPDATE meta SET value=? WHERE key="trace"', (json.dumps(saved),))
    with storage.open_trace(source) as trace:
        assert trace._resume_offset == 0
        assert_matches(trace, FSTReader(source).parse())


def test_no_cache_ignores_checkpoint_and_cleans_up(tmp_path, monkeypatch):
    source = tmp_path / 'temporary.fst'
    create_fst(source, events=20000)
    saved = interrupt(source, monkeypatch, lambda state, writer: bool(writer.changes))
    with storage.open_trace(source, use_cache=False) as trace:
        assert trace._resume_offset == 0
        temporary = Path(trace._store.path)
        assert_matches(trace, FSTReader(source).parse())
    assert not temporary.exists()
    assert metadata(source) == saved


@pytest.mark.parametrize('variant', ['4', 'F', 'Z', 'c'])
def test_completed_chains_are_not_decoded_again(tmp_path, monkeypatch, variant):
    source = tmp_path / 'chains.fst'
    source.write_bytes((FIXTURES / f'fst_ref_{variant}.fst').read_bytes())
    eager = FSTReader(source).parse()
    saved = interrupt(source, monkeypatch, lambda state, writer:
                      state['phase'] == 'values' and state['position'] == 0 and state['handle'] > 1)
    first = saved['checkpoint']['state']['handle']
    original = FSTReader._decode_chain
    visited = []

    def decode(self, handle, *args):
        visited.append(handle)
        assert handle >= first
        return original(self, handle, *args)

    monkeypatch.setattr(FSTReader, '_decode_chain', decode)
    with storage.open_trace(source) as trace:
        assert_matches(trace, eager)
    assert visited


@pytest.mark.parametrize('cancel_sort', [False, True])
def test_blackout_checkpoint_and_interrupted_sort_do_not_duplicate_events(tmp_path, monkeypatch, cancel_sort):
    source = tmp_path / 'blackouts.fst'
    events = [(3 + i // 3, str(i % 2)) for i in range(11000)]
    write_trace(source, 1, events)
    # Neither t=1 nor t=2 is already in the chains. Each blackout block adds
    # a new out-of-order event and must be skipped once it is checkpointed.
    for tick in (1, 2):
        data = b'\x01\x00' + varint(tick)
        with source.open('ab') as output:
            output.write(b'\x02' + struct.pack('>Q', len(data) + 8) + data)
    eager = FSTReader(source).parse()
    if cancel_sort:
        original = storage.PageWriter.add
        calls = []

        def add(self, *args):
            original(self, *args)
            calls.append(1)
            if len(calls) == 100:
                self.store.cancel.set()

        with monkeypatch.context() as patch:
            patch.setattr(storage.PageWriter, 'add', add)
            with pytest.raises(ValueError, match='cancelled'):
                storage.open_trace(source, memory_mib=0.1)
        saved = metadata(source)
        assert saved['checkpoint']['state']['phase'] == 'done'
    else:
        saved = interrupt(source, monkeypatch, lambda state, writer:
                          state['phase'] == 'blackouts' and state['block'] > 0)
    assert saved['checkpoint']['disordered']
    with storage.open_trace(source, memory_mib=0.1) as resumed:
        assert_matches(resumed, eager)
        assert resumed.signals['1'].num_changes() == len(events) + 3


def test_source_change_during_indexing_is_not_published(tmp_path, monkeypatch):
    source = tmp_path / 'changing.fst'
    create_fst(source, events=20000)
    original = storage.PageWriter.add_batch
    changed = []

    def add(self, *args):
        original(self, *args)
        if not changed:
            changed.append(1)
            stat = source.stat()
            os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000))

    with monkeypatch.context() as patch:
        patch.setattr(storage.PageWriter, 'add_batch', add)
        with pytest.raises(ValueError, match='Trace changed during indexing'):
            storage.open_trace(source)
    assert not Path(storage.index_path(source)).exists()
    with storage.open_trace(source) as trace:
        assert trace._resume_offset == 0
        assert_matches(trace, FSTReader(source).parse())
