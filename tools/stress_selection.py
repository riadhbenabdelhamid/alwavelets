#!/usr/bin/env python3
"""Measure display edits and sessions on real million/ten-million-signal FSTs.

Preparation builds the index once. Each reader uses a fresh process and a
recording screen; measurements exclude terminal I/O and waveform painting.
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


def worker(path, count, repo):
    sys.path.insert(0, repo)
    from alwavelets.storage import open_trace
    from alwavelets.ui import Alwavelets
    from tools.stress_tree import Screen
    from tools.stress_workflows import peak_rss
    result = {}
    def measure(name, operation):
        before = time.perf_counter()
        value = operation()
        result[name + '_ms'] = (time.perf_counter() - before) * 1000
        return value
    with open_trace(path, memory_mib=8) as trace:
        screen = Screen()
        screen.getmaxyx = lambda: (40, 120)
        for method in ('_init_colors', '_init_mouse', '_apply_config', 'apply_theme'):
            setattr(Alwavelets, method, lambda self: None)
        curses.color_pair = lambda number: 0
        v = measure('viewer_init', lambda: Alwavelets(screen, trace))
        measure('first_scope_paint', v.draw_tree_browser)
        scope = trace.scopes.children['top']
        measure('remove_scope', lambda: v._tree_remove_scope_signals(scope))
        assert len(v.display_order) == 0
        measure('add_scope', lambda: v._tree_add_scope_signals(scope))
        assert len(v.display_order) == count and v._tree_scope_all_checked(scope)
        sid = trace.signal_order[-1]
        measure('focus_last', lambda: v._focus_on(sid))
        measure('move_last', lambda: v.move_signal(-1))
        measure('undo_move', v.undo)
        assert v.display_order[-1] == sid
        v._focus_on(sid)
        measure('delete_last', v.delete_signal)
        measure('undo_delete', v.undo)
        assert v.display_order[-1] == sid
        restore = v._undo_groups_snapshot()
        measure('group_distant', lambda: v.create_group('pair', [trace.signal_order[0], sid], parent='outer'))
        measure('collapse_group', lambda: v.toggle_group_collapse('outer'))
        measure('group_rows', v._rebuild_view_rows)
        assert len(v.view_rows) == count - 1
        measure('undo_group', restore)
        assert len(v.display_order) == count
        encoded = measure('serialize_session', lambda: json.dumps(v._session_state()))
        result['session_bytes'] = len(encoded.encode())
        Path(v.session_path(str(path))).write_text(encoded)
        v.display_order = []
        assert measure('restore_session', v.restore_session)
        assert len(v.display_order) == count and v.display_order[-1] == sid
        result['peak_rss_mib'] = peak_rss()
        result['signals'] = count
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--signals', nargs='+', type=int, default=[1000000, 10000000])
    parser.add_argument('--repo', default=ROOT)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--worker', type=Path)
    args = parser.parse_args()
    if min(args.signals) < 1:
        parser.error('signal counts must be positive')
    if args.worker:
        print(json.dumps(worker(args.worker, args.signals[0], args.repo)))
        return
    sys.path.insert(0, ROOT)
    from tools.stress_fst import create_fst
    results = {}
    with tempfile.TemporaryDirectory(prefix='alwavelets-selection-stress-') as directory:
        env = dict(os.environ, XDG_CACHE_HOME=os.path.join(directory, 'cache'))
        for count in args.signals:
            source = Path(directory) / ('wide%d.fst' % count)
            print('Preparing %d-signal FST and index…' % count, file=sys.stderr, flush=True)
            create_fst(source, signals=count)
            # A separate process keeps preparation allocations out of UI RSS.
            subprocess.run([sys.executable, str(Path(__file__).with_name('stress_catalog.py')),
                            '--signals', str(count), '--worker', str(source), 'cold'],
                           check=True, env=env, stdout=subprocess.DEVNULL)
            print('Checking %d-signal selection/session workflow…' % count, file=sys.stderr, flush=True)
            run = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--repo', args.repo,
                                  '--signals', str(count), '--worker', str(source)],
                                 check=True, env=env, stdout=subprocess.PIPE, text=True)
            results[str(count)] = json.loads(run.stdout)
            print(json.dumps(results[str(count)], indent=2), flush=True)
    if args.output:
        args.output.write_text(json.dumps(results, indent=2) + '\n')


if __name__ == '__main__':
    main()
