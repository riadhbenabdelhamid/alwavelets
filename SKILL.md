---
name: alwavelets-viewer-control
description: Drive a running Alwavelets terminal waveform viewer (VCD/FST) that a user has open — group signals, find when a boolean expression over signals holds and put the cursor there, zoom to a window, change radix/analog/colors, and read back both the view state and the exact waveform the user is looking at. Use when the user is looking at a waveform in Alwavelets and describes what they want done to it ("group the handshake signals", "find where fifo_full && wr_en", "zoom to the first glitch"), or asks about what is currently on their screen. Requires the viewer to have been started with --control.
license: same as the Alwavelets project
---

# Controlling a live Alwavelets session

Alwavelets is a terminal VCD/FST waveform viewer. Started with `--control`, a
session exposes a token-authenticated Unix socket that lets you inspect and
steer **the exact view the user is looking at** — changes appear on their
screen immediately.

**If you are an MCP client, you probably don't need this file.** The Alwavelets
MCP server (`alwavelets.py --mcp`) exposes the same capability as five tools —
`list_viewer_sessions`, `viewer_state`, `viewer_render`, `viewer_command`,
`viewer_key` — which proxy the socket described here. Use those and skip the
protocol below; the working method (§6) and the trust boundary (§7) still
apply.

Read on when you are **not** an MCP client, or when you want a persistent
connection instead of one round trip per tool call.

Both are distinct from the MCP server's *file* tools (`render_waveform`,
`find_when`, …), which are headless: they parse a trace of their own and know
nothing about the user's screen. Use those to analyze a file nobody has open;
use the live channel when the user is looking at something and wants it
changed.

## 1. Find a session

Every controllable viewer publishes a descriptor under
`$XDG_RUNTIME_DIR/alwavelets/sessions/<pid>.json` (falling back to
`$XDG_CACHE_HOME` then `~/.cache`). Both it and the socket are mode 0600.

```bash
python3 /path/to/alwavelets.py --list-sessions     # live sessions as JSON
```

```json
[{"protocol": 1, "pid": 12345,
  "socket": "/run/user/1000/alwavelets/sessions/12345.sock",
  "token":  "9f2c…", "trace": "/work/bench.vcd", "started": 1.75e9}]
```

Pick by `trace` when several are open. If the list is empty the user either
has no viewer open or started it without `--control` — say so and suggest
`alwavelets.py trace.vcd --control` rather than guessing.

**Do not launch a viewer yourself unless asked.** Attaching to what the user
already has open is the point; a second viewer they cannot see is not.

## 2. Talk to it

Connect to `socket`, then write **one JSON object per line** and read **one
JSON response per line**. Every request must carry the `token` from the
descriptor. Responses are `{"ok": true, …}` or `{"ok": false, "error": "…"}`.

Python clients can skip the plumbing:

```python
from alwavelets.control import list_sessions, ControlClient

session = next(s for s in list_sessions() if not s["stale"])
with ControlClient(session) as c:
    c.command("when valid && ready")     # cursor lands on the first match
    print(c.render(width=100)["text"])   # see what the user sees
```

Raw equivalent, no dependency on the package:

```bash
printf '{"token":"%s","op":"command","text":"fit"}\n' "$TOKEN" \
  | nc -U /run/user/1000/alwavelets/sessions/12345.sock
```

## 3. Operations

| op | arguments | returns |
|---|---|---|
| `ping` | — | protocol version, trace path, signal count |
| `capabilities` | — | ops + the live list of `;` command verbs |
| `state` | — | `state` (see schema below) |
| `render` | `width` (20–400, default 100) | `text`: the current view as Unicode waveforms |
| `command` | `text` | runs one `;` command; returns `message`, `quit`, and fresh `state` |
| `key` | `keys` (≤64 chars) | feeds keystrokes; returns `applied`, `message`, `state` |

`ping`, `capabilities`, `state` and `render` never modify the view.

Call `capabilities` rather than hard-coding the command list — it is generated
from the viewer's own dispatch table, so it cannot drift from what the build
in front of you actually supports.

### A crash usually means a stale session, not a missing capability

