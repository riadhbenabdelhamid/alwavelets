"""Regression tests for the defects found in the high-effort review of the
finder / packing / cache / measurement / undo / session work.

Each test names the bug it pins down, so a future refactor that reintroduces
one fails here rather than in a user's terminal.
"""
import json
import types


def _viewer(w, order=("a", "b", "c", "d")):
    """Headless stub with the real list-mutation + group machinery bound on."""
    from alwavelets.ui import Alwavelets
    s = types.SimpleNamespace(
        display_order=list(order), _display_order_set=set(order),
        groups=[], _group_by_name={}, _member_group={}, _group_depth={},
        view_rows=list(order), sig_offset=0, focus_idx=0, visible_signals=20,
        _undo_stack=[], _needs_layout=False, messages=[])
    s._show_message = s.messages.append
    s.get_signal = lambda sid: types.SimpleNamespace(name=sid)
    for m in ("delete_signal", "move_signal", "push_undo", "undo", "_focus_on",
              "focused_signal_id", "focused_token", "create_group", "ungroup",
              "_reindex_groups", "_rebuild_view_rows", "_sync_display_set",
              "_group_chain", "_undo_groups_snapshot"):
        setattr(s, m, getattr(Alwavelets, m).__get__(s))
    s.is_group_header = Alwavelets.is_group_header
    return s


# ── group headers shift the focus index (d / J / K hit the wrong signal) ──

def test_delete_uses_focused_signal_not_row_index(w):
    s = _viewer(w)
    s.create_group("G", ["a"])                  # view_rows: [hdr, a, b, c, d]
    s.focus_idx = 1                             # the 'a' row
    assert s.focused_signal_id() == "a"
    s.delete_signal()
    assert s.display_order == ["b", "c", "d"]   # 'a' went, not 'b'


def test_move_uses_focused_signal_not_row_index(w):
    s = _viewer(w)
    s.create_group("G", ["a"])
    s.focus_idx = 2                             # the 'b' row
    assert s.focused_signal_id() == "b"
    s.move_signal(1)                            # swap b and c
    assert s.display_order == ["a", "c", "b", "d"]


def test_delete_on_group_header_is_a_no_op(w):
    s = _viewer(w)
    s.create_group("G", ["a", "b"])
    s.focus_idx = 0                             # the header row
    assert s.focused_signal_id() is None
    s.delete_signal()
    s.move_signal(1)
    assert s.display_order == ["a", "b", "c", "d"]


def test_focus_follows_moved_signal_across_headers(w):
    s = _viewer(w)
    s.create_group("G", ["b"])                  # [a, hdr, b, c, d]
    s.focus_idx = 2                             # 'b'
    s.move_signal(1)                            # b swaps with c
    assert s.focused_signal_id() == "b"         # focus tracks the signal


# ── undo must survive untracked list mutations ──

def test_move_undo_survives_untracked_mutation(w):
    s = _viewer(w)
    s.focus_idx = 2                             # 'c'
    s.move_signal(1)                            # -> a, b, d, c
    assert s.display_order == ["a", "b", "d", "c"]
    s.display_order.insert(0, "z")              # e.g. the tree browser
    s._display_order_set.add("z")
    s.undo()
    assert s.display_order == ["z", "a", "b", "c", "d"]   # c back in place


def test_delete_undo_survives_untracked_mutation(w):
    s = _viewer(w)
    s.focus_idx = 1                             # 'b'
    s.delete_signal()
    assert s.display_order == ["a", "c", "d"]
    s.display_order.insert(0, "z")              # shifts every index
    s.undo()
    assert s.display_order == ["z", "a", "b", "c", "d"]   # anchored on 'c'


# ── the finder's membership set must track removals ──

def test_delete_syncs_display_order_set(w):
    s = _viewer(w)
    s.focus_idx = 1
    s.delete_signal()
    assert "b" not in s._display_order_set      # else '/' refuses to re-add it
    s.undo()
    assert "b" in s._display_order_set


