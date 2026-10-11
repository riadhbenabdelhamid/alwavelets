"""Command-line frontend: demo-trace generation, sidecar auto-loading,
the curses bootstrap (main_curses), and argument parsing (main)."""

import argparse
import curses
import fnmatch
import json
import os
import sys
import time as _time

from .parsers import *  # noqa: F401,F403
from .persistence import *  # noqa: F401,F403
from .ui import *  # noqa: F401,F403


# ══════════════════════════════════════════════════════════════
#  DEMO VCD GENERATOR
# ══════════════════════════════════════════════════════════════

def generate_demo_vcd(filename):
    import random
    random.seed(42)

    with open(filename, 'w') as f:
        f.write("$date Sat Feb 28 2026 $end\n")
        f.write("$version Alwavelets Demo $end\n")
        f.write("$timescale 1ns $end\n")

        f.write("$scope module top $end\n")
        f.write("$var wire 1 ! clk $end\n")
        f.write("$var wire 1 \" rst_n $end\n")
        f.write("$var wire 1 # enable $end\n")
        f.write("$var wire 8 $ data_in [7:0] $end\n")
        f.write("$var wire 8 % data_out [7:0] $end\n")
        f.write("$var wire 1 & valid $end\n")
        f.write("$var wire 1 ' ready $end\n")
        f.write("$var wire 4 ( state [3:0] $end\n")
        f.write("$var real 1 ~ temperature $end\n")
        f.write("$var wire 8 3 adc [7:0] $end\n")

        f.write("$scope module alu $end\n")
        f.write("$var wire 8 ) op_a [7:0] $end\n")
        f.write("$var wire 8 * op_b [7:0] $end\n")
        f.write("$var wire 8 + result [7:0] $end\n")
        f.write("$var wire 2 , alu_op [1:0] $end\n")
        f.write("$var wire 1 - overflow $end\n")
        f.write("$upscope $end\n")

        f.write("$scope module memory $end\n")
        f.write("$var wire 16 . addr [15:0] $end\n")
        f.write("$var wire 8 / wdata [7:0] $end\n")
        f.write("$var wire 8 0 rdata [7:0] $end\n")
        f.write("$var wire 1 1 we $end\n")
        f.write("$var wire 1 2 re $end\n")
        f.write("$upscope $end\n")
        f.write("$upscope $end\n")
        f.write("$enddefinitions $end\n")

        f.write("$dumpvars\n")
        f.write("0!\n0\"\nx#\nbxxxxxxxx $\nbxxxxxxxx %\n0&\n1'\n")
        f.write("b0000 (\nr25.0 ~\nb10000000 3\n")
        f.write("b00000000 )\nb00000000 *\nb00000000 +\nb00 ,\n0-\n")
        f.write("b0000000000000000 .\nb00000000 /\nbxxxxxxxx 0\n01\n02\n")
        f.write("$end\n")

        max_time = 2000

        # Clock
        for t in range(0, max_time, 5):
            f.write(f"#{t}\n{(t//5)%2}!\n")

        # Reset, enable
        f.write("#0\n0\"\n#20\n1\"\n#30\n1#\n#500\n0#\n#700\n1#\n")

        # Data buses
        for t in range(40, max_time, 50):
            f.write(f"#{t}\nb{random.randint(0,255):08b} $\n")
        for t in range(45, max_time, 50):
            f.write(f"#{t}\nb{random.randint(0,255):08b} %\n")

        # Handshake
        for t in range(40, max_time, 100):
            f.write(f"#{t}\n1&\n#{t+10}\n0&\n")
        for t in range(35, max_time, 80):
            f.write(f"#{t}\n0'\n#{t+15}\n1'\n")

        # State machine
        states = ['0000','0001','0010','0011','0100','0101','0110','0001']
        for i, t in enumerate(range(50, max_time, 120)):
            f.write(f"#{t}\nb{states[i%len(states)]} (\n")

        # Analog temperature — sine wave with noise
        import math
        for t in range(0, max_time, 10):
            temp = 25.0 + 10.0 * math.sin(t / 200.0) + random.gauss(0, 0.5)
            f.write(f"#{t}\nr{temp:.2f} ~\n")

        # ADC — clean 8-bit sine sweep, sampled finely. Shown as a multi-row
        # analog line graph in the demo (see _setup_demo_view).
        for t in range(0, max_time, 8):
            val = int(127.5 + 127.0 * math.sin(t / 130.0))
            f.write(f"#{t}\nb{val & 0xFF:08b} 3\n")

        # ALU
        for t in range(60, max_time, 70):
            a, b = random.randint(0,255), random.randint(0,255)
            f.write(f"#{t}\nb{a:08b} )\nb{b:08b} *\nb{(a+b)&0xFF:08b} +\nb{random.randint(0,3):02b} ,\n{1 if a+b>255 else 0}-\n")

        # Memory
        for t in range(80, max_time, 90):
            f.write(f"#{t}\nb{random.randint(0,65535):016b} .\nb{random.randint(0,255):08b} /\nb{random.randint(0,255):08b} 0\n{random.randint(0,1)}1\n{random.randint(0,1)}2\n")

        # Dumpoff/Dumpon test
        f.write(f"#{max_time + 100}\n$dumpoff $end\n")
        f.write(f"#{max_time + 200}\n$dumpon $end\n")
        f.write(f"#{max_time + 200}\n1!\n0\"\n")

    return filename


# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════

def _auto_load_sidecars(viewer, vcd_path):
    try:
        p, n = viewer.annotations.auto_load(vcd_path)
        if n > 0:
            viewer._show_message(f"Loaded {n} annotations from {os.path.basename(p)}")
    except Exception:
        pass
    try:
        p, n = viewer.bookmarks.auto_load(vcd_path)
        if n > 0:
            viewer._show_message(f"Loaded {n} bookmarks from {os.path.basename(p)}")
    except Exception:
        pass


def _setup_demo_view(viewer):
    """Pre-configure the demo view so it showcases features out of the box
    (applied only to --demo, never to real files): a couple of collapsible
    signal groups, plus a multi-row analog signal. Members/signals are all
    top-level, so they stay visible while the alu/memory scopes start folded."""
    by_full = {s.full_name: sid for sid, s in viewer.vcd.signals.items()}
    # Collapsible groups, nested under one "Interface" parent to showcase
    # group nesting: (name, member full-names, start collapsed?)
    for name, fulls, collapsed in (
            ("Handshake", ["top.valid", "top.ready"], False),
            ("Data bus", ["top.data_in", "top.data_out"], True)):
        members = [by_full[f] for f in fulls if f in by_full]
        if members and viewer.create_group(name, members, parent="Interface") \
                and collapsed:
            viewer.toggle_group_collapse(name)
    # Multi-row analog: render the ADC sine sweep as a 3-row-tall line graph.
    sid = by_full.get("top.adc")
    if sid:
        viewer.analog_signals.add(sid)
        viewer.sig_height[sid] = 3


