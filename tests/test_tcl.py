"""Tests for the small TCL-like scripting engine (the parts that don't need a
live viewer): argument tokenizing, variables, and puts output."""


def test_parse_args_quotes_and_braces(w):
    te = w.TclEngine(None)
    assert te._parse_args('cmd "a b" c') == ["cmd", "a b", "c"]
    assert te._parse_args("cmd {d e} f") == ["cmd", "d e", "f"]
    assert te._parse_args("plain one two") == ["plain", "one", "two"]


def test_execute_set_puts_and_substitution(w):
    te = w.TclEngine(None)
    out = te.execute("set x 5\nputs hello\nputs $x")
    assert out == "hello\n5"


def test_execute_ignores_comments_and_blanks(w):
    te = w.TclEngine(None)
    out = te.execute("# a comment\n\nputs done")
    assert out == "done"


def test_marker_command_adds_a_marker_in_ticks_or_units(w):
    """The control channel routes `marker` here; it used to raise NameError
    because Marker was never imported into the engine."""
    class _Vcd:
        timescale_unit, timescale_number = "ps", 1
    class _Viewer:
        vcd = _Vcd()
        markers = []
    te = w.TclEngine(_Viewer())
    te.execute("marker 220000 first bad pop")
    te.execute("marker 260ns")
    assert [(m.time, m.label) for m in te.viewer.markers] == [(220000, "first bad pop"), (260000, "")]
