"""Startup must paint before preparation, transfer ownership, and cancel safely."""
import curses
from pathlib import Path
import sqlite3
import sys
import threading

import pytest

from alwavelets import cache, cli, index_cache, storage
from alwavelets.fst import FSTReader
from alwavelets.loading import StartupJob, draw_loading, prepare_native, wait_for_trace
from alwavelets.parsers import VCDFile
from alwavelets.streaming import OperationCancelled
from test_fst_resume import interrupt
from test_index_resume import interrupt_after_checkpoint
from test_scope_store import drop_scope_metadata
from tools.stress_fst import create_fst, wrap_fst
from tools.stress_scopes import create_vcd


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    monkeypatch.delenv('ALWAVELETS_CACHE_DIR', raising=False)
    monkeypatch.setattr(cache, 'DIRECTORY', None)
    monkeypatch.setattr(cache, 'ENABLED', True)
    monkeypatch.setattr(index_cache, 'QUOTA_BYTES', None)


class Screen:
    def __init__(self, key=None):
        self.owner = threading.get_ident()
        self.frames, self.lines = [], []
        self.key = key or (lambda: -1)
        self.size = 8, 100
        self.nonblocking = False

    def keypad(self, enabled):
        assert threading.get_ident() == self.owner

    def nodelay(self, enabled):
        self.nonblocking = enabled

    def getmaxyx(self):
        return self.size

    def erase(self):
        self.lines = []

    def addnstr(self, row, col, text, count):
        assert threading.get_ident() == self.owner
        assert row < self.size[0] and count < self.size[1]
        self.lines.append(text[:count])

    def refresh(self):
        self.frames.append(tuple(self.lines))

    def getch(self):
        return self.key()


class Trace:
    def __init__(self):
        self.closed_by = []

    def close(self):
        self.closed_by.append(threading.get_ident())


def test_first_frame_precedes_work_and_handoff_keeps_trace_open():
    screen, trace = Screen(), Trace()

    def prepare(cancel, report):
        assert screen.frames and screen.frames[0][0] == 'Opening trace.vcd'
        assert threading.get_ident() != screen.owner
        report('Reading declarations', 1000)
        return trace

    assert wait_for_trace(screen, 'trace.vcd', prepare) is trace
    assert not trace.closed_by and not screen.nonblocking


@pytest.mark.parametrize('key', [ord('q'), 27, 3, KeyboardInterrupt])
def test_cancel_during_preparation_closes_on_worker(key):
    entered = threading.Event()
    trace = Trace()

    def prepare(cancel, report):
        entered.set()
        assert cancel.wait(5)
        return trace  # A loader can finish just as cancellation arrives.

    def press():
        if not entered.is_set():
            return -1
        if key is KeyboardInterrupt:
            raise KeyboardInterrupt
        return key

    screen = Screen(press)
    with pytest.raises(OperationCancelled):
        wait_for_trace(screen, 'cancel.vcd', prepare)
    assert len(trace.closed_by) == 1 and trace.closed_by[0] != screen.owner
    assert not screen.nonblocking


@pytest.mark.parametrize('claim', [False, True])
def test_cancel_after_ready_or_close_after_claim_has_one_owner(claim):
    trace = Trace()
    job = StartupJob(lambda cancel, report: trace)
    job.start()
    assert job.ready.wait(5)
    if claim:
        assert job.take() is trace
    job.close()
    assert job.finished.is_set()
    if claim:
        assert not trace.closed_by and not job.cancel.is_set()
    else:
        assert len(trace.closed_by) == 1 and trace.closed_by[0] == job.thread.ident
        with pytest.raises(OperationCancelled):
            job.take()


def test_preparation_error_restores_screen_and_propagates():
    screen = Screen()

    def prepare(cancel, report):
        raise ValueError('broken trace')

    with pytest.raises(ValueError, match='broken trace'):
        wait_for_trace(screen, 'broken.vcd', prepare)
    assert screen.frames and not screen.nonblocking


