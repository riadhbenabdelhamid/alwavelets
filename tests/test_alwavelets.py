"""Regression tests for Alwavelets's pure-Python parsing/decoding layer.

These run without curses and establish a safety net around the parsers,
decoders, formatters, and the lazy-load machinery.
"""
import os
import threading

import pytest

from conftest import make_vcd

DEMO = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "demo_waveform.vcd")


# ── Basic VCD parsing ────────────────────────────────────────────────

def test_parse_demo(w):
    vcd = w.VCDFile.parse(DEMO)
    assert len(vcd.signal_order) == 19
    assert vcd.max_time == 2200
    assert vcd.timescale_unit == "ns"
    # every signal's timestamps are sorted (bisect invariant)
    for sid in vcd.signal_order:
        t = list(vcd.signals[sid]._times)
        assert t == sorted(t)


def test_signal_readers(w):
    vcd = w.VCDFile.parse(DEMO)
    clk = next(vcd.signals[s] for s in vcd.signal_order
               if vcd.signals[s].name == "clk")
    assert clk.value_at(0) in ("0", "1")
    # next/prev edge are consistent
    e = clk.next_edge_after(0)
    assert e is not None and e > 0
    assert clk.prev_edge_before(e + 1) == e
    # changes_in_window returns parallel, equal-length arrays
    ts, vs = clk.changes_in_window(0, vcd.max_time)
    assert len(ts) == len(vs)


# ── Parser edge cases ────────────────────────────────────────────────

_EDGE_VCD = """$comment hello $end
$timescale 10ps $end
$scope module top $end
$var wire 1 ! clk $end
$scope module sub $end
$var wire 8 " bus $end
$var real 64 # temp $end
$upscope $end
$upscope $end
$enddefinitions $end
$dumpvars
0!
b00000001 "
r3.14 #
$end
#10
1!
b00000010 "
#20
$dumpoff
$end
#30
$dumpon
0!
$end
"""


def test_parse_edge_cases(w, tmp_path):
    p = tmp_path / "edge.vcd"
    p.write_text(_EDGE_VCD)
    vcd = w.VCDFile.parse(str(p))

    # timescale with a multiplier + nested scopes
    assert (vcd.timescale_number, vcd.timescale_unit) == (10, "ps")
    by_name = {vcd.signals[s].name: vcd.signals[s] for s in vcd.signal_order}
    assert by_name["clk"].full_name == "top.clk"
    assert by_name["bus"].full_name == "top.sub.bus"
    assert by_name["temp"].full_name == "top.sub.temp"
    assert by_name["temp"].signal_type == "real" and by_name["temp"].size == 64

    # values track over time
    assert by_name["clk"].value_at(5) == "0" and by_name["clk"].value_at(15) == "1"
    assert by_name["bus"].value_at(15) == "00000010"
    assert by_name["temp"].numeric_value_at(5) == 3.14

    # $dumpoff sets signals to x; $dumpon restores them
    assert by_name["clk"].value_at(25) == "x"
    assert by_name["clk"].value_at(30) == "0"
    assert vcd.max_time == 30


def test_time_to_str(w):
    vcd = w.VCDFile.parse(DEMO)            # timescale 1 ns
    assert vcd.time_to_str(0) == "0ns"
    assert vcd.time_to_str(1000) == "1us"
    assert vcd.time_to_str(1) == "1ns"


def test_manual_builds(w):
    # Instantiating builds every page; guards the manual.py extraction against
    # missed references and exercises page navigation.
    man = w.AlwaveletsManual()
    assert len(man.pages) > 0
    man.next_page()
    man.prev_page()
    title, lines = man.current()
    assert isinstance(title, str) and isinstance(lines, list)


# ── Value formatting ─────────────────────────────────────────────────

@pytest.mark.parametrize("bits,radix,size,expected", [
    ("1010", "BIN", 4, "1010"),
    ("1010", "HEX", 4, "A"),
    ("1010", "DEC", 4, "-6"),       # signed 4-bit two's complement
    ("1010", "UNSIGNED", 4, "10"),
    ("11111111", "DEC", 8, "-1"),
    ("x", "HEX", 4, "X"),
    ("zzzz", "HEX", 4, "ZZZZ"),
])
def test_format_value(w, bits, radix, size, expected):
    assert w.format_value(bits, w.Radix[radix], size) == expected


# ── RISC-V decoder ───────────────────────────────────────────────────

@pytest.mark.parametrize("code,mnemonic", [
    (0x00100093, "addi"),   # addi x1, x0, 1
    (0x00000013, "addi"),   # nop
    (0x00008067, "jalr"),   # ret
])
def test_rv32_decode(w, code, mnemonic):
    assert w.RV32Decoder().decode(code)["mnemonic"] == mnemonic


# ── Frequency detection ──────────────────────────────────────────────

def test_detect_frequency(w):
    vcd = w.VCDFile.parse(DEMO)
    clk = next(vcd.signals[s] for s in vcd.signal_order
               if vcd.signals[s].name == "clk")
    period, _ = clk.detect_frequency()
    assert period and period > 0