def test_finder_can_readd_a_deleted_signal(w):
    from alwavelets.finder import FinderMixin
    from alwavelets.core import VCDSignal
    sigs = {"!": VCDSignal("clk", "!", 1), '"': VCDSignal("data", '"', 8)}
    s = _viewer(w, order=("!", '"'))
    s.vcd = types.SimpleNamespace(signals=sigs, signal_order=["!", '"'])
    s.get_signal = lambda sid: sigs.get(sid)
    s._invalidate_scope_cache = lambda: None
    s.search_term, s.search_results, s.search_idx = "", [], 0
    s.height, s.width = 30, 80
    s.toggle_group_collapse = lambda n: None
    for m in ("open_finder", "_finder_refilter", "finder_handle_key",
              "_finder_commit"):
        setattr(s, m, getattr(FinderMixin, m).__get__(s))

    s.focus_idx = 1
    s.delete_signal()                           # remove 'data'
    assert s.display_order == ["!"]
    s.open_finder()
    for ch in "data":
        s.finder_handle_key(ord(ch))
    s.finder_handle_key(10)                     # Enter
    assert '"' in s.display_order               # re-added, not silently ignored


# ── packed bus storage must tolerate a malformed empty value ──

def test_empty_bus_value_does_not_abort_the_parse(w):
    sig = w.VCDSignal("d", "!", 8)
    sig.add_change(0, "10101010")
    sig.add_change(10, "")                      # 'b !' from a truncated dump
    sig.add_change(20, "11110000")
    assert [sig.value_str(i) for i in range(3)] == ["10101010", "", "11110000"]


def test_empty_bus_value_in_a_real_vcd(w, tmp_path):
    path = tmp_path / "empty.vcd"
    path.write_text(
        "$timescale 1ns $end\n$scope module top $end\n"
        '$var wire 8 " d $end\n$upscope $end\n$enddefinitions $end\n'
        '#0\nb10101010 "\n#10\nb "\n')
    vcd = w.VCDFile.parse(str(path))            # must not raise
    assert vcd.signals['"'].num_changes() == 2


# ── measurement must not divide by a zero period ──

def test_glitchy_signal_reports_no_period(w):
    from alwavelets.analysis import measure_signal
    sig = w.VCDSignal("g", "!", 1)
    for t, v in [(0, "0"), (10, "1"), (10, "0"), (10, "1"), (20, "0")]:
        sig.add_change(t, v)
    m = measure_signal(sig, 0, 20)              # two rises share t=10
    assert m.get("period") != 0                 # 0 would be divided by
    assert "period" not in m


# ── a fractional row stride must not produce a float focus index ──

def test_click_focus_index_is_an_int_at_fractional_spacing(w):
    import curses
    from alwavelets.ui import Alwavelets
    s = types.SimpleNamespace(
        name_width=10, wave_width=100, time_start=0, time_end=1000,
        _drag_anchor=None, _drag_anchor_x=0, _drag_cur=None,
        wave_top=3, signal_spacing=0.5, visible_signals=20, focus_idx=0,
        cursor_a=0, cursor_b=0, active_cursor="a", cursor_on=True,
        markers=[], zoom_history=[], zoom_history_max=50, messages=[])
    s._show_message = s.messages.append
    for m in ("col_to_time", "time_to_col", "push_zoom_history"):
        setattr(s, m, getattr(Alwavelets, m).__get__(s))
    ev = [(0, 10 + 1 + 20, 9, 0, curses.BUTTON1_CLICKED)]
    real, curses.getmouse = curses.getmouse, lambda: ev.pop(0)
    try:
        Alwavelets.handle_mouse.__get__(s)()
    finally:
        curses.getmouse = real
    assert isinstance(s.focus_idx, int)         # a float breaks every index


# ── session restore ──

