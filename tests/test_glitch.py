"""Tests for glitch / x-hazard anomaly detection."""

VCD = """$timescale 1ns $end
$scope module top $end
$var wire 1 ! a $end
$var wire 1 " b $end
$upscope $end
$enddefinitions $end
$dumpvars
0!
0"
$end
#10
1!
#11
0!
#20
x"
#30
1!
#40
1!
0!
1!
#50
0!
"""


def _parse(tmp_path):
    from alwavelets.parsers import VCDFile
    p = tmp_path / "g.vcd"
    p.write_text(VCD)
    return VCDFile.parse(str(p))


def test_detects_x_event(w, tmp_path):
    from alwavelets.analysis import find_anomalies
    vcd = _parse(tmp_path)
    sigs = list(vcd.signals.values())
    evs = find_anomalies(sigs, 0, vcd.max_time, max_pulse=2)
    x_events = [e for e in evs if e[2] == "x"]
    assert any(e[0] == 20 and e[1].endswith("b") for e in x_events)


def test_detects_short_pulse(w, tmp_path):
    from alwavelets.analysis import find_anomalies
    vcd = _parse(tmp_path)
    sigs = list(vcd.signals.values())
    evs = find_anomalies(sigs, 0, vcd.max_time, max_pulse=2)
    # a is high only on [10, 11] -> a 1-wide runt pulse flagged at t=10
    assert any(e[0] == 10 and e[2] == "glitch" and e[1].endswith("a") for e in evs)


def test_detects_zero_width(w, tmp_path):
    from alwavelets.analysis import find_anomalies
    vcd = _parse(tmp_path)
    sigs = list(vcd.signals.values())
    evs = find_anomalies(sigs, 0, vcd.max_time, max_pulse=2)
    # a toggles 1->0->1 within timestamp #40 -> zero-width glitch(es)
    zw = [e for e in evs if e[0] == 40 and e[2] == "glitch" and e[3] == "zero-width"]
    assert len(zw) >= 1


def test_no_max_pulse_only_x_and_zero_width(w, tmp_path):
    from alwavelets.analysis import find_anomalies
    vcd = _parse(tmp_path)
    sigs = list(vcd.signals.values())
    evs = find_anomalies(sigs, 0, vcd.max_time, max_pulse=0)
    # with max_pulse=0 the 1-wide pulse at t=10 is NOT flagged...
    assert not any(e[0] == 10 for e in evs)
    # ...but x-events and zero-width glitches still are
    assert any(e[2] == "x" for e in evs) and any(e[3] == "zero-width" for e in evs)


def test_results_sorted_by_time(w, tmp_path):
    from alwavelets.analysis import find_anomalies
    vcd = _parse(tmp_path)
    evs = find_anomalies(list(vcd.signals.values()), 0, vcd.max_time, max_pulse=2)
    assert evs == sorted(evs, key=lambda e: e[0])