def test_terminal_error_discards_ready_trace_on_worker():
    trace, prepared = Trace(), threading.Event()

    def prepare(cancel, report):
        prepared.set()
        return trace

    def terminal_error():
        assert prepared.wait(5)
        raise RuntimeError('terminal disconnected')

    screen = Screen(terminal_error)
    with pytest.raises(RuntimeError, match='terminal disconnected'):
        wait_for_trace(screen, 'trace.vcd', prepare)
    assert len(trace.closed_by) == 1 and trace.closed_by[0] != screen.owner
    assert not screen.nonblocking


def test_resize_to_tiny_window_and_edge_errors_are_tolerated():
    screen = Screen()
    for size in [(8, 100), (1, 2), (0, 0), (3, 7)]:
        screen.size = size
        draw_loading(screen, 'trace.vcd', ('Preparing scopes', 1, 100, 'scopes'), 1)
    screen.size = 2, 2
    screen.addnstr = lambda *args: (_ for _ in ()).throw(curses.error())
    draw_loading(screen, 'trace.vcd', ('Preparing scopes', None, None, 'scopes'), 2, True)


def source_file(tmp_path, kind, count=600):
    source = tmp_path / ('trace.' + ('vcd' if kind == 'vcd' else 'fst'))
    if kind == 'vcd':
        create_vcd(source, count)
    else:
        create_fst(source, events=1, signals=count, scope_per_signal=True)
        if kind == 'wrapped':
            wrapped = tmp_path / 'wrapped.fst'
            wrap_fst(source, wrapped)
            source = wrapped
    return source


@pytest.mark.parametrize('kind', ['vcd', 'fst', 'wrapped'])
@pytest.mark.parametrize('mode', ['disk', 'memory'])
def test_native_cold_and_cached_handoff_preserves_all_signals(tmp_path, monkeypatch, kind, mode):
    source = source_file(tmp_path, kind)
    stages = []

    def prepare(cancel, report):
        def stage(*args):
            stages.append(args[0])
            report(*args)
        return prepare_native(source, storage=mode, memory_mib=1, cancel=cancel, stage_cb=stage)

    for cached in (False, True):
        with wait_for_trace(Screen(), source, prepare) as trace:
            store = getattr(trace, '_store', None)
            if store is not None and store.thread is not None:
                store.thread.join(10)
                assert not store.thread.is_alive()
            assert not trace._load_error and len(trace.signal_order) == 600
            assert trace.signals[trace.signal_order[-1]].value_at(0) == '0'
        if not cached:
            assert 'Reading declarations' in stages and 'Preparing scopes' in stages
            stages.clear()
            def no_parse(*args, **kwargs):
                raise AssertionError('cache hit must not reparse declarations')
            monkeypatch.setattr(VCDFile, 'parse_header', no_parse)
            monkeypatch.setattr(FSTReader, '_parse_hierarchy', no_parse)
        else:
            assert 'Reading declarations' not in stages and 'Preparing scopes' not in stages


@pytest.mark.parametrize('kind,stage', [('vcd', 'Reading declarations'), ('fst', 'Unpacking declarations'),
                                      ('wrapped', 'Unpacking FST'), ('vcd', 'Preparing scopes'),
                                      ('fst', 'Counting scope signals'), ('vcd', 'Saving preparation')])
@pytest.mark.parametrize('persistent', [False, True])
def test_cancel_at_preparation_stage_closes_stores_and_allows_retry(tmp_path, monkeypatch, kind, stage, persistent):
    source = source_file(tmp_path, kind)
    cancel, stores, seen = threading.Event(), [], []
    original = storage.TraceStore.__init__

    def track(self, *args, **kwargs):
        original(self, *args, **kwargs)
        stores.append(self)

    def progress(name, *args):
        seen.append(name)
        if name == stage:
            cancel.set()

    monkeypatch.setattr(storage.TraceStore, '__init__', track)
    with pytest.raises(OperationCancelled):
        prepare_native(source, storage='disk', cancel=cancel, stage_cb=progress, use_cache=persistent)
    assert stage in seen and stores and all(store.closed for store in stores)
    assert all(store.temporary is None or not Path(store.temporary.name).exists() for store in stores)
    with storage.open_trace(source) as trace:
        assert len(trace.signals) == 600 and not trace._load_error


