"""Tests for the generalized undo stack (`u`)."""
import types


def _viewer(w, order=("a", "b", "c")):
    from alwavelets.ui import Alwavelets
    s = types.SimpleNamespace(
        display_order=list(order), _display_order_set=set(order),
        groups=[], _group_by_name={}, _member_group={},
        view_rows=list(order), sig_offset=0, focus_idx=0,
        sig_radix={}, sig_color={}, sig_height={}, analog_signals=set(),
        radix=w.Radix.HEX, _undo_stack=[], _needs_layout=False,
        USER_COLORS=[1, 2, 3, 4, 5, 6], visible_signals=20,
        selected_signals=set(), _visual_mode=False, messages=[])
    s._show_message = s.messages.append
    s.get_signal = lambda sid: types.SimpleNamespace(name=sid)
    for m in ("push_undo", "undo", "_undo_dict_entry", "_undo_groups_snapshot",
              "delete_signal", "move_signal", "cycle_signal_color",
              "cycle_signal_height", "cycle_signal_radix", "create_group",
              "ungroup", "_reindex_groups", "_rebuild_view_rows",
              "_sync_display_set", "_group_chain", "_focus_on",
              "focused_signal_id"):
        setattr(s, m, getattr(Alwavelets, m).__get__(s))
    return s


def test_undo_delete(w):
    s = _viewer(w)
    s.focus_idx = 1
    s.delete_signal()
    assert s.display_order == ["a", "c"]
    s.undo()
    assert s.display_order == ["a", "b", "c"]
    assert any("Undid: remove b" in m for m in s.messages)


def test_undo_move(w):
    s = _viewer(w)
    s.move_signal(1)                     # a <-> b
    assert s.display_order == ["b", "a", "c"]
    s.undo()
    assert s.display_order == ["a", "b", "c"]


def test_undo_styling_cycles(w):
    s = _viewer(w)
    s.cycle_signal_color()
    assert s.sig_color == {"a": 1}
    s.cycle_signal_height()
    assert s.sig_height == {"a": 2}
    s.cycle_signal_radix()
    assert "a" in s.sig_radix
    s.undo()                             # radix back
    assert s.sig_radix == {}
    s.undo()                             # height back
    assert s.sig_height == {}
    s.undo()                             # color back
    assert s.sig_color == {}


def test_undo_lifo_order(w):
    s = _viewer(w)
    s.focus_idx = 0
    s.delete_signal()                    # remove a
    s.focus_idx = 1
    s.delete_signal()                    # remove c (b, c left -> b)
    assert s.display_order == ["b"]
    s.undo()
    assert s.display_order == ["b", "c"]
    s.undo()
    assert s.display_order == ["a", "b", "c"]


def test_undo_group_snapshot(w):
    s = _viewer(w)
    s.push_undo("group 'G'", s._undo_groups_snapshot())
    s.create_group("G", ["a", "b"])
    assert s._group_by_name and s.view_rows[0] == ("\x01hdr", "G")
    s.undo()
    assert s.groups == [] and s.view_rows == ["a", "b", "c"]


def test_undo_empty_stack(w):
    s = _viewer(w)
    s.undo()
    assert s.messages == ["Nothing to undo"]


def test_undo_stack_capped(w):
    s = _viewer(w)
    for i in range(150):
        s.push_undo(f"n{i}", lambda: None)
    assert len(s._undo_stack) == 100
    assert s._undo_stack[0][0] == "n50"  # oldest half dropped