A viewer is a long-lived process: it keeps the code it loaded at startup and
does **not** pick up fixes made since. So a response carrying an internal
error (`NameError`, `AttributeError`, …) plus a `hint` field is telling you
this session predates the installed version — not that the feature is
impossible.

When that happens: say so and ask the user to restart the viewer (`q`, then
relaunch with `--control`). Do not conclude a capability is permanently
unavailable and fall back to headless rendering; that silently changes what
you are looking at from *their screen* to *a file*, which is a different
answer to a different question. Check `capabilities` first — if the verb is
listed, the build supports it and the failure is environmental.

## 4. The command vocabulary

`op: "command"` takes exactly what the user would type at the in-app `;`
prompt. The useful ones for agent work:

**Navigate and measure**
- `goto <t>` — move the cursor to a time
- `zoom <a> <b>` / `fit` — set the window / zoom to the whole trace
- `when <expr>` — **jump the cursor to where a boolean expression first holds**,
  e.g. `when valid && state==3 && addr>=0x40`. Operators: `&& || ! == != < <= > >=`
  and parentheses; literals decimal, `0x…`, `0b…`, `x`, `z`
- `glitches` — jump to the next x-hazard or runt pulse
- `stats` — measure the focused signal (transitions, period, frequency, duty,
  or min/max for a bus) over A‥B or the visible window
- `find <pattern>` — focus a signal, **among those already displayed**
- `add <name-or-glob> [more…]` — **put signals on screen** (`add top.valid top.ready`,
  `add top.alu.*`). If a signal the user names is not in `displayed`, add it first —
  `find` alone will not bring it into view
- `remove <pattern>` — take signals off screen

**Organize the view**
- `group <name>` — group the current selection under a collapsible header
- `group <parent>/<child>` — nest a group inside another
- `ungroup [name]` — dissolve one
- `derive <name>=<expr>` — add a virtual signal computed from others
- `radix bin|hex|dec|uns|f16|f32|f64` — set the global radix
- `grid`, `spacing`, `names`, `theme <name>` — display toggles

**Analysis**
- `formal` / `cex`, `power`, `axi`, `wb`, `ahb`, `trace`, `query`
- `diff` — toggle comparison against a second trace

**Persistence**
- `session` — save the whole layout so the user gets it back on reopen
- `bm` / `bookmarks`, `annotate`

Anything unrecognized falls through to the built-in Tcl engine
(`add_signal`, `remove_signal`, `measure`, `foreach`, …).

### Commands that need a keyboard are no-ops here

A few commands open an interactive prompt (`export`, the bare `diff` file
picker, `annotate` without text). Over the control channel those return with
the view unchanged rather than freezing the viewer. Pass arguments inline
instead — `goto 1200`, not `goto` then a prompt.

### Keystrokes for key-only actions

Some capabilities are bound to keys, not commands. Send them with `op: "key"`:

| keys | effect |
|---|---|
| `x` | expand the focused bus into per-bit lanes |
| `a` | toggle analog rendering on the focused signal |
| `w` | cycle wave style (thin → hi-res braille curves for analog) |
| `H` / `v` / `R` | cycle height / color / radix of the focused signal |
| `t` / `T` | toggle value column / time units |
| `V`, `space` | visual select mode, toggle-highlight a signal |
| `tab` | fold/unfold the focused group header |
| `j` / `k` | move focus down / up |
| `u` | undo the last layout or styling change |

Single named keys (`tab`, `enter`, `esc`, `space`, `up`, `down`, `left`,
`right`, `home`, `end`, `backspace`) are accepted as whole words when sent
alone; otherwise each character in `keys` is one keystroke.

## 5. State schema

```jsonc
{
  "trace": "/work/bench.vcd",
  "timescale": "1ns",
  "max_time": 2200,
  "window": [0, 2200],                    // visible time range
  "cursor": {"a": 140, "b": 0, "active": "a",
             "on": true, "b_on": false, "delta": null},
  "focused": "top.valid",                 // or null on a group header
  "displayed": ["top.clk", "top.valid"],  // full names, in display order
  "selected": ["top.valid"],              // visual-mode / highlighted
  "groups": [{"name": "Handshake", "parent": "Interface",
              "collapsed": false, "members": ["top.valid", "top.ready"]}],
  "markers": [{"time": 500, "label": "reset done"}],
  "radix": "hex",
  "message": "⌖ 140ns: valid && ready"    // the viewer's last status flash
}
```

