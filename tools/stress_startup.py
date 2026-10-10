#!/usr/bin/env python3
"""Measure first screen, metadata, browser, and waveform readiness independently.

Fresh processes use real VCD/FST input and a recording terminal. Generation,
imports, curses initialization, and terminal I/O are outside the timings.
--repo supports the previous synchronous preparation path for comparison.
"""
import argparse
import curses
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]


def worker(args):
    sys.path.insert(0, str(args.repo))
    from alwavelets.storage import open_trace
    from alwavelets.ui import Alwavelets
    from tools.stress_tree import Screen
    from tools.stress_workflows import peak_rss
    modern = (args.repo / 'alwavelets' / 'loading.py').is_file()
    if modern:
        from alwavelets.loading import prepare_native, wait_for_trace
    for method in ('_init_colors', '_init_mouse', '_apply_config'):
        setattr(Alwavelets, method, lambda self: None)
    curses.color_pair = lambda n: 0
    measurements, stages = {}, {}
    screen = Screen()
    screen.getmaxyx = lambda: (40, 120)
    screen.keypad = screen.nodelay = lambda *args: None
    screen.getch = lambda: -1
    screen.addnstr = lambda row, col, text, n: screen.addstr(row, col, text[:n])

    def painted():
        measurements.setdefault('first_screen_s', time.perf_counter() - started)
    screen.refresh = painted

    def prepare(cancel, report):
        def progress(stage, *args):
            stages.setdefault(stage, time.perf_counter() - started)
            report(stage, *args)
        trace = prepare_native(args.source, storage='disk', memory_mib=8, cancel=cancel, stage_cb=progress)
        measurements['metadata_ready_s'] = time.perf_counter() - started
        return trace

    started = time.perf_counter()
    if modern:
        trace = wait_for_trace(screen, args.source, prepare)
    else:
        trace = open_trace(args.source, memory_mib=8, background=True)
        measurements['metadata_ready_s'] = time.perf_counter() - started
        painted()
    try:
        viewer = Alwavelets(screen, trace)
        viewer.collapse_all_modules()
        viewer.tree_expanded = {'top'}
        viewer._rebuild_tree()
        viewer.draw_tree_browser()
        measurements['browser_ready_s'] = time.perf_counter() - started
        first = trace.signals[trace.signal_order[0]]
        pause = threading.Event()
        while not first.num_changes() and trace.is_loading:
            if time.perf_counter() - started > args.timeout:
                raise TimeoutError('No readable waveform before timeout')
            pause.wait(0.005)
        assert first.num_changes() and first.value_at(0) == '0', trace._load_error
        measurements['first_readable_value_s'] = time.perf_counter() - started
        if trace._store.thread is not None:
            trace._store.thread.join(args.timeout)
        assert not trace.is_loading and not trace._load_error, trace._load_error
        measurements['complete_s'] = time.perf_counter() - started
        measurements['peak_rss_mib'] = peak_rss()
        measurements['stage_started_s'] = stages
        assert len(trace.signals) == args.scopes
        return measurements
    finally:
        trace.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scopes', type=int, default=100000)
    parser.add_argument('--formats', nargs='+', choices=('vcd', 'fst', 'wrapped'), default=['vcd', 'fst'])
    parser.add_argument('--repo', type=Path, default=ROOT)
    parser.add_argument('--timeout', type=float, default=180)
    parser.add_argument('--source', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.scopes <= 0 or args.timeout <= 0:
        parser.error('--scopes and --timeout must be positive')
    if args.source:
        print(json.dumps(worker(args)))
        return
    sys.path.insert(0, str(ROOT))
    from tools.stress_scopes import create_vcd
    from tools.stress_fst import create_fst, wrap_fst
    result = dict(scopes=args.scopes, repo=str(args.repo), formats={})
    with tempfile.TemporaryDirectory(prefix='alwavelets-startup-') as directory:
        for kind in args.formats:
            source = Path(directory) / ('trace.' + ('vcd' if kind == 'vcd' else 'fst'))
            if kind == 'vcd':
                create_vcd(source, args.scopes)
            else:
                create_fst(source, events=1, signals=args.scopes, scope_per_signal=True)
                if kind == 'wrapped':
                    wrapped = Path(directory) / 'wrapped.fst'
                    wrap_fst(source, wrapped)
                    source = wrapped
            samples = result['formats'][kind] = {}
            for phase in ('cold', 'cached'):
                env = dict(os.environ, ALWAVELETS_CACHE_DIR=str(Path(directory) / ('cache-' + kind)))
                completed = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--source', str(source),
                    '--scopes', str(args.scopes), '--repo', str(args.repo), '--timeout', str(args.timeout)],
                    env=env, capture_output=True, text=True, timeout=args.timeout * 2)
                if completed.returncode:
                    raise RuntimeError(completed.stderr or completed.stdout)
                samples[phase] = json.loads(completed.stdout)
                print(json.dumps(dict(format=kind, phase=phase, **samples[phase])), flush=True)
    if args.output:
        args.output.write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    main()