def main_curses(stdscr, vcd, start_collapsed=True, load_unchecked=False,
                demo_view=False, use_session=True, control=False, workmode=None):
    viewer = Alwavelets(stdscr, vcd)
    if control:
        # Opt-in agent control channel. Failing to bind is not fatal — the
        # viewer is perfectly usable without it.
        from .control import ControlServer
        srv = ControlServer(viewer, getattr(vcd, '_filename', ''))
        if srv.start():
            viewer.control = srv
        else:
            viewer._show_message("Control channel unavailable (socket failed)")
    if load_unchecked:
        viewer.display_order = []
        viewer._sync_display_set()
        viewer.indent_mode = True
    elif start_collapsed:
        viewer.collapse_all_modules()
        viewer.indent_mode = True
    # Ensure tree is built for panel mode
    if viewer.panel_mode and not viewer._tree_flat:
        viewer._rebuild_tree()
    if demo_view:
        _setup_demo_view(viewer)
    if hasattr(vcd, '_filename') and vcd._filename:
        _auto_load_sidecars(viewer, vcd._filename)
        # A session sidecar (written by ';session') restores the whole
        # layout/view, so it only applies when the user hasn't asked for a
        # specific starting layout — otherwise --loadunchecked/--expanded
        # would appear to do nothing, with no way to override from the CLI.
        if use_session and not demo_view:
            viewer.restore_session()
    if workmode is not None:
        # An explicit launch choice wins over config and saved appearance.
        viewer.apply_workmode(workmode, notify=False)
    if getattr(vcd, '_startup_message', None):
        viewer._show_message(vcd._startup_message)
    viewer.run()


def _prepare_native_view(args, cancel, report):
    from .loading import prepare_native
    from .streaming import check_cancel
    vcd = prepare_native(args.file, storage=args.storage, memory_mib=args.memory_mib,
                         use_cache=not args.no_cache, cancel=cancel, stage_cb=report)
    try:
        if args.signals:
            patterns = [p.strip() for p in args.signals.split(',')]
            report('Selecting displayed signals', 0, len(vcd.signal_order), 'signals')
            selected = []
            for i, sid in enumerate(vcd.signal_order):
                if i % 256 == 0:
                    check_cancel(cancel)
                    report('Selecting displayed signals', i, len(vcd.signal_order), 'signals')
                sig = vcd.signals[sid]
                if any(fnmatch.fnmatch(sig.name, p) or fnmatch.fnmatch(sig.full_name, p) for p in patterns):
                    selected.append(sid)
            if selected:
                vcd.signal_order = selected
            else:
                vcd._startup_message = f"No signals match '{args.signals}'; showing all."
        check_cancel(cancel)
        return vcd
    except BaseException:
        vcd.close()
        raise


def _open_native_curses(stdscr, args):
    from .loading import wait_for_trace
    vcd = wait_for_trace(stdscr, args.file, lambda cancel, report: _prepare_native_view(args, cancel, report))
    try:
        main_curses(stdscr, vcd, start_collapsed=not args.expanded,
                    load_unchecked=args.loadunchecked,
                    use_session=not (args.no_session or args.expanded or args.loadunchecked),
                    control=args.control, workmode=getattr(args, 'workmode', None))
    finally:
        vcd.close()


def main():
    """Treat Ctrl-C as an exit request after trace and terminal cleanup."""
    try:
        return _main()
    except KeyboardInterrupt:
        raise SystemExit(130) from None