def _session_viewer(w, tmp_path, max_time=1000):
    from alwavelets.ui import Alwavelets
    sigs = {"!": w.VCDSignal("clk", "!", 1)}
    vcd = types.SimpleNamespace(signals=sigs, signal_order=["!"],
                                max_time=max_time,
                                _filename=str(tmp_path / "t.vcd"))
    s = _viewer(w, order=("!",))
    s.vcd = vcd
    s.analog_signals, s.collapsed_scopes = set(), set()
    s.radix, s.sig_radix, s.sig_color, s.sig_height = w.Radix.HEX, {}, {}, {}
    s.markers, s.time_start, s.time_end = [], 0, max_time
    s.cursor_a, s.cursor_b, s.cursor_b_on = 0, 0, False
    s.name_width, s._name_width_manual = 20, False
    s.signal_spacing, s.show_grid = 0, 0
    s.line_thickness, s.value_label_size = 1, 2
    s.current_theme, s.THEMES = "dark", Alwavelets.THEMES
    s.WORKMODES = Alwavelets.WORKMODES
    s.visible_signals = 20
    s.apply_theme = lambda *a: None
    s.update_layout = lambda: None
    for m in ("session_path", "_session_state", "save_session",
              "restore_session", "clear_session", "apply_workmode"):
        f = getattr(Alwavelets, m)
        setattr(s, m, f if isinstance(f, staticmethod) else f.__get__(s))
    s.session_path = Alwavelets.session_path
    return s


def test_restore_clamps_a_window_past_the_new_end(w, tmp_path):
    s = _session_viewer(w, tmp_path, max_time=1000)
    with open(s.session_path(s.vcd._filename), "w") as f:
        json.dump({"version": 1, "view": [800000, 900000],
                   "cursors": [850000, 860000, True],
                   "markers": [{"time": 999999, "label": "x"}]}, f)
    assert s.restore_session() is True
    assert (s.time_start, s.time_end) == (0, 1000)   # not a blank screen
    assert s.cursor_a <= 1000 and s.cursor_b <= 1000
    assert s.markers[0].time <= 1000


def test_restore_keeps_empty_intermediate_groups(w, tmp_path):
    s = _session_viewer(w, tmp_path)
    with open(s.session_path(s.vcd._filename), "w") as f:
        json.dump({"version": 1, "display_order": ["!"], "groups": [
            {"name": "Outer", "members": [], "collapsed": False, "parent": None},
            {"name": "Mid", "members": [], "collapsed": False, "parent": "Outer"},
            {"name": "Leaf", "members": ["!"], "collapsed": False, "parent": "Mid"},
        ]}, f)
    assert s.restore_session() is True
    assert set(s._group_by_name) == {"Outer", "Mid", "Leaf"}   # chain intact
    assert s._group_chain("Leaf") == ["Leaf", "Mid", "Outer"]


def test_restore_roundtrips_fractional_spacing(w, tmp_path):
    s = _session_viewer(w, tmp_path)
    s.signal_spacing = 0.5
    s.save_session()
    s.signal_spacing = 0
    assert s.restore_session() is True
    assert s.signal_spacing == 0.5              # int() would floor it to 0


# ── CLI flags must win over a saved session ──

def test_layout_flags_disable_session_restore(w):
    import inspect
    from alwavelets import cli
    src = inspect.getsource(cli.main)
    assert "--no-session" in inspect.getsource(cli.main.__wrapped__) \
        if hasattr(cli.main, "__wrapped__") else True
    # use_session is computed from the layout flags and passed through
    assert "use_session" in src
    assert "args.no_session or expanded or load_unchecked" in src
    assert "use_session" in inspect.signature(cli.main_curses).parameters


# ── live reload must not serve the previous run's rows ──

