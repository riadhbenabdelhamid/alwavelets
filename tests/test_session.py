"""Tests for the polish trio: true-color helpers, session save/restore, and
mouse drag-to-zoom (all exercised headlessly)."""
import json
import types


# ── true-color helpers ───────────────────────────────────────────────

def test_hex_to_rgb(w):
    from alwavelets.ui import hex_to_rgb
    assert hex_to_rgb("#000000") == (0, 0, 0)
    assert hex_to_rgb("#ff8000") == (255, 128, 0)
    assert hex_to_rgb("nope") is None
    assert hex_to_rgb("#xyzxyz") is None
    assert hex_to_rgb(42) is None


def test_nearest_color_256(w):
    from alwavelets.ui import nearest_color_256
    assert nearest_color_256(0, 0, 0) == 16          # cube black
    assert nearest_color_256(255, 255, 255) == 231   # cube white
    assert nearest_color_256(255, 0, 0) == 196       # pure red
    assert nearest_color_256(0, 255, 0) == 46        # pure green
    assert nearest_color_256(0, 0, 255) == 21        # pure blue
    assert 232 <= nearest_color_256(128, 128, 128) <= 255  # mid-gray → ramp
    # every result is a valid 256-color index
    for r in range(0, 256, 51):
        for g in range(0, 256, 51):
            for b in range(0, 256, 51):
                assert 16 <= nearest_color_256(r, g, b) <= 255


# ── session persistence ──────────────────────────────────────────────

def _viewer(w, tmp_path):
    """Headless stub with the real session + group machinery bound on."""
    import sys
    sys.path.insert(0, "tests")
    from conftest import make_vcd
    from alwavelets.ui import Alwavelets

    path = str(tmp_path / "s.vcd")
    make_vcd(path, n_steps=50)
    vcd = w.VCDFile.parse(path)

    s = types.SimpleNamespace(
        vcd=vcd, display_order=list(vcd.signal_order),
        groups=[], _group_by_name={}, _member_group={},
        view_rows=list(vcd.signal_order), sig_offset=0, focus_idx=0, visible_signals=20,
        analog_signals=set(), collapsed_scopes=set(),
        radix=w.Radix.HEX, sig_radix={}, sig_color={}, sig_height={},
        markers=[], time_start=0, time_end=vcd.max_time,
        cursor_a=0, cursor_b=0, cursor_b_on=False,
        name_width=20, _name_width_manual=False, signal_spacing=0,
        show_grid=0, line_thickness=1, value_label_size=2,
        current_theme='dark', THEMES=Alwavelets.THEMES, WORKMODES=Alwavelets.WORKMODES, messages=[],
        _display_order_set=set(vcd.signal_order))
    s._show_message = s.messages.append
    s.apply_theme = lambda *a: None
    s.update_layout = lambda: None
    s._sync_display_set = lambda: None
    for m in ("session_path", "_session_state", "save_session",
              "clear_session", "restore_session", "apply_workmode",
              "_reindex_groups", "_rebuild_view_rows", "_group_chain"):
        f = getattr(Alwavelets, m)
        setattr(s, m, f if isinstance(f, staticmethod) else f.__get__(s))
    s.session_path = Alwavelets.session_path
    return s, path


def test_session_roundtrip(w, tmp_path):
    s, path = _viewer(w, tmp_path)
    sids = s.vcd.signal_order
    # arrange a distinctive session
    s.display_order = [sids[1], sids[0]]
    s.analog_signals = {sids[1]}
    s.sig_radix = {sids[1]: w.Radix.DEC}
    s.sig_color = {sids[0]: 3}
    s.sig_height = {sids[1]: 3}
    s.markers = [w.Marker(120, "here")]
    s.time_start, s.time_end = 40, 250
    s.cursor_a, s.cursor_b, s.cursor_b_on = 100, 200, True
    s.groups = [{'name': 'G', 'members': {sids[0], sids[1]}, 'collapsed': True}]
    s.save_session()
    assert any("saved" in m for m in s.messages)

    # a fresh viewer on the same trace restores it all
    f, _ = _viewer(w, tmp_path)
    assert f.restore_session() is True
    assert f.display_order == [sids[1], sids[0]]
    assert f.analog_signals == {sids[1]}
    assert f.sig_radix == {sids[1]: w.Radix.DEC}
    assert f.sig_color == {sids[0]: 3}
    assert f.sig_height == {sids[1]: 3}
    assert [(m.time, m.label) for m in f.markers] == [(120, "here")]
    assert (f.time_start, f.time_end) == (40, 250)
    assert (f.cursor_a, f.cursor_b, f.cursor_b_on) == (100, 200, True)
    assert f.groups[0]['name'] == 'G' and f.groups[0]['collapsed'] is True


def test_session_validates_stale_signals(w, tmp_path):
    s, path = _viewer(w, tmp_path)
    sp = s.session_path(path)
    with open(sp, 'w') as fjson:
        json.dump({'version': 1,
                   'display_order': ['gone1', s.vcd.signal_order[0], 'gone2'],
                   'sig_radix': {'gone1': 'dec'},
                   'analog_signals': ['gone1']}, fjson)
    assert s.restore_session() is True
    assert s.display_order == [s.vcd.signal_order[0]]   # unknown ids dropped
    assert s.sig_radix == {} and s.analog_signals == set()


def test_session_clear_and_missing(w, tmp_path):
    import os
    s, path = _viewer(w, tmp_path)
    assert s.restore_session() is False                 # nothing saved yet
    s.save_session()
    assert os.path.exists(s.session_path(path))
    s.clear_session()
    assert not os.path.exists(s.session_path(path))
    assert s.restore_session() is False


