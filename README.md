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

- **Formats:** VCD (streaming, background, and disk-backed loading for large
  files; aliased identifier codes and `b`-form 1-bit values included) and FST
  read natively — zlib, LZ4 and FastLZ chains and hierarchies, gzip-wrapped
  files, aliases, `$dumpoff` blackouts — with pure-Python block decoders, so
  Verilator, GHDL, Icarus and `vcd2fst` output opens with no extra package
  (`fst2vcd` is only a fallback for anything the reader rejects). Values are
  bit-packed in memory (1-bit signals and all buses up to 64 bits), cutting
  resident size several-fold on bus-heavy traces.
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
| `--storage auto\|memory\|disk` | Use disk above 50 MiB or for FSTs with large estimated expansion; override either way |
| `--memory-mib N` | Decoded waveform page-cache budget, default 256 MiB |
| `--loadunchecked` | Start with no signals shown (pick them in the `B` tree browser) |
| `--expanded` | Start with all scope modules expanded |
| `--demo` | Generate and view a demo trace (written under a per-user temp directory) |
| `--mcp` | Run as an MCP server (stdio) instead of the viewer |
| `--no-cache` | Disable persistent waveform and instruction caches |
| `--analysis-cache-mib N` | Saved instruction-index quota, default 8192 MiB; 0 disables reuse and saving |
| `--purge-analysis-cache` | Remove saved instruction indexes and exit |
| `--no-config` | Ignore `~/.config/alwavelets/config.json` |
| `--no-session` | Ignore a saved session sidecar (open the trace fresh). `--expanded` / `--loadunchecked` imply this |
| `--control` | Expose a token-authenticated control socket so an authorized local agent can drive this live session (see [SKILL.md](SKILL.md)) |
| `--list-sessions` | Print controllable running viewers as JSON, then exit |
| `--jobs`, `-j` | Opt-in parallel parse workers for a large (>16 MB) synchronous parse (e.g. `--sync --jobs 8`) |

Parallel workers use `forkserver` where supported and `spawn` elsewhere, so
they do not inherit the viewer's threads and locks. Library scripts requesting
parallel parsing must guard their entry point with `if __name__ == '__main__':`.
The low-level `parse_parallel_file` API accepts an optional `mp_context`;
`parse_file` falls back to sequential parsing when workers cannot start or fail.
Worker startup and merging add overhead, so `--jobs` remains opt-in and applies
to in-memory VCD parsing (`--storage memory --sync --jobs 8`).

### Very large traces

VCD and FST files above 50 MiB automatically use an indexed SQLite trace store.
For smaller native FSTs, automatic selection also examines uncompressed block
sizes, timestamp counts, and declaration counts. Gzip-wrapped FSTs use disk
storage conservatively, since inspecting their inner block layout requires
decompression. `--storage memory` remains an explicit override.
Every declared signal remains available. `--signals`, `--loadunchecked`, and
tree selection remain optional display controls; disk mode does not require a
signal shortlist. To force disk storage or choose a smaller page cache:

```sh
alwavelets huge.vcd --storage disk --memory-mib 64
alwavelets huge.fst --storage disk --loadunchecked
```

The first open scans the source and writes independently compressed signal
pages. The viewer opens after metadata parsing and displays available data as
indexing progresses. Rows and cursor values load on a background worker; panning
discards obsolete requests. Complete analyses wait until indexing finishes.
The index includes signal/time lookups and a hierarchy of exact min/max
summaries, so overview scaling need not decode the whole history. Reopening an
unchanged source reads metadata immediately and fetches values on demand.
Changed sources rebuild the index; incomplete/cancelled indexes are never reused.

Pages are evicted from a bounded cache while browsing. The budget covers decoded
pages, **not total process memory**: Python, declarations, SQLite, builder buffers,
and active query pages need additional memory. FST chains, frames, hierarchies,
geometry, block/chain indexes, and timestamp tables use bounded buffers that
spill to temporary files. zlib, LZ4, FastLZ, and outer gzip decompression process
small chunks and check cancellation. A single declaration name or signal value
can still exceed the working-buffer budget, and metadata grows with signal
count. There is no new native dependency. Disk mode
trades a slower first scan and disk space for low memory use and quick subsequent
opens. `--no-cache` still uses temporary disk storage, removed on close.
Declarations share metadata with the page store, and sequence wrappers are
created only when a signal is accessed. Pending index-summary groups spill to
disk when their bounded builder cache fills. Metadata still grows with the
number of declared signals; all signals remain available.

