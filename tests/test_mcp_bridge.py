"""Tests for the MCP -> live-viewer bridge (the viewer_* tools).

These proxy to control.ControlClient, so an MCP client can drive the session
the user has open without implementing the socket protocol itself. The
integration tests run a real ControlServer over a stub viewer — no curses
terminal needed.
"""
import os
import threading

import pytest


def _mcp():
    from alwavelets import mcp_server
    return mcp_server


# ── session selection ────────────────────────────────────────────────

def _fake_sessions(monkeypatch, *sessions):
    from alwavelets import mcp_server
    monkeypatch.setattr(mcp_server, "_sessions", lambda: list(sessions))


def test_no_viewer_running_explains_how_to_start_one(monkeypatch):
    m = _mcp()
    _fake_sessions(monkeypatch)
    with pytest.raises(RuntimeError) as e:
        m._pick_session()
    assert "--control" in str(e.value)          # actionable, not just "failed"


def test_single_session_is_picked_implicitly(monkeypatch):
    m = _mcp()
    s = {"pid": 1, "trace": "/w/bench.vcd"}
    _fake_sessions(monkeypatch, s)
    assert m._pick_session() is s
    assert m._pick_session(None) is s


def test_several_sessions_refuse_to_guess(monkeypatch):
    """Silently steering one of several open viewers is the wrong guess."""
    m = _mcp()
    _fake_sessions(monkeypatch, {"pid": 1, "trace": "/w/a.vcd"},
                   {"pid": 2, "trace": "/w/b.vcd"})
    with pytest.raises(RuntimeError) as e:
        m._pick_session()
    msg = str(e.value)
    assert "a.vcd" in msg and "b.vcd" in msg    # tells the agent the choices


def test_session_selected_by_pid_or_trace_name(monkeypatch):
    m = _mcp()
    a = {"pid": 11, "trace": "/w/alpha.vcd"}
    b = {"pid": 22, "trace": "/w/beta.vcd"}
    _fake_sessions(monkeypatch, a, b)
    assert m._pick_session("22") is b
    assert m._pick_session("alpha.vcd") is a
    assert m._pick_session("/w/beta.vcd") is b


def test_unmatched_session_hint_lists_what_is_open(monkeypatch):
    m = _mcp()
    _fake_sessions(monkeypatch, {"pid": 1, "trace": "/w/a.vcd"})
    with pytest.raises(RuntimeError) as e:
        m._pick_session("nope.vcd")
    assert "a.vcd" in str(e.value)


def test_list_viewer_sessions_when_none(monkeypatch):
    m = _mcp()
    _fake_sessions(monkeypatch)
    assert "--control" in m._h_viewer_sessions()


# ── state formatting (what the model actually reads) ─────────────────

def test_fmt_state_is_compact_and_complete():
    m = _mcp()
    out = m._fmt_state({
        "trace": "/w/bench.vcd", "window": [0, 100], "max_time": 200,
        "cursor": {"a": 10, "b": 40, "active": "a", "delta": 30},
        "focused": "top.valid", "radix": "hex",
        "displayed": ["top.clk", "top.valid"], "selected": ["top.valid"],
        "groups": [{"name": "HS", "parent": "IF", "collapsed": True,
                    "members": ["top.valid"]}],
        "markers": [{"time": 50, "label": "reset"}],
        "message": "⌖ 10ns: valid",
    })
    for probe in ("/w/bench.vcd", "0 .. 100", "A=10 B=40", "delta=30",
                  "top.valid", "hex", "HS", "IF", "collapsed", "reset @ 50",
                  "⌖ 10ns: valid"):
        assert probe in out, probe
    assert len(out.splitlines()) < 15          # terse: read on every call


def test_fmt_state_tolerates_a_sparse_state():
    m = _mcp()
    out = m._fmt_state({})                      # nothing set yet
    assert "focused:   (none)" in out and "displayed: (none)" in out


# ── tool registration ────────────────────────────────────────────────

def test_viewer_tools_are_registered_and_well_formed():
    m = _mcp()
    by_name = {t["name"]: t for t in m.TOOLS}
    for name in ("list_viewer_sessions", "viewer_state", "viewer_render",
                 "viewer_command", "viewer_key"):
        t = by_name[name]
        assert callable(t["handler"])
        assert t["inputSchema"]["type"] == "object"
        assert len(t["description"]) > 60       # tells the model when to use it
    # the two write tools declare their required argument
    assert by_name["viewer_command"]["inputSchema"]["required"] == ["command"]
    assert by_name["viewer_key"]["inputSchema"]["required"] == ["keys"]


