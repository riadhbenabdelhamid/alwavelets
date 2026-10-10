"""Cache retention must never unlink an open index or lose a full-disk checkpoint."""
import errno
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from alwavelets import analysis_cache, cache, cli, index_cache, storage
from alwavelets.index_cache import WaveformCache
from alwavelets.parsers import VCDFile
from conftest import make_vcd
from tools.stress_vcd import create_vcd


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'xdg'))
    monkeypatch.delenv('ALWAVELETS_CACHE_DIR', raising=False)
    monkeypatch.setattr(cache, 'DIRECTORY', None)
    monkeypatch.setattr(cache, 'ENABLED', True)
    monkeypatch.setattr(index_cache, 'QUOTA_BYTES', None)
    monkeypatch.setattr(analysis_cache, 'QUOTA_BYTES', analysis_cache.QUOTA_BYTES)


def entry(directory, name, size=100, age=1, suffix=''):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (name + '.0123456789abcdef.wlc' + suffix)
    path.write_bytes(b'x' * size)
    os.utime(path, ns=(age, age))
    return path


def run_cli(monkeypatch, *args):
    monkeypatch.setattr(sys, 'argv', ['alwavelets', *map(str, args)])
    cli.main()


def test_directory_override_and_temporary_storage(tmp_path, monkeypatch, capsys):
    env_directory = tmp_path / 'env-cache'
    monkeypatch.setenv('ALWAVELETS_CACHE_DIR', str(env_directory))
    assert cache._cache_dir() == str(env_directory)
    chosen = tmp_path / 'large-drive' / 'cache'
    run_cli(monkeypatch, '--cache-dir', chosen, '--list-waveform-cache')
    report = json.loads(capsys.readouterr().out)
    assert report['directory'] == str(chosen)
    assert report['entries'] == [] and report['used_bytes'] == 0
    assert analysis_cache.cache_directory() == chosen / 'instructions'
    source = make_vcd(tmp_path / 'trace.vcd', n_steps=100)
    assert cache.save_cache(VCDFile.parse(source), source)
    assert Path(cache.cache_path(source)).parent == chosen
    for use_cache in (True, False):
        with storage.open_trace(source, use_cache=use_cache) as trace:
            path = Path(trace._store.path)
            assert chosen in path.parents
            assert len(trace.signals) == 2
        assert path.exists() == use_cache
    assert not env_directory.exists()


def test_lru_trims_all_waveform_kinds_and_counts_sidecars(tmp_path):
    manager = WaveformCache(tmp_path / 'cache', quota=210)
    oldest = entry(manager.directory, 'old', age=1)
    partial = entry(manager.directory, 'partial', age=2, suffix='.sqlite.partial')
    journal = Path(str(partial) + '-journal')
    journal.write_bytes(b'x' * 20)
    newest = entry(manager.directory, 'new', age=3, suffix='.sqlite')
    report = manager.inspect()
    assert report['used_bytes'] == 320
    assert [e['kind'] for e in report['entries']] == ['eager', 'partial', 'index']
    assert report['free_bytes'] > 0 and report['quota_bytes'] == 210
    assert manager.trim() == (2, 220, 0)
    assert not oldest.exists() and not partial.exists() and not journal.exists()
    assert newest.exists()


def test_trim_skips_active_and_explicit_exclusions(tmp_path):
    manager = WaveformCache(tmp_path / 'cache', quota=1)
    active = entry(manager.directory, 'active', suffix='.sqlite')
    excluded = entry(manager.directory, 'excluded', age=2)
    removable = entry(manager.directory, 'removable', age=3)
    lease = manager.acquire(active, shared=True)
    try:
        assert manager.trim(exclude=(excluded,)) == (1, 100, 1)
        assert active.exists() and excluded.exists() and not removable.exists()
        assert manager.inspect()['entries'][0]['busy']
    finally:
        lease.close()
    assert manager.trim() == (2, 200, 0)


def test_purge_only_exact_entries_and_keeps_unrelated_files(tmp_path):
    manager = WaveformCache(tmp_path / 'cache')
    chosen = entry(manager.directory, 'chosen', suffix='.sqlite')
    other = entry(manager.directory, 'other')
    unrelated = manager.directory / 'source.vcd'
    unrelated.write_text('source data')
    symlink = manager.directory / 'link.0123456789abcdef.wlc'
    symlink.symlink_to(unrelated)
    assert manager.purge([chosen.name]) == (1, 100, 0)
    assert other.exists() and symlink.exists() and unrelated.read_text() == 'source data'
    for name in ('../' + other.name, str(other), 'source.vcd', '*'):
        with pytest.raises(ValueError, match='exact cache entry names'):
            manager.purge([name])
    assert manager.purge() == (1, 100, 0)
    assert symlink.exists() and unrelated.exists()


