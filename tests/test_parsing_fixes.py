"""Parser regressions: 'b'-form values on 1-bit signals, aliased identifier
codes (one net under several names), DEC sign on short values and real
formatting, follow mode past 2^31 ticks, the thinned lazy time index, and
converter failures raising instead of exiting."""
import os

import pytest

HEADER = ("$timescale 1ns $end\n$scope module top $end\n"
          "$var wire 1 ! clk $end\n$var wire 1 \" en [0] $end\n"
          "$var wire 8 # data [7:0] $end\n$upscope $end\n"
          "$scope module sub $end\n$var wire 1 ! clk_alias $end\n$upscope $end\n"
          "$enddefinitions $end\n")
BODY = "#0\n0!\nb0 \"\nb0 #\n#5\n1!\nb1 \"\nb1 #\n#10\n0!\nbx \"\nb10000000 #\n"



# ── parser: data that used to be dropped ─────────────────────────────

def _check_bform_and_alias(vcd):
    en, data, alias = vcd.signals['"'], vcd.signals['#'], None
    for k, s in vcd.signals.items():
        if s.full_name == "sub.clk_alias":
            alias = s
    assert en.changes == [(0, '0'), (5, '1'), (10, 'x')]       # 'b1 "' on a 1-bit var
    assert data.changes[-1] == (10, '10000000')
    assert alias is not None and alias.num_changes() == 3        # reused id code
    assert alias._times is vcd.signals['!']._times               # shares storage


def test_sync_parse_bform_scalar_and_alias(w, tmp_path):
    p = tmp_path / "t.vcd"
    p.write_text(HEADER + BODY)
    _check_bform_and_alias(w.VCDFile.parse(str(p)))


def test_background_parse_bform_scalar_and_alias(w, tmp_path):
    p = tmp_path / "t.vcd"
    p.write_text(HEADER + BODY)
    vcd, off = w.VCDFile.parse_header(str(p))
    vcd._parse_values_background(off)
    assert vcd._load_error is None
    _check_bform_and_alias(vcd)


def test_lazy_parse_bform_scalar(w, tmp_path):
    p = tmp_path / "t.vcd"
    p.write_text(HEADER + BODY)
    vcd, off = w.VCDFile.parse_header(str(p))
    vcd._build_time_index(off)
    vcd.ensure_loaded(0, 10)
    assert vcd.signals['"'].changes == [(0, '0'), (5, '1'), (10, 'x')]


def test_parallel_worker_bform_scalar(w, tmp_path):
    from alwavelets import parallel
    p = tmp_path / "t.vcd"
    p.write_text(HEADER + BODY)
    vcd, off = w.VCDFile.parse_header(str(p))
    meta = [(k.encode(), s.size == 1 and s.signal_type != 'real')
            for k, s in vcd.signals.items() if s.owns_storage]
    _, out = parallel._worker((str(p), off, os.path.getsize(str(p)), meta))
    assert out['"'][1] == b'011x'[:0] + bytes([48, 49, 120])


def test_alias_is_skipped_by_storage_loops(w, tmp_path):
    """$dumpoff appends 'x' once per net, not once per name of it."""
    p = tmp_path / "t.vcd"
    p.write_text(HEADER + BODY + "#20\n$dumpoff $end\n")
    vcd = w.VCDFile.parse(str(p))
    assert vcd.signals['!'].changes[-1] == (20, 'x')
    assert vcd.signals['!'].num_changes() == 4


def test_cache_round_trips_alias(w, tmp_path, monkeypatch):
    from alwavelets import cache
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    p = tmp_path / "t.vcd"
    p.write_text(HEADER + BODY)
    assert cache.save_cache(w.VCDFile.parse(str(p)), str(p))
    back = cache.load_cache(str(p))
    assert back is not None
    _check_bform_and_alias(back)
    alias = next(s for s in back.signals.values() if s.full_name == "sub.clk_alias")
    assert isinstance(alias, w.AliasSignal)


# ── core formatting ──────────────────────────────────────────────────

def test_dec_sign_uses_true_msb(w):
    assert w.format_value('1', w.Radix.DEC, 8) == '1'            # short string: +1
    assert w.format_value('10000000', w.Radix.DEC, 8) == '-128'
    assert w.format_value('01111111', w.Radix.DEC, 8) == '127'


def test_real_formatting_is_not_truncated(w):
    assert w.format_value('1.2345e-05', w.Radix.DEC, 64) == '1.2345e-05'
    assert w.format_value('123456.789', w.Radix.HEX, 64) == '123457'
    assert w.format_real('abc') == 'abc'


# ── follow mode past 2^31 ────────────────────────────────────────────

def test_follow_widens_32bit_timestamps(w, tmp_path):
    p = tmp_path / "f.vcd"
    p.write_text("$timescale 1ps $end\n$scope module t $end\n$var wire 1 ! clk $end\n"
                 "$upscope $end\n$enddefinitions $end\n#0\n0!\n#10\n1!\n")
    vcd = w.VCDFile.parse(str(p))
    s = vcd.signals['!']
    assert s._times.typecode == 'i'                    # compacted after parse
    vcd.start_follow()
    with open(p, "a") as f:
        f.write("#20\n0!\n#3000000000\n1!\n#3000000010\n0!\n")
    assert vcd.tail() == 3
    assert s._times.typecode == 'q'
    assert list(s._times) == [0, 10, 20, 3000000000, 3000000010]
    assert len(s._values) == len(s._times)
    assert vcd.max_time == 3000000010


def test_follow_resyncs_after_bad_append(w, tmp_path, monkeypatch):
    p = tmp_path / "f.vcd"
    p.write_text("$timescale 1ns $end\n$scope module t $end\n$var wire 1 ! clk $end\n"
                 "$var wire 4 \" b $end\n$upscope $end\n$enddefinitions $end\n#0\n0!\nb0 \"\n")
    vcd = w.VCDFile.parse(str(p))
    vcd.start_follow()
    bus = vcd.signals['"']
    # Make the timestamp append fail after the value went in.
    import array

    class Boom(array.array):
        def append(self, v):
            raise OverflowError("boom")
    bus._times = Boom('q', bus._times)
    with open(p, "a") as f:
        f.write("#5\nb1 \"\n")
    vcd.tail()
    assert len(bus._values) == len(bus._times) * bus._stride


# ── lazy index ───────────────────────────────────────────────────────

def test_lazy_time_index_is_thinned_and_bisected(w, tmp_path):
    from conftest import make_vcd
    p = str(tmp_path / "big.vcd")
    make_vcd(p, n_steps=2000)
    vcd, off = w.VCDFile.parse_header(p)
    vcd._build_time_index(off)
    n_hash = sum(1 for l in open(p, "rb") if l.startswith(b"#"))
    assert 1 <= len(vcd._time_index) < n_hash
    size = os.path.getsize(p)
    assert len(vcd._time_index) <= size // vcd._INDEX_STRIDE + 1
    # a window past every indexed time still starts at the last entry
    s_off, e_off = vcd._chunk_offsets(vcd.max_time * 10, vcd.max_time * 11)
    assert s_off == vcd._time_index[-1][1] and e_off == size
    vcd.ensure_loaded(0, vcd.max_time)
    full = w.VCDFile.parse(p)
    for sid in full.signal_order:
        assert vcd.signals[sid].changes == full.signals[sid].changes


# ── converters raise, never exit ─────────────────────────────────────

def test_missing_converter_raises(w, tmp_path, monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: None)
    p = tmp_path / "t.lxt"
    p.write_bytes(b"\x01\x02junk")
    with pytest.raises(w.ConverterError):
        w.parse_file(str(p), use_cache=False)
