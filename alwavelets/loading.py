"""Preparation ownership and a responsive terminal screen before metadata exists."""
import curses
import os
import threading
import time

from .streaming import OperationCancelled, check_cancel


def report_stage(callback, stage, done=None, total=None, unit='bytes'):
    if callback is not None:
        callback(stage, done, total, unit)


class StartupJob:
    """Keep an unclaimed trace on the worker, including cancellation cleanup.

    The caller claims a prepared trace with take(), then owns closing it.
    Cancelling before that handoff closes it on the worker. No worker uses curses.
    """
    def __init__(self, prepare):
        self.prepare = prepare
        self.cancel = threading.Event()
        self.ready, self.finished, self.decision = (threading.Event() for _ in range(3))
        self.lock = threading.Lock()
        self.progress = ('Checking file', None, None, 'bytes')
        self.result = self.error = None
        self.claimed = False
        self.started = time.monotonic()
        self.prepared_at = None
        self.stages = {}
        self.thread = threading.Thread(target=self._run, name='wavelet-open', daemon=True)

    def report(self, stage, done=None, total=None, unit='bytes'):
        with self.lock:
            self.progress = stage, done, total, unit
            self.stages.setdefault(stage, time.monotonic() - self.started)

    def snapshot(self):
        with self.lock:
            return self.progress

    def start(self):
        self.thread.start()

    def _run(self):
        trace = None
        try:
            trace = self.prepare(self.cancel, self.report)
            check_cancel(self.cancel)
            with self.lock:
                self.result = trace
                self.prepared_at = time.monotonic()
            self.ready.set()
            self.decision.wait()
        except BaseException as exc:
            self.error = exc
        finally:
            try:
                if trace is not None and not self.claimed:
                    trace.close()
            except BaseException as exc:
                if self.error is None:
                    self.error = exc
            finally:
                self.finished.set()
                self.ready.set()

    def take(self):
        with self.lock:
            check_cancel(self.cancel)
            if self.error is not None:
                raise self.error
            if self.result is None or self.claimed:
                raise RuntimeError('No prepared trace to claim')
            self.claimed = True
            trace, self.result = self.result, None
            self.decision.set()
            return trace

    def stop(self):
        with self.lock:
            if not self.claimed:
                self.cancel.set()
                self.decision.set()

    def close(self):
        self.stop()
        if self.thread.ident is not None:
            self.thread.join()


def draw_loading(screen, filename, progress, elapsed, cancelling=False):
    """Handle narrow windows, resizing, and terminal-edge write errors."""
    height, width = screen.getmaxyx()
    stage, done, total, unit = progress
    spinner = '|/-\\'[int(elapsed * 8) % 4]
    lines = [f'Opening {os.path.basename(filename)}',
             'Cancelling; finishing cleanup...' if cancelling else f'{spinner} {stage}',
             f'{elapsed:.1f}s elapsed', 'Esc / q / Ctrl-C: cancel']
    if done is not None and not cancelling:
        amount = f'{done:,}' + (f' / {total:,}' if total is not None else '')
        lines.insert(2, f'{amount} {unit}')
    screen.erase()
    for row, text in enumerate(lines[:height]):
        try:
            screen.addnstr(row, 0, text, max(0, width - 1))
        except curses.error:
            pass
    screen.refresh()


def wait_for_trace(screen, filename, prepare):
    """Draw before starting work and wake immediately when metadata is ready."""
    screen.keypad(True)
    screen.nodelay(True)
    job = StartupJob(prepare)
    try:
        draw_loading(screen, filename, job.snapshot(), 0)
        job.start()
        painted = 0
        while True:
            if job.cancel.is_set() and job.finished.is_set():
                raise OperationCancelled('Opening cancelled')
            try:
                key = screen.getch()
                if key in (27, ord('q'), ord('Q'), 3):
                    job.stop()
                    painted = 0
                if job.cancel.is_set():
                    if job.finished.is_set():
                        raise OperationCancelled('Opening cancelled')
                elif job.ready.is_set():
                    return job.take()
                now = time.monotonic()
                if now - painted >= 0.1 or key == curses.KEY_RESIZE:
                    draw_loading(screen, filename, job.snapshot(), now - job.started, job.cancel.is_set())
                    painted = now
                # A cache hit wakes this wait immediately, without a fixed UI delay.
                (job.finished if job.cancel.is_set() else job.ready).wait(0.02)
            except KeyboardInterrupt:
                job.stop()
                painted = 0
    finally:
        job.close()
        screen.nodelay(False)


def prepare_native(filename, *, storage='auto', memory_mib=256, use_cache=True,
                   cancel=None, stage_cb=None):
    """Prepare native VCD/FST input while preserving the selected storage mode."""
    from . import cache
    from .fst import FSTReader
    from .parsers import VCDFile
    from .storage import open_trace, use_disk_storage
    check_cancel(cancel)
    report_stage(stage_cb, 'Checking file')
    if use_disk_storage(filename, storage, cancel=cancel):
        return open_trace(filename, memory_mib=memory_mib, background=True,
                          use_cache=use_cache, cancel=cancel, stage_cb=stage_cb)
    report_stage(stage_cb, 'Reading saved waveform')
    trace = cache.load_cache(filename, cancel=cancel) if use_cache else None
    try:
        check_cancel(cancel)
        if trace is None:
            if FSTReader.is_fst(filename):
                trace = FSTReader(filename).parse(cancel=cancel, memory_mib=memory_mib, stage_cb=stage_cb)
            else:
                trace, _ = VCDFile.parse_header(filename, cancel=cancel, stage_cb=stage_cb)
                if os.path.getsize(filename) > 5 * 1024 * 1024:
                    trace.start_background_load(lazy_threshold=float('inf'), cancel=cancel, use_cache=use_cache)
                else:
                    report_stage(stage_cb, 'Reading values')
                    trace._read_values(trace._values_offset, cancel=cancel)
            check_cancel(cancel)
            if use_cache and not trace.is_loading:
                report_stage(stage_cb, 'Saving waveform cache')
                cache.save_cache(trace, filename, cancel=cancel)
        check_cancel(cancel)
        if not trace.signal_order:
            raise ValueError('No signals found in trace')
        return trace
    except BaseException:
        if trace is not None:
            trace.close()
        raise
