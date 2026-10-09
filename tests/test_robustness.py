"""Robustness: malformed-input handling, a small VCD fuzzer, and lazy-chunk
parity with a full parse. The parser must never crash on garbage — it returns
whatever it could parse — and the lazy loader must agree with a full parse on
the values it loads."""
import random

BASE = """$date today $end
$version sim $end
$comment a comment $end
$timescale 1ns $end
$scope module top $end
$var wire 1 ! clk $end
$var reg 8 " data $end
$var real 1 # v $end
$scope module sub $end
$var wire 4 $ st $end
$upscope $end
$upscope $end
$enddefinitions $end
$dumpvars
0!
b0 "
r0.0 #
b0 $
$end
#5
1!
#10
0!
b10101010 "
r3.14 #
b1010 $
$dumpoff
$end
#15
$dumpon
1!
#20
b11111111 "
b1111 $
#25
0!
"""


def _parse(path, text):
    path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8", "replace"))
    from alwavelets.parsers import VCDFile
    return VCDFile.parse(str(path))


def _exercise(vcd):
    """Touch the public API the way the viewer/snapshots would — must not raise."""
    from alwavelets import rendering as r
    mt = max(vcd.max_time, 1)
    for sid in list(vcd.signals)[:50]:
        s = vcd.signals[sid]
        for t in (0, mt // 2, mt, mt + 5):
            s.value_at(t)
            s.numeric_value_at(t)
        s.changes_in_window(0, mt)
        if s.size == 1 and s.signal_type != "real":
            r.draw_single_bit_culled(s, 0, mt, 40)
            r.draw_single_bit_subcell(s, 0, mt, 40)
        else:
            r.draw_bus_culled(s, 0, mt, 40, None)


# ── hand-crafted malformed inputs ─────────────────────────────────────

def test_malformed_inputs_dont_crash(w, tmp_path):
    cases = [
        "",                                            # empty
        "garbage not a vcd at all\n\x00\x01\x02",      # binary-ish junk
        "$enddefinitions $end\n#10\n0!\n",             # values, no $var
        BASE[:len(BASE) // 2],                         # truncated mid-stream
        BASE.replace("$enddefinitions $end", ""),      # no end-of-defs
        BASE + "#3\n1!\n",                             # out-of-order timestamp
        BASE + "b101 \n0\nxyz\n#\n#abc\n",             # malformed change lines
        "$var wire 1 ! clk $end\n$enddefinitions $end\n#1\nz!\nx!\n",
        "$timescale 1ns $end\n$enddefinitions $end\n" + "#1\n0!\n" * 0,
    ]
    for i, text in enumerate(cases):
        p = tmp_path / f"m{i}.vcd"
        vcd = _parse(p, text)
        assert vcd is not None
        _exercise(vcd)


# ── fuzzer: random mutations of a valid VCD ───────────────────────────

def _mutate(rng, data):
    kind = rng.randrange(6)
    b = bytearray(data)
    if not b:
        return b"#1\n0!\n"
    if kind == 0:                                      # truncate
        return bytes(b[:rng.randrange(len(b) + 1)])
    if kind == 1:                                      # flip a byte
        i = rng.randrange(len(b)); b[i] = rng.randrange(256); return bytes(b)
    if kind == 2:                                      # insert random bytes
        i = rng.randrange(len(b) + 1)
        return bytes(b[:i] + bytes(rng.randrange(256) for _ in range(rng.randrange(8))) + b[i:])
    lines = data.decode("utf-8", "replace").splitlines(keepends=True)
    if not lines:
        return data
    if kind == 3:                                      # delete a line
        del lines[rng.randrange(len(lines))]
    elif kind == 4:                                    # duplicate a line
        i = rng.randrange(len(lines)); lines.insert(i, lines[i])
    else:                                              # shuffle a window
        i = rng.randrange(len(lines)); j = min(len(lines), i + rng.randrange(1, 6))
        win = lines[i:j]; rng.shuffle(win); lines[i:j] = win
    return "".join(lines).encode("utf-8", "replace")


def test_fuzz_parser_never_crashes(w, tmp_path):
    rng = random.Random(20260626)
    base = BASE.encode()
    p = tmp_path / "fuzz.vcd"
    for _ in range(400):
        data = base
        for _ in range(rng.randrange(1, 4)):           # stack a few mutations
            data = _mutate(rng, data)
        vcd = _parse(p, data)                           # must not raise
        assert vcd is not None
        _exercise(vcd)                                  # downstream must not raise


# ── lazy-chunk parity with a full parse ───────────────────────────────

def test_lazy_matches_full_parse(w, tmp_path):
    from alwavelets.parsers import VCDFile
    # a longer trace so lazy windows have content
    lines = ["$timescale 1ns $end", "$scope module top $end",
             "$var wire 1 ! clk $end", "$var wire 8 \" d $end",
             "$upscope $end", "$enddefinitions $end", "$dumpvars", "0!", "b0 \"", "$end"]
    for t in range(1, 400):
        lines.append(f"#{t}")
        lines.append("1!" if t % 2 else "0!")
        if t % 3 == 0:
            lines.append("b%s \"" % format(t & 0xFF, "b"))
    src = tmp_path / "big.vcd"
    src.write_text("\n".join(lines) + "\n")

    full = VCDFile.parse(str(src))
    lazy, off = VCDFile.parse_header(str(src))
    lazy._build_time_index(off)
    lazy.ensure_loaded(100, 200)

    for sid in full.signals:
        ft, fv = full.signals[sid].changes_in_window(100, 200)
        lt, lv = lazy.signals[sid].changes_in_window(100, 200)
        assert list(lt) == list(ft)  # missing events and baselines must fail
        assert lv == fv
        for t in range(100, 201):
            assert lazy.signals[sid].value_at(t) == full.signals[sid].value_at(t)
