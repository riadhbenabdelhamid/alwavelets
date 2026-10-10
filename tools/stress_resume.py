#!/usr/bin/env python3
"""Measure a fresh VCD load against reopening after interrupted indexing.

Each phase runs in a fresh process with an isolated cache and verifies sampled
histories. Generation and validation are excluded from load times. The default
checkpoint cadence is unchanged; short traces may only save their header.
"""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]


def worker(args):
    sys.path.insert(0, str(ROOT))
    from alwavelets.storage import index_path, open_trace, partial_index_path
    from tools.stress_vcd import value_at
    from tools.stress_workflows import peak_rss
    source, phase = args.worker
    cancel = threading.Event()

    def progress(done, total):
        if phase == 'interrupt' and done >= total * args.stop_fraction:
            cancel.set()

    started, cpu = time.perf_counter(), time.process_time()
    trace = None
    try:
        trace = open_trace(source, memory_mib=args.memory_mib, progress_cb=progress, cancel=cancel)
        elapsed, cpu_s = time.perf_counter() - started, time.process_time() - cpu
        assert phase != 'interrupt', 'trace ended before the requested interruption'
        assert len(trace.signals) == args.signals + 1
        assert trace.load_changes == args.events
        for signal in sorted({0, 1, 2, 3, args.signals // 2, args.signals - 1}):
            if signal >= args.signals:
                continue
            expected = max(0, (args.events - signal + args.signals - 1) // args.signals)
            actual = trace.signals['s%d' % signal]
            assert actual.num_changes() == expected
            for tick in (0, expected // 2, expected - 1) if expected else ():
                assert actual.value_at(tick) == value_at(signal, tick, True)
        return {'phase': phase, 'elapsed_s': elapsed, 'cpu_s': cpu_s, 'peak_rss_mib': peak_rss(),
                'resume_offset': trace._resume_offset, 'index_bytes': Path(index_path(source)).stat().st_size}
    except ValueError as exc:
        if phase != 'interrupt' or not cancel.is_set() or 'cancelled' not in str(exc).lower():
            raise
        elapsed, cpu_s = time.perf_counter() - started, time.process_time() - cpu
        with sqlite3.connect(partial_index_path(source)) as db:
            meta = json.loads(db.execute('SELECT value FROM meta WHERE key="trace"').fetchone()[0])
        return {'phase': phase, 'elapsed_s': elapsed, 'cpu_s': cpu_s, 'peak_rss_mib': peak_rss(),
                'checkpoint_offset': meta['checkpoint']['offset'], 'saved_events': meta['changes'],
                'saved_fraction': meta['checkpoint']['offset'] / Path(source).stat().st_size}
    finally:
        if trace is not None:
            trace.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--signals', type=int, default=128)
    parser.add_argument('--events', type=int, default=4000000)
    parser.add_argument('--memory-mib', type=float, default=8)
    parser.add_argument('--stop-fraction', type=float, default=0.6)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--worker', nargs=2, metavar=('SOURCE', 'PHASE'))
    args = parser.parse_args()
    if min(args.signals, args.events, args.memory_mib) <= 0 or not 0 < args.stop_fraction < 1:
        parser.error('counts and memory must be positive; stop-fraction must lie between 0 and 1')
    if args.worker:
        print(json.dumps(worker(args)))
        return
    sys.path.insert(0, str(ROOT))
    from tools.stress_vcd import create_vcd
    with tempfile.TemporaryDirectory(prefix='alwavelets-resume-stress-') as directory:
        source = Path(directory) / 'mixed.vcd'
        create_vcd(source, args.signals, args.events, 4, True)
        result = {'signals': args.signals, 'events': args.events, 'input_bytes': source.stat().st_size}
        for phase in ('cold', 'interrupt', 'resume'):
            cache = str(Path(directory) / ('cold-cache' if phase == 'cold' else 'resume-cache'))
            completed = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--worker', str(source), phase,
                                        '--signals', str(args.signals), '--events', str(args.events),
                                        '--memory-mib', str(args.memory_mib), '--stop-fraction', str(args.stop_fraction)],
                                       env=dict(os.environ, XDG_CACHE_HOME=cache), capture_output=True, text=True)
            if completed.returncode:
                raise RuntimeError(completed.stderr or completed.stdout)
            result[phase] = json.loads(completed.stdout)
            print(json.dumps(result[phase]), flush=True)
        assert result['resume']['resume_offset'] == result['interrupt']['checkpoint_offset']
        if args.output:
            args.output.write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    main()
