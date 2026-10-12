"""Fuzzy signal finder: an fzf-style overlay for locating signals by name.

`/` opens it. Typing filters *every* signal in the file live (not just the
displayed ones): space-separated terms are AND-ed, each term matches as a
substring or, failing that, a subsequence of the full hierarchical name.
Enter adds the highlighted signal (or all Tab-marked ones) to the display and
jumps to it; a signal that is already displayed is just jumped to. Pure
stdlib. Large catalogs are ranked in a cancellable background worker with
paged results; small in-memory traces filter immediately.
"""
import curses

# Characters that start a "word" inside a hierarchical name. A term matching
# right after one of these (or at position 0) is a boundary match and ranks
# above a mid-word match: `wr` should prefer `cpu.wr_en` over `cpu.lwrite`.
_BOUNDARY = '._[/ '


def _term_score(term, text):
    """Score one lowercase term against one lowercase name, or None if the
    term doesn't occur as a subsequence. Higher is better."""
    pos = text.find(term)
    if pos >= 0:
        # Substring match — the common case. Rank: word boundary, then
        # leaf-name (after the last '.'), then earlier-in-string.
        score = 100
        if pos == 0 or text[pos - 1] in _BOUNDARY:
            score += 40
        if pos > text.rfind('.'):
            score += 30
        return score - min(pos, 20)

    # Subsequence fallback: `cwe` still finds `cpu.wr_en`. Greedy leftmost
    # walk, rewarding consecutive runs and word-boundary hits.
    score = consec = 0
    prev = -2
    i = 0
    for ch in term:
        i = text.find(ch, i)
        if i < 0:
            return None
        if i == prev + 1:
            consec += 1
            score += 4 + consec
        else:
            consec = 0
            score += 1
        if i == 0 or text[i - 1] in _BOUNDARY:
            score += 10
        prev = i
        i += 1
    return score


def fuzzy_match(query, text):
    """Score a whole query (lowercase, whitespace-separated AND terms)
    against one lowercase name. Returns an int score, or None if any term
    fails to match. An empty query matches everything with score 0."""
    total = 0
    for term in query.split():
        s = _term_score(term, text)
        if s is None:
            return None
        total += s
    # Shorter names win ties, so `top.clk` outranks `top.alu.clk_gated`.
    return total - min(len(text), 60) // 6


def match_positions(query, text):
    """Character indices of `query`'s match in `text`, for highlighting the
    visible rows. Mirrors _term_score's greedy choices; both lowercase."""
    pos = set()
    for term in query.split():
        i = text.find(term)
        if i >= 0:
            pos.update(range(i, i + len(term)))
            continue
        i = 0
        for ch in term:
            i = text.find(ch, i)
            if i < 0:
                break
            pos.add(i)
            i += 1
    return pos