def test_cancelled_legacy_upgrade_preserves_existing_waveform_index(tmp_path):
    source = source_file(tmp_path, 'vcd')
    with storage.open_trace(source):
        pass
    target = storage.index_path(source)
    original = drop_scope_metadata(target)
    cancel = threading.Event()

    def progress(stage, *args):
        if stage == 'Preparing scopes':
            cancel.set()

    with pytest.raises(OperationCancelled):
        prepare_native(source, storage='disk', cancel=cancel, stage_cb=progress)
    with sqlite3.connect(target) as db:
        assert db.execute('SELECT value FROM meta WHERE key="trace"').fetchone()[0] == original
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    with storage.open_trace(source) as trace:
        assert trace.signals[trace.signal_order[0]].value_at(0) == '0'


@pytest.mark.parametrize('partial', [False, True])
def test_upgrade_callback_failure_releases_probe_and_lease(tmp_path, monkeypatch, partial):
    source = source_file(tmp_path, 'vcd')
    with storage.open_trace(source):
        pass
    target = Path(storage.index_path(source))
    if partial:
        target = target.rename(storage.partial_index_path(source))
    drop_scope_metadata(target)
    stores = []
    original = storage.TraceStore.__init__

    def track(self, *args, **kwargs):
        original(self, *args, **kwargs)
        stores.append(self)

    def report(stage, *args):
        if stage == 'Preparing scopes':
            raise RuntimeError('progress failed')

    monkeypatch.setattr(storage.TraceStore, '__init__', track)
    with pytest.raises(RuntimeError, match='progress failed'):
        prepare_native(source, storage='disk', stage_cb=report)
    assert stores and all(store.closed for store in stores)
    lease = index_cache.WaveformCache().acquire(target)
    lease.close()
    with storage.open_trace(source) as trace:
        assert len(trace.signals) == 600


@pytest.mark.parametrize('kind', ['vcd', 'fst'])
def test_cancelled_resume_preparation_retains_checkpoint(tmp_path, monkeypatch, kind):
    source = tmp_path / ('resume.' + kind)
    if kind == 'vcd':
        create_vcd(source, 1)
        with source.open('a') as output:
            output.write(''.join(f'#{t}\n{t % 2}s0\n' for t in range(1, 20000)))
        saved = interrupt_after_checkpoint(source, monkeypatch)
    else:
        create_fst(source, events=20000)
        saved = interrupt(source, monkeypatch, lambda state, writer: bool(writer.changes))
    target = Path(storage.partial_index_path(source))
    cancel = threading.Event()

    def progress(stage, *args):
        if stage == 'Saving preparation':
            cancel.set()

    with pytest.raises(OperationCancelled):
        prepare_native(source, storage='disk', cancel=cancel, stage_cb=progress)
    assert target.exists()
    with prepare_native(source, storage='disk') as trace:
        assert trace._resume_offset == saved['checkpoint']['offset']
        trace._store.thread.join(10)
        assert not trace._load_error and not trace.is_loading


def test_viewer_handoff_precedes_completion_of_value_indexing(tmp_path, monkeypatch):
    source = source_file(tmp_path, 'vcd')
    entered, release = threading.Event(), threading.Event()
    original = storage.PageWriter.add_vcd_batch

    def block(self, batch):
        entered.set()
        assert release.wait(5)
        original(self, batch)

    monkeypatch.setattr(storage.PageWriter, 'add_vcd_batch', block)
    trace = None
    try:
        trace = wait_for_trace(Screen(), source, lambda cancel, report:
                               prepare_native(source, storage='disk', cancel=cancel, stage_cb=report))
        assert entered.wait(5) and trace.is_loading
        assert len(trace.scopes.children) == 1 and len(trace.signals) == 600
    finally:
        release.set()
        if trace is not None:
            trace.close()


