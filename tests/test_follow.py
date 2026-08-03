"""Tests for live 'follow' mode: incremental tailing of appended value changes
must match a full re-parse, and partial trailing lines must be deferred."""

HEAD = """$timescale 1ns $end
$scope module top $end
$var wire 1 ! clk $end
$var wire 8 " d $end
$upscope $end
$enddefinitions $end
$dumpvars
0!
b0 "
$end
#5
1!
#10
0!
b10101010 "
"""

MORE = """#15
1!
#20
0!
b11110000 "
#25
1!
"""


def _grid_digest(v):
    mt = max(v.max_time, 1)
    out = []
    for sid in sorted(v.signals):
        s = v.signals[sid]
        out.append("|".join(s.value_at(t) for t in range(0, mt + 2)))
    return "\n".join(out)


def test_tail_matches_full_reparse(w, tmp_path):
    from alwavelets.parsers import VCDFile
    p = tmp_path / "live.vcd"
    p.write_text(HEAD)
    vcd = VCDFile.parse(str(p))
    vcd.start_follow()
    assert vcd.tail() == 0                       # nothing appended yet
    base_max = vcd.max_time

    with open(p, "a") as f:                       # simulation appends more
        f.write(MORE)
    n = vcd.tail()
    assert n > 0
    assert vcd.max_time == 25 > base_max

    full = VCDFile.parse(str(p))                  # oracle: full parse of grown file
    assert _grid_digest(vcd) == _grid_digest(full)


def test_tail_defers_partial_line(w, tmp_path):
    from alwavelets.parsers import VCDFile
    p = tmp_path / "live2.vcd"
    p.write_text(HEAD)
    vcd = VCDFile.parse(str(p))
    vcd.start_follow()
    # append a complete timestamp + a *partial* value line (no newline yet)
    with open(p, "a") as f:
        f.write("#15\n1")
    vcd.tail()
    clk = next(s for s in vcd.signals.values() if s.name == "clk")
    assert clk.value_at(15) != "1"                # the partial '1!' line was deferred
    # now complete the line
    with open(p, "a") as f:
        f.write("!\n")
    vcd.tail()
    assert clk.value_at(15) == "1"                # picked up once the line is whole


def test_tail_appends_lock_free_order(w, tmp_path):
    # values are appended before times, so len(_values) >= len(_times) always
    from alwavelets.parsers import VCDFile
    p = tmp_path / "live3.vcd"
    p.write_text(HEAD)
    vcd = VCDFile.parse(str(p))
    vcd.start_follow()
    with open(p, "a") as f:
        f.write(MORE)
    vcd.tail()
    for s in vcd.signals.values():
        assert len(s._values) >= len(s._times)
