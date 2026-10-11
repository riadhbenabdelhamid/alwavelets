"""The curses TUI: the Alwavelets god-class (waveform/tree/panel drawing,
input dispatch, overlays, export, ISA debug, live reload).
"""

import curses
import fnmatch
import json
import os
import sys
import time
from fractions import Fraction

from .core import *  # noqa: F401,F403
from .rendering import *  # noqa: F401,F403
from .rendering import _update_wave_chars  # noqa: F401
from .decoders import *  # noqa: F401,F403
from .parsers import *  # noqa: F401,F403
from .analysis import *  # noqa: F401,F403
from .persistence import *  # noqa: F401,F403
from .persistence import _write_json_atomic
from .tcl import TclEngine  # noqa: F401
from .manual import AlwaveletsManual  # noqa: F401
from .finder import FinderMixin
from .hierarchy import ScopeIndex, TreeRows
from .scope_store import ScopeFolds, fold_state, restore_folds
from .display_order import (DisplayOrder, DisplayMembership, copy_order,
                            remove_display_ids, member_positions, grouped_rows)
from .ui_livereload import LiveReloadMixin
from .ui_tree import TreePanelMixin
from .ui_export import ExportMixin
from .ui_commands import CommandLineMixin
from .ui_keymap import KeymapMixin
from .ui_debug import DebugActionsMixin, load_analysis_data
from .ui_drawing import DrawingMixin


# ══════════════════════════════════════════════════════════════
#  CURSES VIEWER
# ══════════════════════════════════════════════════════════════





# ── true-color theme support ─────────────────────────────────────────

def hex_to_rgb(spec):
    """'#rrggbb' -> (r, g, b) ints, or None if it isn't one."""
    if isinstance(spec, str) and len(spec) == 7 and spec[0] == '#':
        try:
            return int(spec[1:3], 16), int(spec[3:5], 16), int(spec[5:7], 16)
        except ValueError:
            pass
    return None


