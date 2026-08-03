"""ISA-debug and analysis actions for Alwavelets: signal tagging, instruction
trace, annotations, formal-CEX, power, and bus-transaction decode."""
import curses
import os
import re

from .core import *  # noqa: F401,F403
from .rendering import *  # noqa: F401,F403
from .rendering import _update_wave_chars  # noqa: F401
from .decoders import *  # noqa: F401,F403
from .parsers import *  # noqa: F401,F403
from .analysis import *  # noqa: F401,F403
from .persistence import *  # noqa: F401,F403
from .tcl import TclEngine  # noqa: F401
from .manual import AlwaveletsManual  # noqa: F401


# ══════════════════════════════════════════════════════════════
#  CURSES VIEWER
# ══════════════════════════════════════════════════════════════



# ══════════════════════════════════════════════════════════════
#  PERSISTENCE  (tags, annotations, bookmarks -> alwavelets/persistence.py)
# ══════════════════════════════════════════════════════════════

from .persistence import *  # noqa: F401,F403


# ══════════════════════════════════════════════════════════════
#  ANALYSIS  (FormalAnalyzer, PowerEstimator -> alwavelets/analysis.py)
# ══════════════════════════════════════════════════════════════

from .analysis import *  # noqa: F401,F403


# ══════════════════════════════════════════════════════════════

from .manual import AlwaveletsManual  # noqa: F401  (kept importable here)