# ── FST detection ────────────────────────────────────────────────────

def test_fst_detection(w):
    assert w.FSTReader.is_fst(DEMO) is False


# ── Export escaping ──────────────────────────────────────────────────

def test_xml_escape(w):
    assert w.xml_escape('a<b>&"c') == 'a&lt;b&gt;&amp;&quot;c'
    # result must round-trip through an XML parser
    import xml.etree.ElementTree as ET
    raw = 'sig<=>&"x'
    el = ET.fromstring("<t>" + w.xml_escape(raw) + "</t>")
    assert el.text == 'sig<=>&"x'


def test_pdf_escape(w):
    assert w.pdf_escape(r"a(b)c\d") == r"a\(b\)c\\d"


# ── Restricted trace query (no arbitrary code execution) ─────────────

def test_query_str_blocks_builtins(w):
    tr = w.InstructionTrace()
    _, err = tr.query_str("__import__('os')")
    assert err is not None  # name not available in the restricted namespace
    # a normal lambda still works
    tr.entries = [{"pc": 4}, {"pc": 8}]
    matches, err = tr.query_str("lambda e: e['pc'] > 5")
    assert err is None and matches == [{"pc": 8}]


# ── Lazy-load correctness (regression for the sorted-time bug) ───────

def _changes_in(sig, a, b):
    # sig.changes decodes packed storage (scalar bytes / packed bus bytearray)
    return [(t, v) for t, v in sig.changes if a <= t <= b]


def test_lazy_load_out_of_order(w, tmp_path):
    """Loading a late window then an early one must keep _times sorted and
    must contain exactly the ground-truth changes for each loaded window."""
    path = str(tmp_path / "big.vcd")
    make_vcd(path, n_steps=400)

    full = w.VCDFile.parse(path)             # ground truth

    vcd, off = w.VCDFile.parse_header(path)
    vcd._build_time_index(off)
    assert vcd._lazy_mode and vcd._time_index

    tmax = vcd.max_time
    late = (int(tmax * 0.70), int(tmax * 0.90))
    early = (0, int(tmax * 0.20))

    # Deliberately load out of chronological order — this is the bug trigger.
    vcd.ensure_loaded(*late)
    vcd.ensure_loaded(*early)

    for sid in vcd.signal_order:
        sig = vcd.signals[sid]
        times = list(sig._times)
        assert times == sorted(times), f"{sig.full_name}: _times not sorted"
        ref = full.signals[sid]
        for window in (early, late):
            assert _changes_in(sig, *window) == _changes_in(ref, *window), \
                f"{sig.full_name}: wrong changes in {window}"


def test_lazy_load_overlap_no_duplicates(w, tmp_path):
    """Overlapping ensure_loaded calls must not create duplicate timestamps."""
    path = str(tmp_path / "ov.vcd")
    make_vcd(path, n_steps=300)
    vcd, off = w.VCDFile.parse_header(path)
    vcd._build_time_index(off)
    tmax = vcd.max_time
    vcd.ensure_loaded(0, int(tmax * 0.5))
    vcd.ensure_loaded(int(tmax * 0.3), int(tmax * 0.8))  # overlaps previous
    for sid in vcd.signal_order:
        t = list(vcd.signals[sid]._times)
        assert t == sorted(t)
        assert len(t) == len(set(t)), "duplicate timestamps after overlap"


# ── Keymap dispatch (testability for the refactored input layer) ─────

def test_keymap_wiring(w):
    import curses
    from unittest.mock import MagicMock
    stub = MagicMock()
    km = w.Alwavelets._build_keymap(stub)
    # representative keys present and wired to the expected handler
    assert km[ord('q')] is stub._kb_quit
    assert km[27] is stub._kb_quit
    assert km[curses.KEY_F5] is stub.toggle_live_reload
    assert km[ord('X')] is stub.export_svg
    assert km[ord('/')] is stub.open_finder
    # aliased keycodes resolve to the same handler
    assert km[curses.KEY_RIGHT] is km[ord('l')]
    assert km[ord('+')] is km[ord('=')]
    # every entry is callable
    assert all(callable(h) for h in km.values())


def test_kb_quit_signals_quit(w):
    from unittest.mock import MagicMock
    assert w.Alwavelets._kb_quit(MagicMock()) == 'QUIT'


def test_fmt_num(w):
    # Compact numeric formatting used by analog value/scale labels.
    assert w.Alwavelets._fmt_num(255.0) == "255"
    assert w.Alwavelets._fmt_num(0.0) == "0"
    assert w.Alwavelets._fmt_num(3.14) == "3.14"
    assert w.Alwavelets._fmt_num(-42.0) == "-42"


