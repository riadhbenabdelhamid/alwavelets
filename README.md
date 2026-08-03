<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/alwavelets-logo.png">
    <img src="assets/alwavelets-logo-light.png" width="340"
         alt="Alwavelets — a waveform above the >_ Alwavelets wordmark">
  </picture>
</p>

# Alwavelets — Terminal VCD/FST Waveform Viewer

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](LICENSE)

A dependency-free terminal waveform viewer for digital-design simulation
traces. Runs over SSH / tmux / a bare console — no display server required.

The mark is a symmetric burst of square waves over a `>_` prompt — the shape
of a signal, drawn the way this tool draws one. It ships in two tints so it
holds contrast on either GitHub theme (`assets/alwavelets-logo.png` for dark,
`…-logo-light.png` for light). The in-app `◆` diamond icon used for desktop
entries is separate, generated from scratch by `tools/make_icon.py` — pure
stdlib, original artwork, no third-party assets.

## Features

- **Formats:** VCD (streaming, background, and lazy on-demand loading for large
  files) and FST (native pure-Python reader, with a `fst2vcd` converter
  fallback). Values are bit-packed in memory (1-bit signals and all buses up
  to 64 bits), cutting resident size several-fold on bus-heavy traces.
- **Navigation:** fuzzy signal finder (`/` filters every signal in the file
  live, fzf-style), edge-to-edge jumps, multi-cursor measurement, markers,
  zoom-to-fit / zoom-to-selection, mouse drag-to-zoom, time goto, vim-style
  motions.
- **Display:** binary / hex / dec / unsigned radixes, analog rendering,
  per-signal color & height, IEEE-754 float decode, scope folding, themes, and
  sub-cell (2× horizontal resolution) waveforms for even clock periods (`w`),
  collapsible signal groups — nestable via `group parent/child` — (fold with
  Tab), and true-color
  themes (`#rrggbb` in theme JSON; quantizes gracefully on 256-color
  terminals — see `theme_nord.json`).
- **Analysis:** frequency & transition detection, cross-signal expression
  search (`when valid && state==3 && addr>=0x40`), glitch / x-hazard detection,
  pattern/trigger search,
  protocol decoders (SPI, I²C, UART, AXI, Wishbone, AHB), RISC-V instruction
  trace decoding, formal counter-example & power-activity analysis.
- **Persistence:** whole-session save/restore (`;session` writes a sidecar
  that reopens the trace with the same layout, groups, radixes, colors, zoom
  and theme), plus bookmarks, annotations/notes, signal tags, and saved
  arrangements stored as JSON sidecar files next to the trace; **GTKWave
  `.gtkw` import/export** for interop (`loadgtkw`/`savegtkw`); a persistent
  parse cache for instant re-opens.
- **Export:** CSV, SVG, PDF, screenshot, and a **pixel-accurate image** shown
  inline on kitty / iTerm2 / sixel terminals (falls back to a saved PNG, and to
  the normal character-cell rendering, everywhere else — never hinders portability).
- **Scripting:** a small TCL-like command engine and an in-app command line.

## Requirements

- Python 3.8+ with the standard library `curses` module (Unix/Linux/macOS;
  Windows is not supported because `curses` is not in the stdlib there).

No third-party packages are required.

## Usage

```sh
python3 alwavelets.py <file.vcd>
python3 alwavelets.py <file.vcd> --signals 'clk,data*,alu.*'
python3 alwavelets.py --demo            # generate and open a built-in example
```

Or from a Python REPL, with no curses terminal involved:

```python
>>> import alwavelets
>>> vcd = alwavelets.demo()      # prints a waveform, returns the parsed trace
```

Flags:

| Flag | Meaning |
| --- | --- |
| `--signals`, `-s` | Comma-separated name/glob filters |
| `--sync` | Force synchronous parse (disable background loading) |
| `--loadunchecked` | Start with no signals shown (pick them in the `B` tree browser) |
| `--expanded` | Start with all scope modules expanded |
| `--demo` | Generate and view a demo trace |
| `--mcp` | Run as an MCP server (stdio) instead of the viewer |
| `--no-cache` | Disable the persistent parse cache (force a fresh parse) |
| `--no-config` | Ignore `~/.config/alwavelets/config.json` |
| `--no-session` | Ignore a saved session sidecar (open the trace fresh). `--expanded` / `--loadunchecked` imply this |
| `--control` | Expose a token-authenticated control socket so an authorized local agent can drive this live session (see [SKILL.md](SKILL.md)) |
| `--list-sessions` | Print controllable running viewers as JSON, then exit |
| `--jobs`, `-j` | Opt-in parallel parse workers for a large (>16 MB) synchronous parse (e.g. `--sync --jobs 8`) |

