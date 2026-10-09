"""Command-line and TCL execution for Alwavelets: the ':' command line, its
_cmd_* subcommands, TCL script execution, and translation/decode file loading."""
import fnmatch
import os

from .core import *  # noqa: F401,F403
from .rendering import *  # noqa: F401,F403
from .rendering import _update_wave_chars  # noqa: F401
from .decoders import *  # noqa: F401,F403
from .parsers import *  # noqa: F401,F403
from .analysis import *  # noqa: F401,F403
from .persistence import *  # noqa: F401,F403
from .tcl import TclEngine  # noqa: F401
from .manual import AlwaveletsManual  # noqa: F401


class CommandLineMixin:
    def do_tcl_execute(self):
        """Prompt for Tcl command and execute."""
        cmd = self.prompt_input("Tcl> ")
        if not cmd:
            return
        result = self.tcl.execute(cmd)
        if result:
            self._show_message(result[:self.width - 2])

    def do_command_line(self):
        """General command prompt (>) with built-in commands."""
        cmd = self.prompt_input("> ")
        if not cmd:
            return
        return self.execute_command(cmd)

    def command_verbs(self):
        """Names of every ';' command. Used by the control channel to describe
        itself to an agent, so the list can never drift from the dispatch."""
        return list(self._command_table("")) + ['quit', 'q', 'exit']

    def _command_table(self, args):
        """verb -> zero-arg callable, bound to `args`. Single source for both
        the interactive prompt and the agent control channel."""
        return {
            'help': lambda: self._cmd_help(),
            'zoom': lambda: self._cmd_zoom_range(args),
            'fit': lambda: self.zoom_fit_all(),
            'goto': lambda: self._cmd_goto_time(args),
            'find': lambda: self._cmd_find_sig(args),
            'add': lambda: self.add_signals(args.split()),
            'remove': lambda: self.remove_signals(args.split()),
            'rm': lambda: self.remove_signals(args.split()),
            'when': lambda: self._cmd_find_expr(args),
            'match': lambda: self._cmd_find_expr(args),
            'glitches': lambda: self._cmd_glitches(args),
            'glitch': lambda: self._cmd_glitches(args),
            'derive': lambda: self._cmd_derive(args),
            'group': lambda: self._cmd_group(args),
            'ungroup': lambda: self._cmd_ungroup(args),
            'follow': lambda: self.toggle_follow(),
            'loadgtkw': lambda: self._cmd_load_gtkw(args),
            'savegtkw': lambda: self._cmd_save_gtkw(args),
            'session': lambda: self._cmd_session(args),
            'stats': lambda: self._cmd_stats(),
            'measure': lambda: self._cmd_stats(),
            'radix': lambda: self._cmd_set_radix(args),
            'theme': lambda: self._cmd_load_theme(args),
            'export': lambda: self.do_export_menu(),
            'diff': lambda: self.load_diff_file() if not self.diff_vcd else self.clear_diff(),
            'grid': lambda: setattr(self, 'show_grid', (self.show_grid + 1) % 3),
            'spacing': lambda: self.cycle_signal_spacing(),
            'names': lambda: setattr(self, 'show_full_names', not self.show_full_names),
            'tree': lambda: setattr(self, 'show_tree', True),
            'tcl': lambda: self.do_tcl_execute(),
            'decode': lambda: self._cmd_load_decode_file(args),
            'eval': lambda: self._cmd_eval_tcl(args),
            'trace': lambda: self._build_trace(),
            'query': lambda: self._trace_query_prompt(),
            'tags': lambda: self._show_message(f"Tags: {len(self.signal_tags.tags)} ({', '.join(list(self.signal_tags.tags.keys())[:5])})"),
            'autotag': lambda: self._show_message(f"Auto-detected {self.signal_tags.auto_detect_roles(self.vcd)} roles"),
            'manual': lambda: setattr(self, 'show_manual', True),
            'man': lambda: setattr(self, 'show_manual', True),
            'annotate': lambda: self.add_annotation_prompt(),
            'annotations': lambda: self.list_annotations(),
            'saveann': lambda: self.save_annotations(),
            'hideann': lambda: self.toggle_annotations_display(),
            'formal': lambda: self.analyze_formal_cex(),
            'cex': lambda: self.analyze_formal_cex(),
            'power': lambda: self.run_power_analysis(),
            'axi': lambda: self.decode_axi_transactions(),
            'wb': lambda: self.decode_wishbone(),
            'ahb': lambda: self.decode_ahb(),
            'bookmark': lambda: self.create_bookmark(),
            'bm': lambda: self.create_bookmark(),
            'goto_bookmark': lambda: self.goto_bookmark(),
            'gb': lambda: self.goto_bookmark(),
            'bookmarks': lambda: self.list_bookmarks(),
            'savebm': lambda: self.save_bookmarks(),
            'savetags': lambda: self._save_tags(),
            'quit': lambda: None,  # handled below
            'q': lambda: None,
        }

    def execute_command(self, cmd):
        """Run one ';' command line. Shared by the interactive prompt and the
        agent control channel (control.py), so both see identical behaviour.
        Returns 'QUIT' to exit the viewer."""
        cmd = (cmd or "").strip()
        if not cmd:
            return
        parts = cmd.split(None, 1)
        verb = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""
        commands = self._command_table(args)

        if verb == 'help' or verb == '?':
            self._cmd_help()
        elif verb in ('quit', 'q', 'exit'):
            return 'QUIT'
        elif verb in commands:
            result = commands[verb]()
            if result:
                self._show_message(str(result)[:self.width - 2])
        elif verb in getattr(self.tcl, 'commands', {}):
            # A Tcl-engine command (marker, add_signal, …). Most of them print
            # nothing on success; say so instead of reporting them unknown —
            # an agent on the control channel reads this message as the verdict.
            result = self.tcl.execute(cmd)
            self._show_message((result or f"{verb}: ok")[:self.width - 2])
        else:
            # Fall through to Tcl for anything else (variables, scripts)
            result = self.tcl.execute(cmd)
            if result:
                self._show_message(result[:self.width - 2])
            else:
                self._show_message(f"Unknown command: {verb}. Type 'help' for list.")

    def _cmd_help(self):
        cmds = [
            "help      — Show this list",
            "zoom A B  — Set time range [A..B]",
            "fit       — Zoom to fit all",
            "goto T    — Move cursor to time T",
            "find PAT  — Focus a signal (among those already displayed)",
            "add PAT   — Add signals to the display ('add top.alu.*')",
            "remove PAT— Remove signals from the display (alias: rm)",
            "when EXPR — Jump to when a boolean expr holds (valid && state==3)",
            "glitches  — Jump to next x-hazard / glitch (zero-width / runt pulse)",
            "derive N=E— Add a virtual signal from an expr  (bus = (a1<<1)|a0)",
            "group NAME— Collapsible group from the V selection (Tab folds header)",
            "ungroup   — Remove the focused (or named) group",
            "follow    — Live-tail appended changes + auto-scroll (running sim)",
            "loadgtkw F— Apply a GTKWave .gtkw save (signals + radixes)",
            "savegtkw F— Write current view as a GTKWave .gtkw save file",
            "session   — Save session sidecar (auto-restores on reopen);",
            "            'session clear' forgets it, 'session load' re-applies",
            "stats     — Measure focused signal over A‥B or the visible window",
            "            (transitions, period/freq, duty; min/max for buses)",
            "radix R   — Set radix (bin/hex/dec/uns/f16/f32/f64)",
            "theme T   — Set theme or load JSON file",
            "export    — Export menu (CSV/SVG/PDF/screenshot)",
            "diff      — Toggle diff file comparison",
            "grid      — Toggle vertical grid",
            "spacing   — Cycle signal spacing",
            "names     — Toggle full/short signal names",
            "tree      — Open scope tree browser",
            "decode F  — Load decode/translation JSON file",
            "tcl       — Open Tcl command prompt",
            "eval CMD  — Execute Tcl command directly",
            "quit      — Exit viewer",
        ]
        self._show_message(" | ".join(["help","zoom","fit","goto","find","radix","theme","export","diff","grid","tree","tcl","quit"]))

    def _time_arg(self, text):
        """A time in ticks ('220000') or with a unit ('220ns', '1.5us') —
        the unit form is what a person or an agent naturally writes."""
        from .snapshot import parse_time
        return parse_time(self.vcd, text.strip())

    def _cmd_zoom_range(self, args):
        parts = args.split()
        if len(parts) >= 2:
            try:
                self.time_start = self._time_arg(parts[0])
                self.time_end = self._time_arg(parts[1])
            except ValueError:
                self._show_message("Usage: zoom <start> <end>   (ticks or e.g. 170ns 290ns)")
        else:
            self._show_message("Usage: zoom <start> <end>   (ticks or e.g. 170ns 290ns)")

    def _cmd_goto_time(self, args):
        try:
            t = self._time_arg(args)
            self.cursor_time = t
            self._ensure_cursor_visible()
        except ValueError:
            self._show_message("Usage: goto <time>   (ticks or e.g. 220ns)")

    def _cmd_find_sig(self, args):
        pattern = args.strip().lower()
        for sid in self.display_order:
            sig = self.get_signal(sid)
            if sig and (pattern in sig.name.lower() or pattern in sig.full_name.lower()):
                self._focus_on(sid)   # not sig_offset=i: that indexes view_rows
                self._show_message(f"Found: {sig.full_name}")
                return
        self._show_message(f"Not found: {args}")

    def _cmd_find_expr(self, args):
        """when <expr>: jump the cursor to the next time a multi-signal boolean
        expression holds (e.g. 'valid && state==3 && addr>=0x40'). Repeats with
        the last expression when called with no argument; wraps around the end."""
        from . import expr as _expr
        args = args.strip() or getattr(self, '_expr_last', '')
        if not args:
            self._show_message("Usage: when <expr>   e.g.  when valid && state==3")
            return
        self._expr_last = args
        try:
            t = _expr.find_expression(self.vcd, args, self.cursor_time + 1)
            wrapped = ""
            if t is None:                       # wrap to the start
                t = _expr.find_expression(self.vcd, args, 0)
                wrapped = " (wrapped)"
        except _expr.ExprError as e:
            self._show_message(f"Expr error: {e}")
            return
        if t is None:
            self._show_message(f"No match: {args}")
            return
        self.cursor_on = True
        self.cursor_time = t
        self._ensure_cursor_visible()
        self._show_message(f"⌖ {self.vcd.time_to_str(t)}: {args}{wrapped}")

    def _cmd_glitches(self, args):
        """glitches [max_pulse]: scan displayed signals for x-hazards and
        glitches (zero-width / runt pulses) and jump the cursor to the next one
        after the cursor (wrapping). Optional max_pulse overrides the per-signal
        auto threshold (~1/3 of the detected clock period)."""
        from .analysis import find_anomalies
        sigs = [s for s in (self.get_signal(i) for i in self.display_order) if s]
        if not sigs:
            self._show_message("No signals displayed to scan")
            return
        mp = None
        if args.strip():
            try:
                mp = int(args.strip())
            except ValueError:
                pass
        evs = find_anomalies(sigs, 0, self.vcd.max_time, max_pulse=mp)
        if not evs:
            self._show_message("No glitches or x-hazards found")
            return
        nxt = next((e for e in evs if e[0] > self.cursor_time), evs[0])
        self.cursor_on = True
        self.cursor_time = nxt[0]
        self._ensure_cursor_visible()
        nx = sum(1 for e in evs if e[2] == "x")
        ng = len(evs) - nx
        name = nxt[1].rsplit(".", 1)[-1]
        self._show_message(
            f"⚠ {nx} x-event(s), {ng} glitch(es)  |  {self.vcd.time_to_str(nxt[0])}: "
            f"{name} {nxt[2]} ({nxt[3]})")

    def _cmd_derive(self, args):
        """derive <name> = <expr>: add a virtual signal computed from existing
        signals, e.g.  derive bus = (a3<<3)|(a2<<2)|(a1<<1)|a0  or
        derive busy = state==3 && valid. Supports + - * / % & | ^ << >> ~ and
        the comparison/logical operators."""
        from . import expr as _expr
        if "=" not in args:
            self._show_message("Usage: derive <name> = <expr>")
            return
        name, expr_str = args.split("=", 1)
        name, expr_str = name.strip(), expr_str.strip()
        if not name or not expr_str:
            self._show_message("Usage: derive <name> = <expr>")
            return
        try:
            sig = _expr.make_derived_signal(self.vcd, name, expr_str)
        except _expr.ExprError as e:
            self._show_message(f"Derive error: {e}")
            return
        sid = sig.identifier
        self.virtual_signals[sid] = sig
        pos = self.sig_offset + self.focus_idx + 1
        if sid in self.display_order:
            self.display_order.remove(sid)
        self.display_order.insert(min(pos, len(self.display_order)), sid)
        self._needs_layout = True
        self._show_message(f"Derived '{name}' ({sig.size}-bit, {sig.num_changes()} changes)")

    def _signals_matching(self, patterns):
        """Signal ids whose full or short name equals or globs any pattern, in
        trace order — the same matching `add` uses."""
        pats = [p.strip() for p in patterns if p and p.strip()]
        out = []
        for sid in self.vcd.signal_order:
            sig = self.vcd.signals.get(sid)
            if not sig:
                continue
            for p in pats:
                if (fnmatch.fnmatch(sig.full_name, p) or fnmatch.fnmatch(sig.name, p)
                        or p == sig.full_name or p == sig.name):
                    out.append(sid)
                    break
        return out

    def _cmd_group(self, args):
        """group <name>: collapse-group the visual-mode selection (or focused
        signal) under <name>. 'group A/B' nests group B under group A (nested
        headers indent; collapsing A hides B). Fold with Tab on a header."""
        words = args.split()
        name = words[0] if words else ""
        if not name:
            self._show_message("Usage: group <name> [signal|glob …] or <parent>/<name>  (or select rows with V first)")
            return
        parent = None
        if '/' in name:
            parent, _, name = name.rpartition('/')
            parent, name = parent.strip(), name.strip()
        if not name:
            self._show_message("Usage: group <name> or <parent>/<name>")
            return
        # Named members: 'group ptrs dut.rptr dut.wptr dut.rd_*' — what a script
        # or an agent writes, since neither has a V key. Anything named but not
        # yet on screen is added first, so the group is exactly what was asked.
        members = self._signals_matching(words[1:]) if len(words) > 1 else []
        if len(words) > 1 and not members:
            self._show_message("group: nothing matched %s" % " ".join(words[1:]))
            return
        if members:
            missing = [sid for sid in members if sid not in self._display_order_set]
            if missing:
                for sid in missing:
                    self.display_order.append(sid)
                    self._display_order_set.add(sid)
                self._sync_display_set()
        if not members:
            members = list(self.selected_signals) if self.selected_signals else []
        if not members:
            sid = self.focused_signal_id()
            if sid:
                members = [sid]
        if not members:
            self._show_message("Select signals first (V), then: group <name>")
            return
        self.push_undo(f"group '{name}'", self._undo_groups_snapshot())
        if self.create_group(name, members, parent=parent):
            self.selected_signals.clear()
            self._visual_mode = False
            self._needs_layout = True
            where = f" under '{parent}'" if parent else ""
            self._show_message(f"Grouped {len(members)} signals as '{name}'{where} (Tab folds)")
        else:
            self._show_message("Nothing to group (or nesting would cycle)")

    def _cmd_ungroup(self, args):
        """ungroup [name]: remove a group (default: the focused header's group)."""
        name = args.strip()
        if not name:
            tok = self.focused_token()
            if self.is_group_header(tok):
                name = tok[1]
        if name and name in self._group_by_name:
            self.push_undo(f"ungroup '{name}'", self._undo_groups_snapshot())
        if name and self.ungroup(name):
            self._needs_layout = True
            self._show_message(f"Ungrouped '{name}'")
        else:
            self._show_message("Usage: ungroup <name>  (or focus a group header)")

    def _cmd_stats(self):
        """Measure the focused signal over [A, B] (both cursors on) or the
        visible window, and flash a one-line timing/level summary."""
        from .analysis import measure_signal
        sid = self.focused_signal_id()
        sig = self.get_signal(sid) if sid else None
        if sig is None:
            self._show_message("stats: focus a signal first")
            return
        if self.cursor_on and self.cursor_b_on:
            t0, t1 = sorted((self.cursor_a, self.cursor_b))
            where = "A‥B"
        else:
            t0, t1 = self.time_start, self.time_end
            where = "view"
        m = measure_signal(sig, t0, t1)
        parts = [f"{m['transitions']} trans"]
        if m.get('period'):                    # absent, or 0 on a glitchy signal
            from .ui_drawing import _fmt_freq
            parts.append(f"period {self.format_ruler_time(m['period'])}"
                         f" ({_fmt_freq(1.0 / (m['period'] * self.tick_seconds()))})"
                         f" · {m['cycles']} cyc")
        if 'duty' in m:
            parts.append(f"duty {m['duty'] * 100:.1f}%")
        if 'min' in m:
            parts.append(f"min {self._fmt_num(float(m['min']))}"
                         f" max {self._fmt_num(float(m['max']))}")
        self._show_message(f"{sig.name} [{where}]: " + " · ".join(parts))

    def _cmd_session(self, args):
        a = args.strip().lower()
        if a in ('', 'save'):
            self.save_session()
        elif a in ('clear', 'off', 'rm', 'delete'):
            self.clear_session()
        elif a in ('load', 'restore'):
            if not self.restore_session():
                self._show_message("No saved session for this trace")
        else:
            self._show_message("Usage: session [save|load|clear]")

    def _cmd_load_gtkw(self, args):
        """loadgtkw <file>: apply a GTKWave save file's signal list + radixes."""
        from . import gtkw as _g
        path = args.strip()
        if not path or not os.path.isfile(path):
            self._show_message("Usage: loadgtkw <file.gtkw>")
            return
        try:
            data = _g.load_gtkw(path)
        except Exception as e:
            self._show_message(f"GTKW load error: {e}")
            return
        byfull, byname = {}, {}
        for sid, sig in self.vcd.signals.items():
            byfull.setdefault(sig.full_name, sid)
            byname.setdefault(sig.name, sid)
        order, applied = [], 0
        for name, radix in data['signals']:
            base = _g.base_name(name)
            sid = byfull.get(base) or byname.get(base) or byfull.get(name) or byname.get(name)
            if sid:
                order.append(sid)
                if radix is not None:
                    self.sig_radix[sid] = radix
                applied += 1
        if order:
            self.display_order = order
            self._sync_display_set()
            self._invalidate_scope_cache()
            self._rebuild_view_rows()
            self._needs_layout = True
        self._show_message(
            f"Loaded {applied}/{len(data['signals'])} signals from {os.path.basename(path)}")

    def _cmd_save_gtkw(self, args):
        """savegtkw [file]: write the current view as a GTKWave save file."""
        from . import gtkw as _g
        path = args.strip() or "/tmp/alwavelets_view.gtkw"
        sigs = []
        for sid in self.display_order:
            s = self.get_signal(sid)
            if s:
                sigs.append((s.full_name, self.get_signal_radix(sid), s.size))
        try:
            _g.save_gtkw(path, getattr(self.vcd, '_filename', ''), sigs,
                         self.time_start, self.time_end)
        except Exception as e:
            self._show_message(f"GTKW save error: {e}")
            return
        self._show_message(f"Saved {len(sigs)} signals → {path}")

    def _cmd_set_radix(self, args):
        r = args.strip().lower()
        for rx in Radix:
            if rx.value == r:
                self.radix = rx
                self._show_message(f"Radix: {r}")
                return
        self._show_message(f"Unknown radix: {r}. Options: {', '.join(rx.value for rx in Radix)}")

    def _cmd_load_theme(self, args):
        name = args.strip()
        if os.path.isfile(name):
            self.load_theme_file(name)
        elif name in self.THEMES:
            self.apply_theme(name)
            self._show_message(f"Theme: {name}")
        else:
            self._show_message(f"Themes: {', '.join(self.THEME_ORDER)}")

    def _cmd_load_decode_file(self, args):
        fname = args.strip()
        if fname and os.path.isfile(fname):
            try:
                self.translations = TranslationMap.load_file(fname)
                self._show_message(f"Decode loaded: {fname}")
            except Exception as e:
                self._show_message(f"Error: {e}")
        else:
            self._show_message(f"File not found: {fname}")

    def _cmd_eval_tcl(self, args):
        result = self.tcl.execute(args)
        if result:
            self._show_message(result[:self.width - 2])

    def do_tcl_file(self):
        """Load and execute a Tcl script file."""
        fname = self.prompt_input("Tcl script file: ")
        if not fname:
            return
        try:
            with open(fname) as f:
                script = f.read()
            result = self.tcl.execute(script)
            if result:
                self._show_message(result[:self.width - 2])
            else:
                self._show_message(f"Script executed: {fname}")
        except FileNotFoundError:
            self._show_message(f"File not found: {fname}")
        except Exception as e:
            self._show_message(f"Tcl error: {e}")

    # ── Decode / Translation File ──

    def do_load_translations(self):
        """Load a translation/decode map file (JSON: {signal: {value: name}})."""
        fname = self.prompt_input("Decode file (.json): ")
        if not fname:
            return
        try:
            self.translations = TranslationMap.load_file(fname)
            self._show_message(f"Loaded decode map: {fname}")
        except FileNotFoundError:
            self._show_message(f"File not found: {fname}")
        except Exception as e:
            self._show_message(f"Decode load error: {e}")

    # ── Edge-to-Edge Measurement ──
