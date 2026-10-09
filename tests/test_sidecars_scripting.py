"""Sidecar persistence (atomic writes, shared-sidecar path policy), SVG
snapshot clamping and theme-JSON robustness, TCL foreach, anomaly limits,
lazy expression candidates, and .gtkw line filtering."""
import json
import os


def _sig(w, name, size, changes, stype="wire", scope="top"):
    s = w.VCDSignal(name, name, size, stype, scope)
    for t, v in changes:
        s.add_change(t, v)
    return s


class _VCD:
    """Duck-typed trace for the decoders: signals by full name + max_time."""
    timescale, timescale_number, timescale_unit = "1ns", 1, "ns"

    def __init__(self, *sigs):
        self.signals = {s.full_name: s for s in sigs}
        self.signal_order = list(self.signals)
        self.max_time = max((s._times[-1] for s in sigs if len(s._times)), default=0)

    def time_to_str(self, t):
        return "%dns" % t




# ── persistence ──────────────────────────────────────────────────────

def test_sidecar_save_is_atomic(w, tmp_path, monkeypatch):
    store = w.AnnotationStore()
    store.add(w.WaveformAnnotation(0, 10, None, "keep me"))
    p = str(tmp_path / "n.json")
    assert store.save(p)
    before = open(p).read()
    import alwavelets.persistence as pers
    monkeypatch.setattr(pers.json, "dumps", lambda *a, **k: (_ for _ in ()).throw(TypeError("x")))
    assert store.save(p) is False
    assert open(p).read() == before
    assert not [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]


def test_auto_load_from_shared_sidecar_saves_to_own(w, tmp_path):
    shared = tmp_path / ".alwavelets_notes.json"
    shared.write_text(json.dumps({"version": 1, "annotations": [
        {"time_start": 0, "time_end": 5, "signal": None, "text": "shared"}]}))
    trace = str(tmp_path / "b.vcd")
    store = w.AnnotationStore()
    path, n = store.auto_load(trace)
    assert n == 1 and path == str(shared)
    assert store.path == store.default_path(trace)


# ── snapshot ─────────────────────────────────────────────────────────

def test_svg_clamps_prewindow_change(w):
    bus = _sig(w, "d", 8, [(0, "00000001"), (1500, "00000010")])
    vcd = _VCD(bus)
    svg = w.render_svg(vcd, ["top.d"], 1000, 2000)
    import re
    xs = [int(x) for x in re.findall(r'x1="(-?\d+)"', svg)]
    assert xs and min(xs) >= 0


def test_svg_theme_json_is_robust(w, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"HIGH": 2, "LOW": "nope", "BUS": [None], "VAL": True}))
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"HIGH": ["#12ab34", -1], "BUS": [111, -1]}))
    bus = _sig(w, "d", 8, [(0, "00000001")])
    vcd = _VCD(bus)
    assert "<svg" in w.render_svg(vcd, ["top.d"], 0, 10, theme=str(bad))
    assert "#12ab34" in w.render_svg(vcd, ["top.d"], 0, 10, theme=str(good))


# ── tcl ──────────────────────────────────────────────────────────────

def test_tcl_foreach_runs_every_body_line_per_item(w):
    calls = []

    class V:
        def add_signals(self, pats):
            calls.append(tuple(pats))
            return "ok"
    eng = w.TclEngine(V())
    out = eng.execute("foreach s {a b} {\n  add_signal $s\n  puts got_$s\n}\n"
                      "foreach s {c} {puts again_$s}")
    assert calls == [("a",), ("b",)]
    assert out.splitlines() == ["ok", "got_a", "ok", "got_b", "again_c"]


# ── analysis / expr ──────────────────────────────────────────────────

def test_find_anomalies_limit_is_earliest_overall(w):
    late = _sig(w, "late", 1, [(0, "0")] + [(1000 + i, str(i % 2)) for i in range(40)])
    early = _sig(w, "early", 1, [(0, "0"), (5, "x")])
    evs = w.find_anomalies([late, early], 0, 2000, limit=1)
    assert evs == [(5, "top.early", "x", "x")]


def test_candidate_times_is_lazy(w):
    from alwavelets import expr
    a = _sig(w, "a", 1, [(0, "0"), (10, "1"), (20, "0"), (30, "1")])
    b = _sig(w, "b", 1, [(0, "0"), (10, "1"), (15, "0")])
    gen = expr._candidate_times({"a": a, "b": b}, 0, 30)
    assert hasattr(gen, "__next__")
    assert list(gen) == [0, 10, 15, 20, 30]
    vcd = _VCD(a, b)
    assert w.find_expression(vcd, "a && b") == 10


# ── gtkw ─────────────────────────────────────────────────────────────

def test_gtkw_skips_non_signal_lines(w, tmp_path):
    p = tmp_path / "v.gtkw"
    p.write_text('[dumpfile] "x.vcd"\n@28\ntop.clk\n-a comment\n+{grp}\n^1 filter\n'
                 '>2\ntop.data[7:0]\n')
    got = w.load_gtkw(str(p))
    assert [n for n, _ in got["signals"]] == ["top.clk", "top.data[7:0]"]
    out = tmp_path / "o.gtkw"
    w.save_gtkw(str(out), 'we"ird.vcd', [("top.clk", w.Radix.BIN, 1)])
    assert w.load_gtkw(str(out))["dumpfile"] == "we'ird.vcd"
