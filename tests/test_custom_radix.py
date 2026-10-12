"""Custom formats: numeric boundaries, isolated settings, and UI persistence."""
import json
import math
import struct
from decimal import Decimal, localcontext

import pytest

from alwavelets.core import (Radix, VCDSignal, format_float_value, format_value,
                             format_typed_value)
from alwavelets.custom_radix import (CustomFixed, CustomFloat, custom_radix_state,
                                     load_custom_radixes)
from alwavelets.snapshot import render_svg, render_text
from alwavelets.ui import Alwavelets
from test_ui_chrome import Screen, viewer as _viewer_fixture

viewer = _viewer_fixture
apply_config = Alwavelets._apply_config


@pytest.mark.parametrize('signed', [False, True])
@pytest.mark.parametrize('fraction', [0, 3, 7])
def test_every_eight_bit_fixed_value(signed, fraction):
    fmt = CustomFixed(8 - int(signed) - fraction, fraction, signed)
    for bits in range(256):
        integer = bits - 256 if signed and bits >= 128 else bits
        expected = Decimal(integer) / (1 << fraction)
        assert Decimal(format_value(format(bits, '08b'), fmt, 8)) == expected


def test_fixed_width_sign_zero_extension_and_exact_wide_values():
    fmt = CustomFixed(3, 4)
    assert format_value('11101000', fmt, 8) == '-1.5'
    assert format_value('1', fmt, 8) == '0.0625'
    assert format_value('10000000', fmt, 8) == '-8'
    assert format_value('0', fmt, 8) == '0'
    assert format_value('1', CustomFixed(0, 0), 1) == '-1'
    assert format_value('1' * 128, CustomFixed(128, 0, False), 128) == str(2 ** 128 - 1)
    with localcontext() as ctx:
        ctx.prec = 200
        tiny = format_value('1', CustomFixed(0, 128, False), 128)
        assert Decimal(tiny) == Decimal(2) ** -128


def test_every_custom_half_pattern_against_binary16():
    fmt = CustomFloat()
    for bits in range(65536):
        expected = struct.unpack('>e', struct.pack('>H', bits))[0]
        actual = fmt.decode(bits)[0]
        if math.isnan(expected):
            assert actual.is_nan()
        else:
            assert float(actual) == expected
            assert actual.is_signed() == (math.copysign(1, expected) < 0)


def test_custom_float_special_policies_and_bias():
    # Independent table for all 16 finite E2M1 patterns, including signed zero.
    fmt = CustomFloat(2, 1, specials='finite')
    magnitudes = [0, 0.5, 1, 1.5, 2, 3, 4, 6]
    for bits in range(16):
        value = fmt.decode(bits)[0]
        assert value == Decimal(magnitudes[bits & 7]) * (-1 if bits & 8 else 1)
        assert value.is_signed() == bool(bits & 8)
    fmt = CustomFloat(4, 3, specials='nan')
    assert fmt.decode(0x7e)[0] == 448
    assert fmt.decode(0xfe)[0] == -448
    assert fmt.decode(0x7f)[0].is_nan() and fmt.decode(0xff)[0].is_nan()
    assert fmt.decode(1)[0] == Decimal(2) ** -9
    assert CustomFloat(4, 3, bias=3).decode(0x18)[0] == 1
    assert CustomFloat(3, 0, specials='finite').decode(0b0111)[0] == 16


def test_float_labels_unknowns_and_values_outside_binary64():
    fmt = CustomFloat(4, 3)
    assert format_value('00111100', fmt, 8) == '1.5 [+|E:7|M:4]'
    assert format_float_value('10000000', fmt, 8) == '-0'
    assert format_float_value('01111000', fmt, 8) == 'inf'
    assert format_float_value('01111001', fmt, 8) == 'NaN'
    huge = CustomFloat(15, 2, specials='finite')
    for bits in ('0' + '1' * 17, '1'):
        label = format_value(bits, huge, 18)
        assert 'inf' not in label.lower() and 'NaN' not in label
        assert 'e' in label.lower()
    for fmt in (CustomFixed(3, 4), CustomFloat(4, 3)):
        assert format_value('1', fmt, 16) == 'width! (need 8b)'
        assert format_value('1' * 9, fmt, 8) == 'width! (need 8b)'
        assert format_value('not-binary', fmt, 8) == 'invalid bits'
        for value in ('x', 'Z', '01xZ'):
            assert format_value(value, fmt, 8) == value.upper()
        real = VCDSignal('real', 'r', 1, signal_type='real')
        assert format_typed_value(real, '1.5', fmt) == '1.5'