def test_live_reload_clears_the_row_cache(w, tmp_path):
    import os
    from alwavelets.ui import Alwavelets
    from conftest import make_vcd
    path = str(tmp_path / "live.vcd")
    make_vcd(path, n_steps=20)
    v = types.SimpleNamespace(
        vcd=w.VCDFile.parse(path), live_file=path,
        live_mtime=os.path.getmtime(path), _live_pending=None,
        _live_last_poll=0.0, time_start=0, time_end=100,
        cursor_a=0, cursor_b=0, active_cursor="a", cursor_on=True,
        cursor_b_on=False, sig_offset=0, focus_idx=0, collapsed_scopes=set(),
        display_order=[], _needs_layout=False, messages=[],
        _row_cache={("bit", "!", 5, None): "STALE"},
        _row_cache_window=(0, 100, 80, 1))
    v._show_message = v.messages.append
    v._rebuild_display_order = lambda: None
    v._rebuild_tree = lambda: None
    v._reload_preserving_view = Alwavelets._reload_preserving_view.__get__(v)
    assert v._reload_preserving_view() is True
    assert v._row_cache == {}                   # stale rows must be gone
    assert v._row_cache_window is None


# ── focus jumps must resolve through view_rows, not display_order ─────

def _named(w, names):
    """Viewer stub whose signals have real names, for find/jump tests."""
    from alwavelets.ui import Alwavelets
    sigs, order = {}, []
    for i, full in enumerate(names):
        scope, _, leaf = full.rpartition('.')
        sid = f"s{i}"
        sigs[sid] = w.VCDSignal(leaf, sid, 1, scope=scope)
        order.append(sid)
    s = _viewer(w, order=order)
    s.get_signal = lambda sid: sigs.get(sid)
    s.vcd = types.SimpleNamespace(signals=sigs, signal_order=list(order))
    s.search_results, s.search_idx = [], 0
    s.width = 100
    s.selected_signals = set()
    for m in ("_cmd_find_sig", "_focused_order_index", "_jump_next_module",
              "_vim_goto_first_in_module", "_vim_goto_last_in_module",
              "next_search_result", "toggle_group_collapse"):
        setattr(s, m, getattr(Alwavelets, m).__get__(s))
    return s, sigs, order


def test_find_focuses_the_named_signal_despite_group_headers(w):
    """';find x' set sig_offset to a display_order index, but that indexes
    view_rows — with a group header present it focused the wrong signal."""
    s, sigs, order = _named(w, ["top.clk", "top.valid", "top.ready", "top.data"])
    s.create_group("G", [order[0]])          # header pushes every later row down
    s._cmd_find_sig("ready")
    assert s.get_signal(s.focused_signal_id()).name == "ready"


def test_find_then_select_builds_a_real_multi_selection(w):
    """The agent flow 'find a; space; find b; space; group' must select both."""
    s, sigs, order = _named(w, ["top.clk", "top.valid", "top.ready"])
    s.create_group("G", [order[0]])
    for name in ("valid", "ready"):
        s._cmd_find_sig(name)
        s.selected_signals.add(s.focused_signal_id())
    assert len(s.selected_signals) == 2


def test_find_unfolds_a_collapsed_group_to_reach_its_target(w):
    s, sigs, order = _named(w, ["top.clk", "top.valid", "top.ready"])
    s.create_group("G", [order[1], order[2]])
    s.toggle_group_collapse("G")
    assert order[2] not in s.view_rows        # hidden
    s._cmd_find_sig("ready")
    assert s.get_signal(s.focused_signal_id()).name == "ready"
    assert order[2] in s.view_rows            # unfolded to get there


def test_next_search_result_focuses_through_view_rows(w):
    s, sigs, order = _named(w, ["top.clk", "top.valid", "top.ready"])
    s.create_group("G", [order[0]])
    s.search_results = [1, 2]                 # display_order indices
    s.search_idx = -1
    s.next_search_result()
    assert s.get_signal(s.focused_signal_id()).name == "valid"
    s.next_search_result()
    assert s.get_signal(s.focused_signal_id()).name == "ready"


def test_module_jump_focuses_through_view_rows(w):
    s, sigs, order = _named(w, ["top.clk", "top.alu.a", "top.alu.b"])
    s.create_group("G", [order[0]])
    s._focus_on(order[0])
    s._jump_next_module()
    assert s.get_signal(s.focused_signal_id()).full_name == "top.alu.a"


