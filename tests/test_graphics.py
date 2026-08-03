"""Tests for the pure-Python terminal-graphics layer (PNG encode, sixel,
protocol detection/encoding, and the waveform rasteriser)."""
import base64
import struct
import zlib

DEMO = __import__("os").path.join(
    __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))),
    "demo_waveform.vcd")


def _png_chunks(png):
    """Walk a PNG, verifying CRCs; return (chunk_types, (w, h, bit_depth, color_type))."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    pos, types = 8, []
    while pos < len(png):
        ln = struct.unpack(">I", png[pos:pos + 4])[0]
        typ = png[pos + 4:pos + 8]
        data = png[pos + 8:pos + 8 + ln]
        crc = struct.unpack(">I", png[pos + 8 + ln:pos + 12 + ln])[0]
        assert crc == zlib.crc32(typ + data) & 0xffffffff
        types.append(typ)
        pos += 12 + ln
    w, h, bd, ct = struct.unpack(">IIBB", png[16:26])
    return types, (w, h, bd, ct)


def test_png_encode_valid(w):
    g = w  # the package re-exports graphics
    idx = bytes([0, 1, 2, 1]) * 4         # 4x4 indexed
    png = g.png_encode(4, 4, idx)
    types, (pw, ph, bd, ct) = _png_chunks(png)
    assert types == [b"IHDR", b"PLTE", b"IDAT", b"IEND"]
    assert (pw, ph, bd, ct) == (4, 4, 8, 3)   # 8-bit indexed


def test_render_waveform_png(w):
    vcd = w.VCDFile.parse(DEMO)
    png = w.render_waveform_png(vcd, ["top.clk", "top.state", "top.alu.result"],
                                0, "500ns", width=400, row_h=20)
    types, (pw, ph, _, _) = _png_chunks(png)
    assert (pw, ph) == (400, 3 * 20)


def test_rasterise_has_traces(w):
    from collections import Counter
    vcd = w.VCDFile.parse(DEMO)
    from alwavelets import graphics
    sigs = graphics._resolve_request(vcd, ["top.clk", "top.alu.result"], None, None)[0]
    buf, pw, ph = graphics._rasterise(vcd, sigs, 0, 500, 400, 20, None)
    c = Counter(buf)
    assert c[graphics.BG] > 0                         # background
    assert sum(c[k] for k in (graphics.HIGH, graphics.LOW, graphics.EDGE, graphics.BUS)) > 0


def test_detect_protocol(w):
    assert w.detect_protocol({"KITTY_WINDOW_ID": "1"}) == "kitty"
    assert w.detect_protocol({"TERM_PROGRAM": "iTerm.app"}) == "iterm"
    assert w.detect_protocol({"LC_TERMINAL": "iTerm2"}) == "iterm"
    assert w.detect_protocol({"TERM": "foot"}) == "sixel"
    assert w.detect_protocol({"TERM": "xterm-256color"}) is None
    assert w.detect_protocol({}) is None


def test_kitty_sequence_roundtrip(w):
    png = w.png_encode(2, 2, bytes([0, 1, 2, 0]))
    seq = w.kitty_sequence(png, cols=10, rows=2)
    assert seq.startswith("\x1b_Gf=100,a=T,c=10,r=2,m=") and seq.endswith("\x1b\\")
    b64 = "".join(p.split(";", 1)[1].rsplit("\x1b\\", 1)[0] for p in seq.split("\x1b_G")[1:])
    assert base64.b64decode(b64) == png         # payload reconstructs the PNG


def test_iterm_sequence(w):
    png = w.png_encode(2, 2, bytes([0, 1, 2, 0]))
    seq = w.iterm_sequence(png, cols=10, rows=2)
    assert seq.startswith("\x1b]1337;File=inline=1") and seq.endswith("\x07")
    payload = seq.split(":", 1)[1][:-1]
    assert base64.b64decode(payload) == png


def test_sixel_structure(w):
    seq = w.sixel_encode(8, 8, bytes([0]) * 64)
    assert seq.startswith("\x1bP") and seq.endswith("\x1b\\")
    assert "#0;2;" in seq                        # palette definition present


def test_render_inline(w):
    vcd = w.VCDFile.parse(DEMO)
    proto, payload = w.render_inline(vcd, ["top.clk"], 0, 200, width=200,
                                     protocol="kitty")
    assert proto == "kitty" and payload.startswith("\x1b_G")
    # no protocol -> returns raw PNG bytes for the caller to save
    proto, payload = w.render_inline(vcd, ["top.clk"], 0, 200, protocol="")
    assert proto is None and isinstance(payload, (bytes, bytearray))
    assert payload[:8] == b"\x89PNG\r\n\x1a\n"
