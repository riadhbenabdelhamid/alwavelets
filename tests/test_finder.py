"""Tests for the fuzzy signal finder: the pure scoring functions and the
overlay's filter/commit behaviour (on a headless stub, no curses screen)."""
import types


# ── scoring ──────────────────────────────────────────────────────────

def test_no_match_returns_none(w):
    from alwavelets.finder import fuzzy_match
    assert fuzzy_match("xyz", "top.clk") is None


def test_empty_query_matches_everything(w):
    from alwavelets.finder import fuzzy_match
    assert fuzzy_match("", "anything.at.all") is not None


def test_and_terms(w):
    from alwavelets.finder import fuzzy_match
    assert fuzzy_match("cpu wr", "cpu.wr_en") is not None
    assert fuzzy_match("cpu wr", "cpu.rd_en") is None      # one term fails → no match


def test_subsequence_fallback(w):
    from alwavelets.finder import fuzzy_match
    assert fuzzy_match("cwe", "cpu.wr_en") is not None     # scattered chars still hit


def test_boundary_beats_midword(w):
    from alwavelets.finder import fuzzy_match
    assert fuzzy_match("wr", "cpu.wr_en") > fuzzy_match("wr", "cpu.lwrap")


def test_leaf_name_beats_scope(w):
    from alwavelets.finder import fuzzy_match
    assert fuzzy_match("cpu", "top.cpu") > fuzzy_match("cpu", "cpu.top")


def test_shorter_name_wins_ties(w):
    from alwavelets.finder import fuzzy_match
    assert fuzzy_match("clk", "top.clk") > fuzzy_match("clk", "top.alu.subunit.clk")


def test_match_positions(w):
    from alwavelets.finder import match_positions
    # Substring: contiguous run at the find() position.
    assert match_positions("clk", "top.clk") == {4, 5, 6}
    # Subsequence: one position per query char.
    assert len(match_positions("cwe", "cpu.wr_en")) == 3


# ── overlay behaviour (headless stub) ────────────────────────────────

def _viewer(w, names, displayed=()):
    """Stub viewer with the real finder + group-overlay methods bound on."""
    from alwavelets.ui import Alwavelets
    from alwavelets.finder import FinderMixin

    sigs, order = {}, []
    for i, fn in enumerate(names):
        scope, _, leaf = fn.rpartition('.')
        sid = f"s{i}"
        sigs[sid] = w.VCDSignal(leaf, sid, 1, scope=scope)
        order.append(sid)
    shown = [order[names.index(n)] for n in displayed]
    s = types.SimpleNamespace(
        vcd=types.SimpleNamespace(signals=sigs, signal_order=order),
        display_order=list(shown), _display_order_set=set(shown),
        groups=[], _group_by_name={}, _member_group={},
        view_rows=list(shown), sig_offset=0, focus_idx=0,
        visible_signals=20, height=30, width=80,
        search_term="", search_results=[], search_idx=0, messages=[])
    s.get_signal = lambda sid: sigs.get(sid)
    s._invalidate_scope_cache = lambda: None
    s._show_message = s.messages.append
    s.undo_stack = []
    s.push_undo = lambda desc, fn: s.undo_stack.append((desc, fn))
    for m in ("open_finder", "_finder_refilter", "finder_handle_key",
              "_finder_commit"):
        setattr(s, m, getattr(FinderMixin, m).__get__(s))
    for m in ("toggle_group_collapse", "_rebuild_view_rows"):
        setattr(s, m, getattr(Alwavelets, m).__get__(s))
    return s, order


def _type(s, text):
    for ch in text:
        s.finder_handle_key(ord(ch))


def test_filter_narrows_and_ranks(w):
    s, order = _viewer(w, ["top.clk", "top.alu.clk", "top.data"])
    s.open_finder()
    assert len(s.finder_matches) == 3                     # empty query: all
    _type(s, "clk")
    got = [s._finder_cands[i][1] for i in s.finder_matches]
    assert got == ["top.clk", "top.alu.clk"]              # shorter name ranks first


def test_incremental_equals_fresh(w):
    # Typing char-by-char goes through the narrowing cache; it must land on
    # the same result set as filtering the full query from scratch.
    names = ["top.clk", "top.alu.clk", "top.data", "cpu.wr_en", "cpu.rd_en"]
    s, _ = _viewer(w, names)
    s.open_finder()
    _type(s, "c en")
    incremental = list(s.finder_matches)
    s.finder_query = "c en"
    s._finder_filter_query = None                         # force full recompute
    s._finder_refilter()
    assert s.finder_matches == incremental


def test_backspace_rewidens(w):
    s, _ = _viewer(w, ["top.clk", "top.data"])
    s.open_finder()
    _type(s, "clkz")
    assert s.finder_matches == []
    s.finder_handle_key(127)                              # backspace → "clk"
    assert len(s.finder_matches) == 1


def test_enter_adds_and_jumps(w):
    s, order = _viewer(w, ["top.clk", "top.data"])
    s.open_finder()
    _type(s, "data")
    assert s.finder_handle_key(10) is False               # Enter closes
    assert order[1] in s.display_order
    assert s.view_rows[s.sig_offset + s.focus_idx] == order[1]
    assert any("Added 1" in m for m in s.messages)


def test_enter_on_displayed_signal_just_jumps(w):
    s, order = _viewer(w, ["top.clk", "top.data"], displayed=["top.clk", "top.data"])
    s.open_finder()
    _type(s, "data")
    s.finder_handle_key(10)
    assert s.display_order.count(order[1]) == 1           # no duplicate
    assert s.messages == []                               # nothing was added
    assert s.view_rows[s.sig_offset + s.focus_idx] == order[1]


def test_tab_marks_multi_select(w):
    s, order = _viewer(w, ["cpu.wr_en", "cpu.rd_en", "top.clk"])
    s.open_finder()
    _type(s, "en")
    s.finder_handle_key(ord('\t'))                        # mark 1st match
    s.finder_handle_key(ord('\t'))                        # mark 2nd match
    s.finder_handle_key(10)
    assert len(s.display_order) == 2
    assert set(s.display_order) == {order[0], order[1]}
    assert any("Added 2" in m for m in s.messages)


def test_esc_cancels_without_adding(w):
    s, _ = _viewer(w, ["top.clk"])
    s.open_finder()
    _type(s, "clk")
    assert s.finder_handle_key(27) is False
    assert s.display_order == []


def test_commit_seeds_next_search(w):
    s, order = _viewer(w, ["top.clk", "top.alu.clk", "top.data"])
    s.open_finder()
    _type(s, "clk")
    s.finder_handle_key(ord('\t'))
    s.finder_handle_key(ord('\t'))
    s.finder_handle_key(10)
    assert s.search_term == "clk"
    # both displayed clk signals are cycle targets for `n`
    assert len(s.search_results) == 2
