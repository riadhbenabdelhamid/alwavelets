"""Persistent instruction reuse, invalidation, quotas and atomic publication."""
import copy
import hashlib
import os
from pathlib import Path
import sqlite3
import threading

import pytest

from alwavelets import analysis_cache, cache
from alwavelets.analysis_cache import InstructionCache, identity_for
from alwavelets.decoders import InstructionTrace, RV32Decoder
from alwavelets.instruction_store import InstructionResults
from alwavelets.parsers import parse_file
from alwavelets.persistence import SignalTagStore
from alwavelets.streaming import OperationCancelled
from tools.stress_instructions import entries


def instruction_file(path, count=120):
    header = ('$timescale 1ns $end\n$scope module top $end\n'
              '$var wire 1 ! clk $end\n$var wire 32 a instr_wb $end\n'
              '$var wire 32 b pc.wb $end\n$var wire 1 c valid_wb $end\n'
              '$upscope $end\n$enddefinitions $end\n#0\n0!\nb100000000000010010011 a\nb0 b\n1c\n')
    path.write_text(header + ''.join('#%d\n%d!\nb%s b\n' % (i, i % 2, format(i * 4, 'b')) for i in range(1, count * 2 + 1)))
    return path


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    monkeypatch.setattr(cache, 'ENABLED', True)
    monkeypatch.setattr(analysis_cache, 'QUOTA_BYTES', 8 * 1024 ** 2)
    return instruction_file(tmp_path / 'instructions.vcd')


def tags_for(vcd):
    tags = SignalTagStore()
    tags.auto_detect_roles(vcd)
    return tags


@pytest.mark.parametrize('storage', ['memory', 'disk'])
def test_reopen_uses_index_and_keeps_queries_private(source, storage, monkeypatch):
    with parse_file(source, storage=storage, use_cache=False) as vcd:
        trace = InstructionTrace()
        assert trace.build(vcd, tags_for(vcd), storage='disk') == 120
        assert not trace.entries.cache_hit
        path = Path(trace.entries.path)
    assert path.exists()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with parse_file(source, storage=storage, use_cache=False) as vcd:
        trace = InstructionTrace()
        def forbidden(*args, **kwargs):
            raise AssertionError('cache hit decoded the source again')
        monkeypatch.setattr(trace, 'iter_entries', forbidden)
        assert trace.build(vcd, tags_for(vcd), storage='disk') == 120
        assert trace.entries.cache_hit
        with trace.select(time_lo=150) as matches:
            assert matches[-1]['time'] == 239
            assert matches.read_page(len(matches) - 1, 1)[0] == matches[-1]
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            trace.entries._db.execute('DELETE FROM results')
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize('change', ['tags', 'hart_tags', 'isa', 'hart', 'thread', 'decoder', 'format'])
def test_identity_covers_configuration(source, change, monkeypatch):
    with parse_file(source, use_cache=False) as vcd:
        tags, decoder = tags_for(vcd), RV32Decoder()
        before = identity_for(vcd, tags, decoder)
        if change == 'tags':
            tags.tags['isa.pc.writeback'] = 'different'
        elif change == 'hart_tags':
            tags.hart_tags[(0, 0, 'clock')] = 'different'
        elif change == 'isa':
            tags.isa = 'rv32im'
        elif change == 'hart':
            tags.hart_id = 7
        elif change == 'thread':
            tags.thread_id = 8
        elif change == 'decoder':
            decoder.some_option = True
        else:
            monkeypatch.setattr(analysis_cache, 'FORMAT_VERSION', 999)
        assert before is not None and before != identity_for(vcd, tags, decoder)


def test_changed_source_and_old_loaded_snapshot_are_not_reused(source):
    with parse_file(source, use_cache=False) as vcd:
        tags = tags_for(vcd)
        before = identity_for(vcd, tags, RV32Decoder())
        st = source.stat()
        source.write_text(source.read_text().replace('1c', '0c'))
        os.utime(source, ns=(st.st_atime_ns, st.st_mtime_ns))
        assert identity_for(vcd, tags, RV32Decoder()) is None
    with parse_file(source, use_cache=False) as changed:
        assert identity_for(changed, tags_for(changed), RV32Decoder()) != before
        trace = InstructionTrace()
        assert trace.build(changed, tags_for(changed), storage='disk') == 0


@pytest.mark.parametrize('disabled', ['flag', 'global', 'quota', 'custom'])
def test_disabled_and_custom_builds_remain_temporary(source, disabled, monkeypatch):
    if disabled == 'global':
        monkeypatch.setattr(cache, 'ENABLED', False)
    if disabled == 'quota':
        monkeypatch.setattr(analysis_cache, 'QUOTA_BYTES', 0)
    class CustomDecoder(RV32Decoder):
        pass
    with parse_file(source, use_cache=False) as vcd:
        trace = InstructionTrace()
        trace.build(vcd, tags_for(vcd), CustomDecoder() if disabled == 'custom' else None,
                    storage='disk', use_cache=disabled != 'flag')
        path = Path(trace.entries.path)
        assert not trace.entries.cache_hit
        assert trace.entries._temporary is not None
    assert not path.exists() and not analysis_cache.cache_directory().exists()


