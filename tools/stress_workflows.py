#!/usr/bin/env python3
"""Fresh-process large-file and declaration-count stress checks (stdlib only)."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def generate(path, size_bytes, signals):
    from tools.benchmark import identifier
    count = 0
    with path.open('wb', buffering=1 << 20) as stream:
        stream.write(b'$timescale 1ns $end\n$scope module stress $end\n')
        if signals:
            for i in range(signals):
                stream.write(('$var wire 1 %s s%d $end\n' % (identifier(i), i)).encode())
        else:
            stream.write(b'$var wire 1 ! clk $end\n$var wire 4096 " bus $end\n')
        stream.write(b'$upscope $end\n$enddefinitions $end\n')
        if signals:
            stream.write(b'#0\n')
            for i in range(signals):
                stream.write(('%d%s\n' % (i % 2, identifier(i))).encode())
            stream.write(b'#1\n')
            return signals
        payloads = [bytes([ord(c)]) * 4096 for c in '01xz']
        while stream.tell() < size_bytes:
            stream.write(('#%d\n%d!\nb' % (count, count % 2)).encode())
            stream.write(payloads[count % 4])
            stream.write(b' "\n')
            count += 1
    return count * 2


def peak_rss():
    import resource
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / (2**20 if sys.platform == 'darwin' else 1024)


def worker_results(count):
    from alwavelets.results import PagedResults
    from alwavelets.decoders import DecodedFrame, BusTransaction
    from alwavelets.parsers import VCDFile
    def frames():
        for i in range(count):
            yield DecodedFrame(i * 10, i * 10 + 9, '%02X' % (i % 256))
    def transactions():
        for i in range(count):
            txn = BusTransaction('AXI4', 'read', i * 10)
            txn.t_end, txn.id, txn.length = i * 10 + 20, i % 32, 4
            txn.data, txn.resp = [i, i + 1, i + 2, i + 3], 'OKAY'
            yield txn
    def instructions():
        for i in range(count):
            yield {'time': i * 10, 'pc': i * 4, 'decoded': {'asm': 'nop'}, 'hart_id': i % 4}
    result = {'mode': 'decoded-results', 'records_per_type': count}
    paths = []
    with VCDFile() as owner:
        for name, values, item_type in [('frames', frames(), DecodedFrame),
                                        ('transactions', transactions(), BusTransaction),
                                        ('instructions', instructions(), dict)]:
            before = time.perf_counter()
            records = PagedResults.collect(values, item_type, owner=owner)
            result[name + '_write_s'] = time.perf_counter() - before
            assert len(records) == count
            sample = records[-1]
            assert (sample['time'] if item_type is dict else sample.t_start) == (count - 1) * 10
            if name == 'transactions':
                assert sample.data == list(range(count - 1, count + 3))
            before = time.perf_counter()
            tail = list(records.iter_window(max(0, (count - 10) * 10), count * 10))
            assert len(tail) == min(count, 12 if name == 'transactions' else 10)
            result[name + '_seek_ms'] = (time.perf_counter() - before) * 1000
            result[name + '_bytes'] = os.path.getsize(records.path)
            paths.append(records.path)
            if name == 'frames':
                before = time.perf_counter()
                assert sum(len(s) for _, s in records.overlay(0, count * 10, 160)) <= 160
                result['overview_s'] = time.perf_counter() - before
        result['peak_rss_mib'] = peak_rss()
    assert all(not os.path.exists(path) for path in paths)
    return result


def worker(path, mode, memory_mib, result_frames=1000000):
    if mode == 'decoded-results':
        return worker_results(result_frames)
    from alwavelets.storage import open_trace
    from alwavelets.graphics import write_waveform_png
    from alwavelets.exporting import write_signal_csv
    from alwavelets.decoders import SPIDecoder, DecodedFrame
    from alwavelets.results import PagedResults
    started = time.perf_counter()
    with open_trace(path, memory_mib=memory_mib) as trace:
        load = time.perf_counter() - started
        count = sum(sig.num_changes() for sig in trace.signals.values() if sig.owns_storage)
        seek = []
        sig = trace.signals[trace.signal_order[-1]]
        for fraction in (0, .9, .4, .1, .7):
            t = int(trace.max_time * fraction)
            before = time.perf_counter()
            value = sig.value_at(t)
            seek.append(time.perf_counter() - before)
            if mode == 'wide':
                assert value == '01xz'[t % 4] * 4096
        measurements = {}
        if mode == 'wide':
            clock = trace.signals['!']
            started_export = time.perf_counter()
            with tempfile.TemporaryFile() as image:
                write_waveform_png(trace, image, ['stress.clk'], width=640)
                image.seek(0)
                measurements['png_sha256'] = hashlib.sha256(image.read()).hexdigest()
            measurements['png_s'] = time.perf_counter() - started_export
            started_export = time.perf_counter()
            with tempfile.TemporaryFile(mode='w+') as csv:
                measurements['csv_rows'] = write_signal_csv(clock, csv)
            measurements['csv_s'] = time.perf_counter() - started_export
            started_export = time.perf_counter()
            with PagedResults.collect(SPIDecoder(clock, clock).iter_decode(0, trace.max_time),
                                      DecodedFrame, owner=trace) as frames:
                measurements['spi_frames'] = len(frames)
                assert all(frame.label == '0xFF' for frame in frames[:-1])
            measurements['spi_s'] = time.perf_counter() - started_export
            assert measurements['csv_rows'] == count // 2
            assert measurements['spi_frames'] == ((count // 2 // 2) + 7) // 8
        result = {'mode': mode, 'source_bytes': os.path.getsize(path), 'load_s': load,
                  'signals': len(trace.signals), 'changes': count,
                  'seek_median_ms': statistics.median(seek) * 1000,
                  'page_cache_peak_mib': trace._store.peak_cache_bytes / 2**20,
                  'first_open_peak_rss_mib': peak_rss(),
                  'index_bytes': os.path.getsize(trace._store.path), **measurements}
    before = time.perf_counter()
    with open_trace(path, memory_mib=memory_mib) as reopened:
        result['reopen_s'] = time.perf_counter() - before
        assert reopened._store.page_reads == 0
    result['peak_rss_mib'] = peak_rss()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--size-gib', type=float, default=1)
    parser.add_argument('--signals', type=int, default=100000)
    parser.add_argument('--memory-mib', type=int, default=8)
    parser.add_argument('--result-frames', type=int, default=1000000)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--worker', nargs=2, metavar=('PATH', 'MODE'))
    args = parser.parse_args()
    if args.size_gib <= 0 or args.signals <= 0 or args.memory_mib <= 0 or args.result_frames <= 0:
        parser.error('--size-gib, --signals, --memory-mib, and --result-frames must be positive')
    if args.worker:
        print(json.dumps(worker(*args.worker, args.memory_mib, args.result_frames)))
        return
    report = {'python': sys.version, 'results': []}
    with tempfile.TemporaryDirectory(prefix='alwavelets-stress-') as directory:
        for mode, signals in [('wide', 0), ('many-signals', args.signals), ('decoded-results', 0)]:
            source = Path(directory) / (mode + '.vcd')
            if mode != 'decoded-results':
                print('Generating %s…' % mode, file=sys.stderr, flush=True)
                expected = generate(source, int(args.size_gib * 2**30), signals)
                print('Checking %s: %.2f GiB, %d changes' % (mode, source.stat().st_size / 2**30, expected),
                      file=sys.stderr, flush=True)
            else:
                print('Checking %d decoded records of each type…' % args.result_frames, file=sys.stderr, flush=True)
            command = [sys.executable, str(Path(__file__).resolve()), '--worker', str(source), mode,
                       '--memory-mib', str(args.memory_mib), '--result-frames', str(args.result_frames)]
            result = subprocess.run(command, check=True, capture_output=True, text=True,
                                    env=dict(os.environ, XDG_CACHE_HOME=directory))
            sample = json.loads(result.stdout)
            if mode != 'decoded-results':
                assert sample['changes'] == expected
            report['results'].append(sample)
            print(json.dumps(sample), flush=True)
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    main()
