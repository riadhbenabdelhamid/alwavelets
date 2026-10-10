#!/usr/bin/env python3
"""Measure cold/cached opens and real browser actions with many sibling scopes.

Workers run in fresh processes with isolated caches. Timings exclude generation;
the recording screen excludes terminal I/O. --repo accepts an older checkout for
paired comparisons. All declarations remain available and selected initially.
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

ROOT = Path(__file__).resolve().parents[1]


def create_vcd(path, count):
    with path.open('w') as output:
        output.write('$scope module top $end\n')
        for i in range(count):
            output.write(f'$scope module u{i} $end\n$var wire 1 s{i} value{i} $end\n$upscope $end\n')
        output.write('$upscope $end\n$enddefinitions $end\n#0\n')
        for i in range(count):
            output.write(f'0s{i}\n')


def worker(args):
    sys.path.insert(0, str(args.repo))
    from alwavelets.storage import open_trace
    from alwavelets.ui import Alwavelets
    from tools.stress_tree import Screen, measured
    from tools.stress_workflows import peak_rss
    source, phase = args.worker
    started = time.perf_counter()
    with open_trace(source, memory_mib=8) as trace:
        report = dict(phase=phase, open_s=time.perf_counter() - started, open_peak_rss_mib=peak_rss(),
                      index_bytes=Path(trace._store.path).stat().st_size)
        assert len(trace.signals) == args.scopes
        assert trace.signals[trace.signal_order[-1]].value_at(0) == '0'
        if phase == 'cold':
            return report
        for name in ('_init_colors', '_init_mouse', '_apply_config'):
            setattr(Alwavelets, name, lambda self: None)
        curses.color_pair = lambda n: 0
        screen = Screen()
        screen.getmaxyx = lambda: (40, 120)
        started = time.perf_counter()
        viewer = Alwavelets(screen, trace)
        report['viewer_init_ms'] = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        viewer.tree_expanded = {'top'}
        viewer._rebuild_tree()
        viewer.tree_cursor = len(viewer._tree_flat) - 1
        viewer.draw_tree_browser()
        assert viewer._tree_flat[-1][1].full_path == f'top.u{args.scopes - 1}'
        report['expand_and_end_ms'] = (time.perf_counter() - started) * 1000
        report['browser_peak_rss_mib'] = peak_rss()
        folding = measured(viewer.collapse_all_modules, args.timeout)
        if 'timeout_s' in folding:
            report['default_folding_timeout_s'] = folding['timeout_s']
            report['workflow_peak_rss_mib'] = peak_rss()
            return report
        assert not viewer.display_order and len(viewer.collapsed_scopes) == args.scopes
        report['default_folding_ms'] = folding['ms']
        started = time.perf_counter()
        state = json.dumps(viewer._session_state())
        report['session_bytes'] = len(state.encode())
        report['session_s'] = time.perf_counter() - started
        report['workflow_peak_rss_mib'] = peak_rss()
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scopes', type=int, default=100000)
    parser.add_argument('--formats', nargs='+', choices=('vcd', 'fst'), default=['vcd', 'fst'])
    parser.add_argument('--repo', type=Path, default=ROOT)
    parser.add_argument('--timeout', type=float, default=10, help='default-folding time limit in seconds (POSIX)')
    parser.add_argument('--worker', nargs=2, metavar=('SOURCE', 'PHASE'))
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.scopes <= 0 or args.timeout <= 0:
        parser.error('--scopes and --timeout must be positive')
    if args.worker:
        print(json.dumps(worker(args)))
        return
    sys.path.insert(0, str(ROOT))
    from tools.stress_fst import create_fst
    result = dict(scopes=args.scopes, repo=str(args.repo), formats={})
    with tempfile.TemporaryDirectory(prefix='alwavelets-scopes-') as directory:
        for kind in args.formats:
            source = Path(directory) / ('scopes.' + kind)
            if kind == 'vcd':
                create_vcd(source, args.scopes)
            else:
                create_fst(source, events=1, signals=args.scopes, scope_per_signal=True)
            measurements = result['formats'][kind] = {}
            for phase in ('cold', 'cached'):
                env = dict(os.environ, ALWAVELETS_CACHE_DIR=str(Path(directory) / ('cache-' + kind)))
                completed = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--worker', str(source), phase,
                                            '--scopes', str(args.scopes), '--repo', str(args.repo),
                                            '--timeout', str(args.timeout)],
                                           env=env, capture_output=True, text=True)
                if completed.returncode:
                    raise RuntimeError(completed.stderr or completed.stdout)
                measurements[phase] = json.loads(completed.stdout)
                print(json.dumps(dict(format=kind, **measurements[phase])), flush=True)
    if args.output:
        args.output.write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    main()
