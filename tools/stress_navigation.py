#!/usr/bin/env python3
"""Measure rapid pan/zoom through the real viewer with a recording terminal.

Paired repositories run in fresh processes over identical VCD/FST files.
Timings include viewer drawing, queueing, cursor reads and row completion;
they exclude indexing, physical terminal I/O and the normal input poll delay.
"""
import argparse
import curses
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from stress_queries import summary

ROOT = Path(__file__).resolve().parents[1]


def worker(args):
    sys.path.insert(0, str(args.repo))
    from alwavelets.core import VirtualBitSignal
    from alwavelets.storage import open_trace
    from alwavelets.ui import Alwavelets
    from tools.stress_tree import Screen
    from tools.stress_workflows import peak_rss

    for method in ('_init_colors', '_init_mouse', '_apply_config'):
        setattr(Alwavelets, method, lambda self: None)
    curses.color_pair = lambda n: 0
    screen = Screen()
    screen.getmaxyx = lambda: (max(40, args.signals * (1 + args.expanded_bits) + 10), 183)
    screen.keypad = screen.nodelay = lambda *a: None
    screen.addch = lambda row, col, char, *a: screen.addstr(row, col, str(char))
    screen.addnstr = lambda row, col, text, n, *a: screen.addstr(row, col, text[:n])
    screen.getch = lambda: -1
    screen.refresh = lambda: None
    with open_trace(args.source, use_cache=False, memory_mib=args.memory_mib) as trace:
        viewer = Alwavelets(screen, trace)
        if args.subcell:
            viewer.line_thickness = 2
        signals = list(trace.signal_order)[:args.signals]
        order = []
        for sid in signals:
            order.append(sid)
            for bit in range(args.expanded_bits):
                vid = f'{sid}:bit{bit}'
                viewer.virtual_signals[vid] = VirtualBitSignal(trace.signals[sid], bit)
                order.append(vid)
        viewer.display_order = order
        viewer.update_layout()
        if args.kind == 'real':
            viewer.analog_signals.update(signals)
        store = trace._store
        submits, latest, stable, bursts = [], [], [], []
        frames = hashlib.sha256()
        overlapping, peak_queue = 0, 0
        stable_pages = burst_pages = 0

        def paint():
            screen.erase()
            # Mirrors the main loop's cancellation before header/catalog reads.
            prepare = getattr(viewer, '_prepare_row_window', None)
            if prepare is not None:
                prepare()
            viewer.draw_signals()

        def finish():
            deadline = time.monotonic() + 30
            while True:
                queries = viewer._queries
                if queries.poll():
                    paint()
                assert not queries.errors, queries.errors
                if not queries.pending:
                    return
                if time.monotonic() > deadline:
                    raise TimeoutError('Latest viewport did not finish')
                time.sleep(0.001)

        def reset():
            queries = getattr(viewer, '_queries', None)
            if queries is not None:
                finish()
                for group in list(queries.windows):
                    queries.invalidate(group)
            store.pages.clear()
            store.page_hints.clear()
            store.cache_bytes = 0
            if hasattr(store, 'clear_samples'):
                store.clear_samples()
            viewer.time_start = args.events // 8
            viewer.time_end = viewer.time_start + args.events // 2
            viewer.cursor_a = (viewer.time_start + viewer.time_end) // 2

        def record_frame():
            queries = viewer._queries
            window = (viewer.time_start, viewer.time_end, viewer.wave_width, viewer.line_thickness)
            assert all(token[1][1] == window for token in queries.ready if token[0] == 'rows')
            assert sum(token[0] == 'rows' for token in queries.ready) == len(order)
            frames.update(json.dumps(sorted(screen.lines.items())).encode())
            for sid in order:
                rows = [(token[2][0], value) for token, value in queries.ready.items()
                        if token[0] == 'rows' and token[2][1] == sid]
                frames.update(json.dumps(rows, sort_keys=True).encode())
                extras = [value for (kind, key), value in viewer._row_extras.items() if key == sid]
                frames.update(json.dumps(extras, sort_keys=True).encode())

        reads = store.page_reads
        try:
            for _ in range(args.rounds):
                reset()
                before_pages = store.page_reads
                started = time.perf_counter()
                paint()
                finish()
                stable.append((time.perf_counter() - started) * 1000)
                stable_pages += store.page_reads - before_pages
                record_frame()
                reset()
                before_pages = store.page_reads
                burst_start = time.perf_counter()
                paint()
                for step in range(args.inputs):
                    time.sleep(args.interval_ms / 1000)
                    queries = viewer._queries
                    queries.poll()
                    overlapping += any(token[0] == 'rows' and future.running()
                                       for token, future in queries.pending.items())
                    started = time.perf_counter()
                    if step == args.inputs - 1:
                        viewer.zoom(64 / (viewer.time_end - viewer.time_start), center=args.events // 2)
                    elif step % 2:
                        viewer.zoom(0.8 if step % 4 == 1 else 1.25)
                    else:
                        viewer.scroll_time(1 if step % 4 == 0 else -1)
                    paint()
                    submits.append((time.perf_counter() - started) * 1000)
                    queued = getattr(queries, 'queued_count', None)
                    if queued is None:
                        queued = queries.pool._work_queue.qsize()
                    peak_queue = max(peak_queue, queued)
                finish()
                latest.append((time.perf_counter() - started) * 1000)
                bursts.append((time.perf_counter() - burst_start) * 1000)
                burst_pages += store.page_reads - before_pages
                record_frame()
            assert store.peak_cache_bytes <= store.memory_bytes
            return dict(submit=summary(submits), latest_viewport=summary(latest), burst=summary(bursts),
                        stable_viewport=summary(stable), overlapping_inputs=overlapping,
                        peak_queued_jobs=peak_queue, pages_decoded=store.page_reads - reads,
                        stable_pages_decoded=stable_pages, burst_pages_decoded=burst_pages,
                        sample_cache_hits=getattr(store, 'sample_cache_hits', 0),
                        peak_sample_cache_bytes=getattr(store, 'peak_sample_cache_bytes', 0),
                        frames_sha256=frames.hexdigest(), rendered_rows=len(order),
                        peak_cache_bytes=store.peak_cache_bytes, peak_rss_mib=peak_rss())
        finally:
            if hasattr(viewer, '_queries'):
                viewer._queries.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--events', type=int, default=50000)
    parser.add_argument('--signals', type=int, default=1)
    parser.add_argument('--expanded-bits', type=int, default=0)
    parser.add_argument('--subcell', action='store_true', help='Render bit lanes at twice the horizontal resolution')
    parser.add_argument('--kind', choices=('bus', 'scalar', 'real'), default='bus')
    parser.add_argument('--width', type=int, default=1024)
    parser.add_argument('--rounds', type=int, default=5)
    parser.add_argument('--inputs', type=int, default=8)
    parser.add_argument('--interval-ms', type=float, default=5)
    parser.add_argument('--memory-mib', type=float, default=8)
    parser.add_argument('--format', choices=('vcd', 'fst', 'both'), default='both')
    parser.add_argument('--repo', type=Path, default=ROOT)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--source', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.kind != 'bus':
        args.width = 64 if args.kind == 'real' else 1
    if min(args.signals, args.width, args.rounds, args.inputs, args.memory_mib, args.interval_ms) <= 0 or args.events < 128:
        parser.error('events must be at least 128; other sizes and intervals must be positive')
    if not 0 <= args.expanded_bits <= args.width or (args.expanded_bits and args.kind != 'bus'):
        parser.error('expanded bits require a bus and cannot exceed its width')
    if args.source:
        print(json.dumps(worker(args)))
        return
    from stress_fst_chains import value_at
    formats = ('vcd', 'fst') if args.format == 'both' else (args.format,)
    converter = shutil.which('vcd2fst')
    if 'fst' in formats and converter is None:
        parser.error('vcd2fst is required for FST inputs')
    results = dict(events=args.events, signals=args.signals, expanded_bits=args.expanded_bits, kind=args.kind,
                   width=args.width, rounds=args.rounds, inputs=args.inputs, interval_ms=args.interval_ms,
                   memory_mib=args.memory_mib, repositories={})
    results['subcell'] = args.subcell
    signature = None
    with tempfile.TemporaryDirectory(prefix='alwavelets-navigation-') as directory:
        vcd, fst = (Path(directory) / ('trace.' + fmt) for fmt in ('vcd', 'fst'))
        real = args.kind == 'real'
        with vcd.open('w') as output:
            output.write('$timescale 1ns $end\n$scope module top $end\n')
            for number in range(args.signals):
                output.write(f'$var {"real" if real else "wire"} {args.width} s{number} value{number} $end\n')
            output.write('$upscope $end\n$enddefinitions $end\n')
            for tick in range(args.events):
                output.write(f'#{tick}\n')
                for number in range(args.signals):
                    value = value_at(tick + number * args.events, 'real' if real else 'bus', args.width, 'dense')
                    output.write(f'{"r" if real else "b"}{value} s{number}\n')
        if 'fst' in formats:
            subprocess.run([converter, '-4', str(vcd), str(fst)], check=True, capture_output=True, timeout=300)
        repositories = [('baseline', args.baseline)] if args.baseline else []
        for label, repo in repositories + [('current', args.repo)]:
            samples = results['repositories'][label] = {}
            for fmt in formats:
                command = [sys.executable, str(Path(__file__).resolve()), '--source', str(vcd if fmt == 'vcd' else fst),
                           '--repo', str(repo), '--events', str(args.events), '--signals', str(args.signals),
                           '--expanded-bits', str(args.expanded_bits), '--kind', args.kind, '--width', str(args.width),
                           '--rounds', str(args.rounds), '--inputs', str(args.inputs), '--interval-ms', str(args.interval_ms),
                           '--memory-mib', str(args.memory_mib)]
                if args.subcell:
                    command.append('--subcell')
                completed = subprocess.run(command, capture_output=True, text=True, timeout=300,
                                           env=dict(os.environ, ALWAVELETS_CACHE_DIR=str(Path(directory) / label / fmt)))
                if completed.returncode:
                    raise RuntimeError(completed.stderr or completed.stdout)
                sample = samples[fmt] = json.loads(completed.stdout)
                signature = signature or sample['frames_sha256']
                assert sample['frames_sha256'] == signature, 'Final viewport differs between repositories/formats'
                print(json.dumps(dict(repository=label, format=fmt, **sample)), flush=True)
    if args.output:
        args.output.write_text(json.dumps(results, indent=2) + '\n')


if __name__ == '__main__':
    main()