Press `?` inside the viewer for quick help, or `F1` for the full manual.
For a browsable reference of every key, command, sidecar file and the MCP
integration, read the **[complete field guide](https://riadhbenabdelhamid.github.io/alwavelets/)**
— also in the repo at [`docs/index.html`](docs/index.html), self-contained and
usable offline.

## Configuration

An optional `~/.config/alwavelets/config.json` (or `$XDG_CONFIG_HOME`) sets
startup defaults and remaps keys. All fields are optional; a malformed file is
ignored. `keys` maps an action name to a single character.

```json
{
  "theme": "phosphor",
  "radix": "hex",
  "wave_style": 2,
  "menu_bar": false,
  "grid": 1,
  "name_width": 28,
  "keys": { "quit": "Q", "zoom_in": "=", "toggle_cursor": " " }
}
```

## MCP server (agentic workflows)

Alwavelets can act as a [Model Context Protocol](https://modelcontextprotocol.io)
server so an agent/app can request a **waveform snapshot** — a list of signals
over a time window — and display it in a box. It speaks JSON-RPC over stdio and
needs no extra dependencies:

```sh
python3 alwavelets.py --mcp        # or: python3 -m alwavelets.mcp_server
```

Example MCP client config (e.g. Claude Desktop's `mcpServers`):

```json
{
  "mcpServers": {
    "alwavelets": { "command": "python3", "args": ["/path/to/alwavelets.py", "--mcp"] }
  }
}
```

Tools:

| Tool | Returns |
| --- | --- |
| `render_waveform(file, signals?, start?, end?, width?, radix?, arrangement?)` | Unicode-text snapshot (signal names + waveforms + ruler) |
| `render_waveform_svg(file, signals?, start?, end?, width?, radix?, theme?, arrangement?)` | SVG snapshot as inline **image** content (base64) |
| `render_waveform_png(file, signals?, start?, end?, width?, radix?, arrangement?)` | Pixel-accurate PNG snapshot as inline **image** content (base64) |
| `measure(file, signals, start?, end?)` | Per-signal transition count, period/frequency, and the edge-to-edge delta between the first two signals |
| `list_signals(file, pattern?, limit?)` | Matching signal names |
| `find_when(file, expr, start?, end?, limit?)` | Times where a boolean expression over signals holds (`"valid && state==3 && addr>=0x40"`) |
| `find_glitches(file, signals?, start?, end?, max_pulse?)` | Waveform anomalies: x-hazards and zero-width/runt glitches |

Those are **headless** — they parse a trace of their own. Five more attach to a
**running** viewer started with `--control`, so an MCP client can drive the
session you have open (they proxy the socket below, so there is one
implementation behind two transports):

| Tool | Does |
| --- | --- |
| `list_viewer_sessions()` | Which live viewers can be driven |
| `viewer_state(session?)` | Read window, cursors, focused/displayed signals, groups, markers |
| `viewer_render(session?, width?)` | Render what the user is seeing right now |
| `viewer_command(session?, command)` | Run one `;` command (`when valid && ready`, `group A/B`, `zoom`, `stats`…) |
| `viewer_key(session?, keys)` | Keystrokes for key-only actions (`x`, `a`, `w`, `tab`, `u`) |

`session` is a pid or trace filename; it is inferred when only one viewer is
open, and the tools refuse to guess when several are.

`signals` is a list of names or globs (`"clk"`, `"top.alu.*"`); times are ticks
or unit strings (`"500ns"`, `"1.5us"`) and default to the full trace extent.
`theme` is `dark`/`light`/`phosphor` or a theme-JSON path; `arrangement` is a
saved-view `.json` that supplies the signal list + radix (so a snapshot can
match a view you saved in the viewer). The same renderers are available
in-process via `alwavelets.render_text` / `alwavelets.render_svg` / `alwavelets.measure`.

## Agent control of a live session

The MCP server above is *headless* — it parses a file of its own and knows
nothing about what you have on screen. The control channel is the other half:
start a viewer with `--control` and an authorized local agent can drive **that
exact session** while you watch — group signals, jump the cursor to where an
expression holds, zoom, restyle — and read back both the view state and the
waveform you are seeing.

```sh
python3 alwavelets.py bench.vcd --control     # in your terminal / tmux pane
python3 alwavelets.py --list-sessions         # how an agent finds it
```

If your agent speaks MCP, use the `viewer_*` tools above and you are done. This
socket is the direct route for agents that don't, or that want a persistent
connection. [**SKILL.md**](SKILL.md) is the agent-facing contract (frontmatter +
protocol, ops, command vocabulary, state schema and worked recipes) — point your
agent at it. In Python:

```python
from alwavelets.control import list_sessions, ControlClient

session = next(s for s in list_sessions() if not s["stale"])
with ControlClient(session) as c:
    c.command("group Interface/Handshake")   # organize the view
    c.command("when valid && ready")         # cursor lands on the first match
    print(c.render(width=100)["text"])       # see what the user sees
```

Safety: the socket only exists with `--control`; it and its session descriptor
are `0600` and every request carries a per-session token; commands run with
your authority; and the viewer shows a `◆AGENT` indicator in the status bar
whenever an agent is connected and acting.

## Project layout

`alwavelets.py` is a thin launcher; the implementation is the `alwavelets/` package:

| Module | Responsibility |
| --- | --- |
| `core` | Data structures (`VCDSignal`, `Radix`, …) and value formatting |
| `parsers` | VCD/FST parsing, scope tree, `parse_file` dispatcher |
| `cache` | Persistent on-disk parse cache (instant re-open of unchanged traces) |
| `parallel` | Opt-in multi-process parsing of large VCDs (`--jobs N`) |
| `expr` | Cross-signal boolean expression search + derived signals (`when`/`derive`) |
| `gtkw` | GTKWave `.gtkw` save-file import/export (interop) |
| `config` | Optional `~/.config/alwavelets/config.json` (defaults + keybindings) |
| `decoders` | Protocol (SPI/I²C/UART), RISC-V, and bus (AXI/Wishbone/AHB) decode |
| `rendering` | Pure waveform draw functions |
| `analysis` | Formal counter-example and power-activity analysis |
| `persistence` | JSON sidecar stores (tags, annotations, bookmarks) |
| `tcl` | Small scripting/command engine |
| `manual` | In-app help/manual text |
| `ui` | The `Alwavelets` curses TUI |
| `snapshot` | Headless text/SVG waveform snapshots |
| `graphics` | Pure-Python pixel rasteriser + kitty/iTerm2/sixel image blit |
| `mcp_server` | MCP (JSON-RPC over stdio) server exposing snapshots |
| `control` | Agent control channel for a live session (socket + client) |
| `cli` | Demo generation, bootstrap, and `main()` |

## Development

Tests live in `tests/` and exercise the pure-Python layer (parsing, decoding,
rendering, persistence, and the keymap dispatch) without a curses terminal:

```sh
pip install pytest ruff
python3 -m pytest        # tests
ruff check .             # bug-finding lint (pyflakes + syntax)
```

The suite includes a VCD parser **fuzzer** (random mutations must never crash
the parser) and a **lazy-chunk parity** check (the on-demand loader must agree
with a full parse). Both lint and tests run in CI (`.github/workflows/ci.yml`)
on every push, across Python 3.8–3.13. Ruff is configured in `ruff.toml` for
bug-finding rules only (`E9` + `F`), not full style enforcement.

## License

Alwavelets is **dual-licensed**:

- **Open source — AGPL-3.0-or-later.** Free to use, study, modify, and share
  under the [GNU Affero General Public License v3.0](LICENSE). Note the
  network clause: if you run a modified version to provide a service over a
  network, you must offer that version's source to its users.
- **Commercial.** If AGPL copyleft (including the network/SaaS clause) doesn't
  fit — e.g. you want to embed Alwavelets in a proprietary product or hosted
  service without releasing your source — a separate commercial license is
  available. Contact **riadh.benabdelhamid@gmail.com**.

Copyright © 2026 Riadh Ben Abdelhamid.