@pytest.mark.parametrize('damage', ['header', 'incomplete', 'count', 'index'])
def test_corrupt_or_incomplete_cache_rebuilds(source, damage):
    with parse_file(source, use_cache=False) as vcd:
        trace = InstructionTrace()
        trace.build(vcd, tags_for(vcd), storage='disk')
        path = Path(trace.entries.path)
    if damage == 'header':
        path.write_bytes(b'not a database')
    else:
        with sqlite3.connect(path) as db:
            if damage == 'index':
                db.execute('DROP INDEX instruction_pc')
            else:
                db.execute('UPDATE analysis_meta SET value=? WHERE key=?', ('0' if damage == 'incomplete' else '999', 'complete' if damage == 'incomplete' else 'count'))
    with parse_file(source, use_cache=False) as vcd:
        trace = InstructionTrace()
        assert trace.build(vcd, tags_for(vcd), storage='disk') == 120
        assert not trace.entries.cache_hit
        assert trace.entries[-1]['time'] == 239


def test_cancelled_cache_copy_leaves_no_completed_file(source):
    with parse_file(source, use_cache=False) as vcd:
        identity = identity_for(vcd, tags_for(vcd), RV32Decoder())
        manager, cancel = InstructionCache(), threading.Event()
        with InstructionResults.collect(entries(200), dict) as store:
            def progress(done, total):
                cancel.set()
            with pytest.raises(OperationCancelled):
                manager.publish(store, identity, cancel, progress)
            assert store[-1]['rd_data'] == 199
        assert not manager.path(identity).exists()
        assert not list(manager.directory.glob('.building-*'))


def test_quota_eviction_and_purge_preserve_active_reads(source):
    with parse_file(source, use_cache=False) as vcd:
        identity = identity_for(vcd, tags_for(vcd), RV32Decoder())
        manager = InstructionCache()
        paths = []
        for i in range(2):
            key = dict(identity, case=i)
            with InstructionResults.collect(entries(200), dict) as store:
                assert manager.publish(store, key)
            paths.append(manager.path(key))
        manager.quota = sum(p.stat().st_size for p in paths)
        active = manager.load(dict(identity, case=1))
        try:
            with active.select() as selection:
                with InstructionResults.collect(entries(200), dict) as store:
                    assert manager.publish(store, dict(identity, case=2))
                assert not paths[0].exists() and paths[1].exists()
                assert sum(size for _, _, size in manager._files()) <= manager.quota
                count, size = manager.purge()
                assert count == 2 and size > 0
                assert selection[-1]['rd_data'] == 199
        finally:
            active.close()


def test_too_large_or_unwritable_cache_does_not_break_build(source, monkeypatch):
    monkeypatch.setattr(analysis_cache, 'QUOTA_BYTES', 1)
    with parse_file(source, use_cache=False) as vcd:
        trace = InstructionTrace()
        assert trace.build(vcd, tags_for(vcd), storage='disk') == 120
        assert trace.entries._temporary is not None
    monkeypatch.setattr(analysis_cache, 'QUOTA_BYTES', 8 * 1024 ** 2)
    directory = analysis_cache.cache_directory()
    directory.parent.mkdir(parents=True, exist_ok=True)
    directory.write_text('blocked')
    with parse_file(source, use_cache=False) as vcd:
        trace = InstructionTrace()
        assert trace.build(vcd, tags_for(vcd), storage='disk') == 120
        assert trace.entries._temporary is not None


def test_source_change_during_build_does_not_publish(source, monkeypatch):
    with parse_file(source, use_cache=False) as vcd:
        trace = InstructionTrace()
        original = trace.iter_entries
        def changing(*args, **kwargs):
            yield from original(*args, **kwargs)
            source.write_text(source.read_text() + '\n')
        monkeypatch.setattr(trace, 'iter_entries', changing)
        assert trace.build(vcd, tags_for(vcd), storage='disk') == 120
        assert trace.entries._temporary is not None
        assert not analysis_cache.cache_directory().exists()


def test_concurrent_publication_is_complete(source):
    from concurrent.futures import ThreadPoolExecutor
    with parse_file(source, use_cache=False) as vcd:
        identity = identity_for(vcd, tags_for(vcd), RV32Decoder())
        def publish(_):
            with InstructionResults.collect(entries(1000), dict) as store:
                return InstructionCache().publish(store, copy.deepcopy(identity))
        with ThreadPoolExecutor(max_workers=2) as workers:
            assert all(workers.map(publish, range(2)))
        with InstructionCache().load(identity) as cached:
            assert len(cached) == 1000 and cached[-1]['rd_data'] == 999


def test_orphaned_partial_files_are_reclaimed_without_touching_other_files(source):
    manager = InstructionCache()
    manager.directory.mkdir(parents=True)
    incomplete = manager.directory / '.building-orphan.sqlite'
    incomplete.write_bytes(b'partial')
    unrelated = manager.directory / 'user-file.txt'
    unrelated.write_text('keep')
    assert manager.purge() == (0, 0)
    assert not incomplete.exists() and unrelated.read_text() == 'keep'


def test_cli_purge_and_quota_validation(source):
    import subprocess
    import sys
    with parse_file(source, use_cache=False) as vcd:
        trace = InstructionTrace()
        trace.build(vcd, tags_for(vcd), storage='disk')
        path = Path(trace.entries.path)
    result = subprocess.run([sys.executable, 'alwavelets.py', '--purge-analysis-cache'], capture_output=True, text=True)
    assert result.returncode == 0 and 'Removed 1 instruction indexes' in result.stdout
    assert not path.exists() and source.exists()
    result = subprocess.run([sys.executable, 'alwavelets.py', '--analysis-cache-mib', '-1'], capture_output=True, text=True)
    assert result.returncode != 0 and 'must be nonnegative' in result.stderr
