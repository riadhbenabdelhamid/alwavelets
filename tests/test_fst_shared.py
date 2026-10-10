"""Shared compressed chains belong to distinct one-based signal handles.

Fixtures: stress_formats.create_vcd(path, 12, 24, True), then vcd2fst with
-4, -F and -Z. Signals 0/4/8 share history, alongside a declaration alias,
16/128-bit buses and reals. No converter is needed to run these tests.
"""
from contextlib import ExitStack
import json
from pathlib import Path
import sqlite3
import struct

import pytest

from alwavelets import cache, storage
from alwavelets.fst import FSTReader
from alwavelets.parsers import VCDFile
from test_fst_resume import interrupt

FIXTURES = Path(__file__).parent / 'fixtures'


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv('ALWAVELETS_CACHE_DIR', str(tmp_path / 'cache'))
    monkeypatch.setattr(cache, 'DIRECTORY', None)


def assert_matches(actual):
    with VCDFile.parse(FIXTURES / 'fst_shared.vcd') as expected:
        assert actual.timescale == expected.timescale and actual.max_time == expected.max_time
        assert len(actual.signals) == len(expected.signals)
        for got, ref in zip(actual.signals.values(), expected.signals.values()):
            assert (got.full_name, got.size, got.signal_type) == (ref.full_name, ref.size, ref.signal_type)
            assert got.changes == ref.changes, got.full_name


@pytest.mark.parametrize('kind,table', [(8, b'\x03\x7f\x01\x15\x79\x77'),
                                        (5, b'\x03\x00\x01\x00\x01\x15\x00\x04\x00\x05')])
def test_chain_table_aliases_reference_first_later_and_shared_handles(kind, table):
    reader = FSTReader('unused')
    reader._cancel, reader._limit, reader._handles = None, 0, [None] * 7
    with ExitStack() as resources:
        reader._resources = resources
        offsets, lengths = reader._decode_chain_table(kind, table, 21)
        assert list(offsets) == [0, 1, 1, 1, 11, 11, 11]
        assert list(lengths) == [0, 10, 10, 10, 10, 10, 10]


@pytest.mark.parametrize('codec', ['4', 'F', 'Z'])
@pytest.mark.parametrize('disk', [False, True])
def test_shared_chains_match_vcd_in_memory_disk_and_caches(codec, disk):
    source = FIXTURES / f'fst_shared_{codec}.fst'
    if disk:
        for _ in range(2):
            with storage.open_trace(source, memory_mib=0.1) as trace:
                assert_matches(trace)
    else:
        with FSTReader(source).parse(memory_mib=0.1) as trace:
            assert_matches(trace)
            assert cache.save_cache(trace, source)
        with cache.load_cache(source) as trace:
            assert_matches(trace)


@pytest.mark.parametrize('old_version', [False, True])
def test_shared_chains_resume_or_rebuild_old_decoder_checkpoint(tmp_path, monkeypatch, old_version):
    source = tmp_path / 'resume.fst'
    source.write_bytes((FIXTURES / 'fst_shared_4.fst').read_bytes())
    saved = interrupt(source, monkeypatch,
                      lambda state, writer: state['phase'] == 'values' and state['handle'] == 6)
    if old_version:
        with sqlite3.connect(storage.partial_index_path(source)) as db:
            meta = json.loads(db.execute('SELECT value FROM meta WHERE key="trace"').fetchone()[0])
            meta['version'] = 6
            db.execute('UPDATE meta SET value=? WHERE key="trace"', (json.dumps(meta),))
    parses = []
    original = FSTReader._parse_hierarchy

    def parse(self, *args):
        parses.append(1)
        return original(self, *args)

    monkeypatch.setattr(FSTReader, '_parse_hierarchy', parse)
    with storage.open_trace(source, memory_mib=0.1) as trace:
        assert_matches(trace)
        if not old_version:
            assert trace._resume_offset == saved['checkpoint']['offset']
    assert parses == ([1] if old_version else [])


@pytest.mark.parametrize('kind', ['vcd', 'fst'])
def test_old_eager_fst_caches_are_invalidated_without_invalidating_vcd(tmp_path, kind):
    source = tmp_path / 'source.trace'  # Recognize content regardless of suffix.
    source.write_bytes((FIXTURES / ('fst_shared_4.fst' if kind == 'fst' else 'fst_shared.vcd')).read_bytes())
    trace = FSTReader(source).parse() if kind == 'fst' else VCDFile.parse(source)
    with trace:
        assert cache.save_cache(trace, source)
    with open(cache.cache_path(source), 'r+b') as output:
        output.seek(4)
        output.write(struct.pack('<I', 6))
    loaded = cache.load_cache(source)
    if kind == 'fst':
        assert loaded is None
    else:
        with loaded:
            assert_matches(loaded)


@pytest.mark.parametrize('kind', ['vcd', 'fst'])
def test_old_disk_fst_caches_rebuild_without_invalidating_vcd(tmp_path, monkeypatch, kind):
    source = tmp_path / 'source.trace'
    source.write_bytes((FIXTURES / ('fst_shared_4.fst' if kind == 'fst' else 'fst_shared.vcd')).read_bytes())
    with storage.open_trace(source):
        pass
    with sqlite3.connect(storage.index_path(source)) as db:
        meta = json.loads(db.execute('SELECT value FROM meta WHERE key="trace"').fetchone()[0])
        meta['version'] = 6
        db.execute('UPDATE meta SET value=? WHERE key="trace"', (json.dumps(meta),))
    parses = []
    original = FSTReader._parse_hierarchy

    def parse(self, *args):
        parses.append(1)
        return original(self, *args)

    def no_vcd_parse(*args, **kwargs):
        raise AssertionError('valid VCD caches should remain reusable')

    monkeypatch.setattr(FSTReader, '_parse_hierarchy', parse)
    monkeypatch.setattr(VCDFile, 'parse_header', no_vcd_parse)
    with storage.open_trace(source) as trace:
        by_name = {sig.name: sig for sig in trace.signals.values()}
        assert by_name['signal4'].changes == by_name['signal0'].changes
        assert by_name['signal4'].num_changes() == 24
    assert parses == ([1] if kind == 'fst' else [])