During background disk indexing, `;cancel` stops the load; closing the viewer
also cancels it. Partial indexes are never published, and spill files close on
completion, cancellation, or failure. Library callers can pass a cancellation
event to `FSTReader.parse(cancel=event)` or `storage.open_trace(cancel=event)`.
FST metadata is parsed before the viewer opens; library cancellation also covers
this stage. The prepared reader shares metadata with the viewer and retains
unwrapped input for background indexing, so declarations and outer gzip data
are processed once. Value chains and timestamp tables feed bounded batches;
same-timestamp transitions remain distinct and in their original order.
Disk spilling reduces memory at the cost of additional I/O and can
make the first parse slower. Eager FST parsing still retains decoded history.

Expression searches stream only the referenced signals. `when`/`match` searches
run in the background in disk mode, with progress and a `cancel` command. Derived
signal output also uses temporary pages. Clock measurement uses a disk sort for
its exact median. CSV, SVG, PDF, and PNG file writers stream their input and
output. PNG rasterization retains pixels rather than a complete event list,
including when narrow pulses share a pixel. Protocol decoders expose iterators;
AXI spills unmatched responses to a temporary disk queue.

Viewer protocol decoding, bus decoding, and instruction-trace building run in
the background with progress and `cancel`. Their complete results are indexed
in SQLite files, including when the source uses in-memory storage.
Overlays read the visible time range on a worker and cache only the painted
terminal cells. Changing the viewport cancels obsolete overlay reads. Successful
decodes replace prior results; cancellation or failure leaves prior results
available. Replacing/clearing results, reloading, or closing the trace releases
their temporary storage. No frame-count limit is imposed.

Completed instruction indexes are saved under the user cache directory and
reused by `;trace` on subsequent opens. Reuse requires matching source identity,
size and timestamps, signal tags, decoder settings, Python and implementation
versions. Changed or incomplete indexes rebuild; custom decoder classes and
sources without a stable parse-time identity use temporary storage. Only
completed indexes are published, and concurrent publishers coordinate through
a cache lock. Cache errors leave the temporary index usable.

Saved instruction indexes have a separate 8 GiB quota, evicting the least
recently used first. Larger individual indexes remain temporary. Building and
publishing need additional temporary disk space, including a copy of the index;
the quota covers completed cache files. `--no-cache` disables reuse and saving.
Use `;analysis-cache purge` in the viewer, or
`alwavelets --purge-analysis-cache`, to reclaim saved instruction indexes.
Open analyses remain usable after eviction or purge.

Instruction searches also run in the background. PC, hart, thread ID, exact
mnemonic, time, register-write, and memory-address filters use indexed fields;
matches retain record IDs on disk instead of duplicating instruction payloads.
The first 100 matches become navigation markers. `;results` opens a paged
browser of the complete selection alongside the waveform; moving between rows
updates the waveform cursor, including matches sharing a timestamp. Use
Up/Down or `n`/`p`, Page Up/Down, Home/End, and `g` to jump to a row. Enter or
Escape closes the browser. These commands also work from the command prompt:

```text
results goto 1000000
results next
results prev
results export /path/to/all-matches.csv
```

Export writes every match in capture order, runs in the background, and accepts
`;cancel`. Cancellation or errors preserve any existing destination. Summary
queries keep the last match selection available; a new filter replaces it.
Regex mnemonic searches and custom Python predicates can
require a scan. `cancel` stops scans, SQLite sorting, and index construction;
a single user-supplied predicate must return before cancellation can proceed.

The trace query prompt accepts `pc <lo> [hi]`, `time <lo> <hi>`,
`hart <id> [start end]`, `threadid <id>`, and `mn= <exact-name>`.
Existing `thread <id>` remains an alias for hart selection, and `mn <regex>`
keeps its regex behavior. IPC, fairness, starvation, divergence, heatmaps, and
register snapshots process history incrementally. The register panel reads
indexed snapshots on a background worker. Aggregate memory scales with the
number of harts (and heatmap dimensions), rather than retired instructions.
The historical `ipc` formula remains count divided by elapsed trace ticks;
it does not normalize by a separately tagged clock.

