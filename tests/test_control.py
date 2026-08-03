"""Tests for the agent control channel (alwavelets/control.py).

The op handlers run against a lightweight viewer stub — they only need the
viewer's public state and execute_command — so no curses terminal is required.
The socket/auth layer is exercised against a real ControlServer.
"""
import json
import os
import socket
import types

import pytest


def _viewer(w, tmp_path):
    """Stub carrying the state the control ops touch, with the real command
    dispatch and group machinery bound on."""
    import sys
    sys.path.insert(0, "tests")
    from conftest import make_vcd
    from alwavelets.ui import Alwavelets

    path = str(tmp_path / "c.vcd")
    make_vcd(path, n_steps=40)
    vcd = w.VCDFile.parse(path)

    s = types.SimpleNamespace(
        vcd=vcd, display_order=list(vcd.signal_order),
        _display_order_set=set(vcd.signal_order),
        groups=[], _group_by_name={}, _member_group={}, _group_depth={},
        view_rows=list(vcd.signal_order), sig_offset=0, focus_idx=0,
        visible_signals=20, selected_signals=set(), _visual_mode=False,
        markers=[], time_start=0, time_end=vcd.max_time,
        cursor_a=0, cursor_b=0, active_cursor="a",
        cursor_on=True, cursor_b_on=False,
        radix=w.Radix.HEX, sig_radix={}, sig_color={}, sig_height={},
        analog_signals=set(), collapsed_scopes=set(),
        _undo_stack=[], _needs_layout=False, _dirty=False, _remote_depth=0,
        _message=None, width=100, height=40, show_grid=0,
        signal_spacing=0, show_full_names=False, show_tree=False,
        show_manual=False, virtual_signals={}, control=None,
        tcl=types.SimpleNamespace(execute=lambda c: None),
        translations=w.TranslationMap())
    s._show_message = lambda m: setattr(s, "_message", m)
    s.prompt_input = lambda *a: ""       # nobody at the keyboard
    s.update_layout = lambda: None
    s._invalidate_scope_cache = lambda: None
    s.keymap = {}
    for m in ("execute_command", "_command_table", "command_verbs",
              "focused_signal_id", "focused_token", "get_signal",
              "get_signal_radix", "create_group", "ungroup",
              "toggle_group_collapse", "_reindex_groups", "_rebuild_view_rows",
              "_group_chain", "_sync_display_set", "push_undo", "undo",
              "_undo_groups_snapshot", "zoom_fit_all", "_cmd_zoom_range",
              "_cmd_goto_time", "_cmd_group", "_cmd_ungroup", "_cmd_help",
              "_ensure_cursor_visible", "cursor_delta", "cursor_time",
              "_focus_on"):
        attr = getattr(Alwavelets, m, None)
        if attr is None:
            continue
        if isinstance(attr, property):
            continue
        setattr(s, m, attr.__get__(s))
    # cursor_time / cursor_delta are properties on the real class
    s.cursor_delta = None
    s.cursor_time = 0
    return s


# ── op handlers ──────────────────────────────────────────────────────

def test_ping_and_capabilities(w, tmp_path):
    from alwavelets.control import handle
    v = _viewer(w, tmp_path)
    r = handle(v, {"op": "ping"})
    assert r["ok"] and r["signals"] == len(v.vcd.signal_order)

    caps = handle(v, {"op": "capabilities"})["capabilities"]
    assert {"ping", "state", "render", "command", "key"} <= set(caps["ops"])
    # generated from the live dispatch table, so it can't drift from the UI
    for verb in ("zoom", "goto", "when", "group", "ungroup", "stats", "session"):
        assert verb in caps["commands"], verb


def test_state_schema(w, tmp_path):
    from alwavelets.control import handle
    v = _viewer(w, tmp_path)
    st = handle(v, {"op": "state"})["state"]
    for key in ("trace", "timescale", "max_time", "window", "cursor",
                "focused", "displayed", "selected", "groups", "markers",
                "radix", "message"):
        assert key in st, key
    assert st["window"] == [0, v.vcd.max_time]
    assert st["displayed"] == [v.get_signal(s).full_name for s in v.display_order]


def test_command_runs_and_reports_state(w, tmp_path):
    from alwavelets.control import handle
    v = _viewer(w, tmp_path)
    r = handle(v, {"op": "command", "text": "zoom 100 200"})
    assert r["ok"] and r["state"]["window"] == [100, 200]
    assert r["quit"] is False


def test_command_quit_is_reported_not_swallowed(w, tmp_path):
    from alwavelets.control import handle
    v = _viewer(w, tmp_path)
    assert handle(v, {"op": "command", "text": "quit"})["quit"] is True


def test_grouping_over_the_channel(w, tmp_path):
    from alwavelets.control import handle
    v = _viewer(w, tmp_path)
    v.selected_signals = set(v.display_order[:2])
    r = handle(v, {"op": "command", "text": "group Bus"})
    groups = r["state"]["groups"]
    assert groups and groups[0]["name"] == "Bus"
    # nesting through the same path
    v.selected_signals = {v.display_order[0]}
    r = handle(v, {"op": "command", "text": "group Outer/Bus"})
    g = {x["name"]: x for x in r["state"]["groups"]}
    assert g["Bus"]["parent"] == "Outer"


