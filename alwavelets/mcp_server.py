"""A minimal Model Context Protocol (MCP) server for Alwavelets.

Exposes waveform snapshots to an agentic workflow over stdio (newline-delimited
JSON-RPC 2.0), so a calling app can request a list of signals over a time window
and display the rendered waveform (text or SVG) in a box.

No third-party dependencies — the protocol is implemented directly. Run with:

    python3 alwavelets.py --mcp        (or:  python3 -m alwavelets.mcp_server)

Two families of tools:

* **File tools** parse a trace of their own and answer questions about it —
  render_waveform / _svg / _png, measure, list_signals, find_when,
  find_glitches. They are headless and know nothing about anyone's screen.

* **Viewer tools** (viewer_*) attach to a *running* viewer that the user
  started with --control, so an MCP client can drive the session in front of
  them. These are a thin proxy over control.ControlClient — the socket stays
  the single implementation, and MCP is just another transport in front of
  it (the direct-socket contract for non-MCP agents is SKILL.md).
"""
import base64
import contextlib
import json
import os
import sys

from .parsers import parse_file
from .snapshot import render_text, render_svg, list_signals, measure, parse_time
from .graphics import render_waveform_png
from .expr import find_all_expression, ExprError, resolve_signal
from .analysis import find_anomalies

PROTOCOL_VERSION = "2024-11-05"
SUPPORTED_PROTOCOL_VERSIONS = frozenset({PROTOCOL_VERSION})
__version__ = "0.1.0"

# ── parsed-trace cache (most-recent file only, keyed by path+mtime) ──
_cache = {}


def _load(path):
    if not path or not os.path.isfile(path):
        raise FileNotFoundError("file not found: %s" % path)
    key = (os.path.abspath(path), os.path.getmtime(path))
    if key not in _cache:
        _cache.clear()
        # parse_file may print (e.g. FST reader); keep stdout clean for JSON-RPC.
        with contextlib.redirect_stdout(sys.stderr):
            _cache[key] = parse_file(path)
    return _cache[key]


# ── tool handlers ────────────────────────────────────────────────────

def _h_render(file, signals=None, start=None, end=None, width=100,
              radix=None, arrangement=None):
    return render_text(_load(file), signals, start, end, width=int(width),
                       radix=radix, arrangement=arrangement)


def _h_render_svg(file, signals=None, start=None, end=None, width=900,
                  radix=None, theme=None, arrangement=None):
    svg = render_svg(_load(file), signals, start, end, width=int(width),
                     radix=radix, theme=theme, arrangement=arrangement)
    # Return both inline image content (clients can render it) and a note.
    data = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return [
        {"type": "image", "data": data, "mimeType": "image/svg+xml"},
        {"type": "text", "text": "SVG waveform snapshot (%d bytes)" % len(svg)},
    ]


def _h_render_png(file, signals=None, start=None, end=None, width=900,
                  radix=None, arrangement=None):
    trace = _load(file)
    png = render_waveform_png(trace, signals, start, end,
                              width=int(width), radix=radix, arrangement=arrangement)
    data = base64.b64encode(png).decode("ascii")
    return [
        {"type": "image", "data": data, "mimeType": "image/png"},
        {"type": "text", "text": "PNG waveform snapshot (%d bytes)" % len(png)},
        {"type": "text", "text": render_text(trace, signals, start, end,
             width=100, radix=radix, arrangement=arrangement)},
    ]


def _h_measure(file, signals, start=None, end=None):
    return measure(_load(file), signals, start, end)


def _h_list(file, pattern="*", limit=200):
    names = list_signals(_load(file), pattern, int(limit))
    return "\n".join(names) if names else "(no signals matched)"


def _h_find_when(file, expr, start=None, end=None, limit=50):
    vcd = _load(file)
    s = 0 if start is None else parse_time(vcd, start)
    e = None if end is None else parse_time(vcd, end)
    try:
        hits = find_all_expression(vcd, expr, s, e, limit=int(limit))
    except ExprError as ex:
        return "Expression error: %s" % ex
    if not hits:
        return "No match where: %s" % expr
    lines = ["%d time(s) where '%s' becomes true:" % (len(hits), expr)]
    lines += ["  %s\t(t=%d)" % (vcd.time_to_str(t), t) for t in hits]
    return "\n".join(lines)