def test_corrupt_session_is_ignored(w, tmp_path):
    s, path = _viewer(w, tmp_path)
    with open(s.session_path(path), 'w') as f:
        f.write("{not json")
    assert s.restore_session() is False


# ── drag-to-zoom ─────────────────────────────────────────────────────

def test_drag_zoom_sets_window(w):
    from alwavelets.ui import Alwavelets
    s = types.SimpleNamespace(
        name_width=10, wave_width=100, time_start=0, time_end=1000,
        _drag_anchor=None, _drag_anchor_x=0, _drag_cur=None,
        zoom_history=[], zoom_history_max=50, messages=[],
        wave_top=3, signal_spacing=0, visible_signals=20, focus_idx=0,
        cursor_a=0, cursor_b=0, active_cursor='a', cursor_on=True, markers=[])
    s._show_message = s.messages.append
    for m in ("col_to_time", "time_to_col", "push_zoom_history"):
        setattr(s, m, getattr(Alwavelets, m).__get__(s))
    s.cursor_time = 0

    import curses
    events = []

    def fake_getmouse():
        return events.pop(0)

    curses_getmouse, curses.getmouse = curses.getmouse, fake_getmouse
    try:
        handle = Alwavelets.handle_mouse.__get__(s)
        # press at col 20 (t=200), drag to col 60, release at col 60 (t=600)
        events.append((0, 10 + 1 + 20, 5, 0, curses.BUTTON1_PRESSED))
        handle()
        assert s._drag_anchor == 200
        events.append((0, 10 + 1 + 60, 5, 0, curses.REPORT_MOUSE_POSITION))
        handle()
        assert s._drag_cur == 600
        events.append((0, 10 + 1 + 60, 5, 0, curses.BUTTON1_RELEASED))
        handle()
        assert (s.time_start, s.time_end) == (200, 600)
        assert s.zoom_history == [(0, 1000)]            # Z can undo
        assert s._drag_anchor is None
    finally:
        curses.getmouse = curses_getmouse


def test_click_without_drag_keeps_window(w):
    from alwavelets.ui import Alwavelets
    s = types.SimpleNamespace(
        name_width=10, wave_width=100, time_start=0, time_end=1000,
        _drag_anchor=None, _drag_anchor_x=0, _drag_cur=None,
        zoom_history=[], zoom_history_max=50, messages=[],
        wave_top=3, signal_spacing=0, visible_signals=20, focus_idx=0,
        cursor_a=0, cursor_b=0, active_cursor='a', cursor_on=True, markers=[])
    s._show_message = s.messages.append
    for m in ("col_to_time", "time_to_col", "push_zoom_history"):
        setattr(s, m, getattr(Alwavelets, m).__get__(s))

    import curses
    events = [(0, 10 + 1 + 20, 5, 0, curses.BUTTON1_PRESSED),
              (0, 10 + 1 + 21, 5, 0, curses.BUTTON1_RELEASED)]  # 1 col: click
    curses_getmouse, curses.getmouse = curses.getmouse, lambda: events.pop(0)
    try:
        handle = Alwavelets.handle_mouse.__get__(s)
        handle()
        handle()
        assert (s.time_start, s.time_end) == (0, 1000)  # no zoom
        # cursor placed by the press (plain attr on the stub — the real class
        # routes the cursor_time property to cursor_a/b)
        assert s.cursor_time == 200
    finally:
        curses.getmouse = curses_getmouse


# ── pre-rename compatibility (Wavelets -> Alwavelets) ────────────────

def test_legacy_session_sidecar_is_still_read(w, tmp_path):
    """An upgrade must not orphan a session saved under the old name."""
    from alwavelets.ui import Alwavelets
    trace = str(tmp_path / "t.vcd")
    old = tmp_path / "t.wavelet_session.json"
    old.write_text('{"version": 1}')
    assert Alwavelets.session_path(trace, existing=True) == str(old)
    # writes always go to the new name
    assert Alwavelets.session_path(trace).endswith(".alwavelets_session.json")
    # and the new name wins once it exists
    (tmp_path / "t.alwavelets_session.json").write_text('{"version": 1}')
    assert Alwavelets.session_path(trace, existing=True).endswith(
        ".alwavelets_session.json")


def test_legacy_config_dir_is_still_read(w, tmp_path, monkeypatch):
    from alwavelets import config
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / "wavelets").mkdir()
    (tmp_path / "wavelets" / "config.json").write_text('{"theme": "phosphor"}')
    assert config.load_config() == {"theme": "phosphor"}
    (tmp_path / "alwavelets").mkdir()
    (tmp_path / "alwavelets" / "config.json").write_text('{"theme": "dark"}')
    assert config.load_config() == {"theme": "dark"}      # new name wins


def test_legacy_bookmark_and_note_sidecars_are_still_read(w, tmp_path):
    import json
    from alwavelets.persistence import BookmarkStore, AnnotationStore
    trace = str(tmp_path / "old.vcd")
    (tmp_path / "old.vcd").write_text("$timescale 1ns $end\n$enddefinitions $end\n")
    (tmp_path / "old.wavelet_bookmarks.json").write_text(
        json.dumps({"bookmarks": [{"name": "legacy", "time": 42}]}))
    (tmp_path / "old.wavelet_notes.json").write_text(
        json.dumps({"annotations": [{"time": 7, "text": "legacy note"}]}))
    _, n = BookmarkStore().auto_load(trace)
    assert n == 1
    _, n2 = AnnotationStore().auto_load(trace)
    assert n2 == 1
