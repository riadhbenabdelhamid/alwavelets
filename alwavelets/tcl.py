"""A small TCL-like scripting/command engine that drives a viewer instance
(passed in; duck-typed). Used by the viewer command line and :tcl execution."""
import fnmatch
import os
import re

from .core import Radix, TranslationMap, Marker


# ══════════════════════════════════════════════════════════════
#  TCL SCRIPTING ENGINE
# ══════════════════════════════════════════════════════════════

class TclEngine:
    """Lightweight Tcl-like command interpreter for viewer automation."""
    def __init__(self, viewer):
        self.viewer = viewer
        self.variables = {}
        self.commands = {
            'add_signal': self._cmd_add_signal,
            'remove_signal': self._cmd_remove_signal,
            'zoom': self._cmd_zoom,
            'zoom_fit': self._cmd_zoom_fit,
            'goto': self._cmd_goto,
            'cursor': self._cmd_cursor,
            'marker': self._cmd_marker,
            'radix': self._cmd_radix,
            'analog': self._cmd_analog,
            'set': self._cmd_set,
            'puts': self._cmd_puts,
            'signals': self._cmd_signals,
            'measure': self._cmd_measure,
            'export_csv': self._cmd_export_csv,
            'screenshot': self._cmd_screenshot,
            'translate': self._cmd_translate,
            'theme': self._cmd_theme,
            'expand': self._cmd_expand,
            'foreach': self._cmd_foreach,
        }
        self.output = []

    def execute(self, script):
        """Execute a multi-line Tcl-like script."""
        self.output = []
        lines = script.strip().split('\n')
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if not line or line.startswith('#'):
                i += 1
                continue
            # Handle foreach blocks
            if line.startswith('foreach '):
                block_lines = [line]
                brace_count = line.count('{') - line.count('}')
                while brace_count > 0 and i + 1 < len(lines):
                    i += 1
                    block_lines.append(lines[i])
                    brace_count += lines[i].count('{') - lines[i].count('}')
                # Keep the body's newlines: foreach splits it into lines.
                self._execute_line('\n'.join(block_lines))
            else:
                self._execute_line(line)
            i += 1
        return '\n'.join(self.output)

    def _execute_line(self, line):
        """Execute a single command line."""
        line = line.strip()
        if not line or line.startswith('#'):
            return
        # Parse first, then substitute $vars per argument. A brace-quoted
        # argument is left verbatim, as in Tcl — so a foreach body sees the
        # loop variable's value at each iteration, not the value it had when
        # the foreach line was read.
        tagged = self._parse_args_tagged(line)
        if not tagged:
            return
        cmd = self._subst(tagged[0][0])
        args = [text if braced else self._subst(text) for text, braced in tagged[1:]]
        if cmd in self.commands:
            try:
                self.commands[cmd](args)
            except Exception as e:
                self.output.append(f"Error in '{cmd}': {e}")
        else:
            self.output.append(f"Unknown command: {cmd}")

    def _subst(self, text):
        return re.sub(r'\$(\w+)', lambda m: self.variables.get(m.group(1), m.group(0)), text)

    def _parse_args(self, line):
        """Parse command line respecting braces and quotes."""
        return [text for text, _ in self._parse_args_tagged(line)]

    def _parse_args_tagged(self, line):
        """Like _parse_args, returning (text, brace_quoted) pairs."""
        parts = []
        i = 0
        ws = ' \t\n'
        while i < len(line):
            while i < len(line) and line[i] in ws:
                i += 1
            if i >= len(line):
                break
            if line[i] == '{':
                depth = 1
                j = i + 1
                while j < len(line) and depth > 0:
                    if line[j] == '{': depth += 1
                    elif line[j] == '}': depth -= 1
                    j += 1
                parts.append((line[i+1:j-1], True))
                i = j
            elif line[i] == '"':
                j = line.index('"', i + 1) if '"' in line[i+1:] else len(line)
                parts.append((line[i+1:j], False))
                i = j + 1
            else:
                j = i
                while j < len(line) and line[j] not in ws:
                    j += 1
                parts.append((line[i:j], False))
                i = j
        return parts

    def _cmd_add_signal(self, args):
        """add_signal <pattern>...  (execute() only surfaces self.output, so
        report there — a silent success reads to the caller as 'unknown
        command' when the dispatcher falls through to Tcl.)"""
        self.output.append(self.viewer.add_signals(args))

    def _cmd_remove_signal(self, args):
        """remove_signal <pattern>..."""
        self.output.append(self.viewer.remove_signals(args))

    def _cmd_zoom(self, args):
        from .ui_navigation import set_time_range
        if len(args) >= 2:
            set_time_range(self.viewer, int(args[0]), int(args[1]))
        elif len(args) == 1:
            self.viewer.zoom(float(args[0]))

    def _cmd_zoom_fit(self, args):
        self.viewer.zoom_fit_all()

    def _cmd_goto(self, args):
        if args:
            self.viewer.cursor_time = int(args[0])
            self.viewer._ensure_cursor_visible()

    def _cmd_cursor(self, args):
        if args:
            if args[0] in ('a', 'A', '1'):
                self.viewer.active_cursor = 'a'
            elif args[0] in ('b', 'B', '2'):
                self.viewer.active_cursor = 'b'
                self.viewer.cursor_b_on = True

    def _cmd_marker(self, args):
        if len(args) >= 1:
            try:
                t = int(args[0])
            except ValueError:
                # "220ns" — a unit string, the form an agent naturally writes
                from .snapshot import parse_time
                t = parse_time(self.viewer.vcd, args[0])
            label = " ".join(args[1:]) if len(args) > 1 else ""
            self.viewer.markers.append(Marker(t, label))

    def _cmd_radix(self, args):
        if args:
            for r in Radix:
                if r.value == args[0]:
                    self.viewer.radix = r
                    break

    def _cmd_analog(self, args):
        for pat in args:
            for sid in self.viewer.display_order:
                sig = self.viewer.vcd.signals.get(sid)
                if sig and fnmatch.fnmatch(sig.name, pat):
                    self.viewer.analog_signals.add(sid)

    def _cmd_set(self, args):
        if len(args) >= 2:
            self.variables[args[0]] = args[1]

    def _cmd_puts(self, args):
        self.output.append(' '.join(args))

    def _cmd_signals(self, args):
        for sid in self.viewer.display_order:
            sig = self.viewer.vcd.signals.get(sid)
            if sig:
                self.output.append(f"{sig.full_name} [{sig.size}]")

    def _cmd_measure(self, args):
        if self.viewer.cursor_b_on:
            delta = self.viewer.cursor_delta
            self.output.append(f"Delta: {delta} ({self.viewer.vcd.time_to_str(delta)})")

    def _cmd_export_csv(self, args):
        self.viewer.export_csv()

    def _cmd_screenshot(self, args):
        self.viewer.export_screenshot()

    def _cmd_translate(self, args):
        if args:
            try:
                self.viewer.translations = TranslationMap.load_file(args[0])
                self.output.append(f"Loaded translations from {args[0]}")
            except Exception as e:
                self.output.append(f"Error loading translations: {e}")

    def _cmd_theme(self, args):
        if args:
            path = args[0]
            if os.path.isfile(path):
                self.viewer.load_theme_file(path)
                self.output.append(f"Theme loaded from {path}")
            elif path in self.viewer.THEMES:
                self.viewer.apply_theme(path)
                self.output.append(f"Theme set to {path}")
            else:
                self.output.append(f"Theme not found: {path}. Available: {', '.join(self.viewer.THEME_ORDER)}")
        else:
            self.output.append(f"Current: {self.viewer.current_theme}. Available: {', '.join(self.viewer.THEME_ORDER)}")

    def _cmd_expand(self, args):
        from .display_order import copy_order
        for pat in args:
            for sid in copy_order(self.viewer.display_order):
                sig = self.viewer.vcd.signals.get(sid)
                if sig and sig.size > 1 and fnmatch.fnmatch(sig.name, pat):
                    self.viewer.expand_bus(sid)

    def _cmd_foreach(self, args):
        """foreach var list body"""
        if len(args) < 3:
            return
        var_name = args[0]
        items = args[1].split()
        body = args[2]
        for item in items:
            self.variables[var_name] = item
            for line in body.split('\n'):
                self._execute_line(line.strip())