def _h_find_glitches(file, signals=None, start=None, end=None, max_pulse=None):
    vcd = _load(file)
    if signals:
        sigs = [resolve_signal(vcd, n) for n in signals]
    else:
        sigs = list(vcd.signals.values())
    s = 0 if start is None else parse_time(vcd, start)
    e = vcd.max_time if end is None else parse_time(vcd, end)
    mp = None if max_pulse is None else int(max_pulse)
    evs = find_anomalies([x for x in sigs if x], s, e, max_pulse=mp, limit=500)
    if not evs:
        return "No glitches or x-hazards found."
    lines = ["%d anomaly(ies):" % len(evs)]
    lines += ["  %s\t%s\t%s (%s)" % (vcd.time_to_str(t), name, kind, detail)
              for (t, name, kind, detail) in evs[:200]]
    return "\n".join(lines)


# ── live-viewer proxy (see control.py; SKILL.md is the direct contract) ──

def _sessions():
    from .control import list_sessions
    return [s for s in list_sessions() if not s.get("stale")]


def _pick_session(session=None):
    """Resolve which live viewer to talk to.

    With no hint, attach only when there is exactly one — silently steering
    one of several open viewers would be the wrong guess to make for someone.
    """
    live = _sessions()
    if not live:
        raise RuntimeError(
            "no controllable viewer is running. Ask the user to start one with "
            "'alwavelets.py <trace> --control' (a viewer without --control "
            "cannot be driven).")
    if session:
        key = str(session)
        matches = [s for s in live if key == str(s.get('pid'))]
        if not matches:
            matches = [s for s in live if key == s.get('trace')]
        if not matches:
            matches = [s for s in live if key == os.path.basename(s.get('trace', ''))]
        if not matches:
            matches = [s for s in live if key in os.path.basename(s.get('trace', ''))]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise RuntimeError("ambiguous session %r; choose a pid: %s" % (
                session, ', '.join('%s (%s)' % (s.get('pid'), s.get('trace')) for s in matches)))
        raise RuntimeError("no live viewer matches %r; open ones: %s" % (
            session, ", ".join(os.path.basename(s.get("trace") or "?")
                               for s in live)))
    if len(live) > 1:
        raise RuntimeError(
            "%d viewers are running — pass 'session' (a pid or trace name) to "
            "choose: %s" % (len(live), ", ".join(
                "%s (pid %s)" % (os.path.basename(s.get("trace") or "?"), s.get("pid"))
                for s in live)))
    return live[0]


def _viewer(session, op, **kw):
    from .control import ControlClient
    info = _pick_session(session)
    with ControlClient(info) as c:
        resp = c.request(op, **kw)
    if not resp.get("ok"):
        msg = resp.get("error", "viewer refused the request")
        if resp.get("hint"):            # e.g. the session is running old code
            msg = "%s — %s" % (msg, resp["hint"])
        raise RuntimeError(msg)
    return resp


def _fmt_state(st):
    """Compact, readable state summary — an agent reads this every call, so
    it stays terse rather than dumping the whole JSON."""
    cur = st.get("cursor") or {}
    lines = [
        "trace:     %s" % (st.get("trace") or "?"),
        "window:    %s .. %s  (max %s)" % (*(st.get("window") or ["?", "?"]),
                                           st.get("max_time", "?")),
        "cursor:    A=%s B=%s active=%s%s" % (
            cur.get("a"), cur.get("b"), cur.get("active"),
            "" if cur.get("delta") is None else "  delta=%s" % cur["delta"]),
        "focused:   %s" % (st.get("focused") or "(none)"),
        "radix:     %s" % st.get("radix"),
        "displayed: %s" % (", ".join(st.get("displayed") or []) or "(none)"),
    ]
    if st.get("selected"):
        lines.append("selected:  %s" % ", ".join(st["selected"]))
    for g in st.get("groups") or []:
        lines.append("group:     %s%s%s [%s]" % (
            g["name"], " < %s" % g["parent"] if g.get("parent") else "",
            " (collapsed)" if g.get("collapsed") else "",
            ", ".join(g.get("members") or [])))
    for m in st.get("markers") or []:
        lines.append("marker:    %s @ %s" % (m.get("label") or "(unlabeled)", m.get("time")))
    if st.get("message"):
        lines.append("status:    %s" % st["message"])
    return "\n".join(lines)


def _h_viewer_sessions():
    live = _sessions()
    if not live:
        return ("No controllable viewer is running. Start one with "
                "'alwavelets.py <trace> --control'.")
    return "\n".join(
        "pid %s  %s  (started %s)" % (s.get("pid"), s.get("trace") or "?",
                                      s.get("started"))
        for s in live)