Explicit materialization such as `signal.changes`, `render_svg()`, and decoder
`decode()`/`decode_all()` result lists still uses memory proportional to the
requested output. Library `InstructionTrace.build()` defaults to memory; pass
`storage='disk'` for indexed storage whose open handle is tied to the source
trace's lifetime. Pass `use_cache=False` to disable instruction-cache reuse and
saving for that build.
Use `trace.select(...)` as a context manager for paged matches, or
`trace.iter_matching(...)` for streaming iteration. Existing `find_*`, `query()`,
and thread-extraction methods still return lists for compatibility. Prefer the
streaming APIs for large captures; individual transactions still retain their
payloads. Waveform file exports from the viewer remain synchronous.
Live tailing requires `--storage memory`; disk indexes are snapshots.

Library callers can explicitly select storage and should close traces:

```python
from alwavelets import parse_file
from alwavelets.exporting import write_signal_csv, write_svg
from alwavelets.graphics import write_waveform_png
from alwavelets.decoders import SPIDecoder

with parse_file("huge.vcd", storage="disk", memory_mib=64) as trace:
    clock = trace.signals[trace.signal_order[0]]
    data = trace.signals[trace.signal_order[1]]
    write_signal_csv(clock, "clock.csv")
    write_waveform_png(trace, "clock.png", [clock.full_name])
    write_svg(trace, "clock.svg", [clock.full_name])
    for frame in SPIDecoder(clock, data).iter_decode(0, trace.max_time):
        print(frame.t_start, frame.label)
```

SPI/I²C/UART iterators accept a time range; AXI/Wishbone/AHB provide
`iter_decode()`, and instruction traces provide `iter_entries(trace, tags)`.
Streaming decoders and CSV/SVG/PNG writers accept `cancel=threading.Event()`.
PNG reports progress per signal row; CSV reports processed timestamps.
Cancelled or failed exports leave an existing destination path intact.
Caller-supplied streams may already contain partial output.

External LXT/LXT2/GHW/VZT converters and the FST converter fallback write to
temporary files instead of capturing the converted trace in RAM. The converted
VCD follows the selected storage policy. Converted disk indexes are temporary
and removed when the trace closes; keep a converted VCD to reuse its index.

`VCDFile.parse()` remains the explicit eager reader. The old lazy reader remains
available through its low-level API and the benchmark's `lazy` mode.

Press `?` inside the viewer for quick help, or `F1` for the full manual.
For a browsable reference of every key, command, sidecar file and the MCP
integration, read the **[complete field guide](https://riadhbenabdelhamid.github.io/alwavelets/)**
— also in the repo at [`docs/index.html`](docs/index.html), self-contained and
usable offline.

Lazy views preserve the value entering a window and all changes at the same
timestamp. Analysis commands load their requested interval before returning
results; a whole-trace search can therefore load substantially more data than
the visible window. Commands report when the initial background load is still
in progress instead of searching incomplete data.

New session and arrangement files identify signals by hierarchical reference,
declared range, width, and type, so layouts survive regenerated VCD identifier
codes. Missing or ambiguous references are reported. Older version-1 files
remain readable using their original identifiers; save them again against the
original trace to upgrade their references. Saved time positions are restored
after background loading establishes the trace extent.

Diff comparison normalizes both traces to the same time units and compares
their overlapping time extent, including values held through the end of that
interval. Separately declared bits and non-zero-based or ascending bus ranges
retain their indices in signal names and expanded bit labels.

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
| `parsers` | VCD parsing (sync / background / lazy / follow), scope tree, `parse_file` dispatcher |
| `value_stream` | Shared incremental value, comment, and dump-state decoding |
| `fst` | Native FST reader with pure-Python LZ4 and FastLZ block decoders |
| `fst_stream` | Bounded FST decompression and temporary storage for blocks and numeric tables |
| `cache` | Persistent on-disk parse cache (instant re-open of unchanged traces) |
| `storage` | Paged VCD/FST storage, LRU decoding cache, persistent summary hierarchy |
| `background` | Bounded UI read queue and cancellable analysis jobs |
| `instruction_store` | Indexed instruction fields and paged record-ID selections |
| `instruction_analysis` | Streaming instruction filters, statistics, and register snapshots |
| `analysis_cache` | Reusable instruction indexes, validation, quotas, and eviction |
| `ui_results` | Background paging, waveform jumps, and full-selection CSV export |
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
the parser), a **lazy-chunk parity** check (the on-demand loader must agree
with a full parse), and an **FST parity** check: `tests/fixtures/fst_ref.vcd`
converted by `vcd2fst` with each compression (`-4`, `-F`, `-Z`, `-c`) must
decode to exactly what the VCD parser reads. Both lint and tests run in CI
(`.github/workflows/ci.yml`) on every push, across Python 3.8–3.13. Ruff is
configured in `ruff.toml` for bug-finding rules only (`E9` + `F`), not full
style enforcement.

