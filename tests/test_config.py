"""Tests for the user config loader and keybinding-override application."""
import json


def _write(tmp_path, monkeypatch, obj):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from alwavelets import config
    p = tmp_path / "cfg" / "alwavelets"
    p.mkdir(parents=True, exist_ok=True)
    (p / "config.json").write_text(json.dumps(obj))
    return config


def test_load_missing_returns_empty(w, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from alwavelets import config
    assert config.load_config() == {}


def test_load_valid(w, tmp_path, monkeypatch):
    config = _write(tmp_path, monkeypatch, {"theme": "phosphor", "radix": "hex"})
    cfg = config.load_config()
    assert cfg["theme"] == "phosphor" and cfg["radix"] == "hex"


def test_load_bad_json_returns_empty(w, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from alwavelets import config
    p = tmp_path / "cfg" / "alwavelets"
    p.mkdir(parents=True, exist_ok=True)
    (p / "config.json").write_text("{ not valid json ")
    assert config.load_config() == {}


def test_disabled_switch(w, tmp_path, monkeypatch):
    config = _write(tmp_path, monkeypatch, {"theme": "phosphor"})
    config.ENABLED = False
    try:
        assert config.load_config() == {}
    finally:
        config.ENABLED = True


def test_keymap_overrides(w):
    from alwavelets.ui import Alwavelets

    def _kb_quit(self=None):
        return "QUIT"

    def _kb_zoom_in(self=None):
        pass

    stub = type("Stub", (), {})()
    stub.keymap = {ord("q"): _kb_quit, ord("+"): _kb_zoom_in}
    Alwavelets._apply_keymap_overrides(
        stub, {"quit": "Q", "zoom_in": "=", "bogus": "Z", "quit_multi": "xx"})
    assert stub.keymap[ord("Q")] is _kb_quit       # remapped
    assert stub.keymap[ord("=")] is _kb_zoom_in
    assert ord("Z") not in stub.keymap             # unknown action ignored
    assert stub.keymap[ord("q")] is _kb_quit       # original kept (additive)