class DebugActionsMixin:
    def _tag_signal_at_cursor(self):
        """Tag the signal at tree cursor with an ISA role."""
        n = len(self._tree_flat)
        if self.tree_cursor >= n:
            return
        kind, item, _, _, extra = self._tree_flat[self.tree_cursor]
        if kind != 'signal':
            self._show_message("Select a signal to tag (not a scope)")
            return
        sig = item
        # Show available roles
        roles = SignalTagStore.ROLES
        current_role = self.signal_tags.get_role(sig.full_name)
        prompt = f"Tag {sig.name}"
        if current_role:
            prompt += f" [{current_role}]"
        prompt += ": "
        role = self.prompt_input(prompt)
        if not role:
            return
        if role == '-':
            # Remove tag
            if current_role:
                del self.signal_tags.tags[current_role]
                del self.signal_tags.reverse[sig.full_name]
                self._show_message(f"Untagged {sig.name}")
            return
        if role not in roles:
            # Try fuzzy match
            matches = [r for r in roles if role in r]
            if len(matches) == 1:
                role = matches[0]
            elif matches:
                self._show_message(f"Ambiguous: {', '.join(matches[:5])}")
                return
            else:
                # Custom role
                pass
        self.signal_tags.set_tag(role, sig.full_name)
        self._show_message(f"Tagged: {sig.name} → {role}")

    def _load_tag_file(self):
        """Load ISA tags from JSON file."""
        fname = self.prompt_input("Tag file (.alwavelets_tags.json): ")
        if not fname:
            # Try default
            base = os.path.splitext(self.vcd._filename)[0]
            candidates = [
                base + '.alwavelets_tags.json',
                os.path.join(os.path.dirname(self.vcd._filename), '.alwavelets_tags.json'),
            ]
            for c in candidates:
                if os.path.isfile(c):
                    fname = c
                    break
        if not fname or not os.path.isfile(fname):
            self._show_message("No tag file found")
            return
        try:
            self.signal_tags = SignalTagStore.load(fname)
            self._show_message(f"Loaded {len(self.signal_tags.tags)} tags from {fname}")
        except Exception as e:
            self._show_message(f"Tag load error: {e}")

    def _save_tags(self):
        """Save current tags."""
        base = os.path.splitext(self.vcd._filename)[0]
        path = base + '.alwavelets_tags.json'
        try:
            self.signal_tags.save(path)
            self._show_message(f"Tags saved to {path}")
        except Exception as e:
            self._show_message(f"Save error: {e}")

    def _build_trace(self):
        """Build instruction trace from current tags."""
        if not self.signal_tags.tags:
            # Try auto-detect
            n = self.signal_tags.auto_detect_roles(self.vcd)
            if n == 0:
                self._show_message("No tagged signals. Use B→t to tag, or T to load tags.")
                return
            self._show_message(f"Auto-detected {n} signal roles")
        n = self.instr_trace.build(self.vcd, self.signal_tags, self.isa_decoder)
        self._show_message(f"Trace built: {n} instructions")

    def _trace_query_prompt(self):
        """Interactive trace query."""
        if not self.instr_trace.built:
            self._build_trace()
            if not self.instr_trace.built:
                return
        q = self.prompt_input("Trace> ")
        if not q:
            return

        # Built-in query shortcuts
        q_low = q.strip().lower()
        matches = None

        if q_low.startswith('mn ') or q_low.startswith('mnemonic '):
            pat = q.split(None, 1)[1]
            matches = self.instr_trace.find_mnemonic(pat)
        elif q_low.startswith('store'):
            parts = q.split()
            if len(parts) >= 3:
                lo = int(parts[1], 0)
                hi = int(parts[2], 0)
                matches = self.instr_trace.find_stores(lo, hi)
            else:
                matches = self.instr_trace.find_stores()
        elif q_low.startswith('reg '):
            parts = q.split()
            reg = int(parts[1]) if parts[1].isdigit() else RV32Decoder.REG_NAMES.index(parts[1]) if parts[1] in RV32Decoder.REG_NAMES else 0
            matches = self.instr_trace.find_reg_writes(reg)
        elif q_low.startswith('csr '):
            csr_name = q.split()[1]
            csr_addr = None
            for addr, name in CSR_NAMES.items():
                if name == csr_name:
                    csr_addr = addr; break
            if csr_addr is None:
                try: csr_addr = int(csr_name, 0)
                except ValueError: pass
            if csr_addr is not None:
                matches = self.instr_trace.find_csr_changes(csr_addr)
        elif q_low == 'stall' or q_low == 'stalled':
            matches = self.instr_trace.find_stalled()
        elif q_low == 'summary':
            self._show_message(self.instr_trace.summary())
            return
        elif q_low.startswith('thread '):
            parts = q.split()
            hart = int(parts[1])
            thread_trace = self.instr_trace.extract_thread_trace(hart)
            n = len(thread_trace)
            if n == 0:
                self._show_message(f"Hart {hart}: no instructions")
            else:
                ipc = self.instr_trace.hart_ipc(hart)
                self.markers.clear()
                for e in thread_trace[:100]:
                    self.markers.append(Marker(e['time'], f"H{hart}:{e['decoded']['asm'][:15]}"))
                self.cursor_time = thread_trace[0]['time']
                self._ensure_cursor_visible()
                self._show_message(f"Hart {hart}: {n} instrs, IPC={ipc:.3f} ({min(n,100)} markers)")
            return
        elif q_low == 'ipc':
            ipcs = self.instr_trace.all_hart_ipc(self.time_start, self.time_end)
            parts = [f"H{h}:{v:.3f}" for h, v in sorted(ipcs.items())]
            self._show_message("IPC: " + " ".join(parts[:8]))
            return
        elif q_low == 'fairness':
            stats = self.instr_trace.fairness_analysis()
            msgs = []
            for h, s in sorted(stats.items()):
                mark = "⚠" if s['starved'] else "✓"
                msgs.append(f"H{h}:{mark}avg={s['avg_gap']:.0f} max={s['max_gap']}")
            self._show_message(" ".join(msgs[:6]))
            # Place markers at starvation events
            events = self.instr_trace.find_starvation_events()
            if events:
                for ev in events[:50]:
                    self.markers.append(Marker(ev['time'], f"H{ev['hart_id']}:starved×{ev['ratio']:.1f}"))
                self._show_message(f"Fairness: {len(events)} starvation events marked")
            return
        elif q_low.startswith('diverge '):
            parts = q.split()
            if len(parts) >= 3:
                ha, hb = int(parts[1]), int(parts[2])
                after = int(parts[3]) if len(parts) > 3 else 0
                result = self.instr_trace.thread_divergence(ha, hb, after)
                if result['identical']:
                    self._show_message(f"H{ha} vs H{hb}: {result['reason']}")
                else:
                    self.cursor_time = result['diverge_time']
                    self._ensure_cursor_visible()
                    msg = f"Diverge at t={result['diverge_time']}: {result['reason']}"
                    self._show_message(msg)
            else:
                self._show_message("Usage: diverge <hart_a> <hart_b> [after_time]")
            return
        elif q_low.startswith('contention'):
            parts = q.split()
            lo = int(parts[1], 0) if len(parts) > 1 else 0
            hi = int(parts[2], 0) if len(parts) > 2 else 0xFFFFFFFF
            results = self.instr_trace.shared_resource_contention(lo, hi)
            if not results:
                self._show_message("No contention found")
            else:
                for c in results[:3]:
                    hazards = []
                    if c['has_waw']: hazards.append('WAW')
                    if c['has_raw']: hazards.append('RAW')
                    if c['has_war']: hazards.append('WAR')
                    hz = '+'.join(hazards) if hazards else 'shared'
                    self._show_message(f"@0x{c['addr']:x}: {c['n_harts']} harts ({hz}), {len(c['accesses'])} accesses")
                # Mark first contention
                if results[0]['accesses']:
                    self.cursor_time = results[0]['accesses'][0]['time']
                    self._ensure_cursor_visible()
            return
        elif q_low == 'heatmap':
            hm = self.instr_trace.scheduling_heatmap(self.time_start, self.time_end, self.wave_width)
            if hm and hm['harts']:
                self._show_message(f"Heatmap: {len(hm['harts'])} harts, {len(hm['heatmap'])} buckets")
                self._heatmap_data = hm
            return
        elif q_low.startswith('regs '):
            parts = q.split()
            hart = int(parts[1]) if len(parts) > 1 else 0
            regs = self.instr_trace.register_snapshot(hart, self.cursor_time)
            lines = []
            for i in range(0, 32, 4):
                row = []
                for j in range(4):
                    r = i + j
                    v = regs[r]
                    name = RV32Decoder.REG_NAMES[r]
                    val = f"{v:08x}" if v is not None else "--------"
                    row.append(f"{name}={val}")
                lines.append(" ".join(row))
            self._show_message(lines[0])  # show first row, rest need panel
            return
        elif q_low == 'harts':
            harts = self.instr_trace.get_active_harts()
            self._show_message(f"Active harts: {harts}")
            return
        elif q_low.startswith('lambda'):
            matches, err = self.instr_trace.query_str(q)
            if err:
                self._show_message(f"Error: {err}")
                return
        else:
            self._show_message("Commands: mn <pat>, store [lo hi], reg <n>, csr <name>, stall, summary, lambda e: ...")
            return

        if matches is not None:
            n = len(matches)
            if n == 0:
                self._show_message("No matches")
            elif n == 1:
                e = matches[0]
                self.cursor_time = e['time']
                self._ensure_cursor_visible()
                self._show_message(f"t={e['time']} {e['decoded']['asm']}")
            else:
                # Place markers at all matches, jump to first
                self.markers.clear()
                for e in matches[:100]:
                    self.markers.append(Marker(e['time'], e['decoded']['asm'][:20]))
                self.cursor_time = matches[0]['time']
                self._ensure_cursor_visible()
                self._show_message(f"{n} matches ({min(n,100)} markers placed). < > to navigate.")




    # ══ FEATURE 1: ANNOTATIONS ══

    def add_annotation_prompt(self):
        text = self.prompt_input("Annotation: ")
        if not text: return
        category = 'note'
        if text.startswith('bug:'): category='bug'; text=text[4:].strip()
        elif text.startswith('todo:'): category='todo'; text=text[5:].strip()
        elif text.startswith('warn:'): category='warning'; text=text[5:].strip()
        sig_name = None
        sid = self.focused_signal_id()
        if sid:
            sig = self.get_signal(sid)
            if sig: sig_name = sig.full_name
        ann = WaveformAnnotation(
            time_start=self.cursor_time, signal=sig_name,
            text=text, category=category,
        )
        self.annotations.add(ann)
        icon = AnnotationStore.CATEGORY_MARKERS.get(category, ('*', 7))[0]
        self._show_message(f"{icon} Annotation @t={self.cursor_time}: {text[:40]}")

    def save_annotations(self):
        path = self.annotations.path or self.annotations.default_path(self.vcd._filename)
        if self.annotations.save(path):
            self._show_message(f"Saved {len(self.annotations.annotations)} annotations")
        else:
            self._show_message("Failed to save annotations")

    def list_annotations(self):
        n = len(self.annotations.annotations)
        if n == 0:
            self._show_message("No annotations")
            return
        self.markers.clear()
        for a in self.annotations.annotations:
            icon = AnnotationStore.CATEGORY_MARKERS.get(a.category, ('*',))[0]
            self.markers.append(Marker(a.time_start, f"{icon} {a.text[:20]}"))
        self._show_message(f"{n} annotations → markers, use <> to navigate")

    def toggle_annotations_display(self):
        self.show_annotations = not self.show_annotations
        self._show_message(f"Annotations: {'visible' if self.show_annotations else 'hidden'}")

    def draw_annotation_overlays(self):
        if not self.show_annotations or not self.annotations.annotations:
            return
        for a in self.annotations.in_range(self.time_start, self.time_end):
            col = self.time_to_col(a.time_start)
            ac = self.name_width + 1 + col
            if 0 <= col < self.wave_width and ac < self.width:
                icon, color = AnnotationStore.CATEGORY_MARKERS.get(a.category, ('*', 7))
                try:
                    self.stdscr.addch(2, ac, icon, curses.color_pair(self.C_MARKER) | curses.A_BOLD)
                except curses.error:
                    pass

    # ══ FEATURE 2: FORMAL CEX ══

    def analyze_formal_cex(self):
        if not self.formal_analyzer:
            self.formal_analyzer = FormalAnalyzer(self.vcd)
        asserts = self.formal_analyzer.detect_assertion_signals()
        if not asserts:
            self._show_message("No assertion signals found")
            return
        t_fail, sig_fail = self.formal_analyzer.find_failure_time()
        if t_fail is None:
            self._show_message(f"{len(asserts)} assertions, no failures detected")
            return
        self.cursor_time = t_fail
        self._ensure_cursor_visible()
        coi = self.formal_analyzer.walk_cone_of_influence(sig_fail, t_fail)
        added = 0
        for c in coi[:15]:
            csig = c['signal']
            csid = None
            for sid, s in self.vcd.signals.items():
                if s.full_name == csig.full_name:
                    csid = sid; break
            if csid and csid not in self._display_order_set:
                self.display_order.insert(0, csid)
                added += 1
        self._sync_display_set()
        self.annotations.add(WaveformAnnotation(
            time_start=t_fail, signal=sig_fail.full_name,
            text=f"ASSERTION FAILURE: {sig_fail.name}", category='bug',
        ))
        self._show_message(f"! Failure @t={t_fail} on {sig_fail.name}. {added} COI signals added.")

    # ══ FEATURE 3: POWER ══

    def run_power_analysis(self):
        if not self.power_est:
            self.power_est = PowerEstimator(self.vcd)
        self.power_est.compute(self.time_start, self.time_end)
        hot = self.power_est.hot_scopes(5)
        if not hot:
            self._show_message("No activity in window")
            return
        parts = [f"{s.split('.')[-1][:10]}={a:.0f}" for s, a in hot]
        self._show_message("Hot: " + " ".join(parts))

    # ══ FEATURE 4: BUS DECODE ══

    def decode_axi_transactions(self):
        axi_tags = {k: v for k, v in self.signal_tags.tags.items() if k.startswith('axi.')}
        if not axi_tags:
            self._auto_tag_axi()
            axi_tags = {k: v for k, v in self.signal_tags.tags.items() if k.startswith('axi.')}
        if not axi_tags:
            self._show_message("No AXI tags. Tag axi.aw.valid, axi.aw.addr, ...")
            return
        decoder = AXIDecoder(self.vcd, axi_tags)
        txns = decoder.decode_all()
        self.bus_transactions = txns
        if not txns:
            self._show_message("No AXI transactions decoded")
        else:
            self.markers.clear()
            for txn in txns[:50]:
                label = f"{txn.type[0].upper()} @0x{txn.addr:x}" if txn.addr is not None else txn.type
                if txn.resp: label += f" [{txn.resp}]"
                self.markers.append(Marker(txn.t_start, label))
            self._show_message(f"{len(txns)} AXI txns ({min(len(txns),50)} markers)")

    def decode_wishbone(self):
        wb_tags = {k: v for k, v in self.signal_tags.tags.items() if k.startswith('wb.')}
        if not wb_tags:
            self._auto_tag_wishbone()
            wb_tags = {k: v for k, v in self.signal_tags.tags.items() if k.startswith('wb.')}
        if not wb_tags:
            self._show_message("No Wishbone tags. Tag wb.cyc, wb.stb, wb.addr, wb.ack")
            return
        decoder = WishboneDecoder(self.vcd, wb_tags)
        txns = decoder.decode_all()
        self.bus_transactions = txns
        if txns:
            self.markers.clear()
            for txn in txns[:50]:
                label = f"{txn.type[0].upper()} @0x{txn.addr:x}" if txn.addr is not None else txn.type
                self.markers.append(Marker(txn.t_start, label))
            self._show_message(f"{len(txns)} Wishbone txns")
        else:
            self._show_message("No Wishbone transactions")

    def decode_ahb(self):
        ahb_tags = {k: v for k, v in self.signal_tags.tags.items() if k.startswith('ahb.')}
        if not ahb_tags:
            self._show_message("No AHB tags. Tag ahb.hready, ahb.htrans, ahb.haddr, ...")
            return
        decoder = AHBDecoder(self.vcd, ahb_tags)
        txns = decoder.decode_all()
        self.bus_transactions = txns
        if txns:
            self.markers.clear()
            for txn in txns[:50]:
                label = f"{txn.type[0].upper()} @0x{txn.addr:x}" if txn.addr is not None else txn.type
                self.markers.append(Marker(txn.t_start, label))
            self._show_message(f"{len(txns)} AHB txns")
        else:
            self._show_message("No AHB transactions")

    def _auto_tag_axi(self):
        patterns = {
            'axi.aw.valid': r'awvalid|aw_valid',
            'axi.aw.ready': r'awready|aw_ready',
            'axi.aw.addr':  r'awaddr|aw_addr',
            'axi.aw.len':   r'awlen|aw_len',
            'axi.aw.burst': r'awburst|aw_burst',
            'axi.aw.id':    r'awid|aw_id',
            'axi.w.valid':  r'^wvalid|_wvalid|w_valid',
            'axi.w.ready':  r'^wready|_wready|w_ready',
            'axi.w.data':   r'^wdata|_wdata|w_data',
            'axi.w.last':   r'wlast|w_last',
            'axi.b.valid':  r'bvalid|b_valid',
            'axi.b.ready':  r'bready|b_ready',
            'axi.b.resp':   r'bresp|b_resp',
            'axi.ar.valid': r'arvalid|ar_valid',
            'axi.ar.ready': r'arready|ar_ready',
            'axi.ar.addr':  r'araddr|ar_addr',
            'axi.ar.len':   r'arlen|ar_len',
            'axi.r.valid':  r'^rvalid|_rvalid|r_valid',
            'axi.r.ready':  r'^rready|_rready|r_ready',
            'axi.r.data':   r'^rdata|_rdata|r_data',
            'axi.r.resp':   r'rresp|r_resp',
        }
        found = 0
        for sid, sig in self.vcd.signals.items():
            for role, pat in patterns.items():
                if role not in self.signal_tags.tags:
                    if re.search(pat, sig.name, re.I):
                        self.signal_tags.set_tag(role, sig.full_name)
                        found += 1
        return found

    def _auto_tag_wishbone(self):
        patterns = {
            'wb.cyc':   r'cyc',
            'wb.stb':   r'^stb|_stb',
            'wb.we':    r'^we|_we',
            'wb.addr':  r'adr|addr',
            'wb.dat_i': r'dat_i|i_dat',
            'wb.dat_o': r'dat_o|o_dat',
            'wb.ack':   r'ack',
        }
        found = 0
        for sid, sig in self.vcd.signals.items():
            for role, pat in patterns.items():
                if role not in self.signal_tags.tags:
                    if re.search(pat, sig.name, re.I):
                        self.signal_tags.set_tag(role, sig.full_name)
                        found += 1
        return found

    # ══ FEATURE 6: BOOKMARKS ══
