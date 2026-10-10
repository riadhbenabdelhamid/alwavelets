#!/usr/bin/env python3
"""Measure cold and resumed FST indexing of a single large compressed chain.

Workers have separate processes and isolated caches. Uses production checkpoint
cadence; generation and validation are excluded from timings. The interrupted
chain is decompressed again on resume, but saved events must not be decoded twice.
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
    sys.path.insert(0, str(args.repo))
    from alwavelets.storage import index_path, open_trace, partial_index_path
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
        assert phase != 'interrupt', 'trace ended before interruption'
        signal = trace.signals['1']
        assert len(trace.signals) == 1 and signal.num_changes() == args.events
        assert trace.load_changes == args.events and trace.max_time == args.events - 1
        for tick in (0, args.events // 2, args.events - 1):
            assert signal.value_at(tick) == str(tick % 2)
        return dict(phase=phase, elapsed_s=elapsed, cpu_s=cpu_s, peak_rss_mib=peak_rss(),
                    resume_offset=trace._resume_offset, index_bytes=Path(index_path(source)).stat().st_size)
    except ValueError as exc:
        if phase != 'interrupt' or not cancel.is_set() or 'cancelled' not in str(exc).lower():
            raise
        elapsed, cpu_s = time.perf_counter() - started, time.process_time() - cpu
        with sqlite3.connect(partial_index_path(source)) as db:
            meta = json.loads(db.execute('SELECT value FROM meta WHERE key="trace"').fetchone()[0])
        return dict(phase=phase, elapsed_s=elapsed, cpu_s=cpu_s, peak_rss_mib=peak_rss(),
                    checkpoint_offset=meta['checkpoint']['offset'], saved_events=meta['changes'],
                    saved_fraction=meta['changes'] / args.events)
    finally:
        if trace is not None:
            trace.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--events', type=int, default=10000000)
    parser.add_argument('--memory-mib', type=float, default=8)
    parser.add_argument('--stop-fraction', type=float, default=0.6)
    parser.add_argument('--wrapped', action='store_true')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--repo', type=Path, default=ROOT)
    parser.add_argument('--worker', nargs=2, metavar=('SOURCE', 'PHASE'))
    args = parser.parse_args()
    if min(args.events, args.memory_mib) <= 0 or not 0 < args.stop_fraction < 1:
        parser.error('counts and memory must be positive; stop-fraction must lie between 0 and 1')
    if args.worker:
        print(json.dumps(worker(args)))
        return
    sys.path.insert(0, str(ROOT))
    from tools.stress_fst import create_fst, wrap_fst
    with tempfile.TemporaryDirectory(prefix='alwavelets-fst-resume-') as directory:
        source = Path(directory) / 'deep.fst'
        create_fst(source, events=args.events)
        if args.wrapped:
            wrapped = Path(directory) / 'wrapped.fst'
            wrap_fst(source, wrapped)
            source = wrapped
        result = dict(events=args.events, input_bytes=source.stat().st_size, wrapped=args.wrapped)
        for phase in ('cold', 'interrupt', 'resume'):
            cache_dir = str(Path(directory) / ('cold-cache' if phase == 'cold' else 'resume-cache'))
            env = dict(os.environ, ALWAVELETS_CACHE_DIR=cache_dir)
            completed = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--worker', str(source), phase,
                                        '--events', str(args.events), '--memory-mib', str(args.memory_mib),
                                        '--stop-fraction', str(args.stop_fraction), '--repo', str(args.repo)],
                                       env=env, capture_output=True, text=True)
            if completed.returncode:
                raise RuntimeError(completed.stderr or completed.stdout)
            result[phase] = json.loads(completed.stdout)
            print(json.dumps(result[phase]), flush=True)
        assert result['resume']['resume_offset'] == result['interrupt']['checkpoint_offset']
        if args.output:
            args.output.write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    main()
