"""Live reload for Alwavelets: watch the open VCD on disk and re-parse it
(debounced) when it changes, preserving the current view."""
import os
import time as _time

from .parsers import *  # noqa: F401,F403


class LiveReloadMixin:
    def toggle_live_reload(self):
        """Toggle watching the open file and auto-reloading when it changes."""
        if self.live_file:
            self.live_file = None
            self._live_pending = None
            self._show_message("Live reload: OFF")
            return
        path = getattr(self.vcd, '_filename', None)
        if not path or not os.path.isfile(path):
            self._show_message("Live reload: no file on disk to watch")
            return
        if not self._is_vcd_file(path):
            self._show_message("Live reload: only VCD files are supported")
            return
        self.live_file = path
        try:
            self.live_mtime = os.path.getmtime(path)
        except OSError:
            self.live_mtime = 0
        self._live_pending = None
        self._live_last_poll = 0.0
        self._show_message(f"Live reload: ON (watching {os.path.basename(path)})")

    def toggle_follow(self):
        """Toggle live 'follow': incrementally tail value changes appended to
        the open file and auto-scroll to the latest time. Cheaper than live
        reload (no full re-parse) — ideal for a simulation writing in real time."""
        if getattr(self, 'follow_on', False):
            self.follow_on = False
            self._show_message("Follow: OFF")
            return
        path = getattr(self.vcd, '_filename', None)
        if not path or not os.path.isfile(path) or not self._is_vcd_file(path):
            self._show_message("Follow: no VCD file on disk to follow")
            return
        if self.vcd.is_loading or getattr(self.vcd, '_lazy_mode', False):
            # tail() appends to the same arrays the background parser is
            # filling (or lazy mode merges into); two writers would break
            # the sorted-times contract every reader relies on.
            self._show_message("Follow: wait for the background load to finish"
                               if self.vcd.is_loading else
                               "Follow: not available in lazy (large-file) mode")
            return
        self.vcd.start_follow()
        self.follow_on = True
        self._follow_last_poll = 0.0
        self._show_message("Follow: ON (tailing live appends, auto-scroll to now)")

    def check_follow(self):
        """Poll the followed file; append new changes and scroll to now."""
        if not getattr(self, 'follow_on', False):
            return False
        now = _time.monotonic()
        if now - getattr(self, '_follow_last_poll', 0.0) < 0.2:
            return False
        self._follow_last_poll = now
        try:
            n = self.vcd.tail()
        except Exception:
            n = 0
        if not n:
            return False
        span = self.time_end - self.time_start
        if span <= 0:
            span = max(1, self.vcd.max_time)
        self.time_end = self.vcd.max_time
        self.time_start = max(0, self.time_end - span)
        self._needs_layout = True
        return True

    @staticmethod
    def _is_vcd_file(path):
        """True if the file begins with a VCD header byte ('$')."""
        try:
            with open(path, 'rb') as f:
                b = f.read(1)
            return bool(b) and b[0] == 0x24
        except OSError:
            return False

    def check_live_reload(self):
        """Poll the watched file; reload (debounced) if it changed on disk.

        Returns True if the view was reloaded. Polling is throttled and a
        reload only fires once the file's mtime+size are stable across two
        polls, so a trace being rewritten mid-simulation is never parsed
        half-written.
        """
        if not self.live_file:
            return False
        now = _time.monotonic()
        if now - self._live_last_poll < 0.25:
            return False
        self._live_last_poll = now
        try:
            st = os.stat(self.live_file)
        except OSError:
            return False
        if st.st_mtime <= self.live_mtime:
            return False
        sig = (st.st_mtime, st.st_size)
        if self._live_pending != sig:
            self._live_pending = sig          # changed; wait for it to settle
            return False
        self._live_pending = None
        self.live_mtime = st.st_mtime
        return self._reload_preserving_view()

    def _reload_preserving_view(self):
        """Re-parse the watched file, keeping the current view/cursor state."""
        try:
            new_vcd = VCDFile.parse(self.live_file)
        except Exception as e:
            self._show_message(f"Reload failed: {e}")
            return False
        if not new_vcd.signal_order:
            self._show_message("Reload skipped: file has no signals yet")
            return False

        # Snapshot view state, swap the model, then restore (clamped to the
        # possibly-changed time extent and signal count).
        ts, te = self.time_start, self.time_end
        ca, cb = self.cursor_a, self.cursor_b
        ac, con, cbon = self.active_cursor, self.cursor_on, self.cursor_b_on
        soff, fidx = self.sig_offset, self.focus_idx
        collapsed = set(self.collapsed_scopes)

        self.vcd = new_vcd
        # These signals are new objects with new values. The row cache keys on
        # (kind, sig_id, num_changes) within a window, and we restore the same
        # window below — a re-run with the same signal names and change counts
        # would otherwise redraw entirely from the previous run's cache.
        self._row_cache.clear()
        self._row_cache_window = None
        self._rebuild_display_order()
        self._rebuild_tree()

        mx = new_vcd.max_time if new_vcd.max_time > 0 else te
        self.time_start = max(0, min(ts, mx))
        self.time_end = min(te, mx)
        if self.time_end <= self.time_start:
            self.time_start, self.time_end = 0, mx
        self.cursor_a = min(ca, mx)
        self.cursor_b = min(cb, mx)
        self.active_cursor, self.cursor_on, self.cursor_b_on = ac, con, cbon
        self.collapsed_scopes = collapsed

        n = len(self.display_order)
        self.sig_offset = min(soff, max(0, n - 1))
        self.focus_idx = min(fidx, max(0, n - 1 - self.sig_offset))
        self._needs_layout = True
        self._show_message(
            f"Reloaded {os.path.basename(self.live_file)} "
            f"({len(new_vcd.signal_order)} signals, max t={new_vcd.max_time:,})")
        return True

    # ── Zoom History ──