def test_vim_goto_first_and_last_in_module(w):
    s, sigs, order = _named(w, ["top.a", "top.b", "top.c"])
    s.create_group("G", [order[0]])
    s._focus_on(order[1])
    s._vim_goto_last_in_module()
    assert s.get_signal(s.focused_signal_id()).name == "c"
    s._vim_goto_first_in_module()
    assert s.get_signal(s.focused_signal_id()).name == "a"


# ── adding signals to the display (the agent's blocked path) ─────────

def _full(w, tmp_path):
    """Real demo viewer stub with collapsed scopes, as --demo opens."""
    import alwavelets as al
    from alwavelets.cli import generate_demo_vcd
    from alwavelets.ui import Alwavelets
    p = str(tmp_path / "d.vcd")
    generate_demo_vcd(p)
    vcd = al.parse_file(p)
    s = types.SimpleNamespace(
        vcd=vcd, display_order=list(vcd.signal_order),
        _display_order_set=set(vcd.signal_order), collapsed_scopes=set(),
        groups=[], _group_by_name={}, _member_group={}, _group_depth={},
        view_rows=[], sig_offset=0, focus_idx=0, visible_signals=20,
        _undo_stack=[], _needs_layout=False, virtual_signals={}, messages=[])
    s._show_message = s.messages.append
    s._invalidate_scope_cache = lambda: None
    for m in ("add_signals", "remove_signals", "get_signal", "push_undo", "undo",
              "_focus_on", "_rebuild_view_rows", "_reindex_groups", "_group_chain",
              "focused_signal_id", "_sync_display_set", "_rebuild_display_order",
              "collapse_all_modules"):
        setattr(s, m, getattr(Alwavelets, m).__get__(s))
    return s


def test_collapsing_scopes_keeps_the_display_set_in_sync(w, tmp_path):
    """A stale _display_order_set made every signal hidden by a collapsed
    scope look already-displayed, so '/' and ';add' silently refused it."""
    s = _full(w, tmp_path)
    s.collapse_all_modules()
    assert s._display_order_set == set(s.display_order)


def test_add_signals_reaches_signals_hidden_by_a_collapsed_scope(w, tmp_path):
    s = _full(w, tmp_path)
    s.collapse_all_modules()                      # top.alu.* now hidden
    assert not any("alu" in s.get_signal(x).full_name for x in s.display_order)
    msg = s.add_signals(["top.alu.*"])
    assert msg.startswith("Added")
    assert any("alu" in s.get_signal(x).full_name for x in s.display_order)


def test_add_signals_by_exact_name_and_undo(w, tmp_path):
    s = _full(w, tmp_path)
    s.remove_signals(["top.*"])
    assert s.display_order == []
    assert s.add_signals(["top.valid", "top.ready"]).startswith("Added 2")
    assert len(s.display_order) == 2
    assert s._display_order_set == set(s.display_order)
    s.undo()
    assert s.display_order == []


def test_add_signals_reports_when_nothing_matches(w, tmp_path):
    s = _full(w, tmp_path)
    assert "nothing matched" in s.add_signals(["no.such.signal"])
    assert "give a signal" in s.add_signals([])


def test_add_signals_is_idempotent(w, tmp_path):
    s = _full(w, tmp_path)
    s.remove_signals(["top.*"])
    s.add_signals(["top.clk"])
    n = len(s.display_order)
    assert "nothing matched" in s.add_signals(["top.clk"])   # already shown
    assert len(s.display_order) == n


def test_tcl_add_signal_no_longer_raises_nameerror(w, tmp_path):
    """fnmatch was used in tcl.py but never imported — add_signal,
    remove_signal, analog and expand all died with NameError."""
    import alwavelets.tcl as tclmod
    assert hasattr(tclmod, "fnmatch")
    s = _full(w, tmp_path)
    s.remove_signals(["top.*"])
    eng = tclmod.TclEngine(s)
    out = eng.execute("add_signal top.valid top.ready")
    assert "Added 2" in out                       # and it reports, not silence
    assert len(s.display_order) == 2
