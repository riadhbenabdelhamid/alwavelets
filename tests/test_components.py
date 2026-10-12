"""Numeric field views stay correct without copying the parent's history."""
import array
import threading

import pytest

from alwavelets import rendering, storage
from alwavelets.background import BackgroundQueries
from alwavelets.components import ComponentSignal, make_component_signals
from alwavelets.core import Radix, VCDSignal, VirtualBitSignal, make_float_field_signals
from alwavelets.custom_radix import CustomFixed, CustomFloat
from alwavelets.streaming import OperationCancelled, read_cancellation
from test_custom_radix import custom_viewer as _custom_viewer_fixture
from test_ui_chrome import Screen, viewer as _viewer_fixture
from test_writer_metadata import writer_for

viewer = _viewer_fixture
custom_viewer = _custom_viewer_fixture


def signal(width, values):
    sig = VCDSignal('data', '!', width, scope='top')
    for i, value in enumerate(values):
        sig.add_change(i * 10, value)
    return sig


def test_fixed_fields_are_raw_twos_complement_with_exact_decoded_value():
    parent = signal(8, ['11101000', '00011000', '1', '1110xxxx', 'z'])
    fields = dict(make_component_signals(parent, CustomFixed(3, 4)))
    assert list(fields) == ['sign', 'integer', 'fraction', 'value']
    expected = [('1', '110', '1000', '-1.5'), ('0', '001', '1000', '1.5'),
                ('0', '000', '0001', '0.0625'), ('1', '110', 'xxxx', 'x'),
                ('z', 'zzz', 'zzzz', 'x')]
    for tick, values in enumerate(expected):
        assert tuple(s.value_at(tick * 10) for s in fields.values()) == values
    assert fields['integer'].declared_range == (6, 4)
    assert fields['value'].numeric_range_in_window(0, 40) == (-1.5, 1.5)


@pytest.mark.parametrize('fmt,word,names,value', [
    (CustomFixed(4, 4, False), '11101000', ['integer', 'fraction', 'value'], '14.5'),
    (CustomFixed(0, 7), '10000001', ['sign', 'fraction', 'value'], '-0.9921875'),
    (CustomFixed(7, 0), '11111110', ['sign', 'integer', 'value'], '-2'),
    (CustomFixed(0, 0), '1', ['sign', 'value'], '-1'),
    (CustomFloat(3, 0, specials='finite'), '0111', ['sign', 'exp', 'value'], '16'),
])
def test_zero_width_fields_are_omitted(fmt, word, names, value):
    fields = dict(make_component_signals(signal(len(word), [word]), fmt))
    assert list(fields) == names
    assert fields['value'].value_at(0) == value


@pytest.mark.parametrize('fmt,width,word,exponent,mantissa', [
    (Radix.FLOAT16, 16, 0xbe00, '01111', '1' + '0' * 9),
    (Radix.BFLOAT16, 16, 0xbfc0, '01111111', '1' + '0' * 6),
    (Radix.FLOAT32, 32, 0xbfc00000, '01111111', '1' + '0' * 22),
    (Radix.FLOAT64, 64, 0xbff8000000000000, '01111111111', '1' + '0' * 51),
])
def test_builtin_float_fields(fmt, width, word, exponent, mantissa):
    fields = dict(make_float_field_signals(signal(width, [format(word, f'0{width}b')]), fmt))
    assert [s.value_at(0) for s in fields.values()] == ['1', exponent, mantissa, '-1.5']


def test_unknown_bits_only_affect_their_own_fields():
    fields = dict(make_component_signals(signal(8, ['00111xzz', 'x', 'z01']), CustomFloat(4, 3)))
    assert [s.value_at(0) for s in fields.values()] == ['0', '0111', 'xzz', 'x']
    assert [s.value_at(10) for s in fields.values()] == ['x', 'xxxx', 'xxx', 'x']
    assert [s.value_at(20) for s in fields.values()] == ['z', 'zzzz', 'z01', 'x']


def test_creation_reads_no_history_and_views_follow_appended_events(monkeypatch):
    parent = signal(8, ['00111000'])

    def unexpected(*args):
        raise AssertionError('Expansion must not read the trace history')

    with monkeypatch.context() as patch:
        for name in ('value_at', 'value_str', 'iter_changes', 'changes_in_window', 'num_changes'):
            patch.setattr(VCDSignal, name, unexpected)
        patch.setattr(VCDSignal, 'changes', property(unexpected))
        fields = dict(make_component_signals(parent, CustomFloat(4, 3)))
    assert all(s._times is parent._times for s in fields.values())
    parent.add_change(10, '01000000')
    assert fields['value'].value_at(10) == '2'
    assert fields['exp'].num_changes() == 2
    assert list(fields['exp'].iter_changes()) == [(0, '0111'), (10, '1000')]