def _h_viewer_state(session=None):
    return _fmt_state(_viewer(session, "state")["state"])


def _h_viewer_render(session=None, width=100):
    return _viewer(session, "render", width=int(width))["text"]


def _h_viewer_command(session=None, command=None):
    if not command:
        return "Error: 'command' is required (e.g. 'when valid && ready')."
    r = _viewer(session, "command", text=str(command))
    out = []
    if r.get("message"):
        out.append("viewer: %s" % r["message"])
    if r.get("quit"):
        out.append("NOTE: that command closed the user's viewer.")
    out.append(_fmt_state(r["state"]))
    return "\n".join(out)


def _h_viewer_key(session=None, keys=None):
    if not keys:
        return "Error: 'keys' is required (e.g. 'x' to expand a bus)."
    r = _viewer(session, "key", keys=str(keys))
    out = ["applied: %s" % (r.get("applied") or "(none bound)")]
    if r.get("message"):
        out.append("viewer: %s" % r["message"])
    out.append(_fmt_state(r["state"]))
    return "\n".join(out)


_SESSION_ARG = {"type": "string", "description":
                "Which running viewer to drive — a pid or a trace filename. "
                "Omit when only one is open."}

_SIGNALS_SCHEMA = {"type": "array", "items": {"type": "string"},
                   "description": "Signal names or globs (e.g. 'clk', 'top.alu.*'). "
                                  "Include the clock: columns map to uniform "
                                  "TIME, not to cycles, so the clock row is what "
                                  "shows where the cycle boundaries fall and how "
                                  "much of a cycle a column covers. "
                                  "Optional if 'arrangement' is given."}
_TIME_DESC = ("Time as ticks or a unit string like '500ns'/'1.5us'; omit for "
              "the trace extent. WINDOW IT: a whole trace squeezed into ~100 "
              "columns aliases into unreadable runs of transition marks. Find "
              "the moment of interest first (find_when / find_glitches) and "
              "render a window around it — tens of cycles reads clearly")
_FILE = {"type": "string", "description": "Path to a VCD or FST file"}
_ARRANGEMENT = {"type": "string", "description": "Path to a saved arrangement (.json) "
                                                 "supplying the signal list + radix"}

