#!/usr/bin/env python3
"""Measure real hierarchy UI operations with 100k/1m metadata-only signals.

Each sample uses a fresh process and a recording terminal surface. Parsing,
waveform rendering and terminal I/O are outside these measurements.
"""
import argparse
import curses
import json
from pathlib import Path
import signal
import subprocess
import sys
import time
import tracemalloc

ROOT = str(Path(__file__).resolve().parents[1])


class Screen:
    def __init__(self):
        self.lines = {}

    def erase(self):
        self.lines.clear()

    def addstr(self, row, column, text, *args):
        self.lines[row, column] = text

    def addch(self, *args):
        pass


def make_viewer(count, scopes=1):
    from alwavelets.core import VCDSignal
    from alwavelets.parsers import VCDFile, build_scope_tree
    from alwavelets.ui import Alwavelets
    trace = VCDFile()
    trace.signals = {}
    for i in range(count):
        sid = str(i)
        scope = 'top' if scopes == 1 else 'top.block%d' % (i * scopes // count)
        trace.signals[sid] = VCDSignal('signal%d' % i, sid, 1, scope=scope, metadata_only=True)
        trace.signal_order.append(sid)
    trace.scopes = build_scope_tree(trace)
    viewer = Alwavelets.__new__(Alwavelets)
    viewer.vcd, viewer.scope_tree = trace, trace.scopes
    viewer.stdscr = Screen()
    viewer.display_order, viewer._display_order_set = list(trace.signal_order), set(trace.signal_order)
    viewer.virtual_signals, viewer._scope_check_cache = {}, {}
    viewer.tree_expanded = set()
    viewer.tree_cursor = viewer.tree_scroll = 0
    viewer.height, viewer.width, viewer.visible_signals = 40, 120, 30
    viewer.wave_top, viewer.ruler_row, viewer.panel_tree_width = 5, 4, 40
    viewer.panel_mode, viewer.sidebar_width = True, min(viewer.panel_tree_width, viewer.width // 4)
    viewer.panel_focus, viewer.panel_selected_scope = 0, 'top' if scopes == 1 else 'top.block0'
    viewer.groups, viewer._group_by_name, viewer._member_group, viewer._group_depth = [], {}, {}, {}
    viewer.view_rows, viewer.sig_offset, viewer.focus_idx = list(viewer.display_order), 0, 0
    viewer._undo_stack, viewer.collapsed_scopes = [], set()
    viewer._needs_layout, viewer.selected_signals = False, set()
    viewer._show_message = lambda text: None
    return viewer


def measured(operation, timeout):
    def expired(*args):
        raise TimeoutError('operation exceeded %.1fs' % timeout)
    alarm = hasattr(signal, 'setitimer')
    if alarm:
        prior = signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, timeout)
    before = time.perf_counter()
    try:
        operation()
        return {'ms': (time.perf_counter() - before) * 1000}
    except TimeoutError:
        return {'timeout_s': timeout}
    finally:
        if alarm:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, prior)


def worker(count, scopes, repo, timeout):
    sys.path.insert(0, repo)
    from tools.stress_workflows import peak_rss
    curses.color_pair = lambda number: 0
    viewer = make_viewer(count, scopes)
    timings = {}
    timings['open'] = measured(viewer._rebuild_tree, timeout)
    assert len(viewer._tree_flat) == 1
    timings['first_paint'] = measured(viewer.draw_tree_browser, timeout)
    lookups = [0]
    get_signal = viewer.get_signal
    def resolve(sid):
        lookups[0] += 1
        return get_signal(sid)
    viewer.get_signal = resolve
    timings['expand'] = measured(lambda: viewer.tree_handle_key(curses.KEY_RIGHT), timeout)
    if scopes > 1:
        viewer.tree_cursor = 1
        timings['expand_child'] = measured(lambda: viewer.tree_handle_key(curses.KEY_RIGHT), timeout)
    resolved_on_expand = lookups[0]
    # Fully expand for arbitrary jumps and the allocation check.
    viewer.tree_expanded = {'top'} | {'top.block%d' % i for i in range(scopes) if scopes > 1}
    viewer._rebuild_tree()
    expected_rows = count + 1 + (scopes if scopes > 1 else 0)
    assert len(viewer._tree_flat) == expected_rows
    viewer.tree_cursor = expected_rows - 1
    timings['scroll_last'] = measured(viewer.draw_tree_browser, timeout)
    assert viewer._tree_flat[-1][4] == str(count - 1)
    viewer.panel_selected_scope = viewer._tree_flat[-1][1].scope
    lookups[0] = 0
    timings['panel_paint'] = measured(viewer.draw_panel_tree, timeout)
    resolved_on_panel = lookups[0]
    timings['toggle_signal_off'] = measured(lambda: viewer.tree_handle_key(ord(' ')), timeout)
    assert str(count - 1) not in viewer._display_order_set
    assert not viewer._tree_flat[-1][3]
    timings['toggle_signal_on'] = measured(lambda: viewer.tree_handle_key(ord(' ')), timeout)
    assert len(viewer.display_order) == count and viewer._tree_flat[-1][3]
    viewer.tree_cursor = 0
    timings['collapse'] = measured(lambda: viewer.tree_handle_key(curses.KEY_LEFT), timeout)
    assert len(viewer._tree_flat) == 1
    timings['deselect_scope'] = measured(lambda: viewer.tree_handle_key(ord(' ')), timeout)
    assert not viewer.display_order
    timings['select_scope'] = measured(lambda: viewer.tree_handle_key(ord(' ')), timeout)
    assert len(viewer.display_order) == count
    timings['waveform_row_order'] = measured(viewer._rebuild_view_rows, timeout)
    assert len(viewer.view_rows) == count
    viewer.tree_expanded.add('top')
    tracemalloc.start()
    try:
        viewer._rebuild_tree()
        allocations = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    return {'signals': count, 'leaf_scopes': scopes, 'timings': timings,
            'signals_resolved_on_expand': resolved_on_expand,
            'signals_resolved_on_panel': resolved_on_panel,
            'expanded_tree_peak_mib': allocations / 1024 ** 2, 'process_peak_rss_mib': peak_rss()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--signals', type=int, nargs='+', default=[100000, 1000000])
    parser.add_argument('--scopes', type=int, nargs='+', default=[1, 1000])
    parser.add_argument('--timeout', type=float, default=5, help='Per-operation limit, including old revisions with slow redraws')
    parser.add_argument('--repo', default=ROOT)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--worker', nargs=2, type=int)
    args = parser.parse_args()
    if min(args.signals + args.scopes) < 1 or args.timeout <= 0:
        parser.error('counts and timeout must be positive')
    if args.worker:
        print(json.dumps(worker(*args.worker, args.repo, args.timeout)))
        return
    results = []
    for count in args.signals:
        for scopes in args.scopes:
            if scopes > count:
                continue
            print('Checking %d signals in %d leaf scopes…' % (count, scopes), file=sys.stderr, flush=True)
            run = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--worker', str(count), str(scopes),
                                  '--repo', args.repo, '--timeout', str(args.timeout)],
                                 check=True, capture_output=True, text=True, timeout=max(60, args.timeout * 20))
            results.append(json.loads(run.stdout))
    if args.output:
        args.output.write_text(json.dumps(results, indent=2) + '\n')
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
