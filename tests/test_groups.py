"""Tests for collapsible display groups (the view_rows overlay over
display_order). Exercised on a lightweight stub so no curses terminal is needed."""
import types


def _stub(order):
    from alwavelets.ui import Alwavelets
    s = types.SimpleNamespace(
        display_order=list(order), groups=[], _group_by_name={}, _member_group={},
        view_rows=list(order), sig_offset=0, focus_idx=0)
    for m in ("create_group", "ungroup", "toggle_group_collapse", "_reindex_groups",
              "_rebuild_view_rows", "focused_token", "focused_signal_id",
              "_group_chain"):
        setattr(s, m, getattr(Alwavelets, m).__get__(s))
    s.is_group_header = Alwavelets.is_group_header
    return s


def test_group_inserts_header_at_first_member(w):
    s = _stub(["a", "b", "c", "d"])
    s.create_group("G", ["b", "c"])
    assert s.view_rows == ["a", ("\x01hdr", "G"), "b", "c", "d"]


def test_collapse_hides_members_keeps_header(w):
    s = _stub(["a", "b", "c", "d"])
    s.create_group("G", ["b", "c"])
    s.toggle_group_collapse("G")
    assert s.view_rows == ["a", ("\x01hdr", "G"), "d"]
    s.toggle_group_collapse("G")                      # expand again
    assert s.view_rows == ["a", ("\x01hdr", "G"), "b", "c", "d"]


def test_ungroup_restores_flat_view(w):
    s = _stub(["a", "b", "c"])
    s.create_group("G", ["b"])
    assert s.ungroup("G") is True
    assert s.view_rows == ["a", "b", "c"]


def test_focused_signal_id_none_on_header(w):
    s = _stub(["a", "b", "c"])
    s.create_group("G", ["b", "c"])
    # view_rows: a, hdr, b, c   -> focus the header (index 1)
    s.focus_idx = 1
    assert s.is_group_header(s.focused_token())
    assert s.focused_signal_id() is None
    s.focus_idx = 2                                   # the 'b' row
    assert s.focused_signal_id() == "b"


def test_member_moves_between_groups(w):
    s = _stub(["a", "b", "c", "d"])
    s.create_group("G1", ["a", "b"])
    s.create_group("G2", ["b", "c"])                  # b moves out of G1 into G2
    g1 = s._group_by_name["G1"]
    g2 = s._group_by_name["G2"]
    assert g1["members"] == {"a"} and g2["members"] == {"b", "c"}


def test_collapse_clamps_offsets(w):
    s = _stub(["a", "b", "c", "d", "e"])
    s.create_group("G", ["b", "c", "d", "e"])
    s.sig_offset = 4
    s.focus_idx = 1
    s.toggle_group_collapse("G")                      # view shrinks to [a, hdr]
    assert s.sig_offset + s.focus_idx < len(s.view_rows)


# ── nested groups ────────────────────────────────────────────────────

def test_nested_group_headers_indent_order(w):
    s = _stub(["a", "b", "c", "d"])
    s.create_group("Inner", ["a", "b"], parent="Outer")
    # parent auto-created as an empty node; headers emit outermost-first
    assert s.view_rows == [("\x01hdr", "Outer"), ("\x01hdr", "Inner"),
                           "a", "b", "c", "d"]
    assert s._group_depth == {"Outer": 0, "Inner": 1}


def test_collapsing_parent_hides_child_header_and_members(w):
    s = _stub(["a", "b", "c"])
    s.create_group("Inner", ["a", "b"], parent="Outer")
    s.toggle_group_collapse("Outer")
    assert s.view_rows == [("\x01hdr", "Outer"), "c"]
    s.toggle_group_collapse("Outer")            # expand: all back
    assert ("\x01hdr", "Inner") in s.view_rows and "a" in s.view_rows


def test_collapsing_child_keeps_parent_visible(w):
    s = _stub(["a", "b", "c"])
    s.create_group("Inner", ["a", "b"], parent="Outer")
    s.toggle_group_collapse("Inner")
    assert s.view_rows == [("\x01hdr", "Outer"), ("\x01hdr", "Inner"), "c"]


def test_two_children_under_one_parent(w):
    s = _stub(["a", "b", "c", "d"])
    s.create_group("G1", ["a", "b"], parent="P")
    s.create_group("G2", ["c", "d"], parent="P")
    assert s.view_rows == [("\x01hdr", "P"), ("\x01hdr", "G1"), "a", "b",
                           ("\x01hdr", "G2"), "c", "d"]
    s.toggle_group_collapse("P")
    assert s.view_rows == [("\x01hdr", "P")]


def test_ungroup_reparents_children(w):
    s = _stub(["a", "b"])
    s.create_group("Inner", ["a", "b"], parent="Outer")
    assert s.ungroup("Outer") is True
    # Inner survives at top level
    assert s.view_rows == [("\x01hdr", "Inner"), "a", "b"]
    assert s._group_by_name["Inner"].get("parent") is None


def test_ungroup_child_prunes_empty_parent(w):
    s = _stub(["a", "b"])
    s.create_group("Inner", ["a", "b"], parent="Outer")
    s.ungroup("Inner")
    assert s.groups == [] and s.view_rows == ["a", "b"]


def test_nesting_cycle_rejected(w):
    s = _stub(["a", "b", "c", "d"])
    s.create_group("A", ["a", "b"])
    s.create_group("B", ["c"], parent="A")
    assert s.create_group("A", ["d"], parent="B") is False   # A under its child
    assert s.create_group("X", ["d"], parent="X") is False   # self-parent


def test_collapse_all_keeps_root_signals(w, tmp_path):
    # Regression: collapse_all_modules used to add the ROOT scope ('top') to
    # collapsed_scopes, hiding every signal — a trace with top/top.alu opened
    # EMPTY. Only first-level sub-scopes (top.alu) may be folded.
    import types
    from alwavelets.ui import Alwavelets
    path = str(tmp_path / "t.vcd")
    with open(path, "w") as f:
        f.write("$timescale 1ns $end\n$scope module top $end\n"
                "$var wire 1 ! clk $end\n$scope module alu $end\n"
                '$var wire 1 " op $end\n$upscope $end\n$upscope $end\n'
                "$enddefinitions $end\n#0\n0!\n0\"\n")
    vcd = w.VCDFile.parse(path)
    s = types.SimpleNamespace(vcd=vcd, collapsed_scopes=set(),
                              display_order=[], _display_order_set=set(),
                              sig_offset=0, focus_idx=0, virtual_signals={})
    for m in ("get_signal", "_rebuild_display_order", "collapse_all_modules",
              "_sync_display_set"):
        setattr(s, m, getattr(Alwavelets, m).__get__(s))
    s.collapse_all_modules()
    assert s.collapsed_scopes == {"top.alu"}
    names = [s.get_signal(sid).name for sid in s.display_order]
    assert names == ["clk"]           # root-scope signal visible, alu folded