TOOLS = [
    {
        "name": "render_waveform",
        "description": "Render a Unicode-text waveform snapshot of the given "
                       "signals over a time window (signal names + waveforms). "
                       "Portable to any terminal, and to logs and transcripts; "
                       "prefer it unless a raster image is specifically wanted. "
                       "Pass start/end to window the view — see their notes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": _FILE,
                "signals": _SIGNALS_SCHEMA,
                "start": {"type": "string", "description": "Start " + _TIME_DESC},
                "end": {"type": "string", "description": "End " + _TIME_DESC},
                "width": {"type": "integer", "description": "Total character width (default 100)"},
                "radix": {"type": "string", "description": "bin | hex | dec | unsigned (default hex)"},
                "arrangement": _ARRANGEMENT,
            },
            "required": ["file"],
        },
        "handler": _h_render,
    },
    {
        "name": "render_waveform_svg",
        "description": "Render an SVG waveform snapshot (inline image) of the "
                       "given signals over a time window.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": _FILE,
                "signals": _SIGNALS_SCHEMA,
                "start": {"type": "string", "description": "Start " + _TIME_DESC},
                "end": {"type": "string", "description": "End " + _TIME_DESC},
                "width": {"type": "integer", "description": "SVG pixel width (default 900)"},
                "radix": {"type": "string", "description": "bin | hex | dec | unsigned (default hex)"},
                "theme": {"type": "string", "description": "dark | light | phosphor, or a "
                                                           "theme-JSON path"},
                "arrangement": _ARRANGEMENT,
            },
            "required": ["file"],
        },
        "handler": _h_render_svg,
    },
    {
        "name": "render_waveform_png",
        "description": "Render a pixel-accurate PNG waveform snapshot (inline "
                       "raster image) of the given signals over a time window.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": _FILE,
                "signals": _SIGNALS_SCHEMA,
                "start": {"type": "string", "description": "Start " + _TIME_DESC},
                "end": {"type": "string", "description": "End " + _TIME_DESC},
                "width": {"type": "integer", "description": "PNG pixel width (default 900)"},
                "radix": {"type": "string", "description": "bin | hex | dec | unsigned (default hex)"},
                "arrangement": _ARRANGEMENT,
            },
            "required": ["file"],
        },
        "handler": _h_render_png,
    },
    {
        "name": "measure",
        "description": "Measure per-signal stats (transition count, period and "
                       "frequency) over a window, plus the edge-to-edge delta "
                       "between the first two signals.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": _FILE,
                "signals": _SIGNALS_SCHEMA,
                "start": {"type": "string", "description": "Start " + _TIME_DESC},
                "end": {"type": "string", "description": "End " + _TIME_DESC},
            },
            "required": ["file", "signals"],
        },
        "handler": _h_measure,
    },
    {
        "name": "list_signals",
        "description": "List signal full-names in a trace, optionally filtered "
                       "by a glob pattern.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": _FILE,
                "pattern": {"type": "string", "description": "Glob filter (default '*')"},
                "limit": {"type": "integer", "description": "Max names to return (default 200)"},
            },
            "required": ["file"],
        },
        "handler": _h_list,
    },
    {
        "name": "find_when",
        "description": "Find the times where a boolean expression over multiple "
                       "signals becomes true — e.g. \"valid && state==3 && "
                       "addr>=0x40\". Operators: && || ! == != < <= > >= and "
                       "parentheses; literals are decimal/0x/0b or 'x'/'z'.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": _FILE,
                "expr": {"type": "string", "description": "Boolean expression over signal names"},
                "start": {"type": "string", "description": "Start " + _TIME_DESC},
                "end": {"type": "string", "description": "End " + _TIME_DESC},
                "limit": {"type": "integer", "description": "Max matches to return (default 50)"},
            },
            "required": ["file", "expr"],
        },
        "handler": _h_find_when,
    },
    {
        "name": "find_glitches",
        "description": "Find waveform anomalies — transitions into x (unknown) "
                       "and glitches (zero-width / runt pulses). Useful to spot "
                       "bugs without scrolling. Optional max_pulse (ticks) sets "
                       "the runt-pulse threshold; default auto per signal.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": _FILE,
                "signals": _SIGNALS_SCHEMA,
                "start": {"type": "string", "description": "Start " + _TIME_DESC},
                "end": {"type": "string", "description": "End " + _TIME_DESC},
                "max_pulse": {"type": "integer", "description": "Runt-pulse width threshold (ticks)"},
            },
            "required": ["file"],
        },
        "handler": _h_find_glitches,
    },

    # ── live-viewer tools: drive the session the user is looking at ──
    {
        "name": "list_viewer_sessions",
        "description": "List the running Alwavelets viewers that can be driven "
                       "(started with --control). Call this first when the user "
                       "talks about what is on their screen, or when another "
                       "viewer_* tool reports more than one open session.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": _h_viewer_sessions,
    },
    {
        "name": "viewer_state",
        "description": "Read what a running viewer is currently showing: time "
                       "window, cursors, focused and displayed signals, groups, "
                       "markers, radix. Read-only. Use it before acting so you "
                       "know the real signal names and layout.",
        "inputSchema": {
            "type": "object",
            "properties": {"session": _SESSION_ARG},
        },
        "handler": _h_viewer_state,
    },
    {
        "name": "viewer_render",
        "description": "Render what the user is seeing right now in their "
                       "viewer, as Unicode-text waveforms. Read-only. Use it to "
                       "answer questions about the current screen, and to check "
                       "your own change landed. Window the view first (see "
                       "viewer_command 'zoom') — a whole trace in ~100 columns "
                       "aliases into unreadable transition marks. Note this "
                       "renders the signal names too; the user's own screen "
                       "hides them while the split panel is open, so send key "
                       "'s' if you are framing a view for them to look at.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session": _SESSION_ARG,
                "width": {"type": "integer",
                          "description": "Character width, 20-400 (default 100)"},
            },
        },
        "handler": _h_viewer_render,
    },
    {
        "name": "viewer_command",
        "description": "Run one command in a running viewer — the same text the "
                       "user could type at its ';' prompt — and get the "
                       "resulting state back. This is how you act on their "
                       "screen. Useful: 'when <expr>' jumps the cursor to where "
                       "a boolean expression over signals first holds "
                       "(\"when valid && state==3\"); 'goto <t>', 'zoom <a> <b>' (ticks or units: 'zoom 170ns 290ns'), "
                       "'fit'; 'group <name> <signal|glob …>' bundles exactly those signals "
                       "under a collapsible header (adding any not yet shown; "
                       "'group <parent>/<child> …' nests), "
                       "'ungroup'; 'stats' to measure the focused signal; "
                       "'glitches' to jump to the next x-hazard or runt pulse; "
                       "'radix hex|bin|dec|uns'; 'marker <time> [label…]' drops a "
                       "labelled marker at a time in ticks or units "
                       "('marker 220ns first bad pop'); 'session' to save the layout. "
                       "To change WHICH signals are shown: 'add <name-or-glob> "
                       "[more…]' puts them on screen (e.g. 'add top.valid "
                       "top.ready' or 'add top.alu.*') and 'remove <pattern>' "
                       "takes them off — 'find' only moves focus among signals "
                       "already displayed, so add first if a signal is missing. "
                       "IMPORTANT: bus "
                       "literals in an expression are decimal unless prefixed "
                       "— when the user quotes a value they read off the "
                       "screen, check the display radix in viewer_state first "
                       "and write it accordingly ('data_out == 0x62', not "
                       "'== 62', when the view is hex), or the search finds "
                       "nothing and you will wrongly report that it never "
                       "happens. Commands that would open an interactive "
                       "prompt are no-ops here, so pass arguments inline. "
                       "Never send 'quit' — it closes the user's viewer.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session": _SESSION_ARG,
                "command": {"type": "string",
                            "description": "Command text, without the leading ';'"},
            },
            "required": ["command"],
        },
        "handler": _h_viewer_command,
    },
    {
        "name": "viewer_key",
        "description": "Send keystrokes to a running viewer, for the actions "
                       "bound only to keys: 'x' expand the focused bus into "
                       "per-bit lanes, 'a' toggle analog, 'w' cycle wave style "
                       "(hi-res draws analog as braille curves), 'H'/'v'/'R' "
                       "cycle the focused signal's height/color/radix, 'j'/'k' "
                       "move focus, 'space' highlight it, 'tab' fold a group "
                       "header, 'u' undo the last layout change.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session": _SESSION_ARG,
                "keys": {"type": "string",
                         "description": "Keystrokes, one per character, or a "
                                        "single named key (tab, enter, esc, "
                                        "space, up, down, left, right)"},
            },
            "required": ["keys"],
        },
        "handler": _h_viewer_key,
    },
]