class FinderMixin:
    """Modal fuzzy-finder overlay (state lives on the viewer; see run())."""

    def open_finder(self):
        from .finder_search import FinderCandidates, FinderSearch
        FinderMixin.close_finder(self)
        cands = FinderCandidates(self.vcd)
        # Small in-memory traces retain immediate feedback. Wide or disk-backed
        # traces never duplicate the whole name set on the UI thread.
        asynchronous = hasattr(self.vcd, '_store') or len(cands) > 4096
        self._finder_cands = cands if asynchronous else list(cands)
        self._finder_worker = FinderSearch(self.vcd) if asynchronous else None
        self._finder_busy = False
        self.finder_query = ""
        self._finder_filter_query = None   # query finder_matches corresponds to
        self.finder_matches = []           # candidate indices, best first
        self.finder_marked = []            # Tab-marked sids, in mark order
        self._finder_marked_set = set()
        self.finder_sel = 0                # highlight, as index into matches
        self.finder_scroll = 0
        self._finder_refilter()
        self.show_finder = True

    def _finder_refilter(self):
        q = ' '.join(self.finder_query.lower().split())
        prev = self._finder_filter_query
        cands = self._finder_cands
        worker = getattr(self, '_finder_worker', None)
        if worker is not None:
            from .results import close_results
            close_results(self.finder_matches)
            worker.invalidate()
            self.finder_matches = [] if q else range(len(cands))
            self._finder_busy = bool(q)
            if q:
                worker.request(cands, q)
            self._finder_filter_query = q
            self.finder_sel = self.finder_scroll = 0
            return
        if not q:
            self.finder_matches = list(range(len(cands)))
        else:
            # Extending a query only ever narrows it, so re-rank just the
            # previous matches instead of the whole file.
            if prev and q.startswith(prev):
                pool = self.finder_matches
            else:
                pool = range(len(cands))
            scored = []
            for i in pool:
                s = fuzzy_match(q, cands[i][2])
                if s is not None:
                    scored.append((-s, i))
            scored.sort()
            self.finder_matches = [i for _, i in scored]
        self._finder_filter_query = q
        self.finder_sel = 0
        self.finder_scroll = 0

    def finder_handle_key(self, key):
        """Handle a key in finder mode. Returns False to close the overlay."""
        FinderMixin.poll_finder(self)
        if key == 27:                                  # Esc: cancel
            FinderMixin.close_finder(self)
            return False
        if key in (10, curses.KEY_ENTER):              # Enter: commit
            if getattr(self, '_finder_busy', False) and not self.finder_marked:
                return True
            self._finder_commit()
            FinderMixin.close_finder(self)
            return False

        n = len(self.finder_matches)
        page = max(1, self.height - 5)
        if key == ord('\t'):                           # Tab: mark + advance
            if n and self.finder_sel < n:
                sid = self._finder_cands[self.finder_matches[self.finder_sel]][0]
                if sid in self._finder_marked_set:
                    self._finder_marked_set.discard(sid)
                    self.finder_marked.remove(sid)
                else:
                    self._finder_marked_set.add(sid)
                    self.finder_marked.append(sid)
                if self.finder_sel < n - 1:
                    self.finder_sel += 1
        elif key in (curses.KEY_DOWN, 14):             # ↓ / Ctrl-N
            if n:
                self.finder_sel = min(self.finder_sel + 1, n - 1)
        elif key in (curses.KEY_UP, 16):               # ↑ / Ctrl-P
            self.finder_sel = max(self.finder_sel - 1, 0)
        elif key == curses.KEY_HOME:
            self.finder_sel = 0
        elif key == curses.KEY_END:
            self.finder_sel = max(0, n - 1)
        elif key == curses.KEY_NPAGE:
            if n:
                self.finder_sel = min(self.finder_sel + page, n - 1)
        elif key == curses.KEY_PPAGE:
            self.finder_sel = max(self.finder_sel - page, 0)
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            if self.finder_query:
                self.finder_query = self.finder_query[:-1]
                self._finder_refilter()
        elif key == 21:                                # Ctrl-U: clear query
            if self.finder_query:
                self.finder_query = ""
                self._finder_refilter()
        elif key == 23:                                # Ctrl-W: delete word
            q = self.finder_query.rstrip()
            cut = q.rfind(' ')
            self.finder_query = q[:cut + 1] if cut >= 0 else ""
            self._finder_refilter()
        elif 32 <= key <= 126:                         # printable → query
            self.finder_query += chr(key)
            self._finder_refilter()
        return True

    def poll_finder(self):
        changed = FinderMixin.poll_finder_navigation(self)
        worker = getattr(self, '_finder_worker', None)
        if worker is None or not getattr(self, '_finder_busy', False):
            return changed
        try:
            matches = worker.poll()
        except Exception as exc:
            self._finder_busy = False
            self._show_message('Signal search failed: ' + str(exc))
            return True
        if matches is None:
            return changed
        self.finder_matches = matches
        self._finder_busy = False
        return True

    def close_finder_navigation(self):
        from .results import close_results
        worker = getattr(self, '_finder_next_worker', None)
        if worker is not None:
            worker.close()
        self._finder_next_worker = None
        self._finder_next_order = None
        self._finder_next_jump = False
        close_results(getattr(self, 'search_results', ()))
        self.search_results = []

    def poll_finder_navigation(self):
        worker = getattr(self, '_finder_next_worker', None)
        if worker is None:
            return False
        try:
            matches = worker.poll()
        except Exception as exc:
            FinderMixin.close_finder_navigation(self)
            self._show_message('Signal search failed: ' + str(exc))
            return True
        if matches is None:
            return False
        worker.close()
        self._finder_next_worker = None
        if self.display_order == self._finder_next_order:
            self.search_results = matches
            if self._finder_next_jump:
                self.next_search_result()
        else:
            matches.close()
        self._finder_next_order = None
        self._finder_next_jump = False
        return True

    def close_finder(self):
        from .results import close_results
        worker = getattr(self, '_finder_worker', None)
        if worker is not None:
            worker.close()
            self._finder_worker = None
        close_results(getattr(self, 'finder_matches', ()))
        self.finder_matches = []
        self._finder_busy = False
        self.show_finder = False

    def _finder_commit(self):
        """Add the marked (or highlighted) signals to the display, jump to
        the first one, and seed `n` (next match) with the query."""
        chosen = list(self.finder_marked)
        if not chosen:
            m = self.finder_matches
            if not m or self.finder_sel >= len(m):
                return
            chosen = [self._finder_cands[m[self.finder_sel]][0]]

        added_sids = [sid for sid in chosen if sid not in self._display_order_set]
        for sid in added_sids:
            self.display_order.append(sid)
            self._display_order_set.add(sid)
        added = len(added_sids)
        if added:
            self._invalidate_scope_cache()

            def _undo(sids=added_sids):
                gone = set(sids)
                from .display_order import remove_display_ids
                remove_display_ids(self.display_order, gone)
            self.push_undo(f"add {added} signal{'s' if added != 1 else ''}", _undo)

        # Jump to the first chosen signal, unfolding its group if collapsed.
        target = chosen[0]
        gname = self._member_group.get(target)
        if gname is not None and self._group_by_name.get(gname, {}).get('collapsed'):
            self.toggle_group_collapse(gname)
        self._rebuild_view_rows()
        try:
            row = self.view_rows.index(target)
        except ValueError:
            row = None
        if row is not None:
            self.sig_offset = max(0, row - max(1, self.visible_signals) // 2)
            self.focus_idx = row - self.sig_offset

        # Let `n` keep cycling this query's matches among displayed signals.
        q = ' '.join(self.finder_query.lower().split())
        FinderMixin.close_finder_navigation(self)
        self.search_term = q
        self.search_results = []
        if q and len(self.display_order) > 4096:
            from .finder_search import FinderCandidates, FinderSearch
            from .display_order import copy_order
            self._finder_next_order = copy_order(self.display_order)
            self._finder_next_worker = FinderSearch(self.vcd)
            self._finder_next_worker.request(FinderCandidates(self.vcd, self._finder_next_order,
                                                             dict(getattr(self, 'virtual_signals', {}))), q,
                                             declaration_order=True)
            self.search_idx = 0
        elif q:
            for i, sid in enumerate(self.display_order):
                sig = self.get_signal(sid)
                if sig and fuzzy_match(q, sig.full_name.lower()) is not None:
                    self.search_results.append(i)
            self.search_idx = 0
        if added:
            self._show_message(f"Added {added} signal{'s' if added != 1 else ''}")

    # ── drawing ──────────────────────────────────────────────────────

    def draw_finder(self):
        self.stdscr.erase()
        h, w = self.height, self.width
        put = self.stdscr.addstr

        title = (" ◆ Find Signal — type to filter · ↑↓ Navigate · "
                 "Tab Mark · Enter Add & Jump · Esc Cancel")
        try:
            put(0, 0, title[:w - 1].ljust(w - 1),
                curses.color_pair(self.C_HEADER) | curses.A_BOLD)
        except curses.error:
            pass

        # Query line with a block cursor.
        try:
            put(1, 0, " ❯ ", curses.color_pair(self.C_MENUKEY) | curses.A_BOLD)
            put(1, 3, (self.finder_query + "▌")[:max(0, w - 4)], curses.A_BOLD)
        except curses.error:
            pass

        n = len(self.finder_matches)
        marked = len(self.finder_marked)
        busy = getattr(self, '_finder_busy', False)
        counts = f"  {n}/{len(self._finder_cands)} signals" if not busy else "  searching…"
        if marked:
            counts += f" · {marked} marked"
        try:
            put(2, 0, counts[:w - 1], curses.A_DIM)
        except curses.error:
            pass

        # Result list. Keep the highlight inside the window.
        top, vis = 3, max(1, h - 4)
        if self.finder_scroll > self.finder_sel:
            self.finder_scroll = self.finder_sel
        if self.finder_sel >= self.finder_scroll + vis:
            self.finder_scroll = self.finder_sel - vis + 1
        q = self._finder_filter_query or ""

        if n == 0:
            try:
                from .ui_chrome import elide
                put(top, 1, elide("Searching signals…" if busy else "No matching signals", w - 2), curses.A_DIM)
                if top + 1 < h - 1 and not busy:
                    put(top + 1, 1, elide("Ctrl-U Clear search · Esc Return", w - 2), curses.A_DIM)
            except curses.error:
                pass
        for i in range(min(vis, n - self.finder_scroll)):
            idx = self.finder_scroll + i
            sid, full, low, type_ch, size = \
                self._finder_cands[self.finder_matches[idx]]
            is_cur = (idx == self.finder_sel)
            mark = "▪" if sid in self._finder_marked_set else " "
            shown = "•" if sid in self._display_order_set else " "
            name = full if size == 1 else f"{full} [{size}]"
            line = f" {mark}{shown}{type_ch} {name}"
            if is_cur:
                base = curses.color_pair(self.C_SELECT) | curses.A_BOLD
            elif sid in self._finder_marked_set:
                base = curses.color_pair(self.C_HIGH)
            else:
                base = curses.A_NORMAL
            try:
                put(top + i, 0, line[:w - 1].ljust(w - 1), base)
                # Overdraw the matched characters so the query lights up.
                if q and not is_cur:
                    hl = curses.color_pair(self.C_MENUKEY) | curses.A_BOLD
                    for p in match_positions(q, low):
                        col = 5 + p          # name starts at column 5
                        if col < w - 1:
                            self.stdscr.addch(top + i, col, full[p], hl)
            except curses.error:
                pass

        status = (" • already displayed · ▪ marked │ "
                  "space AND-terms · Ctrl-U clear · Ctrl-W del word")
        try:
            put(h - 1, 0, status[:w - 1].ljust(w - 1),
                curses.color_pair(self.C_HEADER))
        except curses.error:
            pass
