"""Round-trip and query tests for the JSON-backed persistence layer:
bookmarks, annotations, and ISA signal tags."""


# ── Bookmarks ────────────────────────────────────────────────────────

def test_bookmark_store_roundtrip(w, tmp_path):
    store = w.BookmarkStore()
    store.add(w.WaveformBookmark("zoomA", 100, 200, cursor_a=150,
                                 cursor_on=True, signals=["top.clk"]))
    store.add(w.WaveformBookmark("zoomB", 300, 400))
    path = str(tmp_path / "bm.json")
    assert store.save(path) is True

    loaded = w.BookmarkStore()
    assert loaded.load(path) == 2
    assert loaded.list_names() == ["zoomA", "zoomB"]
    a = loaded.get("zoomA")
    assert (a.time_start, a.time_end, a.cursor_a) == (100, 200, 150)
    assert a.cursor_on is True and a.signals == ["top.clk"]


def test_bookmark_store_remove(w):
    store = w.BookmarkStore()
    store.add(w.WaveformBookmark("x", 0, 10))
    assert store.remove("x") is True
    assert store.remove("missing") is False
    assert store.list_names() == []


def test_bookmark_default_path(w):
    store = w.BookmarkStore()
    assert store.default_path("/tmp/foo.vcd").endswith("foo.alwavelets_bookmarks.json")


# ── Annotations ──────────────────────────────────────────────────────

def test_annotation_queries(w):
    store = w.AnnotationStore()
    store.add(w.WaveformAnnotation(100, 200, signal="top.clk", text="glitch",
                                   category="bug"))
    store.add(w.WaveformAnnotation(500, 600, text="note here"))
    # at_time: inside the first range, between ranges
    assert len(store.at_time(150)) == 1
    assert store.at_time(350) == []
    # in_range: overlap detection
    assert len(store.in_range(0, 250)) == 1
    assert len(store.in_range(0, 1000)) == 2
    # on_signal
    assert len(store.on_signal("top.clk")) == 1
    assert store.on_signal("nope") == []


def test_annotation_roundtrip(w, tmp_path):
    store = w.AnnotationStore()
    store.add(w.WaveformAnnotation(10, 20, signal="a", text="hi", category="todo"))
    path = str(tmp_path / "ann.json")
    assert store.save(path) is True

    loaded = w.AnnotationStore()
    assert loaded.load(path) == 1
    a = loaded.annotations[0]
    assert (a.time_start, a.time_end, a.signal, a.text, a.category) == \
        (10, 20, "a", "hi", "todo")


# ── ISA signal tags ──────────────────────────────────────────────────

def test_tag_store_roundtrip(w, tmp_path):
    store = w.SignalTagStore()
    store.set_tag("clock", "top.clk")
    store.set_tag("isa.pc.writeback", "cpu.pc", hart=1)
    assert store.get_signal("clock") == "top.clk"
    assert store.get_role("top.clk") == "clock"
    assert store.get_signal("isa.pc.writeback", hart=1) == "cpu.pc"

    path = str(tmp_path / "tags.json")
    store.save(path)
    loaded = w.SignalTagStore.load(path)
    assert loaded.get_signal("clock") == "top.clk"
    assert loaded.get_role("top.clk") == "clock"
    assert loaded.get_signal("isa.pc.writeback", hart=1) == "cpu.pc"


def test_tag_auto_detect(w):
    class _VCD:
        pass
    vcd = _VCD()
    vcd.signals = {}
    vcd.signal_order = []
    for nm in ["clk", "rst_n", "instr_wb"]:
        vcd.signals[nm] = w.VCDSignal(nm, nm, 32, scope="cpu")
        vcd.signal_order.append(nm)

    store = w.SignalTagStore()
    store.auto_detect_roles(vcd)
    assert store.get_signal("clock") == "cpu.clk"
    assert store.get_signal("reset") == "cpu.rst_n"
    assert store.get_signal("isa.instr.writeback") == "cpu.instr_wb"
