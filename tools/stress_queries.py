#!/usr/bin/env python3
"""Compare cold reads, nearby cursor reads and pan/zoom rendering on VCD/FST.

Each repository/format runs in a fresh process with a temporary index. Cold
means an empty decoded-page cache; SQLite and OS caches may remain warm.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def summary(samples):
    ordered = sorted(samples)
    return dict(samples=len(samples), median_ms=statistics.median(samples),
                p95_ms=ordered[min(len(samples) - 1, int(len(samples) * 0.95))],
                total_s=sum(samples) / 1000)


def worker(args):
    sys.path.insert(0, str(args.repo))
    from alwavelets.core import Radix
    from alwavelets.rendering import draw_analog_culled, draw_bus_culled, draw_single_bit_culled
    from alwavelets.storage import open_trace
    from tools.stress_fst_chains import value_at
    from tools.stress_workflows import peak_rss

    def clear_pages(store):
        store.pages.clear()
        store.cache_bytes = 0

    kind = 'real' if args.kind == 'real' else 'bus'
    started = time.perf_counter()
    with open_trace(args.source, use_cache=False, memory_mib=args.memory_mib) as trace:
        result = dict(load_s=time.perf_counter() - started, index_bytes=Path(trace._store.path).stat().st_size,
                      indexing_peak_rss_mib=peak_rss())
        signal, alias = trace.signals.values()
        assert signal.num_changes() == alias.num_changes() == args.events
        digest = hashlib.sha256()
        # Verify the entire history outside the timed queries.
        for i, (timestamp, value) in enumerate(signal.iter_changes()):
            assert timestamp == i and value == value_at(i, kind, args.width, args.pattern)
            digest.update(str(timestamp).encode() + b':' + value.encode() + b'\n')
        assert i + 1 == args.events
        result['waveform_sha256'] = digest.hexdigest()
        store, rng = trace._store, random.Random(0)
        random_ticks = [rng.randrange(args.events) for _ in range(args.samples)]
        nearby_ticks = [min(args.events - 1, args.events // 2 + i % 256) for i in range(args.samples * 4)]
        queries = {}
        for name, ticks in [('cold_random', random_ticks), ('nearby_cursor', nearby_ticks)]:
            expected = [value_at(t, kind, args.width, args.pattern) for t in ticks]
            clear_pages(store)
            samples, reads = [], store.page_reads
            for j, (tick, want) in enumerate(zip(ticks, expected)):
                if name == 'cold_random':
                    clear_pages(store)
                # Alternate the signal and its alias to check shared locality.
                target = alias if j % 2 else signal
                before = time.perf_counter()
                got = target.value_at(tick)
                samples.append((time.perf_counter() - before) * 1000)
                assert got == want
            queries[name] = dict(summary(samples), pages_decoded=store.page_reads - reads)
        frames, samples = hashlib.sha256(), []
        clear_pages(store)
        reads = store.page_reads
        renderer = draw_analog_culled if kind == 'real' else draw_single_bit_culled if args.width == 1 else draw_bus_culled
        extra = (Radix.HEX,) if renderer is draw_bus_culled else ()
        for span in (32, 320, 3200, 32000, args.events - 1):
            span = min(span, args.events - 1)
            for pan in range(6):
                start = max(0, min(args.events - 1 - span, args.events // 3 + pan * max(1, span // 8)))
                before = time.perf_counter()
                frame = renderer(signal, start, start + span, 160, *extra)
                samples.append((time.perf_counter() - before) * 1000)
                frames.update(json.dumps(frame, sort_keys=True).encode() + b'\n')
        queries['pan_zoom'] = dict(summary(samples), pages_decoded=store.page_reads - reads)
        result.update(queries=queries, frames_sha256=frames.hexdigest(), peak_rss_mib=peak_rss(),
                      page_cache_peak_bytes=store.peak_cache_bytes)
        assert store.peak_cache_bytes <= store.memory_bytes
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--events', type=int, default=200000)
    parser.add_argument('--samples', type=int, default=1000)
    parser.add_argument('--kind', choices=('bus', 'scalar', 'real'), default='bus')
    parser.add_argument('--width', type=int, default=1024)
    parser.add_argument('--pattern', choices=('counter', 'dense'), default='dense')
    parser.add_argument('--format', choices=('vcd', 'fst', 'both'), default='both')
    parser.add_argument('--memory-mib', type=float, default=8)
    parser.add_argument('--repo', type=Path, default=ROOT)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--source', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.kind != 'bus':
        args.width = 64 if args.kind == 'real' else 1
    if args.events < 2 or min(args.samples, args.width, args.memory_mib) <= 0:
        parser.error('events must be at least two; sample count, width and memory must be positive')
    if args.source:
        print(json.dumps(worker(args)))
        return
    sys.path.insert(0, str(ROOT))
    from tools.stress_fst_chains import create_vcd
    formats = ('vcd', 'fst') if args.format == 'both' else (args.format,)
    converter = shutil.which('vcd2fst')
    if 'fst' in formats and converter is None:
        parser.error('vcd2fst is required to generate FST benchmark inputs')
    results = dict(events=args.events, kind=args.kind, width=args.width, pattern=args.pattern,
                   memory_mib=args.memory_mib, repositories={})
    signature = None
    with tempfile.TemporaryDirectory(prefix='alwavelets-queries-') as directory:
        vcd, fst = (Path(directory) / ('trace.' + fmt) for fmt in ('vcd', 'fst'))
        create_vcd(vcd, args.events, 'real' if args.kind == 'real' else 'bus', args.width, args.pattern)
        if 'fst' in formats:
            subprocess.run([converter, '-4', str(vcd), str(fst)], check=True, capture_output=True, timeout=300)
        repositories = [('baseline', args.baseline)] if args.baseline else []
        for label, repo in repositories + [('current', args.repo)]:
            results['repositories'][label] = {}
            for fmt in formats:
                command = [sys.executable, str(Path(__file__).resolve()), '--source', str(vcd if fmt == 'vcd' else fst),
                           '--repo', str(repo), '--events', str(args.events), '--samples', str(args.samples),
                           '--kind', args.kind, '--width', str(args.width), '--pattern', args.pattern,
                           '--memory-mib', str(args.memory_mib)]
                env = dict(os.environ, ALWAVELETS_CACHE_DIR=str(Path(directory) / label / fmt))
                completed = subprocess.run(command, env=env, capture_output=True, text=True, timeout=300)
                if completed.returncode:
                    raise RuntimeError(completed.stderr or completed.stdout)
                sample = json.loads(completed.stdout)
                actual = (sample['waveform_sha256'], sample['frames_sha256'])
                if signature is None:
                    signature = actual
                assert actual == signature, 'Waveform or viewport mismatch'
                results['repositories'][label][fmt] = sample
                print(json.dumps(dict(repository=label, format=fmt, **sample)), flush=True)
    if args.output:
        args.output.write_text(json.dumps(results, indent=2) + '\n')


if __name__ == '__main__':
    main()
