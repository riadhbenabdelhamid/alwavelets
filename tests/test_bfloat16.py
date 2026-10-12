"""BF16 decoding and every public route for choosing the radix."""
import math
import struct

import pytest

from alwavelets.core import (Radix, VCDSignal, format_float_value, format_value,
                             ieee754_decode, make_float_field_signals)
from alwavelets.snapshot import render_svg, render_text
from alwavelets.ui import Alwavelets
from test_ui_chrome import viewer as _viewer_fixture

viewer = _viewer_fixture
apply_config = Alwavelets._apply_config  # The headless fixture disables startup config.


def test_every_bfloat16_pattern_matches_expansion_to_binary32():
    # Independent oracle: BF16 expands exactly to binary32 by adding 16 low
    # zero bits. Include subnormals and both signs of zero, infinity and NaN.
    for bits in range(65536):
        expected = struct.unpack('>f', struct.pack('>I', bits << 16))[0]
        actual, sign, _, _, _, special = ieee754_decode(bits, Radix.BFLOAT16)
        if math.isnan(expected):
            assert math.isnan(actual) and special == 'nan'
        else:
            assert actual == expected, hex(bits)
            assert math.copysign(1, actual) == math.copysign(1, expected), hex(bits)
        assert sign == bits >> 15


@pytest.mark.parametrize('bits,text', [
    (0x3F80, '1'), (0x3FC0, '1.5'), (0xC000, '-2'),
    (0x0000, '+0'), (0x8000, '-0'), (0x7F80, 'inf'),
    (0xFF80, '-inf'), (0x7FC0, 'NaN'), (0x0001, '9.1835e-41'),
])
def test_bfloat16_labels(bits, text):
    binary = format(bits, '016b')
    assert format_float_value(binary, Radix.BFLOAT16, 16) == text
    label = '9.184e-41' if bits == 0x0001 else text
    assert format_value(binary, Radix.BFLOAT16, 16).startswith(label + ' [')


def test_bfloat16_fields_and_unknown_values():
    assert format_value('0011111111000000', Radix.BFLOAT16, 16) == '1.5 [+|E:7F|M:40]'
    for value in ('x', 'z', '10xx01zz'):
        assert format_value(value, Radix.BFLOAT16, 16) == value.upper()
    parent = VCDSignal('weight', '!', 16, scope='top')
    for t, bits in enumerate((0x3FC0, 0xC000, 0x8000)):
        parent.add_change(t, format(bits, '016b'))
    parent.add_change(3, 'x')
    fields = dict(make_float_field_signals(parent, Radix.BFLOAT16))
    assert [fields[name].size for name in ('sign', 'exp', 'mant')] == [1, 8, 7]
    assert fields['exp'].value_at(0) == '01111111'
    assert fields['mant'].value_at(0) == '1000000'
    assert fields['value'].value_at(0) == '1.5'
    assert fields['value'].value_at(1) == '-2.0'
    assert fields['value'].value_at(2) == '-0.0'
    assert all('x' in sig.value_at(3) for sig in fields.values())


@pytest.fixture
def bf_viewer(viewer, monkeypatch):
    monkeypatch.setattr(viewer, 'apply_theme', lambda *a: None)
    for sid in ('!', 'b'):
        sig = VCDSignal('weight' + sid, sid, 16, scope='top')
        sig.add_change(0, format(0x3FC0, '016b'))
        viewer.vcd.signals[sid] = sig
    viewer.vcd.signal_order = ['!', 'b']
    viewer.display_order = ['!', 'b']
    viewer._sync_display_set()
    viewer._rebuild_view_rows()
    return viewer


def test_bfloat16_cycles_menu_selection_undo_and_redo(bf_viewer):
    v = bf_viewer
    v.radix = Radix.FLOAT16
    v.keymap[ord('r')]()
    assert v.radix == Radix.BFLOAT16
    v.selected_signals = {'!', 'b'}
    v.sig_radix['!'] = Radix.FLOAT16
    v.keymap[ord('R')]()
    assert v.sig_radix == {'!': Radix.BFLOAT16, 'b': Radix.BFLOAT16}
    v.undo()
    assert v.sig_radix == {'!': Radix.FLOAT16}
    v.redo()
    assert v.sig_radix == {'!': Radix.BFLOAT16, 'b': Radix.BFLOAT16}
    v.sig_radix.clear()
    v.open_signal_menu()
    v.signal_menu_handle_key(10)  # Number format
    v._signal_menu['index'] = next(i for i, item in enumerate(v.signal_menu_items())
                                  if item[2] == Radix.BFLOAT16)
    assert 'BF16' in v.signal_menu_items()[v._signal_menu['index']][0]
    v.signal_menu_handle_key(10)
    assert v.sig_radix == {'!': Radix.BFLOAT16, 'b': Radix.BFLOAT16}
    assert v.format_signal_value(v.get_signal('!'), '!') == '1.5 [+|E:7F|M:40]'


@pytest.mark.parametrize('global_radix', [False, True])
def test_field_expansion_honors_bfloat16(bf_viewer, global_radix):
    v = bf_viewer
    if global_radix:
        v.radix = Radix.BFLOAT16
    else:
        v.sig_radix['!'] = Radix.BFLOAT16
    v.expand_float_fields()
    assert v.virtual_signals['!__fexp'].size == 8
    assert v.virtual_signals['!__fmant'].size == 7
    assert v.virtual_signals['!__fval'].value_at(0) == '1.5'


def test_bfloat16_commands_config_and_session(bf_viewer, monkeypatch, tmp_path):
    from alwavelets import config
    v = bf_viewer
    monkeypatch.setattr(config, 'load_config', lambda: {'radix': 'bf16'})
    apply_config(v)
    assert v.radix == Radix.BFLOAT16
    v.radix = Radix.HEX
    v.execute_command('radix BF16')
    assert v.radix == Radix.BFLOAT16
    v.radix = Radix.HEX
    v.tcl.execute('radix bf16')
    assert v.radix == Radix.BFLOAT16
    v.sig_radix = {'b': Radix.BFLOAT16}
    v.vcd._filename = str(tmp_path / 'weights.vcd')
    v.save_session()
    v.radix, v.sig_radix = Radix.HEX, {}
    assert v.restore_session()
    assert v.radix == Radix.BFLOAT16 and v.sig_radix == {'b': Radix.BFLOAT16}


def test_bfloat16_text_and_svg_exports(bf_viewer):
    trace = bf_viewer.vcd
    text = render_text(trace, ['top.weight!'], 0, 100, width=100, radix='bf16')
    svg = render_svg(trace, ['top.weight!'], 0, 100, radix='bf16')
    assert '1.5 [+|E:7F|M:40]' in text
    assert '1.5 [+|E:7F|M:40]' in svg
