"""Static analyses over a parsed trace: formal counter-example assertion
detection and switching-activity (power) estimation."""
import bisect
import re


# ══════════════════════════════════════════════════════════════
#  FEATURE 2: FORMAL VERIFICATION COUNTEREXAMPLE
# ══════════════════════════════════════════════════════════════

class FormalAnalyzer:
    ASSERT_PATTERNS = [
        r'assert', r'assume', r'cover',
        r'__trigger__', r'__assume__',
        r'sva_', r'fail(ed)?', r'violation',
        r'property_fail', r'counterexample',
    ]

    def __init__(self, vcd):
        self.vcd = vcd
        self.assertion_signals = []
        self.failure_time = None
        self.root_cause_signal = None
        self.coi = []

    def detect_assertion_signals(self):
        combined = re.compile('|'.join(self.ASSERT_PATTERNS), re.I)
        found = []
        for sid, sig in self.vcd.signals.items():
            if sig.size == 1 and combined.search(sig.full_name):
                found.append((sid, sig))
        self.assertion_signals = found
        return found

    def find_failure_time(self):
        if not self.assertion_signals:
            self.detect_assertion_signals()
        earliest = None
        root_sig = None
        for sid, sig in self.assertion_signals:
            for i in range(len(sig._times)):
                t = sig._times[i]
                v = sig.value_str(i)
                if v in ('1', 'x', 'X'):
                    if i > 0 or v in ('x', 'X'):
                        if earliest is None or t < earliest:
                            earliest = t
                            root_sig = sig
                        break
        self.failure_time = earliest
        self.root_cause_signal = root_sig
        return earliest, root_sig

    def walk_cone_of_influence(self, start_sig, start_time, max_signals=30):
        if not start_sig:
            return []
        window = max(100, start_time // 10 if start_time else 100)
        for sid, sig in self.vcd.signals.items():
            if sig.size == 1 and any(p in sig.name.lower() for p in ('clk', 'clock')):
                if len(sig._times) >= 4:
                    periods = [sig._times[i+1] - sig._times[i] for i in range(3)]
                    window = int(sum(periods) / len(periods) * 8)
                break
        t_lo = max(0, start_time - window)
        t_hi = start_time
        contributors = []
        for sid, sig in self.vcd.signals.items():
            if sig.full_name == start_sig.full_name:
                continue
            times = sig._times
            lo = bisect.bisect_left(times, t_lo)
            hi = bisect.bisect_right(times, t_hi)
            n_trans = hi - lo
            last_val = sig.value_str(hi - 1) if n_trans else None
            last_change = times[hi - 1] if n_trans else 0
            if n_trans > 0:
                contributors.append({
                    'signal': sig, 'transitions': n_trans,
                    'final_value': last_val, 'first_change_time': last_change,
                })
        contributors.sort(key=lambda c: -c['first_change_time'])
        self.coi = contributors[:max_signals]
        return self.coi


# ══════════════════════════════════════════════════════════════
#  FEATURE 3: POWER ESTIMATION HEATMAP
# ══════════════════════════════════════════════════════════════

class PowerEstimator:
    def __init__(self, vcd):
        self.vcd = vcd
        self.scope_activity = {}
        self.signal_activity = {}

    def compute(self, t_start, t_end):
        self.scope_activity = {}
        self.signal_activity = {}
        span = max(1, t_end - t_start)
        for sid, sig in self.vcd.signals.items():
            n_trans = sig.transition_count_in_window(t_start, t_end)   # O(log n)
            activity = n_trans * sig.size / span * 1000
            self.signal_activity[sid] = activity
            if sig.scope:
                parts = sig.scope.split('.')
                for i in range(len(parts)):
                    scope_path = '.'.join(parts[:i+1])
                    self.scope_activity[scope_path] = \
                        self.scope_activity.get(scope_path, 0) + activity
        return self.scope_activity

    def hot_scopes(self, top_n=10):
        return sorted(self.scope_activity.items(), key=lambda x: -x[1])[:top_n]

    def hot_signals(self, top_n=20):
        sorted_ids = sorted(self.signal_activity.items(), key=lambda x: -x[1])[:top_n]
        return [(self.vcd.signals[sid], act) for sid, act in sorted_ids]


# ══════════════════════════════════════════════════════════════
#  GLITCH / X-HAZARD DETECTION
# ══════════════════════════════════════════════════════════════

def find_anomalies(signals, t_start, t_end, max_pulse=None, limit=10000):
    """Scan signals for waveform anomalies, returning sorted
    (time, full_name, kind, detail) tuples. Kinds:

      'x'      — a transition into an unknown (x) value (often a real bug)
      'glitch' — a zero-width pulse (multiple changes at one timestamp / a
                 delta-cycle glitch), or a runt pulse narrower than `max_pulse`.

    For single-bit signals `max_pulse` defaults per-signal to ~1/3 of the
    detected clock period, so normal clock cycles are never flagged — only
    pulses too short to be real. Pass an explicit `max_pulse` to override, or 0
    to detect only x-events and zero-width glitches. Heuristic by nature: a
    legitimately short strobe with a steady period may register as a glitch.
    """
    events = []
    for sig in signals:
        if sig is None:
            continue
        scalar = (sig.size == 1 and sig.signal_type != "real")
        thr = max_pulse
        if thr is None:
            period = sig.detect_frequency()[0] if scalar else None
            thr = period // 3 if period else 0
        times, values = sig.changes_in_window(t_start, t_end)
        prev_t = prev_v = None
        n_before = len(events)
        for i in range(len(times)):
            t = times[i]
            v = values[i]
            if t < t_start or t > t_end:
                prev_t, prev_v = t, v
                continue
            if prev_v is not None and "x" in v.lower() and "x" not in prev_v.lower():
                events.append((t, sig.full_name, "x", v))
            if prev_t is not None and t == prev_t:
                events.append((t, sig.full_name, "glitch", "zero-width"))
            elif scalar and thr and prev_t is not None and 0 < (t - prev_t) <= thr:
                events.append((prev_t, sig.full_name, "glitch", "%d-wide pulse" % (t - prev_t)))
            prev_t, prev_v = t, v
            if len(events) - n_before >= limit:
                break              # this signal alone already fills the cap
    # Every signal was scanned up to `limit` events, so the global first
    # `limit` by time is exact (an early global break favoured the first
    # signals in list order instead).
    events.sort(key=lambda e: e[0])
    return events[:limit]


# ══════════════════════════════════════════════════════════════
#  SIGNAL MEASUREMENT (';stats' / status-bar timing readout)
# ══════════════════════════════════════════════════════════════

def measure_signal(sig, t_start, t_end):
    """Timing/level statistics for one signal over [t_start, t_end].

    Returns a dict that always has 'transitions'; single-bit signals add
    'duty' (fraction of the range spent at 1), and 'period'/'cycles' when
    at least two rising edges land in range (period = median rise-to-rise
    delta, in timescale ticks); multi-bit/real signals add numeric
    'min'/'max' instead. Purely informational — never raises on x/z-soup.
    """
    out = {'transitions': sig.transition_count_in_window(t_start, t_end)}
    span = t_end - t_start
    if span <= 0:
        return out
    times, values = sig.changes_in_window(t_start, t_end)

    if getattr(sig, 'size', 1) == 1 and getattr(sig, 'signal_type', 'wire') != 'real':
        rises = []
        high_time = 0
        # Baseline = the value just BEFORE the window (the one-before entry
        # from changes_in_window), so a rising edge exactly at t_start counts.
        cur_t, cur_v = t_start, 'x'
        for t, v in zip(times, values):
            if t < t_start:
                cur_v = v                    # the one-before-window entry
                continue
            if t > t_end:
                break
            if cur_v == '1':
                high_time += t - cur_t
            if v == '1' and cur_v != '1':
                rises.append(t)
            cur_t, cur_v = t, v
        if cur_v == '1':
            high_time += t_end - cur_t
        out['duty'] = high_time / span
        if len(rises) >= 2:
            deltas = sorted(rises[i + 1] - rises[i] for i in range(len(rises) - 1))
            period = deltas[len(deltas) // 2]
            # A zero median period is real (two rising edges at one timestamp,
            # i.e. delta-cycle glitches) but meaningless as a period — and
            # callers derive a frequency from it. Report it as absent.
            if period > 0:
                out['period'] = period
                out['cycles'] = len(rises) - 1
    else:
        vals = []
        for v in values:
            try:
                vals.append(int(v, 2) if set(v) <= {'0', '1'} else float(v))
            except (ValueError, TypeError):
                pass
        if vals:
            out['min'], out['max'] = min(vals), max(vals)
    return out