@pytest.mark.parametrize('quota', [None, 0])
def test_default_and_disabled_quota_do_not_delete_existing_files(tmp_path, quota):
    manager = WaveformCache(tmp_path / 'cache', quota=quota)
    path = entry(manager.directory, 'keep')
    assert manager.trim() == (0, 0, 0)
    assert path.exists()


def test_zero_disables_waveform_reuse_and_save(tmp_path, monkeypatch):
    source = make_vcd(tmp_path / 'trace.vcd', n_steps=100)
    eager = VCDFile.parse(source)
    assert cache.save_cache(eager, source)
    with storage.open_trace(source):
        pass
    saved = {path: Path(path).read_bytes() for path in (cache.cache_path(source), storage.index_path(source))}
    monkeypatch.setattr(index_cache, 'QUOTA_BYTES', 0)
    assert cache.load_cache(source) is None
    assert not cache.save_cache(eager, source)
    with storage.open_trace(source) as trace:
        assert trace._store.temporary is not None
        temporary = Path(trace._store.path)
        assert trace.signals['!'].changes == eager.signals['!'].changes
    assert not temporary.exists()
    assert all(Path(path).read_bytes() == content for path, content in saved.items())


@pytest.mark.parametrize('kind', ['vcd', 'fst'])
def test_multiple_completed_readers_protect_index_until_last_close(tmp_path, monkeypatch, kind):
    if kind == 'vcd':
        source = make_vcd(tmp_path / 'trace.vcd', n_steps=100)
    else:
        source = tmp_path / 'trace.fst'
        source.write_bytes((Path(__file__).parent / 'fixtures/fst_ref_Z.fst').read_bytes())
    monkeypatch.setattr(index_cache, 'QUOTA_BYTES', 1)
    manager = WaveformCache()
    first = storage.open_trace(source)
    second = None
    try:
        target = Path(storage.index_path(source))
        assert target.exists()
        second = storage.open_trace(source)
        assert second._store.path == first._store.path == str(target)
        assert second._store.temporary is None
        assert manager.purge() == (0, 0, 1)
        first.close()
        assert manager.trim() == (0, 0, 1)
        key = next(iter(second.signals))
        assert second.signals[key].num_changes() > 0
        assert target.exists()
    finally:
        first.close()
        if second is not None:
            second.close()
    assert not target.exists()


def test_reopening_refreshes_retention_order(tmp_path):
    source = make_vcd(tmp_path / 'trace.vcd', n_steps=100)
    with storage.open_trace(source):
        pass
    path = Path(storage.index_path(source))
    os.utime(path, ns=(1, 1))
    old = entry(path.parent, 'other', size=path.stat().st_size, age=2)
    with storage.open_trace(source):
        pass
    manager = WaveformCache(quota=path.stat().st_size)
    assert manager.trim()[0] == 1
    assert path.exists() and not old.exists()
    eager = VCDFile.parse(source)
    assert cache.save_cache(eager, source)
    eager_path = Path(cache.cache_path(source))
    os.utime(eager_path, ns=(1, 1))
    assert cache.load_cache(source) is not None
    assert eager_path.stat().st_mtime_ns > 1