def _main():
    parser = argparse.ArgumentParser(
        description='Alwavelets — Terminal Waveform Viewer',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Run with --demo to try a built-in example. Press ? in the viewer for help."
    )
    parser.add_argument('file', nargs='?', help='VCD file to view')
    parser.add_argument('--demo', action='store_true', help='Generate and view a demo VCD')
    parser.add_argument('--signals', '-s', help="Signal filter patterns, comma-separated (e.g. 'clk,data*,alu.*')")
    parser.add_argument('--sync', action='store_true', help="Force synchronous parse (disable background loading)")
    parser.add_argument('--storage', choices=('auto', 'memory', 'disk'), default='auto',
                        help="Waveform storage (auto uses disk for large files, many declarations, or large FST expansion)")
    parser.add_argument('--memory-mib', type=int, default=256, metavar='N',
                        help="Decoded waveform page cache budget in MiB (default: 256)")
    parser.add_argument('--loadunchecked', action='store_true', help="Load with no signals shown (select via B tree browser)")
    parser.add_argument('--expanded', action='store_true', help="Start with all modules expanded (default: collapsed)")
    parser.add_argument('--workmode', choices=tuple(Alwavelets.WORKMODES),
                        help="UI presentation: calm, classic, or minimal (overrides saved appearance)")
    parser.add_argument('--mcp', action='store_true', help="Run as an MCP server (stdio) instead of the viewer")
    parser.add_argument('--no-cache', action='store_true', help="Disable persistent waveform and instruction caches")
    parser.add_argument('--cache-dir', metavar='PATH',
                        help="Cache directory (also ALWAVELETS_CACHE_DIR; default follows XDG_CACHE_HOME)")
    parser.add_argument('--waveform-cache-mib', type=int, metavar='N',
                        help="Saved waveform cache quota in MiB (default: unlimited; 0 disables)")
    parser.add_argument('--analysis-cache-mib', type=int, default=8192, metavar='N',
                        help="Persistent instruction cache quota in MiB (default: 8192; 0 disables)")
    maintenance = parser.add_mutually_exclusive_group()
    maintenance.add_argument('--purge-analysis-cache', action='store_true',
                        help="Remove cached instruction indexes and exit")
    maintenance.add_argument('--purge-partial-indexes', action='store_true',
                        help="Remove idle incomplete waveform indexes and exit")
    maintenance.add_argument('--list-waveform-cache', action='store_true',
                             help="Show waveform cache entries, usage, and free space as JSON, then exit")
    maintenance.add_argument('--purge-waveform-cache', action='store_true',
                             help="Remove all idle waveform caches and checkpoints, then exit")
    maintenance.add_argument('--remove-waveform-cache', action='append', metavar='NAME',
                             help="Remove a named idle cache entry (repeatable); get names from --list-waveform-cache")
    parser.add_argument('--no-config', action='store_true', help="Ignore ~/.config/alwavelets/config.json")
    parser.add_argument('--no-session', action='store_true',
                        help="Ignore a saved session sidecar (open the trace fresh)")
    parser.add_argument('--control', action='store_true',
                        help="Expose a token-authenticated control socket so an "
                             "authorized local agent can drive this session "
                             "(see SKILL.md); off unless asked for")
    parser.add_argument('--list-sessions', action='store_true',
                        help="List controllable running viewers as JSON, then exit")
    parser.add_argument('--jobs', '-j', type=int, default=None, metavar='N',
                        help="Opt-in parallel parse workers for a large (>16MB) synchronous "
                             "VCD parse, e.g. --sync --jobs 8 (omit = sequential)")
    args = parser.parse_args()
    if args.memory_mib <= 0:
        parser.error('--memory-mib must be positive')
    if args.analysis_cache_mib < 0:
        parser.error('--analysis-cache-mib must be nonnegative')
    if args.waveform_cache_mib is not None and args.waveform_cache_mib < 0:
        parser.error('--waveform-cache-mib must be nonnegative')
    from . import analysis_cache, index_cache, cache as _cache
    if args.cache_dir:
        _cache.DIRECTORY = args.cache_dir
    if args.waveform_cache_mib is not None:
        index_cache.QUOTA_BYTES = args.waveform_cache_mib * 1024 * 1024
    analysis_cache.QUOTA_BYTES = args.analysis_cache_mib * 1024 * 1024
    if args.no_cache:
        _cache.ENABLED = False
    if args.purge_analysis_cache:
        count, size = analysis_cache.InstructionCache().purge()
        print(f'Removed {count} instruction indexes ({size / 1024 ** 2:.1f} MiB)')
        return
    if args.purge_partial_indexes or args.list_waveform_cache or args.purge_waveform_cache or args.remove_waveform_cache:
        import sqlite3
        try:
            manager = index_cache.WaveformCache()
            if args.list_waveform_cache:
                print(json.dumps(manager.inspect(), indent=2))
            else:
                count, size, skipped = manager.purge(args.remove_waveform_cache, partial_only=args.purge_partial_indexes)
                label = 'incomplete waveform indexes' if args.purge_partial_indexes else 'waveform cache entries'
                print(f'Removed {count} {label} ({size / 1024 ** 2:.1f} MiB); skipped {skipped} busy or unavailable')
        except (OSError, sqlite3.Error, ValueError) as exc:
            print(f'Waveform cache error: {exc}', file=sys.stderr)
            sys.exit(1)
        return
    if getattr(args, 'no_config', False):
        from . import config as _config
        _config.ENABLED = False

    if args.list_sessions:
        # Discovery for an agent: which live viewers can be driven right now.
        from .control import list_sessions
        print(json.dumps([s for s in list_sessions() if not s['stale']], indent=2))
        return

    if args.mcp:
        # Serve waveform snapshots over stdio (JSON-RPC) to an agentic workflow.
        from .mcp_server import serve
        serve()
        return

    if args.demo:
        demo_file = _demo_path()
        print(f"Generating demo VCD: {demo_file}")
        generate_demo_vcd(demo_file)
        vcd = VCDFile.parse(demo_file)
    elif args.file:
        if not os.path.isfile(args.file):
            print(f"Error: File not found: {args.file}", file=sys.stderr)
            sys.exit(1)
        # Enter curses before storage detection, cache upgrades, or declarations.
        # --sync and converted legacy formats retain their synchronous path.
        with open(args.file, 'rb') as source:
            native = source.read(1) in (b'$', b'\0', b'\xfe')
        if native and not args.sync:
            import sqlite3
            from .streaming import OperationCancelled
            try:
                curses.wrapper(_open_native_curses, args)
            except OperationCancelled:
                print('Opening cancelled.')
            except (OSError, ValueError, sqlite3.Error) as exc:
                print(f'Error: {exc}', file=sys.stderr)
                sys.exit(1)
            return
        fsize = os.path.getsize(args.file)
        print(f"Parsing: {args.file} ({fsize/1024/1024:.1f} MB)")

        ext = os.path.splitext(args.file)[1].lower()

        # Detect actual file type by content
        is_vcd_content = False
        try:
            with open(args.file, 'rb') as f:
                fb = f.read(1)
                is_vcd_content = (fb and fb[0] == 0x24)  # '$' = VCD
        except Exception:
            pass

        from . import cache as _cache
        if args.no_cache:
            _cache.ENABLED = False

        # Fast path: a valid persistent cache loads the whole trace instantly.
        t_c = _time.time()
        from .storage import use_disk_storage, open_trace
        disk = use_disk_storage(args.file, args.storage)
        vcd = None if disk else _cache.load_cache(args.file)
        if disk:
            vcd = open_trace(args.file, memory_mib=args.memory_mib,
                             background=not args.sync, use_cache=not args.no_cache)
            print(f"  Disk storage: {len(vcd.signal_order)} signals, {args.memory_mib} MiB page cache")
        elif vcd is not None:
            print(f"  Cache hit: {len(vcd.signal_order)} signals in {(_time.time()-t_c)*1000:.0f}ms")
        # Use background loading for VCD files >5MB (unless --sync)
        elif not args.sync and is_vcd_content and fsize > 5 * 1024 * 1024:
            t0 = _time.time()
            vcd, offset = VCDFile.parse_header(args.file)
            t_hdr = _time.time() - t0
            print(f"  Header: {len(vcd.signal_order)} signals in {t_hdr*1000:.0f}ms")
            print(f"  Background loading: viewer starts immediately")
            vcd.start_background_load(lazy_threshold=float('inf'))
        else:
            # Small file or non-VCD: synchronous parse
            def progress(read, total):
                pct = read * 100 // total
                bar = '█' * (pct // 2) + '░' * (50 - pct // 2)
                print(f"\r  [{bar}] {pct}%", end='', flush=True)
            try:
                vcd = parse_file(args.file, progress_cb=progress if fsize > 1 << 20 else None,
                                 jobs=args.jobs, storage=args.storage, memory_mib=args.memory_mib)
            except ConverterError as e:
                print(f"\nError: {e}", file=sys.stderr)
                sys.exit(1)
            if fsize > 1 << 20:
                print()
    else:
        parser.print_help()
        sys.exit(0)

    # ── Empty VCD check ──
    if not vcd.signal_order:
        print("Error: No signals found in VCD file.", file=sys.stderr)
        sys.exit(1)

    # ── Signal filter ──
    if args.signals:
        patterns = [p.strip() for p in args.signals.split(',')]
        filtered = []
        for sid in vcd.signal_order:
            sig = vcd.signals[sid]
            for pat in patterns:
                if fnmatch.fnmatch(sig.name, pat) or fnmatch.fnmatch(sig.full_name, pat):
                    filtered.append(sid)
                    break
        if filtered:
            vcd.signal_order = filtered
            print(f"Filtered to {len(filtered)} signals matching: {args.signals}")
        else:
            print(f"Warning: No signals match '{args.signals}', showing all.", file=sys.stderr)

    store = getattr(vcd, '_store', None)
    total = store.total_changes if store is not None else sum(s.num_changes() for s in vcd.signals.values())
    if vcd.is_loading:
        print(f"Loaded {len(vcd.signal_order)} signals (values loading in background...)")
    else:
        print(f"Loaded {len(vcd.signal_order)} signals, {total:,} changes, max t={vcd.max_time:,}")
    print("Launching Alwavelets... (press ? for help)")
    expanded = getattr(args, 'expanded', False)
    load_unchecked = getattr(args, 'loadunchecked', False)
    # An explicit layout flag (or --no-session) means "open it this way", so
    # don't let a saved session silently overrule it.
    use_session = not (args.no_session or expanded or load_unchecked)
    try:
        curses.wrapper(main_curses, vcd, start_collapsed=not expanded,
                       load_unchecked=load_unchecked, demo_view=args.demo,
                       use_session=use_session, control=args.control, workmode=args.workmode)
    finally:
        vcd.close()


def _demo_path(name="demo_waveform.vcd"):
    """A per-user, 0700 directory under the temp dir for generated demo
    traces (and their sidecars) — never a fixed world-writable /tmp name."""
    import tempfile
    try:
        tag = str(os.getuid())
    except AttributeError:                       # no getuid on Windows
        tag = os.environ.get('USERNAME', 'user')
    d = os.path.join(tempfile.gettempdir(), f"alwavelets-{tag}")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return os.path.join(d, name)


def demo(signals=None, start="0ns", end="300ns", width=88, path=None):
    """Print a demo waveform — the fastest way to show what this does.

    Generates the built-in demo trace (once, cached at `path`) and renders it
    as Unicode text, so it works in a plain Python REPL, a notebook, or a
    pasted snippet with no curses terminal involved:

        >>> import alwavelets
        >>> alwavelets.demo()

    Returns the parsed VCDFile so the session can continue against it:

        >>> vcd = alwavelets.demo()
        >>> alwavelets.measure(vcd, ["top.clk"])
    """
    from .snapshot import render_text
    if path is None:
        path = _demo_path("alwavelets_demo.vcd")
    if not os.path.isfile(path):
        generate_demo_vcd(path)
    vcd = parse_file(path)
    if signals is None:
        signals = ["top.clk", "top.rst_n", "top.valid", "top.ready",
                   "top.data_out", "top.state"]
    print(render_text(vcd, signals, start, end, width=width))
    print("\n  trace: %s   (%d signals, max t=%s)"
          % (path, len(vcd.signal_order), vcd.time_to_str(vcd.max_time)))
    print("  next:  alwavelets.render_text(vcd, ['top.*'], '0ns', '1us')")
    print("         alwavelets.measure(vcd, ['top.clk'])")
    print("         alwavelets.py --demo          # the full interactive viewer")
    return vcd