def test_panel_tree_passthrough(w):
    """The tree consumes its own nav keys and passes everything else through
    to the global keymap (no whitelist needed)."""
    import curses
    from unittest.mock import MagicMock
    stub = MagicMock()
    stub._tree_flat = [('scope', MagicMock(), 0, 0, 0)]
    stub.tree_cursor = 0
    handle = w.Alwavelets.panel_handle_tree_key
    assert handle(stub, curses.KEY_DOWN) is True      # tree nav: consumed
    assert handle(stub, ord('q')) is False            # global key: falls through
    assert handle(stub, curses.KEY_F5) is False        # global key: falls through


# ── Live reload (F5) ─────────────────────────────────────────────────

def _make_reload_stub(w, path):
    """A minimal object carrying only the state the reload methods touch,
    so we can exercise them without constructing a curses Alwavelets."""
    class Stub:
        pass
    v = Stub()
    v.vcd = w.VCDFile.parse(path)
    v.live_file = path
    v.live_mtime = os.path.getmtime(path)
    v._live_pending = None
    v._live_last_poll = 0.0
    v.time_start, v.time_end = 0, v.vcd.max_time
    v.cursor_a, v.cursor_b = 0, max(1, v.vcd.max_time // 2)
    v.active_cursor, v.cursor_on, v.cursor_b_on = "a", True, False
    v.sig_offset, v.focus_idx = 0, 0
    v.collapsed_scopes = {"top"}
    v.display_order = list(v.vcd.signal_order)
    v._needs_layout = False
    v._row_cache = {}                 # reload must flush the render cache
    v._row_cache_window = None
    v.messages = []
    v._show_message = v.messages.append
    v._rebuild_display_order = lambda: setattr(
        v, "display_order", list(v.vcd.signal_order))
    v._rebuild_tree = lambda: None
    v._reload_preserving_view = w.Alwavelets._reload_preserving_view.__get__(v)
    return v


def test_is_vcd_file(w, tmp_path):
    vcd = make_vcd(str(tmp_path / "a.vcd"))
    assert w.Alwavelets._is_vcd_file(vcd) is True
    other = tmp_path / "b.bin"
    other.write_bytes(b"\x00not a vcd")
    assert w.Alwavelets._is_vcd_file(str(other)) is False
    assert w.Alwavelets._is_vcd_file(str(tmp_path / "missing")) is False


def test_live_reload_debounce_and_state(w, tmp_path):
    path = str(tmp_path / "live.vcd")
    make_vcd(path, n_steps=50)
    v = _make_reload_stub(w, path)
    check = w.Alwavelets.check_live_reload.__get__(v)

    old_max = v.vcd.max_time
    assert check() is False                      # nothing changed yet

    # Rewrite the trace with more steps (new, larger extent) and force a
    # definitively newer mtime to avoid filesystem timestamp granularity.
    make_vcd(path, n_steps=160)
    os.utime(path, (v.live_mtime + 10, v.live_mtime + 10))

    v._live_last_poll = 0.0
    assert check() is False                       # first sighting -> debounced
    assert v._live_pending is not None

    v._live_last_poll = 0.0
    assert check() is True                         # stable -> reloaded
    assert v.vcd.max_time > old_max                # new content loaded
    # view state preserved / clamped
    assert v.collapsed_scopes == {"top"}
    assert v.time_start == 0 and v.time_end <= v.vcd.max_time
    assert 0 <= v.focus_idx < len(v.display_order)
    assert v._needs_layout is True


def test_live_reload_skips_empty_file(w, tmp_path):
    """A reload that parses to zero signals must not clobber the current view."""
    path = str(tmp_path / "live2.vcd")
    make_vcd(path, n_steps=40)
    v = _make_reload_stub(w, path)
    good_vcd = v.vcd
    # Truncate to a header with no signals/changes.
    with open(path, "w") as f:
        f.write("$timescale 1ns $end\n$enddefinitions $end\n")
    os.utime(path, (v.live_mtime + 10, v.live_mtime + 10))
    reload = w.Alwavelets._reload_preserving_view.__get__(v)
    assert reload() is False
    assert v.vcd is good_vcd                       # unchanged
    assert any("no signals" in m for m in v.messages)


# ── Thread-safety smoke test (regression for lock-free reads) ────────

def test_concurrent_reads_during_append(w):
    """A reader must never raise while a writer appends concurrently.

    Queries target the frontier (just-appended end of the arrays), where a
    naive `_times`-before-`_values` append would let a reader index past the
    end of `_values`.
    """
    sig = w.VCDSignal("s", "!", size=1)
    stop = threading.Event()
    errors = []

    def writer():
        t = 0
        while not stop.is_set():
            sig.add_change(t, "1" if t & 1 else "0")
            t += 1

    def reader():
        try:
            for _ in range(100000):
                n = sig.num_changes()
                if n:
                    frontier = n  # one tick past the last known timestamp
                    _ = sig.value_at(frontier)
                    ts, vs = sig.changes_in_window(frontier - 4, frontier)
                    assert len(ts) == len(vs)
        except Exception as exc:           # pragma: no cover - failure path
            errors.append(exc)

    wt = threading.Thread(target=writer)
    wt.start()
    reader()
    stop.set()
    wt.join()
    assert not errors, f"reader raised under concurrency: {errors[:3]}"
