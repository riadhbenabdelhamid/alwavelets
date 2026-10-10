"""Batched VCD ingestion preserves delta order, publication and bounded memory."""
import hashlib
import itertools
import json
from pathlib import Path
import random
import threading
from types import SimpleNamespace

import pytest

from alwavelets.catalog import SignalCatalog
from alwavelets.core import VCDSignal
from alwavelets.parsers import VCDFile
from alwavelets import storage
from tools.stress_vcd import create_vcd


@pytest.mark.parametrize('disordered', [False, True])
def test_mixed_batches_match_eager_with_small_pages_and_blackouts(tmp_path, monkeypatch, disordered):
    monkeypatch.setattr(storage, 'PAGE_EVENTS', 7)
    monkeypatch.setattr(storage, 'PAGE_BYTES', 512)
    monkeypatch.setattr(storage, 'VCD_BATCH_KEYS', 9)
    monkeypatch.setattr(storage, 'VCD_BATCH_BYTES', 2048)
    path = tmp_path / 'mixed.vcd'
    path.write_text('$scope module top $end\n' + ''.join(
        '$var %s %d s%d signal%d $end\n' % (kind, width, i, i)
        for i, (kind, width) in enumerate([('wire', 1), ('wire', 16), ('real', 64), ('wire', 4096)] * 5)) +
        '$var wire 1 s0 alias $end\n$upscope $end\n$enddefinitions $end\n')
    ticks = list(range(32))
    if disordered:
        random.Random(17).shuffle(ticks)
    with path.open('a') as output:
        for t in ticks:
            output.write('#%d\n' % (t // 2))  # duplicate timestamps across batches
            for i in range(20):
                if i % 4 == 0:
                    output.write((' b%s s%d\n' if t % 2 else '%ss%d\n') % ('01XZ'[t % 4], i))
                elif i % 4 == 2:
                    output.write('R%s s%d\n' % (['nan', '-1.25', '1e20', '0'][t % 4], i))
                else:
                    values = ['xz1', '0', 'X', 'z'] if i % 4 == 1 else ['xz1', '0', '', 'invalid']
                    output.write('B%s s%d\n' % (values[t % 4], i))
            output.write('1unknown\n$comment\n#999999\n1s0\n$end\n')
            if t % 8 == 0:
                output.write('$dumpoff $end\n1s0\nb1 s1\n$dumpon $end\n')
    eager = VCDFile.parse(path)
    with storage.open_trace(path, use_cache=False, memory_mib=0.02) as trace:
        assert trace.signal_order == eager.signal_order
        assert trace.max_time == eager.max_time
        for key, expected in eager.signals.items():
            signal = trace.signals[key]
            assert signal.changes == expected.changes, key
            for t in (0, 1, 5, 15, 50):
                assert signal.value_at(t) == expected.value_at(t)
            assert signal.numeric_range_in_window(1, 10) == expected.numeric_range_in_window(1, 10)
        assert trace._store.total_changes == sum(s.num_changes() for s in eager.signals.values())


def test_evicted_builders_and_inline_growth_keep_all_events(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'PAGE_EVENTS', 5)
    trace = VCDFile()
    expected = {}
    for i in range(16):
        key = 's%d' % i
        expected[key] = trace.signals[key] = VCDSignal(key, key)
        trace.signal_order.append(key)
    store = storage.TraceStore(str(tmp_path / 'trace.sqlite'), 65536)
    writer = storage.PageWriter(store, trace)
    writer.buffer_limit = 2000
    try:
        for round_number in range(12):
            batch = {}
            for i, key in enumerate(expected):
                events = [(round_number, '01xz'[j % 4]) for j in range(1 + (i + round_number) % 9)]
                for t, value in events:
                    expected[key].add_change(t, value)
                batch[key] = events
            writer.add_vcd_batch(batch)
            assert writer.buffer_bytes <= writer.buffer_limit
            if round_number % 3 == 0:
                writer.publish(True)
                for key in expected:
                    assert trace.signals[key].changes == expected[key].changes
        writer.publish(True)
        for key in expected:
            assert trace.signals[key].changes == expected[key].changes
    finally:
        trace.close()


def test_payload_bytes_bound_batches_even_with_one_oversized_value(tmp_path, monkeypatch):
    path = tmp_path / 'wide-values.vcd'
    values = ['1' * 2048] * 100 + ['x' * 100000] + ['0' * 2048] * 100
    path.write_text('$var wire 100000 ! bus $end\n$enddefinitions $end\n' +
                    ''.join('#%d\nb%s !\n' % (i, value) for i, value in enumerate(values)))
    original = storage.PageWriter.add_vcd_batch
    sizes = []

    def track(self, batch):
        sizes.append(sum(160 + 4 * len(key) + sum(128 + 4 * len(value) for _, value in events)
                         for key, events in batch.items()))
        return original(self, batch)

    monkeypatch.setattr(storage.PageWriter, 'add_vcd_batch', track)
    with storage.open_trace(path, memory_mib=0.02, use_cache=False) as trace:
        assert trace.signals['!'].changes == list(enumerate(values))
        assert trace._store.peak_cache_bytes <= trace._store.memory_bytes
    assert len(sizes) > 5
    assert sum(size > storage.VCD_BATCH_BYTES + 128 + 4 * 2048 for size in sizes) == 1
    assert max(sizes) <= storage.VCD_BATCH_BYTES + 128 + 4 * 100000


@pytest.mark.parametrize('cancel', [False, True])
def test_wide_vcd_publishes_and_cancels_between_bounded_batches(tmp_path, monkeypatch, cancel):
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    path = tmp_path / 'wide.vcd'
    create_vcd(path, 3000, 3000, 4, False)
    entered, release = threading.Event(), threading.Event()
    original, original_publish = storage.PageWriter.add_vcd_batch, storage.PageWriter.publish
    ticks = itertools.count()
    monkeypatch.setattr(storage, 'time', SimpleNamespace(monotonic=lambda: next(ticks) * 0.3))
    batches = []

    def track(self, batch):
        batches.append((len(batch), sum(map(len, batch.values()))))
        return original(self, batch)

    def pause(self, force=False):
        original_publish(self, force)
        if self.store.total_changes and not entered.is_set():
            entered.set()
            assert release.wait(5)

    monkeypatch.setattr(storage.PageWriter, 'add_vcd_batch', track)
    monkeypatch.setattr(storage.PageWriter, 'publish', pause)
    trace = storage.open_trace(path, background=True, memory_mib=0.1)
    try:
        assert entered.wait(5)
        assert trace.is_loading and not trace._load_error
        assert trace.signals['s0'].value_at(0) == '0'
        assert trace.signals['s2999'].num_changes() == 0
        assert 0 < trace.load_changes < 3000
        if cancel:
            trace._store.cancel.set()
        release.set()
        trace._store.thread.join(10)
        assert not trace.is_loading
        if cancel:
            assert 'cancelled' in trace._load_error.lower()
            assert not Path(storage.index_path(path)).exists()
        else:
            trace.require_loaded()
            assert trace.load_changes == 3000
            assert trace._store.total_changes == 3001  # alias shares its master's history
        assert max(n for n, _ in batches) <= storage.VCD_BATCH_KEYS
        assert max(n for _, n in batches) <= storage.PAGE_EVENTS
    finally:
        release.set()
        trace.close()
    assert not list((tmp_path / 'cache').rglob('alwavelets-index-*'))


def test_declaration_serialization_preserves_existing_session_fingerprints(tmp_path):
    store = storage.TraceStore(str(tmp_path / 'catalog.sqlite'), 65536)
    catalog = SignalCatalog(store, create=True)
    definitions = []
    try:
        for i in range(1100):
            definition = ['name"\\\n\u2603%d' % i, 'id"\\%d' % i, i % 32 + 1,
                          'real' if i % 3 else 'wire', 'top.\u2603"\\\t', [31, 0] if i % 2 else None]
            definitions.append(definition)
            catalog.declare(*definition)
        catalog.flush_declarations()
        expected = hashlib.sha256()
        for definition in definitions:
            name, _, size, kind, scope, bounds = definition
            expected.update(json.dumps([name, size, kind, scope, bounds]).encode() + b'\n')
        assert catalog.layout_signature == expected.hexdigest()
        assert [row[0] for row in catalog.rows('definition')] == [json.dumps(d) for d in definitions]
    finally:
        store.close()