def test_tools_list_exposes_them_over_jsonrpc():
    m = _mcp()
    r = m.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in r["result"]["tools"]}
    assert {"viewer_state", "viewer_command"} <= names
    assert all({"name", "description", "inputSchema"} == set(t)
               for t in r["result"]["tools"])   # no handler leaks to the client


def test_viewer_command_description_warns_against_quit():
    """Sending 'quit' would close the user's viewer — the model must be told."""
    m = _mcp()
    d = next(t for t in m.TOOLS if t["name"] == "viewer_command")["description"]
    assert "quit" in d.lower()


def test_missing_required_arguments_are_reported_not_raised():
    m = _mcp()
    assert "required" in m._h_viewer_command(command=None)
    assert "required" in m._h_viewer_key(keys=None)


# ── integration: real socket, real control server ────────────────────

@pytest.fixture
def live(w, tmp_path, monkeypatch):
    """A ControlServer over a stub viewer, with a pump thread, discoverable
    through the same sessions dir the MCP bridge looks in."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    import sys
    sys.path.insert(0, "tests")
    from test_control import _viewer
    from alwavelets.control import ControlServer

    v = _viewer(w, tmp_path)
    srv = ControlServer(v, str(tmp_path / "t.vcd"))
    assert srv.start()
    stop = threading.Event()
    threading.Thread(target=lambda: [srv.pump() for _ in
                                     iter(lambda: not stop.is_set(), False)],
                     daemon=True).start()
    yield srv, v
    stop.set()
    srv.stop()


def test_bridge_reads_state_from_a_real_session(live):
    m = _mcp()
    out = m._h_viewer_state()
    assert "trace:" in out and "window:" in out


def test_bridge_lists_the_real_session(live):
    m = _mcp()
    srv, _ = live
    assert str(srv.viewer.vcd.max_time) or True
    assert "pid" in m._h_viewer_sessions()


def test_bridge_runs_a_command_and_reports_new_state(live):
    m = _mcp()
    out = m._h_viewer_command(command="zoom 100 200")
    assert "window:    100 .. 200" in out


def test_bridge_renders_the_current_view(live):
    m = _mcp()
    out = m._h_viewer_render(width=80)
    assert "waveform" in out


def test_bridge_reports_a_viewer_side_failure(live, monkeypatch):
    """A refusal from the viewer must surface as an error, not a silent pass."""
    m = _mcp()
    from alwavelets import control
    monkeypatch.setattr(control, "handle",
                        lambda v, r: {"ok": False, "error": "nope"})
    with pytest.raises(RuntimeError) as e:
        m._h_viewer_state()
    assert "nope" in str(e.value)


def test_bridge_surfaces_errors_as_tool_errors_over_jsonrpc(live, monkeypatch):
    """tools/call wraps handler exceptions as isError content the model reads."""
    m = _mcp()
    _fake_sessions(monkeypatch)                 # pretend nothing is running
    r = m.handle_message({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                          "params": {"name": "viewer_state", "arguments": {}}})
    assert r["result"]["isError"] is True
    assert "--control" in r["result"]["content"][0]["text"]


def test_bridge_key_op_round_trips(live):
    m = _mcp()
    srv, v = live
    hits = []
    v.keymap = {ord('a'): lambda: hits.append('a')}
    out = m._h_viewer_key(keys="a")
    assert "applied: a" in out and hits == ['a']


# ── the radix trap: a literal read off a hex screen ──────────────────

def test_viewer_command_description_warns_about_radix_literals():
    """A naive '== 62' against a hex view matches nothing, and reporting
    'that never happens' from a mistyped literal sounds like an answer."""
    m = _mcp()
    d = next(t for t in m.TOOLS if t["name"] == "viewer_command")["description"]
    assert "0x62" in d and "radix" in d


def test_hex_vs_decimal_literals_select_different_times(w):
    """The trap is real, not theoretical: the same digits mean two things."""
    import alwavelets
    from alwavelets.expr import find_all_expression
    vcd = alwavelets.parse_file(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "demo_waveform.vcd"))
    hexes = find_all_expression(vcd, "top.data_out == 0x62", 0, None, limit=5)
    decs = find_all_expression(vcd, "top.data_out == 62", 0, None, limit=5)
    assert hexes and not decs          # 0x62 hits; bare 62 silently finds nothing


def test_combined_handshake_and_bus_value_expression(w):
    """The exact shape a user asks for in words."""
    import alwavelets
    from alwavelets.expr import find_all_expression
    vcd = alwavelets.parse_file(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "demo_waveform.vcd"))
    hits = find_all_expression(
        vcd, "(top.valid && top.ready) && top.data_out == 0x62", 0, None, limit=5)
    assert len(hits) == 1