# ── JSON-RPC plumbing ────────────────────────────────────────────────

def _result(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def handle_message(msg):
    """Handle one JSON-RPC message; return a response dict, or None for
    notifications (and unknown notifications)."""
    if not isinstance(msg, dict):
        return _error(None, -32600, "request must be a JSON object")
    method = msg.get("method")
    mid = msg.get("id")
    if (msg.get('jsonrpc') != '2.0' or not isinstance(method, str)
            or isinstance(mid, bool) or not isinstance(mid, (str, int, type(None)))):
        return _error(None, -32600, "invalid JSON-RPC request")
    if 'id' not in msg:
        return None
    params = msg.get('params', {})
    if not isinstance(params, dict):
        return _error(mid, -32602, "params must be a JSON object")

    if method == "initialize":
        requested = params.get('protocolVersion', PROTOCOL_VERSION)
        if not isinstance(requested, str):
            return _error(mid, -32602, "protocolVersion must be a string")
        ver = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
        return _result(mid, {
            "protocolVersion": ver,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "alwavelets", "version": __version__},
        })
    if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return _result(mid, {})
    if method == "tools/list":
        return _result(mid, {"tools": [
            {k: t[k] for k in ("name", "description", "inputSchema")} for t in TOOLS]})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments", {})
        if not isinstance(name, str) or not isinstance(args, dict):
            return _error(mid, -32602, "tools/call requires a name and object arguments")
        tool = next((t for t in TOOLS if t["name"] == name), None)
        if tool is None:
            return _error(mid, -32602, "unknown tool: %s" % name)
        try:
            out = tool["handler"](**args)
            content = out if isinstance(out, list) else [{"type": "text", "text": out}]
            return _result(mid, {"content": content, "isError": False})
        except Exception as exc:  # surface as a tool error, not a protocol error
            return _result(mid, {"content": [{"type": "text", "text": "Error: %s" % exc}],
                                 "isError": True})

    if mid is not None:
        return _error(mid, -32601, "method not found: %s" % method)
    return None


def serve(stdin=None, stdout=None):
    """Run the stdio JSON-RPC loop until stdin closes."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except (json.JSONDecodeError, RecursionError):
            resp = _error(None, -32700, "invalid JSON")
        else:
            resp = handle_message(msg)
        if resp is not None:
            stdout.write(json.dumps(resp) + "\n")
            stdout.flush()


def main():
    serve()


if __name__ == "__main__":
    main()