@pytest.mark.parametrize('cls,kwargs', [
    (CustomFixed, {'integer_bits': -1}), (CustomFixed, {'fraction_bits': 1025}),
    (CustomFixed, {'integer_bits': 1024}), (CustomFixed, {'signed': 'yes'}),
    (CustomFixed, {'integer_bits': 0, 'fraction_bits': 0, 'signed': False}),
    (CustomFixed, {'integer_bits': True}), (CustomFixed, {'fraction_bits': 1.5}),
    (CustomFloat, {'exponent_bits': 0}), (CustomFloat, {'exponent_bits': 16}),
    (CustomFloat, {'mantissa_bits': -1}), (CustomFloat, {'mantissa_bits': 1024}),
    (CustomFloat, {'bias': 65536}), (CustomFloat, {'bias': True}),
    (CustomFloat, {'specials': 'anything'}),
    (CustomFloat, {'mantissa_bits': 0, 'specials': 'nan'}),
])
def test_invalid_profiles_are_rejected(cls, kwargs):
    with pytest.raises(ValueError):
        cls(**kwargs)


@pytest.fixture
def custom_viewer(viewer, monkeypatch):
    monkeypatch.setattr(viewer, 'apply_theme', lambda *a: None)
    for sid in ('!', 'b'):
        sig = VCDSignal('data' + sid, sid, 8, scope='top')
        sig.add_change(0, '00111100')
        viewer.vcd.signals[sid] = sig
    viewer.vcd.signal_order = ['!', 'b']
    viewer.display_order = ['!', 'b']
    viewer._sync_display_set()
    viewer._rebuild_view_rows()
    return viewer


def test_configure_selection_cycles_and_history(custom_viewer):
    v = custom_viewer
    v.execute_command('fixed 3 4 signed')
    v.execute_command('float 4 3 auto nan')
    v.selected_signals = {'!', 'b'}
    v.radix = Radix.FLOAT64
    v.keymap[ord('r')]()
    assert v.radix == Radix.CUSTOM_FIXED
    assert v.format_signal_value(v.get_signal('!'), '!') == '3.75'
    v.cycle_signal_radix()
    assert v.sig_radix == {'!': Radix.CUSTOM_FLOAT, 'b': Radix.CUSTOM_FLOAT}
    assert v.format_signal_value(v.get_signal('!'), '!').startswith('1.5 [')
    v.execute_command('float 4 3 3 finite')
    assert v.format_signal_value(v.get_signal('!'), '!').startswith('24 [')
    v.undo()
    assert v.custom_radixes['float'] == CustomFloat(4, 3, specials='nan')
    v.redo()
    assert v.custom_radixes['float'] == CustomFloat(4, 3, 3, 'finite')
    # A setting change is reflected in the bus row cache key immediately.
    label = v._render_row('bus', v.get_signal('!'), '!', v.get_signal_radix('!'))[2][0]
    v.undo()
    changed = v._render_row('bus', v.get_signal('!'), '!', v.get_signal_radix('!'))[2][0]
    assert label.startswith('24 [') and changed.startswith('1.5 [')
    v.execute_command('float 4 3 auto nan')  # no-op preserves redo
    assert v._redo_stack
    v.execute_command('float 4 -1')  # invalid input is atomic
    assert v._redo_stack and v.custom_radixes['float'] == CustomFloat(4, 3, specials='nan')


def test_menu_configure_apply_cancel_and_narrow_display(custom_viewer, monkeypatch):
    v = custom_viewer
    prompts = []

    def answer(label, initial=''):
        prompts.append((label, initial))
        return '3 4 signed'

    monkeypatch.setattr(v, 'prompt_input', answer)
    v.selected_signals = {'!', 'b'}
    v.open_signal_menu()
    v.signal_menu_handle_key(10)
    v._signal_menu['index'] = next(i for i, item in enumerate(v.signal_menu_items())
                                  if item[1:] == ('configure', 'fixed'))
    v.signal_menu_handle_key(10)
    assert prompts[0][1] == '7 8 signed'
    assert v._signal_menu and v.custom_radixes['fixed'] == CustomFixed(3, 4)
    for width in (80, 40, 24):
        v.stdscr = Screen(width)
        v.update_layout()
        v.draw_signal_menu()
    v._signal_menu['index'] = next(i for i, item in enumerate(v.signal_menu_items())
                                  if item[2] == Radix.CUSTOM_FIXED)
    v.signal_menu_handle_key(10)
    assert v.sig_radix == {'!': Radix.CUSTOM_FIXED, 'b': Radix.CUSTOM_FIXED}
    v.undo()
    assert not v.sig_radix
    v.redo()
    assert len(v.sig_radix) == 2
    monkeypatch.setattr(v, 'prompt_input', lambda *a, **kw: '')
    before = (dict(v.custom_radixes), list(v._undo_stack))
    v.configure_custom_radix('float')
    assert (v.custom_radixes, v._undo_stack) == before