def test_other_process_can_share_but_cannot_purge_open_index(tmp_path):
    source = make_vcd(tmp_path / 'trace.vcd', n_steps=100)
    code = '''
import json, sys
from alwavelets import storage
from alwavelets.index_cache import WaveformCache
with storage.open_trace(sys.argv[1]) as trace:
    manager = WaveformCache()
    print(json.dumps([trace._store.temporary is None, manager.inspect()['entries'][0]['busy'], manager.purge()]))
'''
    with storage.open_trace(source):
        result = subprocess.run([sys.executable, '-c', code, str(source)], capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == [True, True, [0, 0, 1]]
    assert WaveformCache().purge()[0] == 1


def test_failed_eager_cache_write_keeps_previous_copy(tmp_path, monkeypatch):
    source = make_vcd(tmp_path / 'trace.vcd', n_steps=100)
    eager = VCDFile.parse(source)
    assert cache.save_cache(eager, source)
    path = Path(cache.cache_path(source))
    before = path.read_bytes()

    def full(*args):
        raise OSError(errno.ENOSPC, 'No space left on device')

    monkeypatch.setattr(cache.os, 'replace', full)
    assert not cache.save_cache(eager, source)
    assert path.read_bytes() == before
    assert not list(path.parent.glob('*.tmp'))
    assert cache.load_cache(source) is not None


@pytest.mark.parametrize('background', [False, True])
def test_sqlite_full_preserves_and_resumes_last_checkpoint(tmp_path, monkeypatch, background):
    source = tmp_path / 'full.vcd'
    create_vcd(source, 16, 12000, 1, True)
    eager = VCDFile.parse(source)
    original = storage.PageWriter.checkpoint
    saved = []

    def limit_after_checkpoint(self, *args):
        original(self, *args)
        if self.changes and not saved:
            metadata = json.loads(self.store.db.execute('SELECT value FROM meta WHERE key="trace"').fetchone()[0])
            prefix = {key: sig.changes for key, sig in self.vcd.signals.items()}
            saved.append((metadata, prefix))
            pages = self.store.db.execute('PRAGMA page_count').fetchone()[0]
            self.store.db.execute(f'PRAGMA max_page_count={pages}')

    with monkeypatch.context() as patch:
        patch.setattr(index_cache, 'QUOTA_BYTES', 1)
        patch.setattr(storage, 'CHECKPOINT_BYTES', 0)
        patch.setattr(storage.PageWriter, 'checkpoint', limit_after_checkpoint)
        if background:
            trace = storage.open_trace(source, background=True, memory_mib=0.1)
            try:
                trace._store.thread.join(15)
                assert not trace._store.thread.is_alive()
                message = trace._load_error
                assert trace.load_changes == saved[0][0]['changes']
                assert trace.max_time == saved[0][0]['max_time']
                assert {key: sig.changes for key, sig in trace.signals.items()} == saved[0][1]
            finally:
                trace.close()
        else:
            with pytest.raises(ValueError) as error:
                storage.open_trace(source, memory_mib=0.1)
            message = str(error.value)
        assert 'Not enough disk space' in message and 'checkpoint is saved' in message
        assert cache._cache_dir() in message
        partial = Path(storage.partial_index_path(source))
        assert partial.exists() and not Path(storage.index_path(source)).exists()
        with sqlite3.connect(partial) as db:
            assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            assert json.loads(db.execute('SELECT value FROM meta WHERE key="trace"').fetchone()[0]) == saved[0][0]
    with storage.open_trace(source, memory_mib=0.1) as resumed:
        assert resumed._resume_offset == saved[0][0]['checkpoint']['offset']
        assert resumed.max_time == eager.max_time
        for key, sig in eager.signals.items():
            assert resumed.signals[key].changes == sig.changes


def test_full_before_first_checkpoint_has_clear_error(tmp_path, monkeypatch):
    source = make_vcd(tmp_path / 'trace.vcd', n_steps=100)

    def full(*args):
        raise OSError(errno.ENOSPC, 'No space left on device')

    monkeypatch.setattr(storage.PageWriter, 'checkpoint', full)
    with pytest.raises(ValueError, match='Not enough disk space.*--cache-dir') as error:
        storage.open_trace(source)
    assert 'checkpoint is saved' not in str(error.value)
    assert not Path(storage.index_path(source)).exists()
    assert WaveformCache().purge()[2] == 0


@pytest.mark.parametrize('error,expected', [
    (OSError(errno.ENOSPC, 'full'), True),
    (OSError(errno.EDQUOT, 'quota exceeded'), True),
    (OSError(errno.EACCES, 'denied'), False),
    (sqlite3.OperationalError('database or disk is full'), True),
    (sqlite3.OperationalError('database is locked'), False),
])
def test_disk_full_detection(error, expected):
    assert index_cache.disk_full(error) == expected


def test_cache_cli_lists_removes_and_purges(tmp_path, monkeypatch, capsys):
    directory = tmp_path / 'cache'
    first = entry(directory, 'first')
    second = entry(directory, 'second', suffix='.sqlite.partial')
    third = entry(directory, 'third', suffix='.sqlite')
    run_cli(monkeypatch, '--cache-dir', directory, '--waveform-cache-mib', 2, '--list-waveform-cache')
    report = json.loads(capsys.readouterr().out)
    assert report['used_bytes'] == 300 and report['quota_bytes'] == 2 * 1024 ** 2
    run_cli(monkeypatch, '--remove-waveform-cache', first.name, '--remove-waveform-cache', second.name)
    assert 'Removed 2 waveform cache entries' in capsys.readouterr().out
    assert not first.exists() and not second.exists() and third.exists()
    run_cli(monkeypatch, '--purge-waveform-cache')
    assert 'Removed 1 waveform cache entries' in capsys.readouterr().out
    assert not third.exists()


@pytest.mark.parametrize('args', [('--waveform-cache-mib', '-1'),
                                  ('--purge-waveform-cache', '--purge-partial-indexes'),
                                  ('--remove-waveform-cache', '../trace.wlc')])
def test_invalid_maintenance_arguments(monkeypatch, args, capsys):
    with pytest.raises(SystemExit) as error:
        run_cli(monkeypatch, *args)
    assert error.value.code in (1, 2)
    assert capsys.readouterr().err