def nearest_color_256(r, g, b):
    """Quantize an RGB color to the closest xterm-256 palette index (the
    6x6x6 cube or the 24-step grayscale ramp, whichever is nearer)."""
    def cube_idx(v):
        return 0 if v < 48 else (1 if v < 115 else min(5, (v - 35) // 40))

    def cube_val(i):
        return 0 if i == 0 else 55 + i * 40

    ir, ig, ib = cube_idx(r), cube_idx(g), cube_idx(b)
    cube = 16 + 36 * ir + 6 * ig + ib
    cr, cg, cb = cube_val(ir), cube_val(ig), cube_val(ib)
    d_cube = (r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2

    gi = max(0, min(23, (((r + g + b) // 3) - 8) // 10))
    gv = 8 + gi * 10
    d_gray = (r - gv) ** 2 + (g - gv) ** 2 + (b - gv) ** 2
    return (232 + gi) if d_gray < d_cube else cube


class Alwavelets(DrawingMixin, DebugActionsMixin, KeymapMixin, CommandLineMixin, ExportMixin, TreePanelMixin, FinderMixin, LiveReloadMixin):
    """Full-featured TUI waveform viewer."""

    # Color pair indices
    C_HIGH    = 1   # signal high (1)
    C_EDGE    = 2   # transition edge
    C_UNDEF   = 3   # unknown (x)
    C_HIZ     = 4   # high-impedance (z)
    C_CURSOR  = 5
    C_SELECT  = 6
    C_MARKER  = 7
    C_HEADER  = 8
    C_PROMPT  = 9
    C_BUSLBL  = 10
    C_CURSORB = 11
    C_ANALOG  = 12
    C_HELP    = 13
    C_DELTA   = 14
    C_LOW     = 15  # signal low (0)
    C_BUS     = 16  # bus body
    C_MENU    = 17  # menu bar background
    C_MENUKEY = 18  # menu bar hotkey
    C_MENUSEP = 19  # menu bar separator
    C_ROWALT  = 20  # alternating row dim bg
    C_FOCUS   = 21  # focused signal name
    C_VAL     = 22  # value text in name column
    C_SCOPE   = 23  # scope prefix dimmed
    C_ANHI    = 24  # analog high
    C_ANMID   = 25  # analog mid
    C_ANLO    = 26  # analog low
    C_BUSNAME = 27  # bus signal name (distinct from wire)
    C_DIFF_BG = 28  # diff highlight background
    C_DIFF_ED = 29  # diff edge/dash color

    # ── Theme definitions ──
    THEMES = {
        'dark': {  # Current default (dark terminal)
            'name': 'Dark',
            'HIGH': (47, -1), 'LOW': (34, -1), 'EDGE': (203, -1),
            'UNDEF': (220, -1), 'HIZ': (75, -1), 'BUS': (111, -1),
            'BUSLBL': (255, 236), 'ANALOG': (87, -1),
            'ANHI': (196, -1), 'ANMID': (220, -1), 'ANLO': (45, -1),
            'CURSOR': (255, 25), 'CURSORB': (255, 90), 'MARKER': (213, -1),
            'DELTA': (187, 236), 'HEADER': (253, 235), 'MENU': (248, 234),
            'MENUKEY': (110, 234), 'MENUSEP': (240, 234), 'PROMPT': (255, 124),
            'HELP': (253, 235), 'SELECT': (255, 24), 'FOCUS': (255, 237),
            'VAL': (117, -1), 'SCOPE': (245, -1), 'ROWALT': (-1, 233),
            'BUSNAME': (111, -1), 'DIFF_BG': (255, 52), 'DIFF_ED': (209, -1),
        },
        'light': {  # Black on white
            'name': 'Light',
            'HIGH': (28, -1), 'LOW': (22, -1), 'EDGE': (160, -1),
            'UNDEF': (208, -1), 'HIZ': (24, -1), 'BUS': (25, -1),
            'BUSLBL': (16, 254), 'ANALOG': (30, -1),
            'ANHI': (160, -1), 'ANMID': (172, -1), 'ANLO': (30, -1),
            'CURSOR': (255, 20), 'CURSORB': (255, 127), 'MARKER': (129, -1),
            'DELTA': (20, 229), 'HEADER': (16, 254), 'MENU': (16, 253),
            'MENUKEY': (22, 253), 'MENUSEP': (247, 253), 'PROMPT': (255, 160),
            'HELP': (16, 153), 'SELECT': (255, 28), 'FOCUS': (16, 252),
            'VAL': (25, -1), 'SCOPE': (242, -1), 'ROWALT': (-1, 255),
            'BUSNAME': (25, -1), 'DIFF_BG': (16, 217), 'DIFF_ED': (166, -1),
        },
        'phosphor': {  # Classic green on black
            'name': 'Phosphor',
            'HIGH': (46, -1), 'LOW': (22, -1), 'EDGE': (118, -1),
            'UNDEF': (226, -1), 'HIZ': (30, -1), 'BUS': (48, -1),
            'BUSLBL': (46, 233), 'ANALOG': (47, -1),
            'ANHI': (46, -1), 'ANMID': (34, -1), 'ANLO': (22, -1),
            'CURSOR': (16, 46), 'CURSORB': (16, 48), 'MARKER': (190, -1),
            'DELTA': (46, 234), 'HEADER': (46, 233), 'MENU': (34, 232),
            'MENUKEY': (46, 232), 'MENUSEP': (22, 232), 'PROMPT': (46, 52),
            'HELP': (46, 234), 'SELECT': (16, 34), 'FOCUS': (46, 234),
            'VAL': (48, -1), 'SCOPE': (22, -1), 'ROWALT': (-1, 233),
            'BUSNAME': (48, -1), 'DIFF_BG': (46, 52), 'DIFF_ED': (226, -1),
        },
        'futuristic': {  # Cyan/magenta neon
            'name': 'Neon',
            'HIGH': (51, -1), 'LOW': (24, -1), 'EDGE': (201, -1),
            'UNDEF': (214, -1), 'HIZ': (60, -1), 'BUS': (141, -1),
            'BUSLBL': (255, 53), 'ANALOG': (87, -1),
            'ANHI': (201, -1), 'ANMID': (141, -1), 'ANLO': (51, -1),
            'CURSOR': (16, 51), 'CURSORB': (16, 201), 'MARKER': (213, -1),
            'DELTA': (51, 53), 'HEADER': (255, 53), 'MENU': (252, 17),
            'MENUKEY': (51, 17), 'MENUSEP': (60, 17), 'PROMPT': (255, 161),
            'HELP': (255, 53), 'SELECT': (16, 87), 'FOCUS': (255, 54),
            'VAL': (141, -1), 'SCOPE': (60, -1), 'ROWALT': (-1, 233),
            'BUSNAME': (141, -1), 'DIFF_BG': (255, 89), 'DIFF_ED': (214, -1),
        },
        'mono': {  # Pure white on black
            'name': 'Mono',
            'HIGH': (255, -1), 'LOW': (240, -1), 'EDGE': (255, -1),
            'UNDEF': (245, -1), 'HIZ': (242, -1), 'BUS': (252, -1),
            'BUSLBL': (255, 236), 'ANALOG': (250, -1),
            'ANHI': (255, -1), 'ANMID': (248, -1), 'ANLO': (242, -1),
            'CURSOR': (16, 255), 'CURSORB': (16, 248), 'MARKER': (255, -1),
            'DELTA': (255, 236), 'HEADER': (255, 236), 'MENU': (250, 234),
            'MENUKEY': (255, 234), 'MENUSEP': (240, 234), 'PROMPT': (255, 240),
            'HELP': (255, 238), 'SELECT': (16, 252), 'FOCUS': (255, 238),
            'VAL': (252, -1), 'SCOPE': (242, -1), 'ROWALT': (-1, 233),
            'BUSNAME': (252, -1), 'DIFF_BG': (255, 236), 'DIFF_ED': (248, -1),
        },
    }
    THEMES['calm'] = {
        **THEMES['dark'], 'name': 'Calm',
        'HIGH': (114, -1), 'LOW': (72, -1), 'EDGE': (109, -1),
        'BUS': (110, -1), 'BUSNAME': (110, -1), 'ANALOG': (110, -1),
        'ANHI': (181, -1), 'ANMID': (180, -1), 'ANLO': (109, -1),
        'UNDEF': (180, -1), 'HIZ': (139, -1),
    }
    THEMES['classic'] = {
        **THEMES['dark'], 'name': 'Classic',
        'DELTA': (228, 25), 'HEADER': (255, 236), 'MENU': (252, 234),
        'MENUKEY': (82, 234), 'HELP': (255, 24),
        'SELECT': (16, 114), 'FOCUS': (255, 238),
    }
    THEMES['minimal'] = {
        **THEMES['mono'], 'name': 'Minimal',
        'HEADER': (252, -1), 'MENU': (245, -1), 'MENUKEY': (255, -1),
        'MENUSEP': (240, -1), 'ROWALT': (-1, -1), 'BUSLBL': (252, -1),
        'DELTA': (252, 234), 'HELP': (252, 234), 'FOCUS': (255, 235),
    }
    THEME_ORDER = ['calm', 'classic', 'minimal', 'dark', 'light', 'phosphor', 'futuristic', 'mono']
    WORKMODES = {
        'calm': {'name': 'Calm', 'theme': 'calm', 'menu_bar': True, 'scope_separators': True},
        'classic': {'name': 'Classic', 'theme': 'classic', 'menu_bar': True, 'scope_separators': False},
        'minimal': {'name': 'Minimal', 'theme': 'minimal', 'menu_bar': False, 'scope_separators': False},
    }

    @property
    def display_order(self):
        return self._display_order

    @display_order.setter
    def display_order(self, order):
        from .catalog import SignalCatalog
        catalog = getattr(getattr(self, 'vcd', None), 'signals', None)
        if isinstance(catalog, SignalCatalog):
            order = DisplayOrder.from_order(catalog, order)
        self._display_order = order
        if isinstance(order, DisplayOrder):
            self._display_order_set = DisplayMembership(order)
            self._scope_check_cache = {}

    def __init__(self, stdscr, vcd):
        self.stdscr = stdscr
        self.vcd = vcd

        # View state
        self.time_start = 0
        self.time_end = vcd.max_time if vcd.max_time > 0 else 100
        self.sig_offset = 0
        self.focus_idx = 0            # focused signal (relative to sig_offset)

        # Dual cursors
        self.cursor_a = 0
        self.cursor_b = vcd.max_time // 2 if vcd.max_time > 0 else 50
        self.active_cursor = 'a'      # 'a' or 'b'
        self.cursor_on = True
        self.cursor_b_on = False

        # Display
        self.show_values = True
        self.show_help = False
        self.show_manual = False
        self.manual = AlwaveletsManual()
        self.show_colors = False
        self.selected_signals = set()
        self.markers = []
        self.radix = Radix.HEX
        self.analog_signals = set()   # sig_ids rendered as analog
        self.search_term = ""
        self.search_results = []
        self.search_idx = 0

        # Signal ordering (mutable copy)
        from .catalog import SignalCatalog
        self.display_order = vcd.signal_order if isinstance(vcd.signals, SignalCatalog) else list(vcd.signal_order)
        self._sync_display_set()
        self._undo_stack = []         # (description, inverse-closure) — see undo()

        # Collapsible display groups (overlay on display_order; see view_rows).
        # 'parent' (a group name or None) nests groups; see create_group.
        self.groups = []              # [{'name', 'members': set(sid), 'collapsed': bool, 'parent'}]
        self._group_by_name = {}
        self._member_group = {}
        self._group_depth = {}        # name -> nesting depth (header indent)
        self.view_rows = self.display_order   # ungrouped rows share the selection

        # Scope folding
        self.scope_tree = vcd.scopes
        self.collapsed_scopes = set()  # set of full_path strings

        # Time units
        self.show_time_units = True   # show ns/us/ms in ruler instead of raw ticks

        # Signal spacing
        self.signal_spacing = 0       # spacing index into SPACING_VALUES


        # Signal name display
        self.show_full_names = False

        # Waveform value label size
        self.value_label_size = 2

        # Hierarchical indent mode
        self.indent_mode = False

        # New features
        self.annotations = AnnotationStore()
        self.bookmarks = BookmarkStore()
        self.power_est = None
        self.formal_analyzer = None
        self.bus_transactions = []
        self.show_annotations = True

        # ISA debug
        self.show_reg_panel = False
        self._heatmap_data = None
        # Hart color map (cycle through 6 distinct colors)
        self.HART_COLORS = [
            196,   # red
            46,    # green
            33,    # blue
            226,   # yellow
            201,   # magenta
            51,    # cyan
            208,   # orange
            99,    # purple
        ]    # Register file panel
        self.signal_tags = SignalTagStore()
        self.isa_decoder = RV32Decoder()
        self.instr_trace = InstructionTrace()

        # Panel mode (split view)
        self.panel_mode = True          # Default: panel mode on
        self.panel_focus = 0            # 0=tree panel, 1=waveform panel
        self.panel_tree_width = 30      # Width of tree panel in split mode
        self.panel_selected_scope = None  # Currently selected scope in tree       # Show signals indented by scope depth     # 0=hidden, 1=compact(4ch), 2=normal(full)

        # VIM-style state
        self._pending_g = False        # True after 'g' pressed, waiting for second key
        self._visual_mode = False      # Shift+V visual line select mode
        self._visual_anchor = -1       # Starting index for visual selection  # Enter toggles short vs full path

        # Vertical grid
        self.show_grid = 0

        # Workmode supplies presentation defaults; the theme can be customized.
        self.workmode = 'classic'
        mode = self.WORKMODES[self.workmode]
        self.current_theme = mode['theme']
        self.show_module_sep = mode['scope_separators']

        # Menu page
        self.menu_page = 0            # 0-based page of command menu
        self.show_menu_bar = mode['menu_bar']  # row 1 command guide (F2 toggles)
        self.ruler_row = 2            # recomputed in update_layout
        self.wave_top = 3             # first signal row (recomputed)

        # Line thickness
        self.line_thickness = 1       # 0=ultra-thin, 1=thin (default), 2=sub-cell hi-res

        # Smooth scroll
        self.scroll_timeout = 20      # ms between redraws during held keys

        # Flash message (non-blocking): drawn on the status row until it expires
        self._message = None
        self._message_expire = 0.0
        self._message_secs = 1.5

        # ── Diff mode ──
        self.diff_vcd = None          # second VCDFile for comparison
        self.diff_ranges = {}         # sig_id -> [(t_start, t_end), ...]
        self.diff_sig_map = {}        # sig_id -> VCDSignal from diff file         # sig_id -> [(t_start, t_end), ...] of differences

        # ── Dealbreaker features ──

        # Fuzzy signal finder ('/'); the rest of its state is (re)set by
        # open_finder in finder.py each time the overlay opens.
        self.show_finder = False

        # Mouse drag-to-zoom (button held + motion selects a time range)
        self._drag_anchor = None      # anchor time, set on button press
        self._drag_anchor_x = 0       # anchor column (distinguishes click/drag)
        self._drag_cur = None         # current drag time, drives the highlight

        # True-color themes: '#rrggbb' -> redefined palette slot bookkeeping
        # (see _resolve_color)
        self._hex_slots = {}          # '#rrggbb' -> palette slot in use
        self._hex_slot_saved = {}     # slot -> original (r,g,b) 0-1000 values
        self._hex_next_slot = 255     # allocate downward through the gray ramp

        # Per-row render cache: valid only for the current time window (see
        # DrawingMixin._render_row). Cursor moves and vertical scrolling then
        # redraw from cache instead of re-rendering every visible row.
        self._row_cache = {}
        self._row_cache_window = None

        # Agent control channel (opt-in, --control; see control.py). Non-None
        # only while a ControlServer is bound. _remote_depth > 0 means the
        # command currently running arrived from an agent, not the keyboard.
        self.control = None
        self._remote_depth = 0

        # Tree browser
        self.show_tree = False
        self.tree_cursor = 0          # cursor in flattened tree list
        self.tree_scroll = 0          # scroll offset in tree
        self.tree_expanded = set()    # set of scope full_paths that are expanded
        self.tree_selected = set()    # sig_ids checked in tree
        self._tree_flat = []
        self._scope_check_cache = {}
        self._rebuild_tree()

        # Bus expansion
        self.expanded_buses = {}      # parent_sig_id -> [virtual_sig_ids]
        self.virtual_signals = {}     # virtual_sig_id -> VirtualBitSignal

        # Enum/state decode
        self.translations = TranslationMap()

        # ── High-impact features ──

        # Per-signal properties
        self.sig_radix = {}           # sig_id -> Radix
        self.sig_color = {}           # sig_id -> color pair index (1-6 user colors)
        self.sig_height = {}          # sig_id -> int (1-4 rows)

        # Edge-to-edge measurement
        self.measure_mode = False
        self.measure_sig_a = None     # sig_id for measurement signal A
        self.measure_sig_b = None     # sig_id for measurement signal B
        self.measure_result = None    # string result

        # Zoom history
        self.zoom_history = []        # list of (time_start, time_end)
        self.zoom_history_max = 50

        # Protocol decode
        self.decode_overlays = {}     # sig_id -> [DecodedFrame, ...]

        # Tcl engine
        self.tcl = TclEngine(self)

        # Pipe/live reload
        self.live_file = None         # path being watched (None = disabled)
        self.live_mtime = 0           # last reloaded mtime
        self.follow_on = False        # live 'follow' (tail appended changes)
        self._follow_last_poll = 0.0
        self._live_pending = None     # (mtime, size) awaiting stability (debounce)
        self._live_last_poll = 0.0    # monotonic time of last stat() poll
        self.keymap = self._build_keymap()  # keycode → handler (waveform mode)

        # Layout
        self.name_width = 22
        self._name_width_manual = False  # True when user adjusts with ( )
        self.update_layout()

        # Mouse
        self.mouse_enabled = False
        self._init_mouse()

        # Colors
        self._init_colors()

        # User config (theme/radix/wave style/keybindings) overrides defaults.
        self._apply_config()


    def _apply_config(self):
        """Apply ~/.config/alwavelets/config.json over the built-in defaults."""
        from . import config as _cfg
        cfg = _cfg.load_config()
        if not cfg:
            return
        mode = cfg.get('workmode')
        if isinstance(mode, str) and mode.lower() in self.WORKMODES:
            self.apply_workmode(mode, notify=False)
        # Explicit settings take precedence over the mode's defaults.
        th = cfg.get('theme')
        if isinstance(th, str) and th.lower() in self.THEMES:
            self.current_theme = th.lower()
            self.apply_theme()        # after _init_colors(): it must not be re-run
        rx = cfg.get('radix')
        if rx is not None:
            for r in Radix:
                if r.value == str(rx).lower():
                    self.radix = r
                    break
        ws = cfg.get('wave_style')
        if ws is not None:
            try:
                self.line_thickness = max(0, min(2, int(ws)))
                _update_wave_chars(1 if self.line_thickness == 2 else self.line_thickness)
            except (ValueError, TypeError):
                pass
        if 'menu_bar' in cfg:
            self.show_menu_bar = bool(cfg['menu_bar'])
        if 'grid' in cfg:
            try:
                self.show_grid = int(cfg['grid']) % 3
            except (ValueError, TypeError):
                pass
        nw = cfg.get('name_width')
        if nw is not None:
            try:
                self.name_width = max(8, int(nw))
                self._name_width_manual = True
            except (ValueError, TypeError):
                pass
        if isinstance(cfg.get('keys'), dict):
            self._apply_keymap_overrides(cfg['keys'])
        self.update_layout()

    def _apply_keymap_overrides(self, keys):
        """Rebind keys from a {action_name: char} map onto the keymap.

        Action names are the keymap handlers' names with any '_kb_' prefix
        stripped (e.g. 'quit', 'zoom_in', 'next_edge'). Unknown actions or
        non-single-character keys are ignored."""
        registry = {}
        for handler in self.keymap.values():
            name = getattr(handler, '__name__', '')
            if name.startswith('_kb_'):
                name = name[4:]
            if name:
                registry.setdefault(name, handler)
        for action, ch in keys.items():
            handler = registry.get(action)
            if handler is not None and isinstance(ch, str) and len(ch) == 1:
                self.keymap[ord(ch)] = handler

    def _resolve_color(self, spec):
        """Turn a theme color spec into a curses color number.

        Ints (xterm-256 indices, -1 = terminal default) pass through.
        '#rrggbb' strings resolve, in order of fidelity: a direct-color
        number on 24-bit terminfo (COLORS == 2^24), a redefined palette
        slot where the palette is writable, else the nearest xterm-256
        index. Redefined slots are taken from the top of the grayscale
        ramp; their original values are saved so switching back to an
        index-based theme can restore them."""
        rgb = hex_to_rgb(spec)
        if rgb is None:
            return spec if isinstance(spec, int) else -1
        r, g, b = rgb
        if curses.COLORS >= 1 << 24:
            return (r << 16) | (g << 8) | b
        if curses.can_change_color() and curses.COLORS >= 256:
            slot = self._hex_slots.get(spec)
            if slot is None and self._hex_next_slot > 231:
                slot = self._hex_next_slot
                try:
                    self._hex_slot_saved[slot] = curses.color_content(slot)
                    curses.init_color(slot, r * 1000 // 255,
                                      g * 1000 // 255, b * 1000 // 255)
                    self._hex_slots[spec] = slot
                    self._hex_next_slot -= 1
                except curses.error:
                    slot = None
            if slot is not None:
                return slot
        return nearest_color_256(r, g, b)

    def _restore_hex_slots(self):
        """Give redefined palette slots their original colors back (used when
        switching from a true-color theme to an index-based one, so shared
        indices like the grayscale ramp look right again)."""
        for slot, (r, g, b) in self._hex_slot_saved.items():
            try:
                curses.init_color(slot, r, g, b)
            except curses.error:
                pass
        self._hex_slot_saved.clear()
        self._hex_slots.clear()
        self._hex_next_slot = 255

    def apply_theme(self, theme_name=None):
        """Apply a theme by name. If None, re-apply current."""
        if theme_name:
            self.current_theme = theme_name
        theme = self.THEMES.get(self.current_theme, self.THEMES['dark'])
        if curses.COLORS < 256:
            self._apply_basic_colors()
            return

        # Entering an index-based theme releases any palette slots a hex
        # theme redefined (they alias grayscale-ramp indices).
        has_hex = any(hex_to_rgb(c) is not None
                      for v in theme.values() if isinstance(v, tuple)
                      for c in v)
        if not has_hex and self._hex_slot_saved:
            self._restore_hex_slots()

        pair_map = {
            'HIGH': self.C_HIGH, 'LOW': self.C_LOW, 'EDGE': self.C_EDGE,
            'UNDEF': self.C_UNDEF, 'HIZ': self.C_HIZ, 'BUS': self.C_BUS,
            'BUSLBL': self.C_BUSLBL, 'ANALOG': self.C_ANALOG,
            'ANHI': self.C_ANHI, 'ANMID': self.C_ANMID, 'ANLO': self.C_ANLO,
            'CURSOR': self.C_CURSOR, 'CURSORB': self.C_CURSORB,
            'MARKER': self.C_MARKER, 'DELTA': self.C_DELTA,
            'HEADER': self.C_HEADER, 'MENU': self.C_MENU,
            'MENUKEY': self.C_MENUKEY, 'MENUSEP': self.C_MENUSEP,
            'PROMPT': self.C_PROMPT, 'HELP': self.C_HELP,
            'SELECT': self.C_SELECT, 'FOCUS': self.C_FOCUS,
            'VAL': self.C_VAL, 'SCOPE': self.C_SCOPE,
            'ROWALT': self.C_ROWALT, 'BUSNAME': self.C_BUSNAME,
            'DIFF_BG': self.C_DIFF_BG, 'DIFF_ED': self.C_DIFF_ED,
        }
        for key, pair_idx in pair_map.items():
            if key in theme:
                fg, bg = theme[key]
                try:
                    curses.init_pair(pair_idx, self._resolve_color(fg),
                                     self._resolve_color(bg))
                except curses.error:
                    pass

    def cycle_theme(self):
        """Cycle to next theme."""
        order = self.THEME_ORDER
        idx = order.index(self.current_theme) if self.current_theme in order else -1
        self.current_theme = order[(idx + 1) % len(order)]
        self.apply_theme()

    def apply_workmode(self, name, notify=True):
        """Apply presentation defaults without changing signals or navigation."""
        name = name.strip().lower() if isinstance(name, str) else ''
        mode = self.WORKMODES.get(name)
        if mode is None:
            if notify:
                self._show_message('Workmodes: calm, classic, minimal')
            return False
        self.workmode = name
        self.current_theme = mode['theme']
        self.show_menu_bar = mode['menu_bar']
        self.show_module_sep = mode['scope_separators']
        self.signal_spacing = 0
        self.menu_page = 0
        self.apply_theme()
        focused_row = self.sig_offset + self.focus_idx
        self.update_layout()
        self.focus_idx = min(self.focus_idx, self.visible_signals - 1)
        self.sig_offset = max(0, focused_row - self.focus_idx)
        self._needs_layout = True
        self._dirty = True
        if notify:
            self._show_message(f"Workmode: {mode['name']}  ·  F3 cycles  ·  ;session saves")
        return True

    def load_theme_file(self, path):
        """Load a custom theme from JSON file."""
        try:
            with open(path) as f:
                data = json.load(f)
            name = data.get('name', 'custom')
            # Convert lists to tuples
            theme = {}
            for k, v in data.items():
                if k == 'name':
                    continue
                if isinstance(v, list) and len(v) == 2:
                    theme[k] = (v[0], v[1])
            self.THEMES[name.lower()] = theme
            self.THEMES[name.lower()]['name'] = name
            if name.lower() not in self.THEME_ORDER:
                self.THEME_ORDER.append(name.lower())
            self.apply_theme(name.lower())
            self._show_message(f"Theme loaded: {name}")
        except Exception as e:
            self._show_message(f"Theme error: {e}")

    def _init_colors(self):
        curses.start_color()
        curses.use_default_colors()
        self.apply_theme()

    def _apply_basic_colors(self):
        """Keep monochrome modes monochrome on an eight-color terminal too."""
        if self.current_theme in ('minimal', 'mono'):
            inverse = {self.C_CURSOR, self.C_CURSORB, self.C_SELECT, self.C_PROMPT}
            for pair in range(self.C_HIGH, self.C_DIFF_ED + 1):
                fg, bg = ((curses.COLOR_BLACK, curses.COLOR_WHITE) if pair in inverse
                          else (curses.COLOR_WHITE, -1))
                curses.init_pair(pair, fg, bg)
        else:
            # ── Fallback 8-color palette ──
            curses.init_pair(self.C_HIGH,    curses.COLOR_GREEN, -1)
            curses.init_pair(self.C_LOW,     curses.COLOR_GREEN, -1)
            curses.init_pair(self.C_EDGE,    curses.COLOR_RED, -1)
            curses.init_pair(self.C_UNDEF,   curses.COLOR_YELLOW, -1)
            curses.init_pair(self.C_HIZ,     curses.COLOR_CYAN, -1)
            curses.init_pair(self.C_BUS,     curses.COLOR_BLUE, -1)
            curses.init_pair(self.C_BUSLBL,  curses.COLOR_GREEN, curses.COLOR_BLACK)
            curses.init_pair(self.C_ANALOG,  curses.COLOR_CYAN, -1)
            curses.init_pair(self.C_ANHI,    curses.COLOR_RED, -1)
            curses.init_pair(self.C_ANMID,   curses.COLOR_YELLOW, -1)
            curses.init_pair(self.C_ANLO,    curses.COLOR_CYAN, -1)
            curses.init_pair(self.C_CURSOR,  curses.COLOR_WHITE, curses.COLOR_BLUE)
            curses.init_pair(self.C_CURSORB, curses.COLOR_WHITE, curses.COLOR_MAGENTA)
            curses.init_pair(self.C_MARKER,  curses.COLOR_MAGENTA, -1)
            curses.init_pair(self.C_DELTA,   curses.COLOR_YELLOW, curses.COLOR_BLUE)
            curses.init_pair(self.C_HEADER,  curses.COLOR_BLACK, curses.COLOR_WHITE)
            curses.init_pair(self.C_MENU,    curses.COLOR_WHITE, curses.COLOR_BLACK)
            curses.init_pair(self.C_MENUKEY, curses.COLOR_GREEN, curses.COLOR_BLACK)
            curses.init_pair(self.C_MENUSEP, curses.COLOR_WHITE, curses.COLOR_BLACK)
            curses.init_pair(self.C_PROMPT,  curses.COLOR_WHITE, curses.COLOR_RED)
            curses.init_pair(self.C_HELP,    curses.COLOR_WHITE, curses.COLOR_BLUE)
            curses.init_pair(self.C_SELECT,  curses.COLOR_BLACK, curses.COLOR_GREEN)
            curses.init_pair(self.C_FOCUS,   curses.COLOR_WHITE, -1)
            curses.init_pair(self.C_VAL,     curses.COLOR_CYAN, -1)
            curses.init_pair(self.C_SCOPE,   curses.COLOR_WHITE, -1)
            curses.init_pair(self.C_ROWALT,  -1, -1)
            curses.init_pair(self.C_BUSNAME, curses.COLOR_BLUE, -1)
            curses.init_pair(self.C_DIFF_BG, curses.COLOR_WHITE, curses.COLOR_RED)
            curses.init_pair(self.C_DIFF_ED, curses.COLOR_YELLOW, -1)

    def _init_mouse(self):
        try:
            # Enable all mouse buttons including scroll
            all_mouse = (
                curses.BUTTON1_CLICKED |
                curses.BUTTON1_PRESSED |
                curses.BUTTON1_RELEASED |   # drag-to-zoom end
                curses.BUTTON3_CLICKED |
                curses.BUTTON4_PRESSED |    # scroll up (zoom in)
                curses.REPORT_MOUSE_POSITION
            )
            # Try to add BUTTON5 for scroll down (not available on all systems)
            try:
                all_mouse |= curses.BUTTON5_PRESSED
            except AttributeError:
                pass
            curses.mousemask(all_mouse)
            # Enable SGR mouse mode for wider terminal support
            sys.stdout.write('\033[?1002h')
            sys.stdout.write('\033[?1006h')
            sys.stdout.flush()
            self.mouse_enabled = True
        except Exception:
            self.mouse_enabled = False

    def _cleanup_mouse(self):
        if self.mouse_enabled:
            try:
                sys.stdout.write('\033[?1002l')
                sys.stdout.write('\033[?1006l')
                sys.stdout.flush()
            except Exception:
                pass

    def update_layout(self):
        self.height, self.width = self.stdscr.getmaxyx()
        # Auto-size name column only if user hasn't manually adjusted
        if self.panel_mode:
            self.name_width = self.panel_tree_width
        elif not self._name_width_manual:
            if self.width < 40:
                self.name_width = max(8, self.width // 3)
            else:
                self.name_width = min(22, self.width // 4)
        # Clamp to terminal width
        self.name_width = min(self.name_width, self.width // 2)
        self.wave_width = max(1, self.width - self.name_width - 1)
        # Color separates the bars, leaving the vertical space for waveforms.
        #   menu shown:  0 title · 1 menu · 2 ruler · 3+ signals
        #   menu hidden: 0 title · 1 ruler · 2+ signals
        self.menu_row = 1
        self.ruler_row = 2 if self.show_menu_bar else 1
        self.wave_top = self.ruler_row + 1
        # Each signal takes (1 + spacing) rows, plus extra for diff pairs
        rows_for_signals = max(1, self.height - self.wave_top - 1)
        # All non-zero spacings add at least 1 real separator row
        if self.signal_spacing > 0 and self.signal_spacing < 1:
            row_stride = 2  # signal row + thin separator row
        elif self.signal_spacing >= 1:
            row_stride = 1 + int(self.signal_spacing)
        else:
            row_stride = 1
        # In diff mode, each differing signal takes 2 rows
        if hasattr(self, 'diff_ranges') and self.diff_ranges:
            # Estimate: assume ~half the signals have diffs
            n_diffs = len(self.diff_ranges)
            n_total = max(len(self.display_order), 1)
            avg_rows = row_stride + (n_diffs / n_total)  # extra row per diff signal on average
            self.visible_signals = max(1, int(rows_for_signals / avg_rows))
        else:
            self.visible_signals = max(1, rows_for_signals // row_stride)

    @property
    def cursor_time(self):
        return self.cursor_a if self.active_cursor == 'a' else self.cursor_b

    @cursor_time.setter
    def cursor_time(self, val):
        if self.active_cursor == 'a':
            self.cursor_a = val
        else:
            self.cursor_b = val

    @property
    def cursor_delta(self):
        if self.cursor_b_on:
            return abs(self.cursor_b - self.cursor_a)
        return None

    # ── Collapsible display groups (overlay; display_order stays canonical) ──

    def _reindex_groups(self):
        gbn = {g['name']: g for g in self.groups}
        # Prune groups that neither hold members nor parent another group
        # (iteratively: removing a leaf may orphan its parent). Auto-created
        # parent nodes survive as long as they have children.
        while True:
            parents = {g.get('parent') for g in gbn.values()}
            dead = [n for n, g in gbn.items()
                    if not g['members'] and n not in parents]
            if not dead:
                break
            for n in dead:
                del gbn[n]
        self.groups = [g for g in self.groups if g['name'] in gbn]
        self._group_by_name = gbn
        self._member_group = {}
        for g in self.groups:
            for sid in g['members']:
                self._member_group[sid] = g['name']
        # Nesting depth per group (for header indentation).
        self._group_depth = {}
        for name in gbn:
            self._group_depth[name] = len(self._group_chain(name)) - 1

    def _group_chain(self, name):
        """[name, parent, grandparent, ...] — innermost first. Cycle-safe."""
        gbn = self._group_by_name
        out = []
        while name is not None and name in gbn and name not in out:
            out.append(name)
            name = gbn[name].get('parent')
        return out

    def _rebuild_view_rows(self):
        """Derive the visible row list from display_order + groups: each
        group's header appears at its first (transitive) member; a collapsed
        group hides its members AND any nested group headers under it.
        Header rows are ('\\x01hdr', name) tuples; everything else is a
        sig_id."""
        mg = self._member_group
        gbn = self._group_by_name
        headed = set()
        # Without group headers, the read-only row view can share the order.
        # Rebuilding a million-entry copy on every split-panel redraw stalls
        # scrolling even when only a few waveform rows are visible.
        compact = isinstance(self.display_order, DisplayOrder)
        rows = grouped_rows(self.display_order, mg, gbn, self._group_chain) if mg and compact else ([] if mg else self.display_order)
        for sid in self.display_order if mg and not compact else ():
            g = mg.get(sid)
            if g is None:
                rows.append(sid)
                continue
            chain = self._group_chain(g)          # innermost .. outermost
            # Emit missing headers outermost-first; a header is hidden when
            # any of ITS ancestors is collapsed.
            for i in range(len(chain) - 1, -1, -1):
                if any(gbn[a]['collapsed'] for a in chain[i + 1:]):
                    break                          # this and deeper are hidden
                name = chain[i]
                if name not in headed:
                    headed.add(name)
                    rows.append(('\x01hdr', name))
            if any(gbn[a]['collapsed'] for a in chain):
                continue
            rows.append(sid)
        self.view_rows = rows
        n = len(rows)
        if self.sig_offset >= n:
            self.sig_offset = max(0, n - 1)
        if self.sig_offset + self.focus_idx >= n:
            self.focus_idx = max(0, n - 1 - self.sig_offset)
        return rows

    def focused_token(self):
        idx = self.sig_offset + self.focus_idx
        if 0 <= idx < len(self.view_rows):
            return self.view_rows[idx]
        return None

    @staticmethod
    def is_group_header(tok):
        return isinstance(tok, tuple) and len(tok) == 2 and tok[0] == '\x01hdr'

    def create_group(self, name, members, parent=None):
        """Group the given sig_ids under `name` (replacing any existing group
        of that name). Members already in another group move into this one.
        With `parent`, the group nests under that group (auto-created as an
        empty node if missing); collapsing the parent then hides this group's
        header and members too."""
        members = [s for s in members if isinstance(s, str)]
        if not name or not members:
            return False
        if parent == name or (parent and name in self._group_chain(parent)):
            return False                          # would create a cycle
        old = self._group_by_name.get(name)
        if parent is None and old is not None:
            parent = old.get('parent')            # regrouping keeps its place
        self.groups = [g for g in self.groups if g['name'] != name]
        mset = set(members)
        for g in self.groups:
            g['members'] -= mset
        if parent and not any(g['name'] == parent for g in self.groups):
            self.groups.append({'name': parent, 'members': set(),
                                'collapsed': False, 'parent': None})
        self.groups.append({'name': name, 'members': mset,
                            'collapsed': False, 'parent': parent})
        # A header spans the rows below it, so the members must sit together:
        # gather them at the first member's slot, keeping their order. A V
        # selection is contiguous already; named members ('group x a c') and
        # a selection with gaps are not.
        if isinstance(self.display_order, DisplayOrder):
            positions = member_positions(self.display_order, mset)
            if positions:
                self.display_order.remove_ids(mset)
                self.display_order[positions[0][0]:positions[0][0]] = [s for _, s in positions]
            first = None
        else:
            first = next((i for i, s in enumerate(self.display_order) if s in mset), None)
        if first is not None:
            picked = [s for s in self.display_order if s in mset]
            rest = [s for s in self.display_order if s not in mset]
            self.display_order = rest[:first] + picked + rest[first:]
        self._reindex_groups()
        self._rebuild_view_rows()
        return True

    def ungroup(self, name):
        before = len(self.groups)
        removed = self._group_by_name.get(name)
        self.groups = [g for g in self.groups if g['name'] != name]
        if removed is not None:
            # Children re-parent to the removed group's parent, so the rest
            # of the hierarchy survives.
            for g in self.groups:
                if g.get('parent') == name:
                    g['parent'] = removed.get('parent')
        self._reindex_groups()
        self._rebuild_view_rows()
        return len(self.groups) != before

    def toggle_group_collapse(self, name):
        g = self._group_by_name.get(name)
        if g is not None:
            g['collapsed'] = not g['collapsed']
            self._rebuild_view_rows()
            return g['collapsed']
        return None

    def focused_signal_id(self):
        """Get the signal ID of the currently focused row, or None (e.g. on a
        group header)."""
        idx = self.sig_offset + self.focus_idx
        if 0 <= idx < len(self.view_rows):
            tok = self.view_rows[idx]
            return tok if isinstance(tok, str) else None
        return None

    # ── Zoom / Scroll ──

    def time_to_col(self, time):
        return time_to_column(time, self.time_start, self.time_end, self.wave_width)

    def col_to_time(self, col):
        return column_to_time(col, self.time_start, self.time_end, self.wave_width)

    def zoom(self, factor, center=None):
        span = self.time_end - self.time_start
        if span <= 0:
            return
        new_span = max(int(span * Fraction(factor)), min(span, 10))
        if new_span == span:
            return
        if center is None:
            center = self.cursor_time if self.cursor_on else (self.time_start + self.time_end) // 2
        if not self.time_start <= center <= self.time_end:
            center = (self.time_start + self.time_end) // 2
        # Keep the anchor at the same fraction of the viewport. Rounding the
        # new offset to a tick and clamping at t=0 are the only adjustments.
        offset = time_to_column(center, self.time_start, self.time_end, new_span)
        self.time_start = max(0, center - offset)
        self.time_end = self.time_start + new_span

    def scroll_time(self, direction):
        span = self.time_end - self.time_start
        step = max(span // 8, 1)
        self.time_start = max(0, self.time_start + step * direction)
        self.time_end = self.time_start + span

    def move_cursor(self, direction):
        span = self.time_end - self.time_start
        step = max(span // self.wave_width, 1)
        new_t = max(0, self.cursor_time + step * direction)
        self.cursor_time = new_t
        # Auto-scroll if cursor exits window
        if new_t < self.time_start:
            diff = self.time_start - new_t
            self.time_start -= diff
            self.time_end -= diff
        elif new_t > self.time_end:
            diff = new_t - self.time_end
            self.time_start += diff
            self.time_end += diff

    # ── Edge Navigation ──

    def jump_next_edge(self, any_visible=False):
        if any_visible:
            sids = self.display_order[self.sig_offset:self.sig_offset + self.visible_signals]
        else:
            sid = self.focused_signal_id()
            sids = [sid] if sid else []

        best = None
        for sid in sids:
            sig = self.vcd.signals.get(sid)
            if sig:
                t = sig.next_edge_after(self.cursor_time)
                if t is not None and (best is None or t < best):
                    best = t
        if best is not None:
            self.cursor_time = best
            self._ensure_cursor_visible()

    def jump_prev_edge(self, any_visible=False):
        if any_visible:
            sids = self.display_order[self.sig_offset:self.sig_offset + self.visible_signals]
        else:
            sid = self.focused_signal_id()
            sids = [sid] if sid else []

        best = None
        for sid in sids:
            sig = self.vcd.signals.get(sid)
            if sig:
                t = sig.prev_edge_before(self.cursor_time)
                if t is not None and (best is None or t > best):
                    best = t
        if best is not None:
            self.cursor_time = best
            self._ensure_cursor_visible()

    def _ensure_cursor_visible(self):
        span = self.time_end - self.time_start
        ct = self.cursor_time
        if ct < self.time_start or ct > self.time_end:
            self.time_start = max(0, ct - span // 4)
            self.time_end = self.time_start + span

    # ── Signal Management ──

    def add_signals(self, patterns):
        """Add every signal matching these name/glob patterns to the display.

        The only other ways in are interactive (the '/' finder, the B tree
        browser), so this is what a script or an agent uses. Returns a short
        report string. Keeps _display_order_set in sync and is undoable.
        """
        if isinstance(patterns, str):
            patterns = [patterns]
        pats = [p.strip() for p in patterns if p and p.strip()]
        if not pats:
            return "add: give a signal name or glob (e.g. 'top.alu.*')"
        added = []
        for sid in self.vcd.signal_order:
            if sid in self._display_order_set:
                continue
            sig = self.vcd.signals.get(sid)
            if not sig:
                continue
            for p in pats:
                if (fnmatch.fnmatch(sig.full_name, p) or fnmatch.fnmatch(sig.name, p)
                        or p == sig.full_name or p == sig.name):
                    added.append(sid)
                    break
        if not added:
            return "add: nothing matched %s" % ", ".join(pats)
        for sid in added:
            self.display_order.append(sid)
            self._display_order_set.add(sid)
        self._invalidate_scope_cache()

        def _undo(sids=list(added)):
            gone = set(sids)
            remove_display_ids(self.display_order, gone)
            self._display_order_set.difference_update(gone)
        self.push_undo("add %d signal%s" % (len(added), "s" * (len(added) != 1)), _undo)
        self._rebuild_view_rows()
        self._focus_on(added[0])
        self._needs_layout = True
        return "Added %d: %s" % (len(added),
                                 ", ".join(self.get_signal(s).full_name for s in added[:6])
                                 + (" …" if len(added) > 6 else ""))

    def remove_signals(self, patterns):
        """Remove every displayed signal matching these patterns. Undoable."""
        if isinstance(patterns, str):
            patterns = [patterns]
        pats = [p.strip() for p in patterns if p and p.strip()]
        if not pats:
            return "remove: give a signal name or glob"
        removed = []
        for i, sid in enumerate(self.display_order):
            sig = self.get_signal(sid)
            if not sig:
                continue
            for p in pats:
                if (fnmatch.fnmatch(sig.full_name, p) or fnmatch.fnmatch(sig.name, p)
                        or p == sig.full_name or p == sig.name):
                    removed.append((i, sid))
                    break
        if not removed:
            return "remove: nothing displayed matched %s" % ", ".join(pats)
        gone = {s for _, s in removed}
        remove_display_ids(self.display_order, gone)
        self._display_order_set.difference_update(gone)
        self._scope_check_cache = {}

        def _undo(items=list(removed)):
            for i, sid in items:                      # ascending: indices hold
                if sid not in self._display_order_set:
                    self.display_order.insert(min(i, len(self.display_order)), sid)
                    self._display_order_set.add(sid)
        self.push_undo("remove %d signal%s" % (len(removed), "s" * (len(removed) != 1)),
                       _undo)
        self._rebuild_view_rows()
        self._needs_layout = True
        return "Removed %d signal%s" % (len(removed), "s" * (len(removed) != 1))

    def _focus_on(self, sid):
        """Put the focus on `sid`'s row, scrolling the minimum needed to keep
        it visible. Rows are view_rows entries, so this stays correct when
        group headers sit between signals.

        If the target is hidden inside a collapsed group, unfold the chain
        first — a jump that silently did nothing (find/next-match/module
        jumps all land here) reads as the command being broken."""
        self._rebuild_view_rows()
        if sid not in self.view_rows:
            for name in self._group_chain(self._member_group.get(sid)):
                g = self._group_by_name.get(name)
                if g is not None:
                    g['collapsed'] = False
            self._rebuild_view_rows()
        try:
            row = self.view_rows.index(sid)
        except ValueError:
            return
        vis = max(1, self.visible_signals)
        if row < self.sig_offset:
            self.sig_offset = row
        elif row >= self.sig_offset + vis:
            self.sig_offset = row - vis + 1
        self.focus_idx = row - self.sig_offset

    def move_signal(self, direction):
        """Move the focused signal up (-1) or down (+1) in display_order.

        Resolves the focused row to a sig_id first: sig_offset + focus_idx is
        an index into view_rows, which includes group headers, so using it
        directly against display_order moved the wrong signal.
        """
        sid = self.focused_signal_id()
        if sid is None:
            return                      # a group header row — nothing to move
        try:
            idx = self.display_order.index(sid)
        except ValueError:
            return
        new_idx = idx + direction
        if not 0 <= new_idx < len(self.display_order):
            return
        do = self.display_order
        other = do[new_idx]                     # the signal we swap past
        do[idx], do[new_idx] = do[new_idx], do[idx]

        def _undo(sid=sid, other=other, direction=direction, back=idx):
            # Anchor on `other` rather than on absolute indices: display_order
            # can be mutated between the move and the undo by paths that don't
            # push onto the stack (tree browser, ;loadgtkw, ;derive), and by
            # then a saved index points at an unrelated signal.
            do = self.display_order
            if sid not in do:
                return
            do.remove(sid)
            if other in do:
                at = do.index(other)
                do.insert(at if direction > 0 else at + 1, sid)
            else:
                do.insert(min(back, len(do)), sid)
        self.push_undo("move signal", _undo)
        self._rebuild_view_rows()
        self._focus_on(sid)             # follow the signal, not the row number

    def delete_signal(self):
        """Remove the focused signal from the display (see move_signal on why
        the row is resolved to a sig_id first)."""
        sid = self.focused_signal_id()
        if sid is None:
            return                      # a group header row — Tab folds it
        try:
            idx = self.display_order.index(sid)
        except ValueError:
            return
        self.display_order.pop(idx)
        self._display_order_set.discard(sid)   # keep the finder's view in sync
        self._scope_check_cache = {}
        sig = self.get_signal(sid)
        name = sig.name if sig else sid
        # Anchor on the following signal so undo restores the original slot
        # even if the list shifted in the meantime.
        anchor = self.display_order[idx] if idx < len(self.display_order) else None

        def _undo(sid=sid, anchor=anchor, back=idx):
            do = self.display_order
            if sid in self._display_order_set:
                return
            at = do.index(anchor) if anchor in do else min(back, len(do))
            do.insert(at, sid)
            self._display_order_set.add(sid)
        self.push_undo(f"remove {name}", _undo)
        self._rebuild_view_rows()       # also clamps sig_offset / focus_idx

    # ── Undo (one stack of inverse actions; `u`) ──

    def push_undo(self, desc, fn):
        """Record the inverse of a layout/styling mutation. `fn` must restore
        the previous state when called; `desc` is shown by undo()."""
        self._undo_stack.append((desc, fn))
        if len(self._undo_stack) > 100:
            self._undo_stack.pop(0)

    def undo(self):
        if not self._undo_stack:
            self._show_message("Nothing to undo")
            return
        desc, fn = self._undo_stack.pop()
        fn()
        self._sync_display_set()
        self._reindex_groups()
        self._rebuild_view_rows()
        self._needs_layout = True
        self._show_message(f"Undid: {desc}")

    def _undo_groups_snapshot(self):
        """Inverse closure restoring the whole groups list as it is now
        (used by ;group / ;ungroup — group edits are compound)."""
        def _copy(gs):
            return [{'name': g['name'], 'members': set(g['members']),
                     'collapsed': g['collapsed'], 'parent': g.get('parent')}
                    for g in gs]
        snap = _copy(self.groups)
        order = copy_order(self.display_order)     # ;group gathers/adds rows too

        def _undo():
            self.groups = _copy(snap)
            self.display_order = copy_order(order)
            self._sync_display_set()
            self._reindex_groups()
            self._rebuild_view_rows()
        return _undo

    def _undo_dict_entry(self, d, sid):
        """Inverse closure for a single dict-entry mutation (color/height/
        radix maps): restores sid's previous value or absence."""
        had, prev = sid in d, d.get(sid)

        def _undo():
            if had:
                d[sid] = prev
            else:
                d.pop(sid, None)
        return _undo

    # ── Scope Folding ──


    def collapse_all_modules(self):
        """Collapse all first-level sub-scopes, showing only the root scope's
        signals. (Collapsing at parts[:1] used to fold the ROOT scope itself,
        which hid every signal — a trace with top/top.alu started EMPTY until
        the user expanded scopes by hand.)"""
        root = self.vcd.scopes
        if hasattr(root, 'hierarchy'):
            # Preserve explicit folds while resetting the default level.
            extra = self.collapsed_scopes.extra if isinstance(self.collapsed_scopes, ScopeFolds) else self.collapsed_scopes
            self.collapsed_scopes = ScopeFolds(root.hierarchy, extra=extra)
        else:
            for parent in root.children.values():
                for child in parent.children.values():
                    self.collapsed_scopes.add(child.full_path)
        self._rebuild_display_order()
        self.sig_offset = 0
        self.focus_idx = 0

    def expand_all_modules(self):
        """Expand all scopes."""
        self.collapsed_scopes.clear()
        self._rebuild_display_order()

    def _rebuild_display_order(self):
        """Rebuild display_order respecting collapsed scopes.

        Always re-syncs _display_order_set: leaving it stale made everything
        hidden by a collapsed scope look already-displayed, so the '/' finder
        and ';add' both silently refused to add those signals.
        """
        catalog = self.vcd.signals
        if getattr(catalog, 'order', None) is self.vcd.signal_order:
            if isinstance(self.collapsed_scopes, ScopeFolds):
                self.display_order = self.collapsed_scopes.order()
                self._sync_display_set()
                return
            order = DisplayOrder.from_order(catalog, catalog.order)
            from .hierarchy import ScopeIndex, scope_ranges
            nodes = ScopeIndex(self.vcd.scopes).nodes if self.collapsed_scopes else {}
            for path in self.collapsed_scopes:
                node = nodes.get(path)
                if node is not None:
                    order.remove_ranges(scope_ranges(node, catalog, ordered=False))
            self.display_order = order
            self._sync_display_set()
            return
        if not self.collapsed_scopes:
            self.display_order = list(self.vcd.signal_order)
            self._sync_display_set()
            return
        catalog = self.vcd.signals
        if getattr(catalog, 'order', None) is self.vcd.signal_order and hasattr(catalog, 'visible_ids'):
            self.display_order = list(catalog.visible_ids(self.collapsed_scopes))
            self._sync_display_set()
            return
        result = []
        for sid in self.vcd.signal_order:
            sig = self.get_signal(sid)
            # Check if any ancestor scope is collapsed
            hidden = False
            if sig.scope:
                parts = sig.scope.split('.')
                path = ""
                for part in parts:
                    path = f"{path}.{part}" if path else part
                    if path in self.collapsed_scopes:
                        hidden = True
                        break
            if not hidden:
                result.append(sid)
        self.display_order = result
        self._sync_display_set()

    # ── Navigation: Zoom ──

    def zoom_fit_all(self):
        """Zoom to show entire time range."""
        self.time_start = 0
        self.time_end = max(self.vcd.max_time, 10)

    def zoom_to_selection(self):
        """Zoom to fit between cursor A and cursor B."""
        if not self.cursor_b_on:
            return
        t1, t2 = min(self.cursor_a, self.cursor_b), max(self.cursor_a, self.cursor_b)
        margin = max((t2 - t1) // 20, 1)
        self.time_start = max(0, t1 - margin)
        self.time_end = t2 + margin

    def snap_cursor_to_edge(self):
        """Snap cursor to nearest transition on focused signal."""
        sid = self.focused_signal_id()
        if not sid:
            return
        sig = self.get_signal(sid)
        t = self.cursor_time
        nxt = sig.next_edge_after(t - 1)  # include current position
        prv = sig.prev_edge_before(t + 1)
        best = None
        if nxt is not None and prv is not None:
            best = nxt if abs(nxt - t) <= abs(prv - t) else prv
        elif nxt is not None:
            best = nxt
        elif prv is not None:
            best = prv
        if best is not None:
            self.cursor_time = best
            self._ensure_cursor_visible()

    def jump_next_marker(self):
        """Jump cursor to next marker after current position."""
        if not self.markers:
            return
        ct = self.cursor_time
        future = sorted([m.time for m in self.markers if m.time > ct])
        if future:
            self.cursor_time = future[0]
        else:
            self.cursor_time = sorted([m.time for m in self.markers])[0]
        self._ensure_cursor_visible()

    def jump_prev_marker(self):
        """Jump cursor to previous marker before current position."""
        if not self.markers:
            return
        ct = self.cursor_time
        past = sorted([m.time for m in self.markers if m.time < ct], reverse=True)
        if past:
            self.cursor_time = past[0]
        else:
            self.cursor_time = sorted([m.time for m in self.markers])[-1]
        self._ensure_cursor_visible()

    # ── Analysis ──

    def get_frequency_info(self):
        """Get frequency/period of focused signal. Returns string or None."""
        sid = self.focused_signal_id()
        if not sid:
            return None
        sig = self.get_signal(sid)
        if not sig: return None
        period, _ = sig.detect_frequency()
        if period is None:
            return None
        period_str = self.vcd.time_to_str(period)
        return f"T={period_str}"

    def get_transition_count(self):
        """Get transition count in current view for focused signal."""
        sid = self.focused_signal_id()
        if not sid:
            return None
        sig = self.get_signal(sid)
        return sig.transition_count_in_window(self.time_start, self.time_end)

    def do_pattern_search(self):
        """Interactive pattern/trigger search: find when conditions meet."""
        if not load_analysis_data(self):
            return
        # Prompt for conditions: "signal=value [AND signal=value]"
        inp = self.prompt_input("Trigger (sig=val [AND sig=val]): ")
        if not inp:
            return
        conditions = [c.strip() for c in inp.split('AND')]
        pairs = []
        for cond in conditions:
            if '=' not in cond:
                continue
            sname, sval = cond.split('=', 1)
            sname = sname.strip()
            sval = sval.strip()
            # Find signal by name
            found_sig = None
            for sid in self.vcd.signal_order:
                sig = self.get_signal(sid)
                if sig.name.lower() == sname.lower() or sig.full_name.lower() == sname.lower():
                    found_sig = sig
                    break
            if found_sig is None:
                return  # signal not found
            pairs.append((found_sig, sval))

        if not pairs:
            return

        # Use first signal as primary, rest as conditions
        primary_sig, primary_val = pairs[0]
        other_conditions = pairs[1:]

        t = primary_sig.find_pattern(primary_val, other_conditions, self.cursor_time + 1)
        if t is None:
            t = primary_sig.find_pattern(primary_val, other_conditions, 0)
        if t is not None:
            self.cursor_time = t
            self._ensure_cursor_visible()

    # ── Export ──

    def export_csv(self):
        """Export focused signal's changes to CSV."""
        sid = self.focused_signal_id()
        if not sid:
            return
        sig = self.get_signal(sid)
        if not sig:
            return
        fname = f"/tmp/vcd_export_{sig.name}.csv"
        try:
            from .exporting import write_signal_csv
            self.vcd.require_loaded()
            count = write_signal_csv(sig, fname, self.vcd.min_time, self.vcd.max_time)
            self._show_message(f"Exported {count} rows → {fname}")
        except Exception as e:
            self._show_message(f"Export error: {e}")

    def export_screenshot(self):
        """Export current view to ANSI-colored text file."""
        fname = "/tmp/vcd_screenshot.txt"
        try:
            with open(fname, 'w') as f:
                # Header
                f.write(f"Alwavelets Screenshot — {os.path.basename(self.vcd._filename)}\n")
                f.write(f"Time range: [{self.time_start}..{self.time_end}]\n")
                f.write("─" * 80 + "\n")

                visible = self.display_order[self.sig_offset:self.sig_offset + self.visible_signals]
                for sig_id in visible:
                    sig = self.get_signal(sig_id)
                    name = sig.full_name[:20].ljust(20)
                    val = format_typed_value(sig, sig.value_at(self.cursor_time), self.radix)

                    if sig.size == 1 and sig.signal_type not in ('real', 'realtime'):
                        chars, _ = draw_single_bit_culled(sig, self.time_start, self.time_end, 60)
                    elif sig_id in self.analog_signals or sig.analog_mode:
                        chars, _ = draw_analog_culled(sig, self.time_start, self.time_end, 60)
                    else:
                        chars, _, labels = draw_bus_culled(sig, self.time_start, self.time_end, 60, self.radix)
                        for col, lbl in labels.items():
                            for j, c in enumerate(lbl):
                                if col + j < 60:
                                    chars[col + j] = c
                    wave = ''.join(chars)
                    f.write(f"{name} {val:>6s} │{wave}\n")

                f.write("─" * 80 + "\n")
            self._show_message(f"Screenshot → {fname}")
        except Exception as e:
            self._show_message(f"Screenshot error: {e}")

    def save_arrangement(self):
        """Save signal arrangement, analog flags, radix to JSON."""
        fname = self.prompt_input("Save arrangement to: ") or "/tmp/vcd_arrangement.json"
        if not fname.endswith('.json'):
            fname += '.json'
        data = {
            **view_order_state(self),
            'analog_signals': list(self.analog_signals),
            **fold_state(self.collapsed_scopes),
            'groups': [{'name': g['name'], 'members': list(g['members']),
                        'collapsed': g['collapsed'], 'parent': g.get('parent')}
                       for g in self.groups],
            'radix': self.radix.value,
            'selected_signals': list(self.selected_signals),
            'markers': [{'time': m.time, 'label': m.label} for m in self.markers],
        }
        try:
            _write_json_atomic(fname, data)
            self._show_message(f"Arrangement saved → {fname}")
        except Exception as e:
            self._show_message(f"Save error: {e}")

    def load_arrangement(self):
        """Load signal arrangement from JSON."""
        fname = self.prompt_input("Load arrangement from: ") or "/tmp/vcd_arrangement.json"
        try:
            with open(fname) as f:
                data = json.load(f)
            data, missing = remap_view_state(self.vcd, data)
            # Validate signal IDs exist
            valid_ids = self.vcd.signals
            saved_order = data.get('display_order', [])
            self.display_order = saved_order if isinstance(saved_order, DisplayOrder) else [s for s in saved_order if s in valid_ids]
            self.analog_signals = set(s for s in data.get('analog_signals', []) if s in valid_ids)
            self.collapsed_scopes = restore_folds(getattr(self.vcd, 'scopes', None), data)
            self.groups = []
            for g in data.get('groups', []):
                mem = set(s for s in g.get('members', []) if s in valid_ids)
                if mem or g.get('parent') is None:
                    self.groups.append({'name': g.get('name', 'group'), 'members': mem,
                                        'collapsed': bool(g.get('collapsed')),
                                        'parent': g.get('parent')})
            self._sync_display_set()
            self._invalidate_scope_cache()
            self._reindex_groups()
            self._rebuild_view_rows()
            radix_val = data.get('radix', 'hex')
            self.radix = next((r for r in Radix if r.value == radix_val), Radix.HEX)
            self.selected_signals = set(s for s in data.get('selected_signals', []) if s in valid_ids)
            self.markers = [Marker(m['time'], m.get('label', '')) for m in data.get('markers', [])]
            self._show_message(f"Arrangement loaded ← {fname}" +
                               (f" ({missing} unresolved signal(s))" if missing else ""))
        except FileNotFoundError:
            self._show_message(f"File not found: {fname}")
        except Exception as e:
            self._show_message(f"Load error: {e}")

    # ── Session persistence (sidecar next to the trace; auto-restored) ──

    @staticmethod
    def session_path(vcd_path, existing=False):
        """Where this trace's session sidecar lives. With existing=True, fall
        back to the pre-rename '.wavelet_session.json' if only that one is
        there, so an upgrade doesn't silently orphan a saved session."""
        base = os.path.splitext(vcd_path)[0]
        new = base + '.alwavelets_session.json'
        if existing and not os.path.exists(new):
            old = base + '.wavelet_session.json'
            if os.path.exists(old):
                return old
        return new

    def _session_state(self):
        """Snapshot everything needed to reopen this trace looking the same:
        layout (order/groups/analog/heights/colors), radixes, markers, and
        the view (zoom window, cursors, display knobs)."""
        return {
            **view_order_state(self),
            'groups': [{'name': g['name'], 'members': sorted(g['members']),
                        'collapsed': g['collapsed'], 'parent': g.get('parent')}
                       for g in self.groups],
            'analog_signals': sorted(self.analog_signals),
            **fold_state(self.collapsed_scopes),
            'radix': self.radix.value,
            'sig_radix': {k: r.value for k, r in self.sig_radix.items()},
            'sig_color': self.sig_color,
            'sig_height': self.sig_height,
            'markers': [{'time': m.time, 'label': m.label} for m in self.markers],
            'view': [self.time_start, self.time_end],
            'cursors': [self.cursor_a, self.cursor_b, self.cursor_b_on],
            'name_width': self.name_width,
            'signal_spacing': self.signal_spacing,
            'show_grid': self.show_grid,
            'line_thickness': self.line_thickness,
            'value_label_size': self.value_label_size,
            'theme': self.current_theme,
            'workmode': getattr(self, 'workmode', 'classic'),
            'menu_bar': getattr(self, 'show_menu_bar', True),
            'scope_separators': getattr(self, 'show_module_sep', False),
        }

    def save_session(self):
        """Write the session sidecar (<trace>.alwavelets_session.json). Once it
        exists, reopening the trace restores it automatically."""
        src = getattr(self.vcd, '_filename', None)
        if not src:
            self._show_message("No trace file to save a session for")
            return
        path = self.session_path(src)
        try:
            _write_json_atomic(path, self._session_state())
            self._show_message(f"Session saved → {os.path.basename(path)} (auto-restores)")
        except Exception as e:
            self._show_message(f"Session save error: {e}")

    def clear_session(self):
        """Delete the session sidecar so the trace opens fresh again."""
        src = getattr(self.vcd, '_filename', None)
        path = self.session_path(src, existing=True) if src else None
        if path and os.path.exists(path):
            try:
                os.remove(path)
                self._show_message("Session cleared — next open starts fresh")
            except OSError as e:
                self._show_message(f"Session clear error: {e}")
        else:
            self._show_message("No saved session for this trace")

    def restore_session(self):
        """Apply the session sidecar if one exists. Returns True if applied.
        Every field is optional and validated, so a stale sidecar (signals
        renamed, file regenerated) degrades gracefully instead of failing."""
        src = getattr(self.vcd, '_filename', None)
        if not src:
            return False
        path = self.session_path(src, existing=True)
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return False
        try:
            data, missing = remap_view_state(self.vcd, data)
            valid = self.vcd.signals
            saved_order = data.get('display_order', [])
            order = saved_order if isinstance(saved_order, DisplayOrder) else [s for s in saved_order if s in valid]
            if order or data.get('version') in (2, 3):
                self.display_order = order
            self.analog_signals = {s for s in data.get('analog_signals', []) if s in valid}
            self.collapsed_scopes = restore_folds(getattr(self.vcd, 'scopes', None), data)
            # Keep every saved group, members or not: an intermediate node in
            # a nesting chain legitimately has none, and dropping it orphans
            # its children (_reindex_groups prunes the genuinely dead ones).
            self.groups = [{'name': g.get('name', 'group'),
                            'members': {s for s in g.get('members', []) if s in valid},
                            'collapsed': bool(g.get('collapsed')),
                            'parent': g.get('parent')}
                           for g in data.get('groups', [])]
            self._reindex_groups()
            by_val = {r.value: r for r in Radix}
            self.radix = by_val.get(data.get('radix'), self.radix)
            self.sig_radix = {k: by_val[v] for k, v in data.get('sig_radix', {}).items()
                              if k in valid and v in by_val}
            self.sig_color = {k: int(v) for k, v in data.get('sig_color', {}).items()
                              if k in valid}
            self.sig_height = {k: int(v) for k, v in data.get('sig_height', {}).items()
                               if k in valid}
            apply_session_times(self, data)
            mode = data.get('workmode')
            if isinstance(mode, str) and mode in self.WORKMODES:
                self.apply_workmode(mode, notify=False)
            for key, attr in (('menu_bar', 'show_menu_bar'), ('scope_separators', 'show_module_sep')):
                if isinstance(data.get(key), bool):
                    setattr(self, attr, data[key])
            if 'name_width' in data:
                self.name_width = max(8, int(data['name_width']))
                self._name_width_manual = True
            if 'signal_spacing' in data:
                # float, not int: the fractional steps (0.25/0.5) would floor
                # to 0 and silently lose the user's spacing.
                self.signal_spacing = float(data['signal_spacing'])
            if 'show_grid' in data:
                self.show_grid = int(data['show_grid']) % 3
            if 'line_thickness' in data:
                self.line_thickness = max(0, min(2, int(data['line_thickness'])))
                _update_wave_chars(1 if self.line_thickness == 2 else self.line_thickness)
            if 'value_label_size' in data:
                self.value_label_size = int(data['value_label_size']) % 3
            theme = data.get('theme')
            if theme in self.THEMES:
                self.current_theme = theme
                self.apply_theme()
            self._sync_display_set()
            self._rebuild_view_rows()
            self.update_layout()
            self._show_message("Session restored  (;session clear to forget it)" +
                               (f" — {missing} unresolved signal(s)" if missing else ""))
            return True
        except Exception as exc:
            if isinstance(data, dict) and data.get('version') == 3:
                self._show_message('Session not restored: ' + str(exc))
            return False    # stale/corrupt sidecar: open normally

    def _show_message(self, msg):
        """Flash a message on the status bar.

        Non-blocking: it just records the text + an expiry time and requests a
        redraw. draw_status_bar() renders it over the status row until it
        expires, and the run loop clears it on an idle tick. (It used to block
        the whole UI for 1.5s via curses.napms — which made every message-
        flashing key, e.g. Tab/s, feel sluggish.)"""
        self._message = msg
        self._message_expire = time.monotonic() + self._message_secs
        self._dirty = True

    # ── Time formatting ──

    def tick_seconds(self):
        """Seconds per timescale tick — the factor that turns a tick delta
        into a real frequency. Single source for the status bar and ;stats."""
        from .ui_drawing import _UNIT_S
        return (_UNIT_S.get(self.vcd.timescale_unit, 1e-9)
                * (self.vcd.timescale_number or 1))

    def format_ruler_time(self, ticks):
        """Format time for ruler: use units if enabled, else raw ticks."""
        if self.show_time_units:
            return self.vcd.time_to_str(ticks)
        return str(ticks)

    def get_signal(self, sig_id):
        """Get signal by ID, supporting both real and virtual signals."""
        if sig_id in self.vcd.signals:
            return self.vcd.signals[sig_id]
        if sig_id in self.virtual_signals:
            return self.virtual_signals[sig_id]
        return None

    def get_signal_radix(self, sig_id):
        """Get radix for signal (per-signal or global)."""
        return self.sig_radix.get(sig_id, self.radix)

    get_radix = get_signal_radix  # alias

    @staticmethod
    def _fmt_num(nv):
        """Compact numeric formatting for analog values/labels."""
        if nv == int(nv) and abs(nv) < 1e15:
            return str(int(nv))
        return "%.4g" % nv

    def format_signal_value(self, sig, sig_id):
        """Format the signal's value at the cursor (numeric for analog signals,
        translated/radix-formatted otherwise)."""
        if getattr(self.vcd, '_store', None) is not None:
            from .background import BackgroundQueries
            if not hasattr(self, '_queries'):
                self._queries = BackgroundQueries()
            cursor = self.cursor_time
            val = self._queries.request('values', (id(self.vcd), cursor),
                                        (sig_id, sig.num_changes()), lambda: sig.value_at(cursor), None)
            if val is None:
                return '…'
        else:
            val = sig.value_at(self.cursor_time)
        if sig_id in self.analog_signals or getattr(sig, 'analog_mode', False):
            nv = numeric_value(val, sig.signal_type)
            if nv is not None:
                return self._fmt_num(nv)
        # Check translations first
        translated = self.translations.translate(sig.name, val, sig.size)
        if translated:
            return translated
        # Per-signal radix
        radix = self.get_signal_radix(sig_id)
        return format_typed_value(sig, val, radix)

    # ── Tree Browser ──

    def _sync_display_set(self):
        """Sync the display_order_set with display_order list."""
        self._display_order_set = (DisplayMembership(self.display_order) if isinstance(self.display_order, DisplayOrder)
                                   else set(self.display_order))
        self._scope_check_cache = {}

    def _invalidate_scope_cache(self):
        self._scope_check_cache = {}

    def _rebuild_tree(self):
        """Rebuild scope runs; resolve individual signals only for visible rows."""
        index = getattr(self, '_scope_index', None)
        if index is None or index.root is not self.scope_tree:
            self._scope_index = ScopeIndex(self.scope_tree)
            self._scope_check_cache = {}
        self._tree_flat = TreeRows(self._scope_index, self.tree_expanded, self.get_signal,
                                   lambda sid: sid in self._display_order_set)
        last = max(0, len(self._tree_flat) - 1)
        self.tree_cursor = min(getattr(self, 'tree_cursor', 0), last)
        self.tree_scroll = min(getattr(self, 'tree_scroll', 0), self.tree_cursor)
        selected = getattr(self, 'panel_selected_scope', None)
        if selected is not None and selected not in self._scope_index.nodes:
            self.panel_selected_scope = None
            self._panel_update_selected_scope()

    def expand_bus(self, sig_id=None):
        """Expand focused bus signal into individual bit lanes."""
        if sig_id is None:
            sig_id = self.focused_signal_id()
        if not sig_id:
            return
        sig = self.get_signal(sig_id)
        if not sig or sig.size <= 1:
            return
        if sig_id in self.expanded_buses:
            self.collapse_bus(sig_id)
            return

        # Create virtual bit signals
        virt_ids = []
        for bit in range(sig.size - 1, -1, -1):  # MSB first
            virt = VirtualBitSignal(sig, bit)
            self.virtual_signals[virt.identifier] = virt
            virt_ids.append(virt.identifier)

        self.expanded_buses[sig_id] = virt_ids

        # Insert virtual signals into display_order after parent
        idx = self.display_order.index(sig_id) if sig_id in self.display_order else len(self.display_order)
        for i, vid in enumerate(virt_ids):
            self.display_order.insert(idx + 1 + i, vid)

    def collapse_bus(self, sig_id=None):
        """Collapse expanded bus back to single signal."""
        if sig_id is None:
            sig_id = self.focused_signal_id()
        if not sig_id or sig_id not in self.expanded_buses:
            return
        virt_ids = self.expanded_buses.pop(sig_id)
        for vid in virt_ids:
            if vid in self.display_order:
                self.display_order.remove(vid)
            self.virtual_signals.pop(vid, None)

    def expand_float_fields(self):
        """Expand focused bus into IEEE 754 float fields (sign/exp/mantissa/value)."""
        sid = self.focused_signal_id()
        if not sid:
            return
        sig = self.get_signal(sid)
        if not sig or sig.size < 16:
            self._show_message("Signal must be ≥16 bits for float decode")
            return

        # If already expanded, collapse
        if sid in self.expanded_buses:
            self.collapse_bus(sid)
            return

        # Auto-detect format from signal width
        if sig.size == 16:
            fmt = Radix.FLOAT16
        elif sig.size == 32:
            fmt = Radix.FLOAT32
        elif sig.size == 64:
            fmt = Radix.FLOAT64
        else:
            # Prompt user
            choice = self.prompt_input(f"Float format for {sig.size}-bit ({sig.name}): [h]alf [s]ingle [d]ouble: ")
            if not choice:
                return
            c = choice[0].lower()
            if c == 'h':
                fmt = Radix.FLOAT16
            elif c == 's':
                fmt = Radix.FLOAT32
            elif c == 'd':
                fmt = Radix.FLOAT64
            else:
                return

        # Set the signal's radix to the float format
        self.sig_radix[sid] = fmt

        # Create field decomposition signals
        fields = make_float_field_signals(sig, fmt)
        virt_ids = []
        for field_name, field_sig in fields:
            vid = field_sig.identifier
            self.virtual_signals[vid] = field_sig
            virt_ids.append(vid)
            # Auto-set the float value channel to analog
            if field_name == 'value':
                self.analog_signals.add(vid)
            # Auto-set exponent to decimal radix
            if field_name == 'exp':
                self.sig_radix[vid] = Radix.DEC

        self.expanded_buses[sid] = virt_ids

        # Insert into display after parent
        idx = self.display_order.index(sid) if sid in self.display_order else len(self.display_order)
        for i, vid in enumerate(virt_ids):
            self.display_order.insert(idx + 1 + i, vid)

    # ── Per-Signal Properties ──

    def cycle_signal_radix(self):
        """Cycle radix on focused signal."""
        sid = self.focused_signal_id()
        if not sid:
            return
        self.push_undo("radix change", self._undo_dict_entry(self.sig_radix, sid))
        current = self.sig_radix.get(sid, self.radix)
        radixes = list(Radix)
        nxt = radixes[(radixes.index(current) + 1) % len(radixes)]
        self.sig_radix[sid] = nxt

    USER_COLORS = [1, 2, 3, 4, 5, 6]  # map to color pairs

    def cycle_signal_color(self):
        """Cycle color assignment on focused signal."""
        sid = self.focused_signal_id()
        if not sid:
            return
        self.push_undo("color change", self._undo_dict_entry(self.sig_color, sid))
        current = self.sig_color.get(sid, 0)
        nxt = (current + 1) % (len(self.USER_COLORS) + 1)  # 0 = default
        if nxt == 0:
            self.sig_color.pop(sid, None)
        else:
            self.sig_color[sid] = nxt

    def cycle_signal_height(self):
        """Cycle waveform height (1-4 rows) on focused signal."""
        sid = self.focused_signal_id()
        if not sid:
            return
        self.push_undo("height change", self._undo_dict_entry(self.sig_height, sid))
        current = self.sig_height.get(sid, 1)
        nxt = (current % 4) + 1
        self.sig_height[sid] = nxt
        self._needs_layout = True   # taller analog reserves more rows

    # ── Edge-to-Edge Measurement ──

    def start_edge_measure(self):
        """Begin edge-to-edge measurement mode."""
        sid = self.focused_signal_id()
        if not sid:
            return
        if self.measure_sig_a is None:
            self.measure_sig_a = sid
            self.measure_mode = True
            self.measure_result = f"Select signal B (focus + press 'e' again)"
        elif self.measure_sig_b is None:
            self.measure_sig_b = sid
            self._compute_edge_measure()
            self.measure_mode = False
        else:
            # Reset
            self.measure_sig_a = sid
            self.measure_sig_b = None
            self.measure_result = f"Select signal B"

    def _compute_edge_measure(self):
        """Compute time between edges on two signals near cursor."""
        sig_a = self.get_signal(self.measure_sig_a)
        sig_b = self.get_signal(self.measure_sig_b)
        if not sig_a or not sig_b:
            self.measure_result = "Invalid signals"
            return
        ct = self.cursor_time
        # Find nearest edge on each signal
        edge_a = sig_a.next_edge_after(ct - 1)
        edge_b = sig_b.next_edge_after(ct - 1)
        if edge_a is None or edge_b is None:
            self.measure_result = "No edges found"
            return
        delta = abs(edge_b - edge_a)
        self.measure_result = f"{sig_a.name}→{sig_b.name}: Δ{self.vcd.time_to_str(delta)}"

    # ── Protocol Decode ──

    def setup_spi_decode(self):
        """Interactive SPI decode setup."""
        if not load_analysis_data(self, self.time_start, self.time_end):
            return
        clk_name = self.prompt_input("SPI CLK signal name: ")
        mosi_name = self.prompt_input("SPI MOSI signal name: ")
        if not clk_name or not mosi_name:
            return
        clk_sig = self._find_signal_by_name(clk_name)
        mosi_sig = self._find_signal_by_name(mosi_name)
        if not clk_sig or not mosi_sig:
            self._show_message("Signal not found")
            return
        cs_name = self.prompt_input("SPI CS signal (empty=none): ")
        cs_sig = self._find_signal_by_name(cs_name) if cs_name else None

        decoder = SPIDecoder(clk_sig, mosi_sig, cs_sig=cs_sig)
        self._decode_protocol(decoder, mosi_sig, 'SPI')

    def setup_i2c_decode(self):
        """Interactive I2C decode setup."""
        if not load_analysis_data(self, self.time_start, self.time_end):
            return
        sda_name = self.prompt_input("I2C SDA signal name: ")
        scl_name = self.prompt_input("I2C SCL signal name: ")
        if not sda_name or not scl_name:
            return
        sda_sig = self._find_signal_by_name(sda_name)
        scl_sig = self._find_signal_by_name(scl_name)
        if not sda_sig or not scl_sig:
            self._show_message("Signal not found")
            return
        decoder = I2CDecoder(sda_sig, scl_sig)
        self._decode_protocol(decoder, sda_sig, 'I2C')

    def setup_uart_decode(self):
        """Interactive UART decode setup."""
        if not load_analysis_data(self, self.time_start, self.time_end):
            return
        tx_name = self.prompt_input("UART TX/RX signal name: ")
        baud_str = self.prompt_input("Baud period (ticks, e.g. 104 for 9600@1MHz): ")
        if not tx_name or not baud_str:
            return
        tx_sig = self._find_signal_by_name(tx_name)
        if not tx_sig:
            self._show_message("Signal not found")
            return
        try:
            baud = int(baud_str)
        except ValueError:
            self._show_message("Invalid baud period")
            return
        decoder = UARTDecoder(tx_sig, baud)
        self._decode_protocol(decoder, tx_sig, 'UART')

    def do_protocol_decode_menu(self):
        """Protocol decode sub-menu."""
        choice = self.prompt_input("Decode: [s]pi [i]2c [u]art [c]lear: ")
        if not choice:
            return
        c = choice[0].lower()
        if c == 's':
            self.setup_spi_decode()
        elif c == 'i':
            self.setup_i2c_decode()
        elif c == 'u':
            self.setup_uart_decode()
        elif c == 'c':
            self._clear_decode_results()
            self._show_message("Decode overlays cleared")

    def _find_signal_by_name(self, name):
        """Find signal object by name (case-insensitive)."""
        name = name.strip().lower()
        for sig in self.vcd.signals.values():
            if sig.name.lower() == name or sig.full_name.lower() == name:
                return sig
        return None

    # ── Tcl Scripting ──

    def do_edge_to_edge(self):
        """Measure time between edges on two signals (setup/hold timing)."""
        if not self.cursor_on:
            self._show_message("Enable cursor first (c)")
            return

        # Get signal A (focused)
        sid_a = self.focused_signal_id()
        if not sid_a:
            self._show_message("Focus a signal first")
            return
        sig_a = self.get_signal(sid_a)

        # Prompt for signal B
        name_b = self.prompt_input(f"Measure {sig_a.name} → signal: ")
        if not name_b:
            return
        sig_b = None
        for sid in self.vcd.signal_order:
            s = self.get_signal(sid)
            if not s: continue
            if s.name.lower() == name_b.lower() or s.full_name.lower() == name_b.lower():
                sig_b = s
                break
        if not sig_b:
            self._show_message(f"Signal not found: {name_b}")
            return

        ct = self.cursor_time

        # Find nearest edge on sig_a at/after cursor
        edge_a = sig_a.next_edge_after(ct - 1)
        if edge_a is None:
            self._show_message(f"No edge on {sig_a.name} near cursor")
            return

        # Find nearest edge on sig_b after edge_a
        edge_b = sig_b.next_edge_after(edge_a)
        if edge_b is None:
            # Try before
            edge_b = sig_b.prev_edge_before(edge_a + 1)
        if edge_b is None:
            self._show_message(f"No edge on {sig_b.name} near cursor")
            return

        delta = edge_b - edge_a
        delta_str = self.vcd.time_to_str(abs(delta))
        sign = "+" if delta >= 0 else "-"
        val_a = sig_a.value_at(edge_a)
        val_b = sig_b.value_at(edge_b)

        msg = f"{sig_a.name}({val_a})@{edge_a} → {sig_b.name}({val_b})@{edge_b} = {sign}{delta_str}"
        self._show_message(msg)
        self.measure_result = msg

    # ── SVG Export ──

    def push_zoom_history(self):
        """Save current zoom state before changing."""
        self.zoom_history.append((self.time_start, self.time_end))
        if len(self.zoom_history) > self.zoom_history_max:
            self.zoom_history.pop(0)

    def pop_zoom_history(self):
        """Restore previous zoom state."""
        if self.zoom_history:
            self.time_start, self.time_end = self.zoom_history.pop()

    # ── Decode Overlay Drawing ──

    def draw_decode_overlay(self, row, sig_id):
        """Draw protocol decode frames on waveform row."""
        if sig_id not in self.decode_overlays:
            return
        frames = self.decode_overlays[sig_id]
        span = self.time_end - self.time_start
        if span <= 0:
            return
        from .results import PagedResults
        if isinstance(frames, PagedResults):
            from .background import BackgroundQueries
            if not hasattr(self, '_queries'):
                self._queries = BackgroundQueries()
            start, end, width = self.time_start, self.time_end, self.wave_width
            runs = self._queries.request('decodes', (id(self.vcd), start, end, width),
                                         (sig_id, frames),
                                         lambda cancel: frames.overlay(start, end, width, cancel=cancel),
                                         [], cancellable=True)
            for col, text in runs:
                try:
                    self.stdscr.addstr(row, self.name_width + 1 + col, text,
                                      curses.color_pair(self.C_BUSLBL) | curses.A_BOLD)
                except curses.error:
                    pass
            return
        for frame in frames:
            if frame.t_end < self.time_start or frame.t_start > self.time_end:
                continue
            col_start = max(0, int((frame.t_start - self.time_start) / span * self.wave_width))
            col_end = min(self.wave_width, int((frame.t_end - self.time_start) / span * self.wave_width))
            if col_end <= col_start:
                continue
            # Draw frame box with label
            ac = self.name_width + 1 + col_start
            width = col_end - col_start
            label = frame.label[:width]
            try:
                self.stdscr.addstr(row, ac, label.center(width)[:width],
                                 curses.color_pair(self.C_BUSLBL) | curses.A_BOLD)
            except curses.error:
                pass

    # ── Drawing ──

    def _attr_for(self, attr_code):
        """Map attribute code to curses attribute with rich colors."""
        if attr_code == 0:  return curses.color_pair(self.C_HIGH)              # high / normal
        if attr_code == 1:  return curses.color_pair(self.C_EDGE) | curses.A_BOLD  # transition
        if attr_code == 2:  return curses.color_pair(self.C_UNDEF)             # unknown x
        if attr_code == 3:  return curses.color_pair(self.C_HIZ)               # high-z
        if attr_code == 4:  return curses.color_pair(self.C_ANALOG)            # analog
        if attr_code == 5:  return curses.color_pair(self.C_LOW)               # low / 0
        if attr_code == 6:  return curses.color_pair(self.C_BUS)               # bus body
        if attr_code == 7:  return curses.color_pair(self.C_ANHI)              # analog hot
        if attr_code == 8:  return curses.color_pair(self.C_ANMID)             # analog warm
        if attr_code == 9:  return curses.color_pair(self.C_ANLO)              # analog cool
        return curses.A_NORMAL

    def _batched_addstr(self, row, start_col, chars, attrs):
        """Write chars to screen batching consecutive same-attribute runs."""
        if not chars:
            return
        max_col = self.width - 1
        i = 0
        n = len(chars)
        while i < n:
            col = start_col + i
            if col >= max_col:
                break
            # Find run of same attribute
            j = i + 1
            while j < n and attrs[j] == attrs[i] and start_col + j < max_col:
                j += 1
            run = ''.join(chars[i:j])
            attr = self._attr_for(attrs[i])
            try:
                self.stdscr.addstr(row, col, run, attr)
            except curses.error:
                pass
            i = j



    def _focused_order_index(self):
        """Index of the focused signal in display_order, or None.

        sig_offset + focus_idx indexes view_rows (which includes group header
        rows), so it must never be used against display_order directly.
        """
        sid = self.focused_signal_id()
        if sid is None:
            return None
        try:
            return self.display_order.index(sid)
        except ValueError:
            return None

    def _jump_next_module(self):
        """Shift+Down: Jump to first signal of the next different module."""
        sid = self.focused_signal_id()
        if not sid:
            return
        sig = self.get_signal(sid)
        if not sig:
            return
        cur_scope = sig.scope or ''
        cur_idx = self._focused_order_index()
        if cur_idx is None:
            return
        for i in range(cur_idx + 1, len(self.display_order)):
            s = self.get_signal(self.display_order[i])
            if s and (s.scope or '') != cur_scope:
                self._focus_on(self.display_order[i])
                return

    def _jump_prev_module(self):
        """Shift+Up: Jump to first signal of the previous different module."""
        sid = self.focused_signal_id()
        if not sid:
            return
        sig = self.get_signal(sid)
        if not sig:
            return
        cur_scope = sig.scope or ''
        cur_idx = self._focused_order_index()
        if cur_idx is None:
            return
        # Find the start of current module
        mod_start = cur_idx
        for i in range(cur_idx - 1, -1, -1):
            s = self.get_signal(self.display_order[i])
            if s and (s.scope or '') == cur_scope:
                mod_start = i
            else:
                break
        # If we're not at module start, go there first
        if mod_start < cur_idx:
            self._focus_on(self.display_order[mod_start])
            return
        # Otherwise jump to previous module
        for i in range(cur_idx - 1, -1, -1):
            s = self.get_signal(self.display_order[i])
            if s and (s.scope or '') != cur_scope:
                # Found prev module — now find its start
                prev_scope = s.scope or ''
                start = i
                for j in range(i - 1, -1, -1):
                    s2 = self.get_signal(self.display_order[j])
                    if s2 and (s2.scope or '') == prev_scope:
                        start = j
                    else:
                        break
                self._focus_on(self.display_order[start])
                return

    def _collapse_focused_scope(self):
        """Shift+Left: Collapse the scope of the focused signal."""
        sid = self.focused_signal_id()
        if not sid:
            return
        sig = self.get_signal(sid)
        if not sig or not sig.scope:
            return
        self.collapsed_scopes.add(sig.scope)
        self._rebuild_display_order()
        self._show_message(f"Collapsed: {sig.scope}")

    def _expand_focused_scope(self):
        """Shift+Right: Expand any collapsed child scope at the focused signal."""
        sid = self.focused_signal_id()
        if not sid:
            return
        sig = self.get_signal(sid)
        if not sig:
            return
        cur_scope = sig.scope or ''
        # Expand direct child scopes
        expanded_any = False
        if isinstance(self.collapsed_scopes, ScopeFolds):
            expanded_any = self.collapsed_scopes.expand(cur_scope)
        else:
            for scope in list(self.collapsed_scopes):
                if scope == cur_scope or scope.startswith(cur_scope + '.'):
                    self.collapsed_scopes.discard(scope)
                    expanded_any = True
        if expanded_any:
            self._rebuild_display_order()
            self._show_message(f"Expanded: {cur_scope}")
        else:
            self._show_message("No collapsed children")

    # ── VIM-style navigation ──

    def _vim_goto_first_in_module(self):
        """gg: Move to first signal at the same hierarchy level (same parent scope)."""
        sid = self.focused_signal_id()
        if not sid:
            return
        sig = self.get_signal(sid)
        if not sig:
            return
        cur_scope = sig.scope or ''
        for did in self.display_order:
            s = self.get_signal(did)
            if s and (s.scope or '') == cur_scope:
                self._focus_on(did)
                return

    def _vim_goto_last_in_module(self):
        """G: Move to last signal at the same hierarchy level (same parent scope)."""
        sid = self.focused_signal_id()
        if not sid:
            return
        sig = self.get_signal(sid)
        if not sig:
            return
        cur_scope = sig.scope or ''
        last = None
        for did in self.display_order:
            s = self.get_signal(did)
            if s and (s.scope or '') == cur_scope:
                last = did
        if last is not None:
            self._focus_on(last)

    def _update_visual_selection(self):
        """Update selected signals between visual anchor and current focus."""
        cur_idx = self.sig_offset + self.focus_idx
        lo = min(self._visual_anchor, cur_idx)
        hi = max(self._visual_anchor, cur_idx)
        self.selected_signals.clear()
        for i in range(lo, hi + 1):
            if 0 <= i < len(self.view_rows):
                tok = self.view_rows[i]
                if isinstance(tok, str):          # skip group-header rows
                    self.selected_signals.add(tok)


    def create_bookmark(self):
        name = self.prompt_input("Bookmark name: ")
        if not name: return
        notes = self.prompt_input("Notes (optional): ")
        sig_names = []
        for sid in self.display_order:
            sig = self.get_signal(sid)
            if sig: sig_names.append(sig.full_name)
        bm = WaveformBookmark(
            name=name, time_start=self.time_start, time_end=self.time_end,
            cursor_a=self.cursor_a, cursor_b=self.cursor_b,
            cursor_on=self.cursor_on, active_cursor=self.active_cursor,
            signals=sig_names, notes=notes,
        )
        self.bookmarks.add(bm)
        self._show_message(f"* Bookmark '{name}' saved ({len(sig_names)} signals)")

    def goto_bookmark(self):
        names = self.bookmarks.list_names()
        if not names:
            self._show_message("No bookmarks. Create with ;bm")
            return
        prompt = "Bookmark [" + ", ".join(names[:8]) + ("..." if len(names) > 8 else "") + "]: "
        name = self.prompt_input(prompt)
        if not name: return
        bm = self.bookmarks.get(name)
        if not bm:
            matches = [n for n in names if n.startswith(name)]
            if len(matches) == 1:
                bm = self.bookmarks.get(matches[0])
            else:
                self._show_message(f"Bookmark '{name}' not found")
                return
        self.apply_bookmark(bm)

    def apply_bookmark(self, bm):
        self.time_start = bm.time_start
        self.time_end = bm.time_end
        self.cursor_a = bm.cursor_a
        self.cursor_b = bm.cursor_b
        self.cursor_on = bm.cursor_on
        self.active_cursor = bm.active_cursor
        self.cursor_time = self.cursor_a if self.active_cursor == 'a' else self.cursor_b
        if bm.signals:
            name_to_sid = {s.full_name: sid for sid, s in self.vcd.signals.items()}
            new_order = [name_to_sid[n] for n in bm.signals if n in name_to_sid]
            if new_order:
                self.display_order = new_order
                self._sync_display_set()
        msg = f"* {bm.name}"
        if bm.notes: msg += f": {bm.notes[:40]}"
        self._show_message(msg)

    def save_bookmarks(self):
        path = self.bookmarks.path or self.bookmarks.default_path(self.vcd._filename)
        if self.bookmarks.save(path):
            self._show_message(f"Saved {len(self.bookmarks.bookmarks)} bookmarks")
        else:
            self._show_message("Failed to save bookmarks")

    def list_bookmarks(self):
        names = self.bookmarks.list_names()
        if not names:
            self._show_message("No bookmarks")
            return
        self.markers.clear()
        for name in names:
            bm = self.bookmarks.get(name)
            self.markers.append(Marker(bm.time_start, f"* {name}"))
        self._show_message(f"{len(names)} bookmarks: {', '.join(names[:6])}")



    def prompt_input(self, prompt_text):
        # Under agent control there is nobody at the keyboard to answer, and
        # blocking here would freeze the viewer for the user too. Commands
        # that would prompt become no-ops; agents pass arguments inline.
        if self._remote_depth:
            return ""
        from .ui_prompt import read_prompt
        try:
            return read_prompt(self.stdscr, prompt_text, curses.color_pair(self.C_PROMPT))
        finally:
            self._needs_layout = True  # the terminal can resize while prompting
            self._dirty = True

    # (do_search was replaced by the fuzzy finder — see finder.py. Committing
    # the finder seeds search_term/search_results, so `n` still cycles.)

    def next_search_result(self):
        if getattr(self, '_finder_next_worker', None) is not None:
            self._finder_next_jump = True
            self._show_message('Finding the next displayed match…')
            return
        if not self.search_results:
            return
        self.search_idx = (self.search_idx + 1) % len(self.search_results)
        target = self.search_results[self.search_idx]   # display_order index
        if 0 <= target < len(self.display_order):
            self._focus_on(self.display_order[target])

    def goto_time(self):
        val = self.prompt_input("Go to time: ")
        try:
            t = int(val)
            self.cursor_time = t
            self._ensure_cursor_visible()
        except ValueError:
            pass

    def do_find_value(self):
        if not load_analysis_data(self):
            return
        sid = self.focused_signal_id()
        if not sid:
            return
        sig = self.get_signal(sid)
        if not sig:
            return
        val = self.prompt_input(f"Find value on {sig.name}: ")
        if not val:
            return
        t = sig.find_value(val, self.cursor_time + 1)
        if t is None:
            # Wrap around from start
            t = sig.find_value(val, 0)
        if t is not None:
            self.cursor_time = t
            self._ensure_cursor_visible()

    def place_marker(self):
        label = self.prompt_input(f"Marker label (t={self.cursor_time}): ")
        self.markers.append(Marker(self.cursor_time, label))

    def do_export_menu(self):
        """Show export sub-menu."""
        choice = self.prompt_input("Export: [c]sv [s]creenshot [v]svg [p]df [i]mage [a]rrange [l]oad [t]heme: ")
        if not choice:
            return
        c = choice[0].lower()
        if c == 'c':
            self.export_csv()
        elif c == 's':
            self.export_screenshot()
        elif c == 'v':
            self.export_svg()
        elif c == 'p':
            self.export_pdf()
        elif c == 'i':
            self.export_image()
        elif c == 'a':
            self.save_arrangement()
        elif c == 'l':
            self.load_arrangement()
        elif c == 't':
            fname = self.prompt_input("Theme JSON file: ")
            if fname:
                self.load_theme_file(fname)

    # ── Signal Spacing ──

    SPACING_VALUES = [0, 0.25, 0.5, 1, 2, 3]

    def cycle_signal_spacing(self):
        """Cycle signal spacing: 0 → 0.25 → 0.5 → 1 → 2 → 3 → 0."""
        idx = -1
        for i, v in enumerate(self.SPACING_VALUES):
            if abs(v - self.signal_spacing) < 0.01:
                idx = i
                break
        self.signal_spacing = self.SPACING_VALUES[(idx + 1) % len(self.SPACING_VALUES)]
        self.update_layout()

    # ── Diff Mode ──

    def load_diff_file(self):
        """Load a second VCD file for split-view diff comparison."""
        fname = self.prompt_input("Diff file: ")
        if not fname:
            return
        try:
            self.vcd.require_loaded()
            diff_vcd = parse_file(fname)
        except Exception as e:
            self._show_message(f"Diff load error: {e}")
            return

        self.diff_vcd = diff_vcd
        self.diff_ranges = {}
        ratio = Fraction(diff_vcd.tick_fs, self.vcd.tick_fs)
        self._diff_start = max(self.vcd.min_time, diff_vcd.min_time * ratio)
        self._diff_end = min(self.vcd.max_time, diff_vcd.max_time * ratio)

        # Build signal name → diff signal mapping
        self.diff_sig_map = {}  # sid_in_A -> VCDSignal_in_B

        matched = 0
        differing = 0
        by_full, by_name = {}, {}
        for dsig in diff_vcd.signals.values():
            by_full.setdefault(dsig.full_name, []).append(dsig)
            by_name.setdefault(dsig.name, []).append(dsig)
        for sid in self.vcd.signal_order:
            sig_a = self.vcd.signals.get(sid)
            if not sig_a:
                continue

            candidates = by_full.get(sig_a.full_name) or by_name.get(sig_a.name, [])
            if len(candidates) != 1:
                continue
            sig_b = candidates[0]
            if (sig_b.size, sig_b.signal_type) != (sig_a.size, sig_a.signal_type):
                continue
            if ratio != 1:
                sig_b = TimeScaledSignal(sig_b, ratio)
            matched += 1
            self.diff_sig_map[sid] = sig_b

            ranges = self._compute_diff_ranges(sig_a, sig_b)
            if ranges:
                self.diff_ranges[sid] = ranges
                differing += 1

        self._show_message(f"Split diff: {matched} matched, {differing} differ")

    def _compute_diff_ranges(self, sig_a, sig_b):
        return compare_signal_ranges(sig_a, sig_b, self._diff_start, self._diff_end)

    def clear_diff(self):
        """Clear diff comparison."""
        queries = getattr(self, '_queries', None)
        if queries is not None:
            queries.close()
            del self._queries
        if self.diff_vcd is not None:
            self.diff_vcd.close()
        self.diff_vcd = None
        self.diff_ranges = {}
        self.diff_sig_map = {}

    # ── Mouse ──

    def handle_mouse(self):
        try:
            _, mx, my, _, bstate = curses.getmouse()
        except curses.error:
            return

        # Clamp to the wave area so a drag can run past its edges.
        wave_x = min(max(mx - self.name_width - 1, 0), max(self.wave_width - 1, 0))
        in_wave = 0 <= mx - self.name_width - 1 < self.wave_width
        t = self.col_to_time(wave_x)

        # ── Drag-to-zoom: press anchors, motion tracks, release zooms ──
        if self._drag_anchor is not None:
            if bstate & curses.BUTTON1_RELEASED:
                if abs(wave_x - self._drag_anchor_x) >= 3:   # a real drag
                    t0, t1 = sorted((self._drag_anchor, t))
                    if t1 > t0:
                        self.push_zoom_history()
                        self.time_start, self.time_end = t0, t1
                        self._show_message(f"Zoom: {t0} – {t1}  (u-turn: Z fit all)")
                self._drag_anchor = self._drag_cur = None
                return
            if bstate & curses.REPORT_MOUSE_POSITION:
                self._drag_cur = t                           # live highlight
                return

        if not in_wave:
            return

        if bstate & curses.BUTTON1_CLICKED or bstate & curses.BUTTON1_PRESSED:
            self.cursor_time = t
            # Update focus based on row clicked. int(): signal_spacing can be
            # fractional (0.25/0.5), and // on a float yields a float, which
            # then blows up every list index derived from focus_idx.
            sig_row = int((my - self.wave_top) // (1 + self.signal_spacing))
            if 0 <= sig_row < self.visible_signals:
                self.focus_idx = sig_row
            if bstate & curses.BUTTON1_PRESSED:              # possible drag start
                self._drag_anchor = t
                self._drag_anchor_x = wave_x
                self._drag_cur = None

        elif bstate & curses.BUTTON3_CLICKED:
            self.markers.append(Marker(t, ""))

        elif bstate & curses.BUTTON4_PRESSED:
            self.zoom(0.5, center=t)  # scroll up = zoom in

        # Scroll down = zoom out (try multiple encodings)
        elif bstate & (getattr(curses, 'BUTTON5_PRESSED', 0) | (1 << 21) | 0x200000):
            self.zoom(2.0, center=t)

    # ── Main Loop ──

    def run(self):
        curses.curs_set(0)
        self.stdscr.keypad(True)
        # Smooth scroll: use halfdelay for fluid key repeat
        curses.halfdelay(self.scroll_timeout // 10)  # tenths of a second

        self.update_layout()  # initial layout
        self._needs_layout = False
        # Redraw only when something changed, instead of every halfdelay tick.
        self._dirty = True
        self._bg_was_loading = self.vcd.is_loading
        self._last_bg_state = None

        try:
            while True:
                # Apply anything an agent queued (control.py). Runs here so all
                # viewer mutation stays on this thread; latency is one
                # halfdelay tick at worst.
                if self.control is not None and self.control.pump():
                    self._dirty = True

                queries = getattr(self, '_queries', None)
                if queries is not None and queries.poll():
                    self._dirty = True
                if queries is not None and queries.errors:
                    self._show_message('Read failed: ' + queries.errors.pop(0))
                if self.poll_finder():
                    self._dirty = True
                self.poll_instruction_results()
                job = getattr(self, '_analysis_job', None)
                if job is not None and job.progress != getattr(self, '_analysis_progress', None):
                    self._analysis_progress = job.progress
                    self._dirty = True
                if job is not None and job.future.done():
                    self._analysis_job = None
                    try:
                        result = job.future.result()
                        if job.cancel.is_set():
                            self._show_message('Analysis cancelled')
                        else:
                            job.complete(result)
                            job.result_claimed = True
                    except Exception as exc:
                        self._show_message(str(exc))
                    finally:
                        job.close()
                    self._dirty = True

                pending_times = getattr(self, '_pending_session_times', None)
                if pending_times is not None and self.vcd._extent_known and not self.vcd.is_loading:
                    apply_session_times(self, pending_times)
                    self._needs_layout = True
                    self._dirty = True

                if self._needs_layout:
                    self.update_layout()
                    self._needs_layout = False
                    self._dirty = True

                if self._dirty:
                    self.stdscr.erase()

                    if self.show_manual:
                        self.draw_manual_overlay()
                    elif self.show_help:
                        self.draw_help_overlay()
                    elif self.show_colors:
                        self.draw_color_guide_overlay()
                    elif self.show_finder:
                        self.draw_finder()
                    elif self.show_tree:
                        self.draw_tree_browser()
                    else:
                        self._prepare_row_window()
                        self.draw_header()
                        self.draw_ruler()
                        self.draw_signals()
                        if self.show_annotations:
                            self.draw_annotation_overlays()
                        if self.panel_mode:
                            self.draw_panel_tree()
                        if self.show_reg_panel:
                            self.draw_register_panel()
                        if self._drag_cur is not None:
                            self.draw_drag_selection()
                        if getattr(self, 'show_instruction_results', False):
                            self.draw_instruction_results()
                        self.draw_status_bar()

                    self.stdscr.refresh()
                    self._dirty = False

                try:
                    key = self.stdscr.getch()
                except curses.error:
                    key = -1  # halfdelay timeout
                if key == -1:
                    # Idle: poll the watched file for live reload, and redraw
                    # only if a background load advanced (to animate the
                    # progress indicator) or has just finished.
                    if self._message is not None and time.monotonic() >= self._message_expire:
                        self._message = None       # flash expired: redraw to clear it
                        self._dirty = True
                    if self.check_live_reload():
                        self._dirty = True
                    if self.check_follow():
                        self._dirty = True
                    if self.vcd.is_loading or self._bg_was_loading:
                        state = (self.vcd.is_loading,
                                 self.vcd.load_progress, self.vcd.load_changes)
                        if state != self._last_bg_state:
                            self._last_bg_state = state
                            self._dirty = True
                        self._bg_was_loading = self.vcd.is_loading
                    continue
                self._dirty = True  # any real key/mouse event triggers a redraw

                # ── Resize applies whatever overlay is open; otherwise the
                #    layout stays stale until a second resize ──
                if key == curses.KEY_RESIZE:
                    self._needs_layout = True
                    continue

                # ── Manual takes full control of keys (F1/?? opens it) ──
                if self.show_manual:
                    if not self.manual_handle_key(key):
                        self.show_manual = False
                    continue

                # ── Help dismiss (except ? which upgrades to manual) ──
                if self.show_help:
                    if key == ord('?'):
                        # ?? → switch from quick help to full manual
                        self.show_help = False
                        self.show_manual = True
                    elif key == curses.KEY_F1:
                        self.show_help = False
                        self.show_manual = True
                    else:
                        self.show_help = False
                    continue

                if self.show_colors:
                    self.show_colors = False
                    continue

                if self.show_finder:
                    if not self.finder_handle_key(key):
                        self.show_finder = False
                    continue

                if self.show_tree:
                    if not self.tree_handle_key(key):
                        self.show_tree = False
                    continue

                if getattr(self, 'show_instruction_results', False):
                    if self.instruction_results_handle_key(key) == 'QUIT':
                        break
                    continue

                # Panel mode: the tree gets first dibs on the key. Anything it
                # doesn't consume falls through to the global keymap below, so
                # there is no hand-maintained whitelist of "global" keys to keep
                # in sync (that drift is what once swallowed F5).
                if self.panel_mode and self.panel_focus == 0:
                    if self.panel_handle_tree_key(key):
                        continue

                # ── VIM 'g' prefix (gg = first in module, gt = goto time).
                #    The second key is consumed here: it must not also run
                #    its own binding (gt used to toggle values, then goto).
                if self._pending_g:
                    self._pending_g = False
                    if key == ord('g'):
                        self._vim_goto_first_in_module()
                    elif key == ord('t'):
                        self.goto_time()
                    else:
                        self._show_message("g- cancelled")
                    continue
                if key == ord('g'):
                    self._pending_g = True
                    self._show_message("g-")
                    continue

                # ── Table-driven dispatch (see _build_keymap) ──
                handler = self.keymap.get(key)
                if handler is not None and handler() == 'QUIT':
                    break

        finally:
            self.close_finder()
            self.close_finder_navigation()
            job = getattr(self, '_analysis_job', None)
            if job is not None:
                job.close()
            queries = getattr(self, '_queries', None)
            if queries is not None:
                queries.close()
            if self.diff_vcd is not None:
                self.diff_vcd.close()
            self.vcd.close()
            self._cleanup_mouse()
            if self.control is not None:
                self.control.stop()      # unlink the socket + descriptor
