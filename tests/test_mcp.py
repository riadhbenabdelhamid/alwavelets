"""Tests for the MCP server's JSON-RPC message handling and tools."""
import os

DEMO = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "demo_waveform.vcd")


def _mcp(w):
    from alwavelets import mcp_server
    return mcp_server


def test_initialize_negotiates_supported_version(w):
    m = _mcp(w)
    r = m.handle_message({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                          "params": {"protocolVersion": "2025-06-18"}})
    assert r["id"] == 0
    assert r["result"]["protocolVersion"] == m.PROTOCOL_VERSION
    assert r["result"]["serverInfo"]["name"] == "alwavelets"
    assert "tools" in r["result"]["capabilities"]


def test_initialized_notification_no_response(w):
    m = _mcp(w)
    assert m.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_find_when_tool(w):
    m = _mcp(w)
    r = m.handle_message({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                          "params": {"name": "find_when",
                                     "arguments": {"file": DEMO, "expr": "clk == 1"}}})
    assert r["result"]["isError"] is False
    text = r["result"]["content"][0]["text"]
    assert "becomes true" in text and "t=" in text
    # a bad expression surfaces as a (non-protocol) tool message, not a crash
    r2 = m.handle_message({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                           "params": {"name": "find_when",
                                      "arguments": {"file": DEMO, "expr": "clk &&"}}})
    assert "error" in r2["result"]["content"][0]["text"].lower()


def test_find_glitches_tool(w):
    m = _mcp(w)
    r = m.handle_message({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                          "params": {"name": "find_glitches",
                                     "arguments": {"file": DEMO, "max_pulse": 0}}})
    assert r["result"]["isError"] is False
    assert isinstance(r["result"]["content"][0]["text"], str)


def test_tools_list(w):
    m = _mcp(w)
    r = m.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {t["name"]: t for t in r["result"]["tools"]}
    # file tools (headless); the live-viewer proxies are in test_mcp_bridge.py
    assert {"render_waveform", "render_waveform_svg", "render_waveform_png",
            "measure", "list_signals", "find_when", "find_glitches"} <= set(tools)
    assert {n for n in tools if not n.startswith(("viewer_", "list_viewer"))} == {
        "render_waveform", "render_waveform_svg", "render_waveform_png",
        "measure", "list_signals", "find_when", "find_glitches"}
    # each tool advertises a JSON-Schema input with required fields
    rw = tools["render_waveform"]["inputSchema"]
    assert rw["type"] == "object"
    assert "file" in rw["required"]
    assert "signals" in rw["properties"]


def test_tools_call_render(w):
    m = _mcp(w)
    r = m.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                          "params": {"name": "render_waveform",
                                     "arguments": {"file": DEMO,
                                                   "signals": ["top.clk", "top.state"],
                                                   "start": 0, "end": "300ns", "width": 80}}})
    assert r["result"]["isError"] is False
    text = r["result"]["content"][0]["text"]
    assert "top.clk" in text and "top.state" in text


def test_tools_list_has_measure_and_optional_signals(w):
    m = _mcp(w)
    r = m.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {t["name"]: t for t in r["result"]["tools"]}
    assert "measure" in tools
    # signals is optional now (an arrangement can supply it)
    assert tools["render_waveform"]["inputSchema"]["required"] == ["file"]
    assert "theme" in tools["render_waveform_svg"]["inputSchema"]["properties"]


def test_tools_call_svg_returns_image_content(w):
    import base64
    import xml.etree.ElementTree as ET
    m = _mcp(w)
    r = m.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                          "params": {"name": "render_waveform_svg",
                                     "arguments": {"file": DEMO, "signals": ["top.clk"],
                                                   "start": 0, "end": "400ns", "theme": "light"}}})
    content = r["result"]["content"]
    assert content[0]["type"] == "image" and content[0]["mimeType"] == "image/svg+xml"
    ET.fromstring(base64.b64decode(content[0]["data"]))   # decodes to valid SVG


def test_tools_call_measure(w):
    m = _mcp(w)
    r = m.handle_message({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                          "params": {"name": "measure",
                                     "arguments": {"file": DEMO,
                                                   "signals": ["top.clk", "top.valid"],
                                                   "end": "2us"}}})
    assert r["result"]["isError"] is False
    assert "freq=" in r["result"]["content"][0]["text"]


def test_tools_call_list_signals(w):
    m = _mcp(w)
    r = m.handle_message({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                          "params": {"name": "list_signals",
                                     "arguments": {"file": DEMO, "pattern": "top.alu.*"}}})
    assert r["result"]["isError"] is False
    assert "top.alu.result" in r["result"]["content"][0]["text"]


def test_tools_call_missing_file_is_tool_error(w):
    m = _mcp(w)
    r = m.handle_message({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                          "params": {"name": "render_waveform",
                                     "arguments": {"file": "/no/such.vcd", "signals": ["x"]}}})
    # a runtime failure is reported as a tool error, not a protocol error
    assert r["result"]["isError"] is True
    assert "not found" in r["result"]["content"][0]["text"]


def test_unknown_tool_is_protocol_error(w):
    m = _mcp(w)
    r = m.handle_message({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                          "params": {"name": "bogus", "arguments": {}}})
    assert "error" in r and r["error"]["code"] == -32602


def test_unknown_method_is_protocol_error(w):
    m = _mcp(w)
    r = m.handle_message({"jsonrpc": "2.0", "id": 6, "method": "foo/bar"})
    assert r["error"]["code"] == -32601


def test_serve_loop_roundtrip(w):
    """The stdio serve loop reads newline-delimited JSON and writes responses."""
    import io
    import json
    m = _mcp(w)
    stdin = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n")
    stdout = io.StringIO()
    m.serve(stdin=stdin, stdout=stdout)
    resp = json.loads(stdout.getvalue().strip())
    assert resp["id"] == 1
    names = {t["name"] for t in resp["result"]["tools"]}
    assert "render_waveform" in names and "viewer_command" in names


# ── REPL demo helper ─────────────────────────────────────────────────

def test_demo_helper_prints_and_returns_a_trace(w, tmp_path, capsys):
    """alwavelets.demo() is the one-call REPL entry point."""
    import alwavelets
    path = str(tmp_path / "d.vcd")
    vcd = alwavelets.demo(path=path, width=60)
    out = capsys.readouterr().out
    assert "waveform" in out and "top.clk" in out
    assert vcd.signal_order and vcd.max_time > 0
    # the trace is cached, so a second call doesn't regenerate it
    mtime = os.path.getmtime(path)
    alwavelets.demo(path=path, width=60)
    assert os.path.getmtime(path) == mtime


def test_demo_helper_accepts_a_signal_selection(w, tmp_path, capsys):
    import alwavelets
    alwavelets.demo(path=str(tmp_path / "d.vcd"), signals=["top.clk"],
                    start="0ns", end="100ns", width=50)
    out = capsys.readouterr().out
    assert "top.clk" in out and "top.data_out" not in out