def test_prompting_command_is_a_no_op_not_a_hang(w, tmp_path):
    """A command that would open a prompt must return with the view intact."""
    from alwavelets.control import handle
    v = _viewer(w, tmp_path)
    before = handle(v, {"op": "state"})["state"]
    r = handle(v, {"op": "command", "text": "goto"})   # no inline arg
    assert r["ok"]
    assert r["state"]["window"] == before["window"]
    assert v._remote_depth == 0                        # depth restored


def test_remote_depth_restored_after_an_exception(w, tmp_path):
    from alwavelets.control import handle
    v = _viewer(w, tmp_path)

    def boom(_):
        raise RuntimeError("kaboom")
    v.execute_command = boom
    with pytest.raises(RuntimeError):
        handle(v, {"op": "command", "text": "zoom 1 2"})
    assert v._remote_depth == 0     # else every later prompt would be dead


def test_render_returns_the_current_view(w, tmp_path):
    from alwavelets.control import handle
    v = _viewer(w, tmp_path)
    r = handle(v, {"op": "render", "width": 80})
    assert r["ok"] and "waveform" in r["text"]
    assert r["window"] == [v.time_start, v.time_end]


def test_render_width_is_clamped(w, tmp_path):
    from alwavelets.control import handle
    v = _viewer(w, tmp_path)
    for width in (1, 100000):
        assert handle(v, {"op": "render", "width": width})["ok"]


def test_key_op_applies_bound_keys_only(w, tmp_path):
    from alwavelets.control import handle
    v = _viewer(w, tmp_path)
    hits = []
    v.keymap = {ord('a'): lambda: hits.append('a'),
                ord('b'): lambda: hits.append('b')}
    r = handle(v, {"op": "key", "keys": "abz"})    # 'z' is unbound
    assert r["applied"] == "ab" and hits == ['a', 'b']


def test_key_op_rejects_bad_input(w, tmp_path):
    from alwavelets.control import handle
    v = _viewer(w, tmp_path)
    assert handle(v, {"op": "key", "keys": ""})["ok"] is False
    assert handle(v, {"op": "key", "keys": "x" * 65})["ok"] is False


def test_unknown_op(w, tmp_path):
    from alwavelets.control import handle
    v = _viewer(w, tmp_path)
    assert handle(v, {"op": "nope"})["ok"] is False


# ── server: auth, permissions, lifecycle ─────────────────────────────

