"""Import the `alwavelets` package for tests.

Importing the package has no side effects (the CLI lives in `alwavelets.main`,
guarded by the launcher), so it's safe to load once per session.
"""
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


@pytest.fixture(scope="session")
def w():
    import alwavelets
    return alwavelets


def make_vcd(path, n_steps=400, step=10):
    """Write a small deterministic VCD: a clock + an 8-bit counter bus.

    No two changes share a timestamp, so dedup behaviour is unambiguous.
    """
    lines = [
        "$timescale 1ns $end",
        "$scope module top $end",
        "$var wire 1 ! clk $end",
        '$var wire 8 " data $end',
        "$upscope $end",
        "$enddefinitions $end",
        "#0",
        "0!",
        'b00000000 "',
    ]
    for t in range(1, n_steps):
        lines.append(f"#{t * step}")
        lines.append(("1" if t % 2 else "0") + "!")
        lines.append("b" + format(t & 0xFF, "08b") + ' "')
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path
