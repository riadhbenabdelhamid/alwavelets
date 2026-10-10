#!/usr/bin/env python3
"""Measure real wide VCD/FST cold opens, cached reopens, browsing and finding.

Each phase runs in a fresh process. UI timings use a recording screen and the
real viewer methods; terminal I/O and waveform painting are excluded.
"""
import argparse
import curses
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = str(Path(__file__).resolve().parents[1])


def create_vcd(path, count):
    with path.open('w') as f:
        f.write('$scope module top $end\n')
        for i in range(count):
            f.write('$var wire 1 s%d signal%d $end\n' % (i, i))
        f.write('$upscope $end\n$enddefinitions $end\n#0\n')
        for i in range(count):
            f.write('0s%d\n' % i)


def worker(path, count, repo, phase):
    sys.path.insert(0, repo)
    from alwavelets.storage import open_trace
    from alwavelets.ui import Alwavelets
    from tools.stress_tree import Screen
    from tools.stress_workflows import peak_rss
    before = time.perf_counter()
    trace = open_trace(path, memory_mib=8)
    result = {'open_s': time.perf_counter() - before, 'open_peak_rss_mib': peak_rss(),
              'signals': len(trace.signals), 'index_bytes': Path(trace._store.path).stat().st_size}
    assert len(trace.signal_order) == count
    for i in (0, count // 2, count - 1):
        assert trace.signals[trace.signal_order[i]].value_at(0) == '0'
    v = None
    try:
        if phase == 'cold':
            return result
        # Run the full constructor, omitting environment-specific color/config
        # initialization. All declarations remain selected by default.
        screen = Screen()
        screen.getmaxyx = lambda: (40, 120)
        for method in ('_init_colors', '_init_mouse', '_apply_config'):
            setattr(Alwavelets, method, lambda self: None)
        curses.color_pair = lambda number: 0
        before = time.perf_counter()
        v = Alwavelets(screen, trace)
        result['viewer_init_s'] = time.perf_counter() - before
        before = time.perf_counter()
        v.draw_header()
        result['header_draw_ms'] = (time.perf_counter() - before) * 1000
        before = time.perf_counter()
        v.draw_tree_browser()
        result['browser_first_paint_ms'] = (time.perf_counter() - before) * 1000
        v.tree_expanded = {'top'}
        before = time.perf_counter()
        v._rebuild_tree()
        v.tree_cursor = len(v._tree_flat) - 1
        v.draw_tree_browser()
        result['browse_end_ms'] = (time.perf_counter() - before) * 1000
        before = time.perf_counter()
        v.open_finder()
        result['finder_open_ms'] = (time.perf_counter() - before) * 1000
        result['queries'] = {}
        for query in ('signal%d' % (count - 1), 'signal'):
            v.finder_query = query
            before = time.perf_counter()
            v._finder_refilter()
            submitted = time.perf_counter()
            if hasattr(v, 'poll_finder'):
                while getattr(v, '_finder_busy', False):
                    v.poll_finder()
                    time.sleep(0.001)
            done = time.perf_counter()
            matches = len(v.finder_matches)
            v.finder_sel = max(0, matches - 1)
            v.draw_finder()
            assert matches >= 1
            if query == 'signal':
                assert matches == count
            result['queries'][query] = {'submit_ms': (submitted - before) * 1000,
                                        'complete_s': done - before, 'matches': matches}
        before = time.perf_counter()
        v._finder_commit()
        result['finder_commit_ms'] = (time.perf_counter() - before) * 1000
        while getattr(v, '_finder_next_worker', None) is not None:
            v.poll_finder()
            time.sleep(0.001)
        result['next_matches_s'] = time.perf_counter() - before
        assert len(v.search_results) == count
        result['workflow_peak_rss_mib'] = peak_rss()
        return result
    finally:
        if v is not None and hasattr(v, 'close_finder'):
            v.close_finder()
            v.close_finder_navigation()
        trace.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--signals', type=int, default=1000000)
    parser.add_argument('--repo', default=ROOT)
    parser.add_argument('--formats', nargs='+', choices=['vcd', 'fst'], default=['vcd', 'fst'])
    parser.add_argument('--output', type=Path)
    parser.add_argument('--worker', nargs=2, metavar=('PATH', 'PHASE'))
    args = parser.parse_args()
    if args.signals < 1:
        parser.error('--signals must be positive')
    if args.worker:
        print(json.dumps(worker(args.worker[0], args.signals, args.repo, args.worker[1])))
        return
    sys.path.insert(0, ROOT)
    from tools.stress_fst import create_fst
    results = {}
    with tempfile.TemporaryDirectory(prefix='alwavelets-catalog-stress-') as directory:
        env = dict(os.environ, XDG_CACHE_HOME=os.path.join(directory, 'cache'))
        for kind in args.formats:
            path = Path(directory) / ('wide.' + kind)
            create_vcd(path, args.signals) if kind == 'vcd' else create_fst(path, signals=args.signals)
            results[kind] = {}
            for phase in ('cold', 'cached'):
                print('Checking %s %s (%d signals)…' % (kind, phase, args.signals), file=sys.stderr, flush=True)
                run = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--repo', args.repo,
                                      '--signals', str(args.signals), '--worker', str(path), phase],
                                     check=True, env=env, stdout=subprocess.PIPE, text=True)
                results[kind][phase] = json.loads(run.stdout)
    if args.output:
        args.output.write_text(json.dumps(results, indent=2) + '\n')
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
