"""The headless half of the package (parsers, snapshots, the MCP server)
must import where curses is missing; the viewer, CLI and finder load lazily."""
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── headless import ──────────────────────────────────────────────────

def test_headless_modules_import_without_curses():
    code = ("import sys; sys.modules['curses'] = None\n"
            "import alwavelets, alwavelets.mcp_server\n"
            "assert 'alwavelets.ui' not in sys.modules\n"
            "print(alwavelets.render_text.__name__)")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "render_text"


def test_lazy_attribute_loads_viewer(w):
    assert callable(w.main) and w.Alwavelets.__name__ == "Alwavelets"
    with pytest.raises(AttributeError):
        w.no_such_name