def test_config_session_arrangement_and_exports(custom_viewer, monkeypatch, tmp_path):
    from alwavelets import config
    v = custom_viewer
    profiles = {'fixed': CustomFixed(3, 4), 'float': CustomFloat(4, 3)}
    settings = {'radix': 'float', 'custom_radixes': custom_radix_state(profiles)}
    monkeypatch.setattr(config, 'load_config', lambda: settings)
    apply_config(v)
    assert v.custom_radixes == profiles
    v.sig_radix = {'b': Radix.CUSTOM_FIXED}
    v.vcd._filename = str(tmp_path / 'custom.vcd')
    v.save_session()
    v.custom_radixes = load_custom_radixes(None)
    v.radix, v.sig_radix = Radix.HEX, {}
    assert v.restore_session()
    assert v.custom_radixes == profiles and v.sig_radix == {'b': Radix.CUSTOM_FIXED}
    path = tmp_path / 'arrangement.json'
    monkeypatch.setattr(v, 'prompt_input', lambda *a: str(path))
    v.save_arrangement()
    v.custom_radixes = load_custom_radixes(None)
    v.radix, v.sig_radix = Radix.HEX, {}
    v.load_arrangement()
    assert v.custom_radixes == profiles and v.sig_radix == {'b': Radix.CUSTOM_FIXED}
    for render in (render_text, render_svg):
        out = render(v.vcd, t_start=0, t_end=100, arrangement=str(path))
        assert '1.5 [+|E:7|M:4]' in out and '3.75' in out
        override = render(v.vcd, t_start=0, t_end=100, arrangement=str(path), radix='fixed')
        assert override.count('3.75') == 2 and '1.5 [' not in override
    state = json.loads(path.read_text())
    assert state['custom_radixes']['float']['exponent_bits'] == 4


def test_bad_optional_settings_keep_valid_slot_and_are_isolated():
    first = load_custom_radixes({'fixed': {'integer_bits': 3, 'fraction_bits': 4},
                                 'float': {'exponent_bits': 999}})
    second = load_custom_radixes(None)
    assert first['float'] == CustomFloat() == second['float']
    assert first['fixed'] == CustomFixed(3, 4) != second['fixed']
    for bad in (None, [], 'bad', {'fixed': {'unknown': 1}}, {'float': {'bias': []}}):
        assert load_custom_radixes(bad) == second


def test_tcl_configuration_and_custom_float_field_expansion(custom_viewer):
    v = custom_viewer
    assert not v.tcl.execute('float 4 3 auto ieee\nradix float')
    v.expand_float_fields()
    assert v.virtual_signals['!__fexp'].size == 4
    assert v.virtual_signals['!__fmant'].size == 3
    assert v.virtual_signals['!__fval'].value_at(0) == '1.5'
    assert v.sig_radix['!'] == Radix.CUSTOM_FLOAT
    assert v._session_state()['sig_radix']['!'] == 'float'
    for field, text in (('sign', '0'), ('exp', '7'), ('mant', '100')):
        sid = '!__f' + field
        assert v.format_signal_value(v.get_signal(sid), sid) == text
    v.execute_command('float 5 10')  # The parent width no longer matches.
    v.expand_float_fields()
    assert '!' not in v.expanded_buses and not v.virtual_signals


def test_float_field_expansion_with_no_mantissa(custom_viewer):
    v = custom_viewer
    sig = VCDSignal('exponent_only', '!', 4, scope='top')
    sig.add_change(0, '0111')
    v.vcd.signals['!'] = sig
    v.execute_command('float 3 0 auto finite')
    v.execute_command('radix float')
    v.expand_float_fields()
    assert '!__fmant' not in v.virtual_signals
    assert v.virtual_signals['!__fval'].value_at(0) == '16'
    assert v.format_signal_value(v.get_signal('!__fexp'), '!__fexp') == '7'
