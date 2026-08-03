"""Tests for measure_signal (the ';stats' / status-bar measurement core)."""


def _clk(w, period=10, n=40, phase_high=5):
    """Clock: rises at k*period, falls at k*period+phase_high."""
    s = w.VCDSignal("clk", "!", 1)
    for k in range(n):
        s.add_change(k * period, "1")
        s.add_change(k * period + phase_high, "0")
    return s


def test_clock_period_freq_cycles(w):
    from alwavelets.analysis import measure_signal
    s = _clk(w, period=10, n=40)
    m = measure_signal(s, 0, 400)
    assert m['period'] == 10
    assert m['cycles'] == 39
    assert m['transitions'] == 80


def test_duty_cycle(w):
    from alwavelets.analysis import measure_signal
    m = measure_signal(_clk(w, period=10, phase_high=5, n=40), 0, 400)
    assert abs(m['duty'] - 0.5) < 0.02
    m = measure_signal(_clk(w, period=10, phase_high=2, n=40), 0, 400)
    assert abs(m['duty'] - 0.2) < 0.02


def test_windowed_measurement(w):
    from alwavelets.analysis import measure_signal
    s = _clk(w, period=10, n=40)
    m = measure_signal(s, 100, 200)          # 10 periods inside the window
    assert m['period'] == 10 and m['cycles'] == 10


def test_bus_min_max(w):
    from alwavelets.analysis import measure_signal
    s = w.VCDSignal("data", "!", 8)
    for t, v in [(0, "00000101"), (10, "11111111"), (20, "00000000"), (30, "xxxxxxxx")]:
        s.add_change(t, v)
    m = measure_signal(s, 0, 40)
    assert m['min'] == 0 and m['max'] == 255  # x value skipped, not crashed
    assert m['transitions'] == 4
    assert 'duty' not in m


def test_constant_signal_no_period(w):
    from alwavelets.analysis import measure_signal
    s = w.VCDSignal("rst", "!", 1)
    s.add_change(0, "0")
    m = measure_signal(s, 0, 100)
    assert 'period' not in m and m['duty'] == 0.0


def test_empty_range(w):
    from alwavelets.analysis import measure_signal
    s = _clk(w)
    assert measure_signal(s, 50, 50) == {'transitions': 1}  # zero-width range
