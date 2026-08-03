"""Alwavelets — terminal VCD/FST waveform viewer.

The viewer is organized as a package of cohesive modules:

  core         data structures (VCDSignal, Radix, …) and value formatting
  parsers      VCD/FST parsing, scope tree, parse_file dispatcher
  decoders     protocol (SPI/I²C/UART), RISC-V, and bus (AXI/Wishbone/AHB) decode
  rendering    pure waveform draw functions
  analysis     formal-CEX and power-activity analysis
  finder       fuzzy signal finder overlay ('/')
  control      agent control channel for a live session (--control)
  persistence  JSON sidecar stores (tags, annotations, bookmarks)
  tcl          small scripting/command engine
  manual       in-app help/manual text
  ui           the Alwavelets curses TUI
  cli          demo generation, bootstrap, and main()

This module re-exports the public API so `import alwavelets` exposes every name,
and `alwavelets.main` is the entry point used by the top-level `alwavelets.py`
launcher.
"""
from .core import *  # noqa: F401,F403
from .rendering import *  # noqa: F401,F403
from .rendering import _bit_char, _update_wave_chars  # noqa: F401
from .decoders import *  # noqa: F401,F403
from .parsers import *  # noqa: F401,F403
from .cache import *  # noqa: F401,F403
from .parallel import *  # noqa: F401,F403
from .expr import *  # noqa: F401,F403
from .gtkw import *  # noqa: F401,F403
from .config import *  # noqa: F401,F403
from .analysis import *  # noqa: F401,F403
from .persistence import *  # noqa: F401,F403
from .tcl import *  # noqa: F401,F403
from .manual import *  # noqa: F401,F403
from .finder import *  # noqa: F401,F403
from .snapshot import *  # noqa: F401,F403
from .graphics import *  # noqa: F401,F403
from .ui import *  # noqa: F401,F403
from .cli import *  # noqa: F401,F403