def test_navigation_windows_numeric_range_and_cancellation():
    parent = signal(8, ['00111000', '00111001', '01000000', '01000001', '00111000'])
    exponent = dict(make_component_signals(parent, CustomFloat(4, 3)))['exp']
    assert exponent.next_edge_after(0) == 20
    assert exponent.prev_edge_before(40) == 20
    assert exponent.values_at([40, 15, -1]) == ['0111', '0111', 'xxxx']
    assert exponent.transition_count_in_window(5, 35) == 1
    assert exponent.numeric_range_in_window(0, 40) == (7, 8)
    times, values = exponent.changes_in_window(15, 30)
    assert list(zip(times, values)) == [(10, '0111'), (20, '1000'), (30, '1000')]
    cancel = threading.Event()
    with pytest.raises(OperationCancelled), read_cancellation(cancel):
        cancel.set()
        exponent.numeric_range_in_window(0, 40)


@pytest.mark.parametrize('fmt', [CustomFixed(3, 4), CustomFloat(4, 3, specials='nan')])
def test_disk_and_eager_rendering_agree_with_bounded_page_reads(tmp_path, monkeypatch, fmt):
    monkeypatch.setattr(storage, 'SHORT_EVENTS', 0)
    trace, writer, eager = writer_for(tmp_path, count=1, width=8)
    words = [format((i * 13) % 256, '08b') for i in range(4096)]
    words[100:103] = ['00111xzz', 'xxxxxxxx', 'zzzzzzzz']
    try:
        writer.add_batch('0', array.array('q', range(len(words))), words)
        writer.publish(True)
        for i, word in enumerate(words):
            eager['0'].add_change(i, word)
        before = writer.store.page_reads
        disk_fields = dict(make_component_signals(trace.signals['0'], fmt))
        assert writer.store.page_reads == before
        eager_fields = dict(make_component_signals(eager['0'], fmt))
        for name, disk in disk_fields.items():
            memory = eager_fields[name]
            assert disk.values_at([0, 100, 2000, 4095]) == memory.values_at([0, 100, 2000, 4095])
            if name == 'value':
                continue
            with monkeypatch.context() as patch:
                def unexpected(*args):
                    raise AssertionError('Dense field rendering must not materialize its window')
                patch.setattr(ComponentSignal, 'changes_in_window', unexpected)
                draw = rendering.draw_single_bit_culled if disk.size == 1 else rendering.draw_bus_culled
                args = () if disk.size == 1 else (Radix.BIN,)
                assert draw(disk, 0, 4095, 32, *args) == draw(memory, 0, 4095, 32, *args)
                # A bit of a field needs both projections applied to sampled raw words.
                if disk.size > 1:
                    draw_bit = rendering.draw_single_bit_subcell
                    assert draw_bit(VirtualBitSignal(disk, 0), 0, 4095, 32) == draw_bit(
                        VirtualBitSignal(memory, 0), 0, 4095, 32)
        assert writer.store.page_reads - before < 80
    finally:
        trace.close()


def test_menu_expansion_and_recursive_collapse_remove_child_state(custom_viewer):
    v = custom_viewer
    v.custom_radixes['fixed'] = CustomFixed(3, 4)
    v.sig_radix['!'] = Radix.CUSTOM_FIXED
    v.open_signal_menu()
    v._signal_menu['index'] = next(i for i, item in enumerate(v.signal_menu_items()) if item[1] == 'components')
    v._run_signal_menu_item()
    children = list(v.expanded_buses['!'])
    assert list(v.display_order) == ['!'] + children + ['b']
    assert [v.format_signal_value(v.get_signal(sid), sid) for sid in children] == ['0', '3', '1100', '3.75']
    v.expand_bus(children[1])
    descendants = set(v.virtual_signals)
    v.selected_signals.update(descendants)
    v.sig_color[children[0]] = 2
    v.sig_height[children[-1]] = 3
    v.expand_float_fields()
    assert not v.virtual_signals and not v.expanded_buses
    assert not v.selected_signals and not v.analog_signals
    assert not v.sig_color and not v.sig_height
    assert v.sig_radix == {'!': Radix.CUSTOM_FIXED}
    assert list(v.display_order) == ['!', 'b']
    assert set(v._display_order_set) == {'!', 'b'}
    v.execute_command('expand-fields')
    assert v.expanded_buses['!'] == children


def test_one_bit_fixed_menu_and_mismatched_width(custom_viewer):
    v = custom_viewer
    v.vcd.signals['!'] = signal(1, ['1'])
    v.custom_radixes['fixed'] = CustomFixed(0, 0)
    v.sig_radix['!'] = Radix.CUSTOM_FIXED
    v.open_signal_menu()
    assert any(action == 'components' for _, action, _ in v.signal_menu_items())
    v.expand_format_fields()
    assert len(v.expanded_buses['!']) == 2
    v.collapse_bus('!')
    v.sig_radix['!'] = Radix.FLOAT64
    v.expand_format_fields()
    assert not v.virtual_signals
    assert 'needs 64 bits' in v._message


