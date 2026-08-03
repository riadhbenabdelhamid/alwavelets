"""Tests for the persistent parse cache: round-trip fidelity, invalidation,
and the enable switch. The cache dir is redirected to a tmp path so tests never
touch the real ~/.cache."""

VCD = """$timescale 1ns $end
$scope module top $end
$var wire 1 ! clk $end
$var wire 8 " data $end
$var real 1 # volt $end
$upscope $end
$enddefinitions $end
$dumpvars
0!
b0 "
r0.0 #
$end
#5
1!
b10101010 "
#10
0!
r3.14 #
#15
1!
b11110000 "
"""


def _write(tmp, text=VCD):
    p = tmp / "t.vcd"
    p.write_text(text)
    return str(p)


def _digest(v):
    out = []
    mt = max(v.max_time, 1)
    for sid in sorted(v.signals):
        s = v.signals[sid]
        out.append(f"{s.full_name}|{s.size}|{s.signal_type}|{s._scalar}|{s.num_changes()}")
        out.append("|".join(s.value_at(t) for t in range(0, mt + 2)))
        _, cv = s.changes_in_window(0, mt)
        out.append(",".join(map(str, cv)))
    return "\n".join(out)


def test_cache_roundtrip_matches_parse(w, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    from alwavelets import cache
    from alwavelets.parsers import VCDFile
    src = _write(tmp_path)
    vcd = VCDFile.parse(src)
    assert cache.save_cache(vcd, src)
    loaded = cache.load_cache(src)
    assert loaded is not None
    assert _digest(loaded) == _digest(vcd)            # byte-for-byte fidelity
    assert loaded.timescale == vcd.timescale
    assert loaded.max_time == vcd.max_time
    assert loaded.signal_order == vcd.signal_order
    assert loaded.scopes is not None                  # scope tree rebuilt
    # scalar stays packed-scalar, bus stays packed-bus (both bytearrays)
    clk = next(s for s in loaded.signals.values() if s.name == "clk")
    data = next(s for s in loaded.signals.values() if s.name == "data")
    assert isinstance(clk._values, bytearray) and clk._scalar
    assert isinstance(data._values, bytearray) and data._packed and not data._scalar


def test_cache_invalidates_on_change(w, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    from alwavelets import cache
    from alwavelets.parsers import VCDFile
    src = _write(tmp_path)
    cache.save_cache(VCDFile.parse(src), src)
    assert cache.load_cache(src) is not None
    # changing the file (size + mtime) must invalidate
    with open(src, "a") as f:
        f.write("#20\n0!\n")
    assert cache.load_cache(src) is None


def test_cache_disabled_switch(w, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    from alwavelets import cache
    from alwavelets.parsers import VCDFile
    src = _write(tmp_path)
    cache.ENABLED = False
    try:
        assert cache.save_cache(VCDFile.parse(src), src) is False
        assert cache.load_cache(src) is None
    finally:
        cache.ENABLED = True


def test_cache_missing_returns_none(w, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    from alwavelets import cache
    assert cache.load_cache(str(tmp_path / "nope.vcd")) is None


def test_parse_file_uses_cache(w, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    from alwavelets import cache
    from alwavelets.parsers import parse_file
    src = _write(tmp_path)
    v1 = parse_file(src)                # parses + writes cache
    assert cache.load_cache(src) is not None
    v2 = parse_file(src)                # served from cache
    assert _digest(v1) == _digest(v2)
