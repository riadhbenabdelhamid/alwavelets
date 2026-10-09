"""Behavioral regressions from the codebase review: all readers must describe
one trace, and analysis/view persistence must retain signal and time meaning.
"""
import io
import json
from fractions import Fraction

import pytest

from alwavelets import core, expr
from alwavelets.analysis import compare_signal_ranges, find_anomalies, measure_signal
from alwavelets.parsers import VCDFile

HEADER = ('$timescale 1ns $end\n$var wire 1 ! flag $end\n'
          '$var wire 8 " data $end\n$var real 1 # voltage $end\n'
          '$enddefinitions $end\n')
BODY = ('$dumpvars\n1!\nb1 "\nr10 #\n$end\n'
        '#20\n0!\n1!\n0!\n'
        '$comment\n#999\n1!\nb111 "\n$end\n'
        '#40\n$dumpoff\nx!\nbx "\n$end\n'
        '#60\n$dumpon\n1!\nb101 "\nr11 #\n$end\n#100\n')


@pytest.fixture
def trace(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    p = tmp_path / 'trace.vcd'
    p.write_text(HEADER + BODY)
    return p


def lazy_trace(path):
    vcd, offset = VCDFile.parse_header(str(path))
    vcd._INDEX_STRIDE = 20       # force boundaries through the small fixture
    vcd._build_time_index(offset)
    assert vcd._load_error is None
    return vcd


def assert_values_equal(a, b, start, end):
    for sid in a.signal_order:
        for t in range(start, end + 1):
            assert a.signals[sid].value_at(t) == b.signals[sid].value_at(t), (sid, t)


def test_lazy_cold_windows_preserve_baselines_events_and_blackouts(trace):
    full = VCDFile.parse(str(trace))
    lazy = lazy_trace(trace)
    for start, end in [(45, 55), (10, 25), (65, 80), (0, 12), (20, 70), (0, 100)]:
        lazy.ensure_loaded(start, end)
        assert_values_equal(full, lazy, start, end)
        for sid in full.signal_order:
            expected = [(t, v) for t, v in full.signals[sid].changes if start <= t <= end]
            actual = [(t, v) for t, v in lazy.signals[sid].changes if start <= t <= end]
            assert actual == expected
    for sid in full.signal_order:
        assert lazy.signals[sid].changes == full.signals[sid].changes
    assert find_anomalies([lazy.signals['!']], 0, 100, 0) == find_anomalies([full.signals['!']], 0, 100, 0)
    assert lazy.max_time == 100            # comment timestamps are ignored


def test_lazy_initial_values_without_timestamp(trace):
    trace.write_text(HEADER + '$dumpvars\n1!\nb10 "\nr10 #\n$end\n')
    lazy = lazy_trace(trace)
    lazy.ensure_loaded(0, 1)
    assert_values_equal(VCDFile.parse(str(trace)), lazy, 0, 1)


def test_lazy_index_handles_never_dumped_signals_and_indented_directives(trace):
    trace.write_text('$var wire 1 ! flag $end\n$var wire 1 ? never $end\n'
                     '$var wire 8 " data $end\n$enddefinitions $end\n'
                     '  #0\n  0!\n\tb1 "\n  $comment\n#999\n1?\n$end\n'
                     '#10\n1!\n#20\n0!\n#30\n')
    full, lazy = VCDFile.parse(str(trace)), lazy_trace(trace)
    lazy.ensure_loaded(12, 29)
    assert_values_equal(full, lazy, 12, 29)
    assert lazy.max_time == 30
    assert b'?' not in lazy._first_changes


def test_storage_fast_paths_match_shared_decoder(trace):
    from alwavelets.value_stream import ValueState
    trace.write_text(HEADER + '#0\nX!\nB1 !\nb "\nr10 #\n'
                     '  #10\n\t0!\n\tb101 "\nR11 #\n'
                     '$comment\n#999\n1!\nb11 "\n$end\n'
                     '#20\n$dumpoff $end\n1!\n#30\n$dumpon $end\nbX !\nBz "\n')
    full = VCDFile.parse(str(trace))
    reference, offset = VCDFile.parse_header(str(trace))
    state = ValueState()
    with trace.open('rb') as f:
        f.seek(offset)
        for line in f:
            event = state.feed(line)
            if event is None:
                continue
            bid, value = event
            signals = reference.signals.values() if bid is None else [reference.signals[bid.decode()]]
            for sig in signals:
                sig.add_change(state.time, value[-1:].lower() if sig._scalar else value)
    for sid in reference.signal_order:
        assert full.signals[sid].changes == reference.signals[sid].changes


def test_lazy_analysis_reads_beyond_the_current_view(trace):
    vcd = lazy_trace(trace)
    vcd.ensure_loaded(0, 10)
    assert expr.find_expression(vcd, 'data == 5') == 60
    derived = expr.make_derived_signal(vcd, 'active', 'data == 5')
    assert derived.value_at(70) == '1'
    assert expr.find_expression(vcd, '1', 101) is None


def test_glitch_command_loads_offscreen_events(w, trace, tmp_path):
    from test_control import _viewer
    from alwavelets.ui_commands import CommandLineMixin
    v = _viewer(w, tmp_path)
    v.vcd = lazy_trace(trace)
    v.vcd.ensure_loaded(0, 10)
    v.display_order = ['!']
    CommandLineMixin._cmd_glitches(v, '0')
    assert v.cursor_time == 20
    assert 'zero-width' in v._message


def test_analysis_does_not_claim_complete_results_while_loading(trace):
    vcd, _ = VCDFile.parse_header(str(trace))
    with pytest.raises(expr.ExprError, match='loading'):
        expr.find_expression(vcd, 'flag')


def test_parallel_preserves_dump_and_comment_state_at_splits(trace):
    from alwavelets.parallel import _worker, _split_points
    from alwavelets.value_stream import states_at_offsets
    vcd, offset = VCDFile.parse_header(str(trace))
    full = VCDFile.parse(str(trace))
    ranges = _split_points(str(trace), offset, trace.stat().st_size, 8)
    meta = [(sid.encode(), s._scalar) for sid, s in vcd.signals.items()]
    states = states_at_offsets(str(trace), offset, [a for a, _ in ranges])
    import array
    events = {sid: [] for sid in vcd.signals}
    for a, b in ranges:
        _, result = _worker((str(trace), a, b, meta, states[a]))
        for sid, (tb, vb, scalar) in result.items():
            times = array.array('q'); times.frombytes(tb)
            values = list(vb.decode()) if scalar else vb.decode().split('\n')
            events[sid].extend(zip(times, values))
    for sid, changes in events.items():
        target = vcd.signals[sid]
        for t, value in changes:
            target.add_change(t, value)
        assert target.changes == full.signals[sid].changes


@pytest.mark.parametrize('cached', [False, True])
def test_follow_resumes_dump_state_and_handles_split_comments(trace, cached):
    from alwavelets import cache
    trace.write_text(HEADER + '#0\n1!\nb1 "\nr10 #\n#10\n$dumpoff $end\n')
    vcd = VCDFile.parse(str(trace))
    if cached:
        assert cache.save_cache(vcd, str(trace))
        vcd = cache.load_cache(str(trace))
    vcd.start_follow()
    for text in ['#20\n0!\nb11 "\n', '$comment\n#900\n1!\n',
                 '$end\n#30\n$dumpon $end\n0!\n#40\n$dumpoff $end\n#50\n']:
        with trace.open('a') as f:
            f.write(text)
        vcd.tail()
    full = VCDFile.parse(str(trace))
    assert vcd.max_time == full.max_time == 50
    for sid in full.signal_order:
        assert vcd.signals[sid].changes == full.signals[sid].changes


def test_follow_keeps_appends_between_parse_and_enable(trace):
    vcd = VCDFile.parse(str(trace))
    with trace.open('a') as f:
        f.write('#110\n0!\n')
    vcd.start_follow()
    assert vcd.tail() == 1
    assert vcd.signals['!'].value_at(110) == '0'


def test_declared_ranges_survive_cache_and_bit_expansion(trace):
    from alwavelets import cache
    from alwavelets.snapshot import resolve_signals
    trace.write_text('$var wire 1 ! data [0] $end\n$var wire 1 " data [1] $end\n'
                     '$var wire 4 # upper [7:4] $end\n$var wire 4 $ asc [0:3] $end\n'
                     '$enddefinitions $end\n#0\n0!\n1"\nb1001 #\nb1001 $\n#10\n')
    vcd = VCDFile.parse(str(trace))
    assert [s.full_name for s in resolve_signals(vcd, ['*'])] == ['data[0]', 'data[1]', 'upper[7:4]', 'asc[0:3]']
    assert vcd.signals['#'].make_expanded_bit(0).name == 'upper[4]'
    assert expr.find_expression(vcd, 'upper[7:4] == 9 && data[1]') == 0
    assert core.VirtualBitSignal(vcd.signals['$'], 3).name == 'asc[0]'
    assert cache.save_cache(vcd, str(trace))
    restored = cache.load_cache(str(trace))
    for sid in vcd.signal_order:
        assert restored.signals[sid].identity() == vcd.signals[sid].identity()
        assert restored.signals[sid].changes == vcd.signals[sid].changes


@pytest.mark.parametrize('spelling, expected', [('10', 10), ('11', 11), ('100', 100), ('1.5', 1.5), ('1e1', 10)])
def test_real_values_keep_their_type_across_analysis_and_rendering(trace, spelling, expected):
    from alwavelets.snapshot import render_svg
    trace.write_text(HEADER + '#0\nr%s #\n#100\n' % spelling)
    vcd = VCDFile.parse(str(trace)); sig = vcd.signals['#']
    assert sig.numeric_value_at(5) == expected
    assert measure_signal(sig, 0, 100)['max'] == expected
    assert expr.find_expression(vcd, 'voltage == %s' % expected) == 0
    assert core.format_typed_value(sig, sig.value_at(5), core.Radix.HEX) == str(expected)
    assert '>%s</text>' % expected in render_svg(vcd, ['voltage'])


def test_uart_contiguous_frames_and_start_at_window_boundary():
    from alwavelets.decoders import UARTDecoder
    sig = core.VCDSignal('rx', '!')
    for t, value in [(0, '1'), (10, '0'), (20, '1'), (110, '0'), (120, '1')]:
        sig.add_change(t, value)
    frames = UARTDecoder(sig, 10).decode(10, 220)
    assert [(f.t_start, f.label) for f in frames] == [(10, '0xFF'), (110, '0xFF')]


def test_real_blackouts_remain_searchable_as_unknown(trace):
    vcd = VCDFile.parse(str(trace))
    assert expr.find_expression(vcd, "voltage == 'x'") == 40


def test_diff_uses_common_time_units_and_trace_extent():
    a, b = core.VCDSignal('flag', '!'), core.VCDSignal('flag', '!')
    for t, value in [(0, '0'), (10, '1')]:
        a.add_change(t, value)
        b.add_change(t * 1000, value)
    scaled = core.TimeScaledSignal(b, Fraction(1, 1000))
    assert compare_signal_ranges(a, scaled, 0, 100) == []
    b = core.VCDSignal('flag', '!'); b.add_change(0, '0')
    assert compare_signal_ranges(a, b, 0, 100) == [(10, 100)]
    # Sub-tick differences must survive normalization, too.
    b.add_change(105, '1')
    scaled = core.TimeScaledSignal(b, Fraction(1, 10))
    assert compare_signal_ranges(a, scaled, 0, 100) == [(10, Fraction(21, 2))]


def test_diff_loader_normalizes_displayed_signal_and_prefers_full_name(trace, tmp_path):
    from types import SimpleNamespace
    from alwavelets.ui import Alwavelets
    trace.write_text('$timescale 1ns $end\n$scope module top $end\n'
                     '$var wire 1 ! flag $end\n$upscope $end\n$enddefinitions $end\n'
                     '#0\n0!\n#10\n1!\n#100\n')
    other = tmp_path / 'other.vcd'
    other.write_text('$timescale 1ps $end\n$scope module unrelated $end\n'
                     '$var wire 1 " flag $end\n$upscope $end\n$scope module top $end\n'
                     '$var wire 1 ! flag $end\n$upscope $end\n$enddefinitions $end\n'
                     '#0\n0!\n1"\n#10000\n1!\n#100000\n')
    v = SimpleNamespace(vcd=VCDFile.parse(str(trace)), prompt_input=lambda _: str(other),
                        _show_message=lambda _: None)
    v._compute_diff_ranges = Alwavelets._compute_diff_ranges.__get__(v)
    Alwavelets.load_diff_file(v)
    assert v.diff_ranges == {}
    assert v.diff_sig_map['!'].full_name == 'top.flag'
    assert list(v.diff_sig_map['!']._times) == [0, 10]


def test_measurement_frequency_uses_requested_window():
    from alwavelets.snapshot import measure
    sig = core.VCDSignal('clk', '!')
    for t in range(0, 100, 5):
        sig.add_change(t, str((t // 5) % 2))
    for t in range(100, 301, 10):
        sig.add_change(t, str(((t - 100) // 10) % 2))
    vcd = VCDFile(); vcd.signals['!'] = sig; vcd.signal_order = ['!']; vcd.max_time = 300
    assert 'period=20ns freq=50MHz' in measure(vcd, ['clk'], 100, 300)


def test_session_remaps_regenerated_ids_and_reports_missing_signals(w, tmp_path):
    from test_session import _viewer
    s, path = _viewer(w, tmp_path)
    s.display_order = ['!']; s.sig_color = {'!': 4}; s.save_session()
    from pathlib import Path
    p = Path(path)
    p.write_text('$var wire 1 ! reset $end\n$var wire 1 % clk $end\n'
                 '$scope module top $end\n$var wire 1 & clk $end\n$upscope $end\n'
                 '$enddefinitions $end\n#0\n0!\n1%\n1&\n#100\n')
    s.vcd = VCDFile.parse(path)
    assert s.restore_session()
    assert s.display_order == ['&']              # exact hierarchy, not reused id/name
    assert s.sig_color == {'&': 4}
    p.write_text(p.read_text().replace('1 & clk', '1 & other'))
    s.vcd = VCDFile.parse(path)
    assert s.restore_session()
    assert s.display_order == []
    assert 'unresolved' in s.messages[-1]


def test_session_defers_time_clamping_until_loading_finishes(w, tmp_path, monkeypatch):
    from test_session import _viewer
    from alwavelets.persistence import apply_session_times
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    s, path = _viewer(w, tmp_path)
    s.time_start, s.time_end, s.cursor_a = 100, 200, 150
    s.markers = [core.Marker(160, 'saved')]
    s.save_session()
    s.vcd, off = VCDFile.parse_header(path)
    assert s.restore_session()
    assert s._pending_session_times['view'] == [100, 200]
    s.vcd._parse_values_background(off)
    assert apply_session_times(s, s._pending_session_times)
    assert (s.time_start, s.time_end, s.cursor_a) == (100, 200, 150)
    assert s.markers[0].time == 160


def test_viewer_render_keeps_virtual_rows_and_radixes(w, tmp_path):
    from test_control import _viewer
    from alwavelets.control import _op_render
    v = _viewer(w, tmp_path)
    derived = expr.make_derived_signal(v.vcd, 'derived_bus', 'data + 1')
    v.virtual_signals[derived.identifier] = derived
    v.display_order = [derived.identifier]
    v.sig_radix[derived.identifier] = core.Radix.UNSIGNED
    v.time_start, v.time_end = 100, 105
    text = _op_render(v, {})['text']
    assert 'derived.derived_bus' in text and '11' in text
    assert '0x' not in text and 'no signals matched' not in text


def test_session_selection_rejects_ambiguity_and_prioritizes_exact_matches(monkeypatch):
    from alwavelets import mcp_server as m
    a = {'pid': 12, 'trace': '/a/top.vcd'}; b = {'pid': 13, 'trace': '/b/top.vcd'}
    monkeypatch.setattr(m, '_sessions', lambda: [a, b])
    with pytest.raises(RuntimeError, match='ambiguous'):
        m._pick_session('top.vcd')
    assert m._pick_session('/b/top.vcd') is b
    assert m._pick_session('13') is b


def test_mcp_invalid_arguments_are_protocol_errors():
    from alwavelets.mcp_server import handle_message
    response = handle_message({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                               'params': {'name': 'list_signals', 'arguments': []}})
    assert response['error']['code'] == -32602


@pytest.mark.parametrize('bad', ['[]', 'null', '1', '{', '{"jsonrpc":"2.0","id":1,"method":"ping","params":[]}'])
def test_mcp_survives_invalid_messages(bad):
    from alwavelets.mcp_server import serve
    stdin = io.StringIO(bad + '\n' + json.dumps({'jsonrpc': '2.0', 'id': 2, 'method': 'ping'}) + '\n')
    out = io.StringIO(); serve(stdin, out)
    responses = list(map(json.loads, out.getvalue().splitlines()))
    assert 'error' in responses[0]
    assert responses[1] == {'jsonrpc': '2.0', 'id': 2, 'result': {}}


def test_mcp_never_claims_an_unsupported_protocol():
    from alwavelets.mcp_server import handle_message, PROTOCOL_VERSION
    response = handle_message({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                               'params': {'protocolVersion': '1900-01-01'}})
    assert response['result']['protocolVersion'] == PROTOCOL_VERSION
