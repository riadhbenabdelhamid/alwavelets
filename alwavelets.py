#!/usr/bin/env python3
"""Launcher for Alwavelets — keeps `python3 alwavelets.py <file.vcd>` working.

The implementation lives in the `alwavelets/` package; this thin entry point just
hands off to alwavelets.main().
"""
from alwavelets import main

if __name__ == "__main__":
    main()
