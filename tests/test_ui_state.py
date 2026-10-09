"""Viewer state handling without a terminal: Esc cancels rather than quits,
group undo restores the row order, tree add/remove keep the display set in
step."""


# ── UI state (no terminal) ───────────────────────────────────────────

class _Stub:
    def __init__(self, **kw):
        self.messages = []
        self.__dict__.update(kw)

    def _show_message(self, m):
        self.messages.append(m)


def test_escape_cancels_instead_of_quitting(w):
    from alwavelets.ui_keymap import KeymapMixin
    st = _Stub(_pending_g=True, _visual_mode=False, selected_signals={"a"})
    assert KeymapMixin._kb_escape(st) != 'QUIT'
    assert st._pending_g is False and st.selected_signals == {"a"}
    KeymapMixin._kb_escape(st)
    assert st.selected_signals == set()


def test_group_undo_restores_row_order(w):
    st = _Stub(groups=[], display_order=["a", "b", "c"], synced=0)
    st._sync_display_set = lambda: st.__dict__.__setitem__("synced", st.synced + 1)
    st._reindex_groups = lambda: None
    st._rebuild_view_rows = lambda: None
    undo = w.Alwavelets._undo_groups_snapshot(st)
    st.groups = [{"name": "g", "members": {"c", "a"}, "collapsed": False, "parent": None}]
    st.display_order = ["c", "a", "b", "new"]
    undo()
    assert st.groups == [] and st.display_order == ["a", "b", "c"] and st.synced == 1


def test_tree_add_remove_scope_uses_set(w):
    from alwavelets.ui_tree import TreePanelMixin
    from alwavelets.parsers import ScopeNode
    root = ScopeNode("top", "top")
    root.signal_ids = ["a", "b"]
    child = ScopeNode("sub", "top.sub")
    child.signal_ids = ["c"]
    root.children["sub"] = child

    class _Tree(TreePanelMixin, _Stub):
        def _sync_display_set(self):
            self._display_order_set = set(self.display_order)
    st = _Tree(display_order=["b", "z"], _display_order_set=set())
    st._tree_add_scope_signals(root)
    assert st.display_order == ["b", "z", "a", "c"]
    st._tree_remove_scope_signals(root)
    assert st.display_order == ["z"] and st._display_order_set == {"z"}
