"""Tests for parallel VCD parsing: parity with the sequential parser, split-point
alignment, and opt-in gating. Skipped where os.fork is unavailable."""
import os
import pytest

pytestmark = pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")


def _make_vcd(tmp_path, n_ts=2000, n_sig=20):
    import random
    rng = random.Random(3)
    lines = ["$timescale 1ns $end", "$scope module top $end"]

    def vid(i):
        s, i = "", i + 1
        while i > 0:
            s += chr(33 + (i % 90)); i //= 90
        return s
    ids = [(vid(i), 1 if i % 3 else 8) for i in range(n_sig)]
    for i, (v, sz) in enumerate(ids):
        lines.append(f"$var wire {sz} {v} s{i} $end")
    lines += ["$upscope $end", "$enddefinitions $end", "$dumpvars"]
    for v, sz in ids:
        lines.append(("0 " + v) if sz == 1 else ("b0 " + v))
    lines.append("$end")
    for t in range(1, n_ts):
        lines.append(f"#{t}")
        for v, sz in ids:
            if rng.random() < 0.5:
                lines.append((("1" if rng.random() < 0.5 else "0") + v) if sz == 1
                             else ("b" + format(rng.getrandbits(8), "b") + " " + v))
    p = tmp_path / "p.vcd"
    p.write_text("\n".join(lines) + "\n")
    return str(p)


def _digest(v):
    mt = max(v.max_time, 1)
    ts = [mt * k // 50 for k in range(51)]
    out = []
    for sid in sorted(v.signals):
        s = v.signals[sid]
        out.append(f"{s.full_name}|{s.size}|{s.num_changes()}")
        out.append("|".join(s.value_at(t) for t in ts))
        _, cv = s.changes_in_window(0, mt)
        out.append(",".join(map(str, cv)))
    return "\n".join(out)


def test_parallel_matches_sequential(w, tmp_path):
    from alwavelets.parsers import VCDFile
    from alwavelets import parallel
    src = _make_vcd(tmp_path)
    par = parallel.parse_parallel_file(src, jobs=4)
    assert par is not None                       # splittable into >=2 chunks
    seq = VCDFile.parse(src)
    assert par.max_time == seq.max_time
    assert _digest(par) == _digest(seq)          # byte-for-byte parity


def test_split_points_align_to_timestamps(w, tmp_path):
    from alwavelets import parallel
    src = _make_vcd(tmp_path)
    size = os.path.getsize(src)
    # find the value-change offset (after $enddefinitions line)
    with open(src, "rb") as f:
        data = f.read()
    offset = data.index(b"$enddefinitions") and data.index(b"\n", data.index(b"$enddefinitions")) + 1
    ranges = parallel._split_points(src, offset, size, 4)
    assert len(ranges) >= 2
    with open(src, "rb") as f:
        for i, (a, b) in enumerate(ranges):
            assert a < b
            if i > 0:                            # every non-first chunk starts on a '#'
                f.seek(a)
                assert f.read(1) == b"#"
        # ranges are contiguous and cover to EOF
        assert ranges[0][0] == offset and ranges[-1][1] == size
        assert all(ranges[i][1] == ranges[i + 1][0] for i in range(len(ranges) - 1))


def test_viable_is_opt_in(w, tmp_path):
    from alwavelets import parallel
    src = _make_vcd(tmp_path, n_ts=50, n_sig=4)
    assert parallel.viable(src, jobs=None) is False       # default off
    assert parallel.viable(src, jobs=1) is False          # 1 = sequential
    assert parallel.viable(src, jobs=8) is False          # below 16MB default floor
    import multiprocessing as mp
    if (mp.cpu_count() or 1) > 1:                          # forced on, fork+multicore
        assert parallel.viable(src, jobs=8, min_size=0) is True