@pytest.fixture
def server(w, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    from alwavelets.control import ControlServer
    v = _viewer(w, tmp_path)
    srv = ControlServer(v, "/tmp/x.vcd")
    assert srv.start()
    yield srv, v
    srv.stop()


def _rpc(srv, payload, pump=True):
    """One request/response against the live socket, pumping the UI thread."""
    import threading
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect(srv.socket_path)
    f = s.makefile("rwb")
    f.write((json.dumps(payload) + "\n").encode())
    f.flush()
    if pump:
        done = threading.Event()

        def drain():
            while not done.is_set():
                srv.pump()
        t = threading.Thread(target=drain, daemon=True)
        t.start()
    try:
        line = f.readline()
    finally:
        if pump:
            done.set()
        s.close()
    return json.loads(line.decode())


def test_rejects_missing_and_wrong_token(server):
    srv, _ = server
    assert _rpc(srv, {"op": "ping"}, pump=False)["ok"] is False
    r = _rpc(srv, {"token": "not-it", "op": "ping"}, pump=False)
    assert r["ok"] is False and "unauthorized" in r["error"]


def test_rejects_unknown_op_before_touching_the_ui(server):
    srv, _ = server
    r = _rpc(srv, {"token": srv.token, "op": "rm -rf"}, pump=False)
    assert r["ok"] is False and "unknown op" in r["error"]


def test_malformed_json_does_not_kill_the_connection(server):
    import threading
    srv, _ = server
    done = threading.Event()
    threading.Thread(target=lambda: [srv.pump() for _ in iter(
        lambda: not done.is_set(), False)], daemon=True).start()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect(srv.socket_path)
    try:
        f = s.makefile("rwb")
        f.write(b"{not json\n")
        f.flush()
        assert json.loads(f.readline().decode())["ok"] is False
        f.write(b'"a bare string"\n')          # valid JSON, wrong shape
        f.flush()
        assert json.loads(f.readline().decode())["ok"] is False
        f.write((json.dumps({"token": srv.token, "op": "ping"}) + "\n").encode())
        f.flush()
        assert json.loads(f.readline().decode())["ok"] is True   # still usable
    finally:
        done.set()
        s.close()


def test_authorized_request_round_trips(server):
    srv, _ = server
    r = _rpc(srv, {"token": srv.token, "op": "state"})
    assert r["ok"] and "window" in r["state"]


def test_socket_and_descriptor_are_user_only(server):
    srv, _ = server
    for path in (srv.socket_path, srv.descriptor_path):
        assert os.stat(path).st_mode & 0o777 == 0o600, path


def test_descriptor_contents_and_discovery(server, monkeypatch):
    from alwavelets.control import list_sessions
    srv, _ = server
    with open(srv.descriptor_path) as f:
        info = json.load(f)
    assert info["token"] == srv.token and info["pid"] == os.getpid()
    found = [s for s in list_sessions() if s["pid"] == os.getpid()]
    assert found and not found[0]["stale"]


def test_stop_removes_socket_and_descriptor(server):
    srv, _ = server
    sock_path, desc_path = srv.socket_path, srv.descriptor_path
    srv.stop()
    assert not os.path.exists(sock_path)
    assert not os.path.exists(desc_path)


def test_stale_sessions_are_pruned(w, tmp_path, monkeypatch):
    from alwavelets import control
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run2"))
    d = control.sessions_dir()
    os.makedirs(d, exist_ok=True)
    dead = os.path.join(d, "999999.json")
    with open(dead, "w") as f:                    # a pid that cannot be alive
        json.dump({"pid": 999999, "socket": os.path.join(d, "999999.sock"),
                   "token": "x", "started": 1}, f)
    assert any(s["stale"] for s in control.list_sessions())
    control.prune_stale_sessions()
    assert not os.path.exists(dead)


def test_pump_is_a_cheap_no_op_when_idle(server):
    srv, _ = server
    assert srv.pump() is False       # runs every frame; must not cost anything


def test_pump_survives_a_handler_exception(server, monkeypatch):
    """A bad request must not take down the curses thread."""
    import queue
    from alwavelets import control
    srv, _ = server
    monkeypatch.setattr(control, "handle",
                        lambda v, r: (_ for _ in ()).throw(RuntimeError("bang")))
    reply = queue.Queue(1)
    srv.requests.put(({"op": "state"}, reply))
    assert srv.pump() is True
    r = reply.get_nowait()
    assert r["ok"] is False and "bang" in r["error"]


# ── the SKILL.md contract must match the code ────────────────────────

def test_skill_md_frontmatter_and_claims(w, tmp_path):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    text = open(os.path.join(root, "SKILL.md")).read()
    assert text.startswith("---\n")
    fm, _, body = text[4:].partition("\n---\n")
    meta = dict(line.split(":", 1) for line in fm.splitlines() if ":" in line)
    assert meta["name"].strip() == "alwavelets-viewer-control"
    assert len(meta["description"].strip()) > 80        # tells an agent when to use it

    from alwavelets.control import READ_OPS, WRITE_OPS
    for op in READ_OPS | WRITE_OPS:                     # every op is documented
        assert f"`{op}`" in body, op
    v = _viewer(w, tmp_path)
    for verb in ("when", "group", "ungroup", "zoom", "goto", "stats",
                 "glitches", "derive", "session"):
        assert verb in v.command_verbs()                # documented => exists


def test_named_keys_match_the_whole_token_not_characters(w, tmp_path):
    """{"keys": "space"} must press Space once — not s,p,a,c,e, which on this
    keymap toggles the panel, register panel, analog, cursor and edge mode."""
    from alwavelets.control import handle
    v = _viewer(w, tmp_path)
    pressed = []
    v.keymap = {32: lambda: pressed.append("SPACE"),
                ord('s'): lambda: pressed.append("s"),
                ord('p'): lambda: pressed.append("p"),
                ord('a'): lambda: pressed.append("a"),
                ord('c'): lambda: pressed.append("c"),
                ord('e'): lambda: pressed.append("e"),
                9: lambda: pressed.append("TAB")}
    handle(v, {"op": "key", "keys": "space"})
    assert pressed == ["SPACE"]
    handle(v, {"op": "key", "keys": "tab"})
    assert pressed == ["SPACE", "TAB"]


def test_multi_character_keys_are_still_per_character(w, tmp_path):
    from alwavelets.control import handle
    v = _viewer(w, tmp_path)
    pressed = []
    v.keymap = {ord(c): (lambda c=c: pressed.append(c)) for c in "ax"}
    r = handle(v, {"op": "key", "keys": "ax"})
    assert pressed == ["a", "x"] and r["applied"] == "ax"


def test_internal_error_carries_a_stale_session_hint(w, tmp_path, monkeypatch):
    """A viewer keeps the code it started with. An agent that reads one
    NameError as a permanent capability gap gives up instead of asking for a
    restart — so an internal crash must say which it is."""
    import queue
    from alwavelets import control
    from alwavelets.control import ControlServer
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    v = _viewer(w, tmp_path)
    srv = ControlServer(v, "/tmp/x.vcd")
    assert srv.start()
    try:
        monkeypatch.setattr(control, "handle", lambda v, r: (_ for _ in ()).throw(
            NameError("name 'fnmatch' is not defined")))
        reply = queue.Queue(1)
        srv.requests.put(({"op": "command", "text": "add x"}, reply))
        srv.pump()
        r = reply.get_nowait()
        assert r["ok"] is False
        assert "fnmatch" in r["error"]
        assert "restart" in r["hint"] and "capabilities" in r["hint"]
        assert r["started"] > 0            # lets an agent judge session age
    finally:
        srv.stop()