`pip install .` installs the package and an `alwavelets` console command
(`pyproject.toml`; no runtime dependencies).

### Performance benchmarks

The stdlib-only harness generates deterministic scalar, bus, real, mixed, and
sparse VCD workloads. Each sample runs in a fresh interpreter, bypasses the
parse cache, and verifies rendered views across loading modes. Reports include
raw samples, medians, input hashes, Python/platform metadata, and Git revision.

```sh
python3 tools/benchmark.py --size-mib 8 --repeat 5 --output before.json
python3 tools/benchmark.py --size-mib 8 --repeat 5 --baseline before.json --output after.json
python3 tools/benchmark.py --scenarios mixed --size-mib 64 --modes full lazy parallel --jobs 4
python3 tools/benchmark.py --scenarios mixed --size-mib 1024 --modes disk --memory-mib 8 --repeat 1
python3 tools/benchmark.py --input /path/to/design.vcd --repeat 5
```

Use `--repo /path/to/checkout` to measure another revision with the same harness.
Compare matching inputs on the same machine and Python version, with other
heavy workloads stopped. Timing is informational, not a machine-dependent CI
pass/fail threshold; ordinary tests check benchmark determinism and reader parity.

- `load(s)` measures a full parse or, in lazy mode, header parsing plus indexing.
  Disk mode measures building a fresh complete index; this includes neither a
  persistent-cache hit nor the UI's progressive display before indexing ends.
- `first(s)` includes loading and rendering the initial 16-signal text view.
  It is a headless proxy for useful first display, not curses startup latency.
- `seek(ms)` measures loading/rendering a previously unvisited window at 90%
  of the trace; `pan(ms)` is the median of three further jumps.
- `main RSS(MiB)` is the main parsing process's peak resident memory, including
  imports and rendering. **Parallel workers are excluded**, so it is not total
  parallel memory usage. It is unavailable on platforms without `resource`.

Generation and imports are outside the timed region. The OS page cache is not
flushed; "cold" refers to Alwavelets' loaded windows, not cold disk I/O. Use
`--size-mib` and `--signals` to test larger files and hierarchy widths; synthetic
results should be supplemented with representative project traces.

To exercise complete workflows and many declarations in fresh processes:

```sh
python3 tools/stress_workflows.py --size-gib 10 --signals 100000 --memory-mib 8 --output stress.json
```

This generates a 4096-bit bus with a clock, verifies indexed values, streams
PNG/CSV exports and paged SPI decoding, and checks reopening without loading
pages. Separate workloads check 100,000 signal declarations and one million
records each for frames, transactions, and instructions (`--result-frames N`).
Reports include process peak RSS before and after reopening, page-cache usage,
index size, and timings. The reopening measurement retains the first closed
trace object, so its metadata is still resident. The generated traces and
indexes require temporary disk space and are removed after the run.

Instruction query stress checks build ten million synthetic retirements,
validate full and selective result counts, and exercise per-hart statistics
and register reconstruction in a fresh process:

```sh
python3 tools/stress_instructions.py --records 10000000 --output instructions.json
python3 tools/stress_instructions.py --records 10000000 --cache-roundtrip --output instruction-cache.json
```

The report includes index size, query/build times, and total process peak RSS.
The synthetic workload includes repeated timestamps and unsigned 64-bit PCs.
The cache roundtrip additionally checks persistence, reopening, final-page
access, and purge. It needs space for both the temporary and saved index.

FST stress checks generate compressed traces without external tools: a single
chain with ten million timestamps, 100,000 declarations, and a wide initial
frame. Optional block checks expand 1 GiB through each supported chain codec:

```sh
python3 tools/stress_fst.py --events 10000000 --signals 100000 --memory-mib 8 --output fst.json
python3 tools/stress_fst.py --block-mib 1024 --output fst-blocks.json
python3 tools/stress_fst.py --background --wrapped --output fst-startup.json
```

Fresh reader processes verify values and report peak RSS, parse time, index
size, and automatic storage selection. `--repo /path/to/checkout` compares
another revision on the same generated workload. Block measurements include
decompression and sampled reads, not full waveform indexing. Temporary files
are removed after each run; disk space must accommodate the expanded blocks.
`--background` also measures when metadata is ready and when the first signal
has readable waveform data (not terminal rendering time). `--wrapped` repeats
the trace checks with outer gzip compression.

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