`message` is how the viewer reports what a command did — read it after every
`command` call, since a command that matched nothing still returns `ok: true`.

## 6. Working method

1. **Look before acting.** Call `state` to learn which signals are displayed
   and how they are named. Signal names are hierarchical (`top.cpu.wr_en`);
   commands generally accept globs.
2. **Act with one `command` per intent.** Each returns the resulting state, so
   you usually do not need a separate `state` call.
3. **Verify by reading, not assuming.** Check `message`, and use `render` when
   the user asks something visual ("does the handshake look right?"). Rendering
   is also how you answer questions about what is on screen.
4. **Window before rendering.** A whole trace squeezed into ~100 columns
   aliases into unreadable runs of transition marks. Locate the moment first
   (`when`, `glitches`), then `zoom` to tens of cycles around it. Include the
   clock in what is displayed — columns map to uniform **time**, not cycles, so
   the clock row is what shows where cycle boundaries fall.
5. **Match the literal to the display radix.** Bus literals are decimal unless
   prefixed. When the user quotes a value they read off the screen — "where
   data_out is 62" — check `radix` in `state` first: on a hex view that means
   `data_out == 0x62`. Writing `== 62` silently matches nothing, and reporting
   "that never happens" from a mistyped literal is the worst failure available
   here, because it sounds like an answer.
6. **Clear the view before showing it.** The split panel occupies the left
   ~30 columns and hides the signal-name column on the user's screen. If you
   are framing something for them to look at, send key `s` to close it.
7. **Say what you changed.** The user is watching their own screen move. Report
   actions in their vocabulary ("grouped the four handshake signals and jumped
   to the first valid&&ready at 140 ns"), not as op names.

### Worked example — "show me where valid && ready and data_out is 62"

```python
with ControlClient(session) as c:
    st = c.state()["state"]
    lit = "0x62" if st["radix"] == "hex" else "62"      # they read it off screen
    r = c.command(f"when (top.valid && top.ready) && top.data_out == {lit}")
    if "⌖" not in r["message"]:                          # no match: say so plainly
        return f"No point where that holds — viewer said: {r['message']}"
    t = r["state"]["cursor"]["a"]
    c.key("s")                                           # names hidden by the panel
    c.command(f"zoom {max(0, t - 60)} {t + 60}")         # frame it
    print(c.render(width=110)["text"])
```

### Worked example — "group the handshake signals and show me the first transfer"

```python
with ControlClient(session) as c:
    st = c.state()["state"]
    # 1. select the signals, then group them
    for name in ("top.valid", "top.ready"):
        c.command(f"find {name}")
        c.key("space")                      # highlight
    c.command("group Handshake")
    # 2. locate the first completed transfer
    r = c.command("when valid && ready")
    t = r["state"]["cursor"]["a"]
    # 3. frame it, then look
    c.command(f"zoom {max(0, t - 200)} {t + 200}")
    print(c.render(width=100)["text"])
```

## 7. Trust boundary — read this before driving

- The channel is **opt-in**: nothing listens unless the user started the viewer
  with `--control`.
- Socket and descriptor are **0600**, so only the user's own account can reach
  them. The token stops an unrelated process that merely guesses the socket
  path; it is not a defense against something already running as that user.
- Commands run with **the user's authority**. Some write files (`session`,
  `savegtkw`, the Tcl `export_csv`/`screenshot`). Treat writing outside the
  trace's directory as something to ask about first.
- `command: "quit"` **closes the user's viewer.** Never send it to tidy up —
  disconnect instead, and leave the session running.
- The viewer shows a `◆AGENT` indicator in its status bar while you are
  connected and acting. Do not try to work around that; the user seeing who is
  driving is a feature.
- Signal *values* are the user's design data. Reading them to answer a question
  is the job; sending them somewhere else is not.