def test_reexpansion_invalidates_results_for_reused_ids(custom_viewer):
    v = custom_viewer
    v.custom_radixes['float'] = CustomFloat(4, 3)
    v.sig_radix['!'] = Radix.CUSTOM_FLOAT
    v.expand_format_fields()
    v._queries = BackgroundQueries()
    try:
        for group in ('rows', 'values', 'inspector'):
            v._queries.ready[(group, 'old-window', '!__fexp')] = 'stale'
        v._row_cache['old'] = 'stale'
        v.expand_format_fields()
        assert not v._row_cache and not v._queries.ready
        v.custom_radixes['float'] = CustomFloat(3, 4)
        v.expand_format_fields()
        assert v.get_signal('!__fexp').size == 3
        assert v.get_signal('!__fmant').size == 4
    finally:
        v._queries.close()


@pytest.mark.parametrize('mode', ['classic', 'calm', 'minimal'])
@pytest.mark.parametrize('width', [40, 120])
def test_component_rows_render_after_resize(custom_viewer, mode, width):
    v = custom_viewer
    v.custom_radixes['fixed'] = CustomFixed(3, 4)
    v.sig_radix['!'] = Radix.CUSTOM_FIXED
    v.expand_format_fields()
    v.workmode = mode
    v.panel_mode = False
    v.stdscr = Screen(width)
    v.update_layout()
    v.draw_header()
    v.draw_ruler()
    v.draw_signals()
    v.draw_status_bar()


def test_session_restore_discards_old_component_views(custom_viewer, tmp_path):
    v = custom_viewer
    v.vcd._filename = str(tmp_path / 'components.vcd')
    v.custom_radixes['fixed'] = CustomFixed(3, 4)
    v.sig_radix['!'] = Radix.CUSTOM_FIXED
    v.save_session()
    v.expand_format_fields()
    assert v.restore_session()
    assert not v.virtual_signals and not v.expanded_buses
    assert list(v.display_order) == ['!', 'b']


def test_component_ids_cannot_shadow_trace_signals(custom_viewer):
    v = custom_viewer
    v.custom_radixes['float'] = CustomFloat(4, 3)
    v.sig_radix['!'] = Radix.CUSTOM_FLOAT
    existing = signal(8, ['00000001'])
    existing.identifier = '!__fexp'
    v.vcd.signals[existing.identifier] = existing
    v.expand_format_fields()
    assert v.get_signal('!__fexp') is existing
    exponent = v.get_signal('!__fexp__1')
    assert exponent.size == 4 and exponent.value_at(0) == '0111'
    v.collapse_bus('!')
    assert v.get_signal('!__fexp') is existing


@pytest.mark.parametrize('declared,expected', [((15, 8), (14, 12)), ((8, 15), (9, 11))])
def test_components_preserve_declared_bit_labels(declared, expected):
    parent = VCDSignal('data', '!', 8, declared_range=declared)
    integer = dict(make_component_signals(parent, CustomFixed(3, 4)))['integer']
    assert integer.declared_range == expected
    assert VirtualBitSignal(integer, 2).name == parent.name + '.integer[' + str(expected[0]) + ']'


def test_find_values_on_fields_accepts_numeric_input():
    fields = dict(make_component_signals(signal(8, ['00111000', '01000000']), CustomFloat(4, 3)))
    assert fields['exp'].find_value('7', 5) == 5
    assert fields['exp'].find_value('0x8') == 10
    assert fields['exp'].find_value('0b1000') == 10
    assert fields['value'].find_value('2.00') == 10
    integer = dict(make_component_signals(signal(8, ['00001010']), CustomFixed(8, 0, False)))
    assert integer['value'].find_value('2') is None  # Decimal 10 must not be read as binary 10.
    assert integer['value'].find_value('10.0') == 0


def test_reloading_discards_views_of_the_old_trace(custom_viewer, tmp_path):
    v = custom_viewer
    v.custom_radixes['fixed'] = CustomFixed(3, 4)
    v.sig_radix['!'] = Radix.CUSTOM_FIXED
    v.expand_format_fields()
    old_value = v.get_signal('!__qval')
    path = tmp_path / 'reload.vcd'
    path.write_text('$scope module top $end\n$var wire 8 ! new $end\n'
                    '$upscope $end\n$enddefinitions $end\n#0\nb11101000 !\n#100\n')
    v.live_file = str(path)
    try:
        assert v._reload_preserving_view()
        assert not v.virtual_signals and not v.expanded_buses
        assert old_value.parent is not v.get_signal('!')
        v.expand_format_fields('!')
        assert v.get_signal('!__qval').value_at(0) == '-1.5'
    finally:
        v.vcd.close()