@pytest.mark.parametrize('cancelled_load', [False, True])
def test_memory_background_worker_closes_and_respects_disabled_cache(tmp_path, monkeypatch, cancelled_load):
    source = source_file(tmp_path, 'vcd', 3)
    with source.open('a') as output:
        output.write('$comment ' + ' ' * (6 * 1024 * 1024) + '$end\n')
    entered, cancelled = threading.Event(), threading.Event()

    def values(self, offset, progress_cb=None, *, cancel=None):
        entered.set()
        assert cancel.wait(5)
        cancelled.set()
        raise OperationCancelled('cancelled')

    if cancelled_load:
        monkeypatch.setattr(VCDFile, '_read_values', values)
    trace = prepare_native(source, storage='memory', use_cache=False)
    if cancelled_load:
        assert entered.wait(5)
    else:
        trace._load_thread.join(5)
        assert not trace.is_loading and not trace._load_error
    trace.close()
    assert cancelled.is_set() == cancelled_load and not trace._load_thread.is_alive()
    assert not Path(cache.cache_path(source)).exists()


@pytest.mark.parametrize('operation', ['load', 'save'])
def test_eager_cache_codec_cancellation_preserves_valid_cache(tmp_path, monkeypatch, operation):
    source = source_file(tmp_path, 'vcd', 2000)
    trace = VCDFile.parse(source)
    assert cache.save_cache(trace, source)
    target = Path(cache.cache_path(source))
    original_bytes = target.read_bytes()
    cancel = threading.Event()
    factory_name = 'decompressobj' if operation == 'load' else 'compressobj'
    original = getattr(cache.zlib, factory_name)

    class Codec:
        def __init__(self, *args):
            self.inner = original(*args)

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def compress(self, *args):
            result = self.inner.compress(*args)
            cancel.set()
            return result

        def decompress(self, *args):
            result = self.inner.decompress(*args)
            cancel.set()
            return result

    monkeypatch.setattr(cache.zlib, factory_name, Codec)
    with pytest.raises(OperationCancelled):
        if operation == 'load':
            cache.load_cache(source, cancel=cancel)
        else:
            cache.save_cache(trace, source, cancel=cancel)
    assert target.read_bytes() == original_bytes
    assert not list(target.parent.glob('*.tmp'))


@pytest.mark.parametrize('options', [[], ['--expanded'], ['--loadunchecked'], ['--no-session'],
                                   ['--signals', 'value1'], ['--sync']])
def test_cli_native_startup_preserves_layout_options_and_optional_filter(tmp_path, monkeypatch, options):
    source = source_file(tmp_path, 'vcd', 3)
    screen, calls = Screen(), []
    monkeypatch.setattr(sys, 'argv', ['alwavelets', str(source), '--storage', 'disk', *options])
    monkeypatch.setattr(curses, 'wrapper', lambda callback, *args, **kwargs: callback(screen, *args, **kwargs))

    def viewer(stdscr, trace, **kwargs):
        assert threading.get_ident() == screen.owner
        assert bool(screen.frames) == ('--sync' not in options)
        assert len(trace.signals) == 3
        assert list(trace.signal_order) == (['s1'] if '--signals' in options else ['s0', 's1', 's2'])
        assert kwargs['start_collapsed'] == ('--expanded' not in options)
        assert kwargs['load_unchecked'] == ('--loadunchecked' in options)
        assert kwargs['use_session'] == (not any(o in options for o in ('--expanded', '--loadunchecked', '--no-session')))
        calls.append(trace)

    monkeypatch.setattr(cli, 'main_curses', viewer)
    cli.main()
    assert len(calls) == 1 and calls[0]._store.closed
