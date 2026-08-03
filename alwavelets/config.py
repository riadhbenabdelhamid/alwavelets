"""User configuration: ~/.config/alwavelets/config.json (or $XDG_CONFIG_HOME).

A small optional JSON file of startup defaults and keybinding remaps. Everything
is optional; unknown keys are ignored and any error falls back to built-in
defaults, so a malformed config never blocks startup. Example:

    {
      "theme": "phosphor",
      "radix": "hex",
      "wave_style": 2,
      "menu_bar": false,
      "grid": 1,
      "name_width": 28,
      "keys": { "quit": "Q", "zoom_in": "=", "toggle_cursor": " " }
    }

`keys` maps an action name (the keymap handler name, e.g. "quit", "zoom_in",
"next_edge") to a single character that should trigger it.
"""
import json
import os

# Module-level switch so `--no-config` can disable loading in one place.
ENABLED = True


def _config_base():
    return (os.environ.get('XDG_CONFIG_HOME')
            or os.path.join(os.path.expanduser('~'), '.config'))


def config_path(existing=False):
    """Where the config lives. With existing=True, fall back to the
    pre-rename ~/.config/wavelets/ if only that one is present, so an
    upgrade keeps the user's settings instead of silently ignoring them."""
    new = os.path.join(_config_base(), 'alwavelets', 'config.json')
    if existing and not os.path.exists(new):
        old = os.path.join(_config_base(), 'wavelets', 'config.json')
        if os.path.exists(old):
            return old
    return new


def load_config():
    """Return the parsed config dict, or {} if disabled / missing / invalid."""
    if not ENABLED:
        return {}
    try:
        with open(config_path(existing=True)) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}
