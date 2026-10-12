"""Read-only numeric component lanes sharing their parent's event storage."""
from .core import (VCDSignal, VirtualBitSignal, float_format_spec, ieee754_decode,
                   numeric_value, time_bisect_left, time_bisect_right)
from .custom_radix import CustomFixed


class ComponentSignal(VirtualBitSignal):
    """A bit field or decoded word, evaluated only when its values are read."""
    __slots__ = ('field', 'fmt')

    def __init__(self, parent, field, suffix, size, offset, fmt):
        self.parent, self.field, self.fmt = parent, field, fmt
        self.bit_index = offset
        self.name = f'{parent.name}.{field}'
        self.full_name = f'{parent.scope}.{self.name}' if parent.scope else self.name
        self.identifier = f'{parent.identifier}__{suffix}'
        self.size = size
        self.signal_type = 'real' if field == 'value' else 'wire'
        self.scope = parent.scope
        self.analog_mode = field == 'value'
        self.is_dumped = True

    @property
    def reference(self):
        return self.name

    @property
    def declared_range(self):
        if self.field == 'value':
            return None
        return (self.parent.bit_label(self.bit_index + self.size - 1),
                self.parent.bit_label(self.bit_index))

    bit_label = VCDSignal.bit_label
    numeric_value_at = VCDSignal.numeric_value_at
    # This is an upper bound for level-of-detail decisions. Counting actual
    # field transitions first would scan the whole window before drawing it.
    lod_event_count = VCDSignal.transition_count_in_window

    @property
    def changes(self):
        # Legacy callers can explicitly materialize a list; expansion never
        # does so. The streaming/window/cursor APIs remain lazy.
        return list(self.iter_changes())

    def _extract_bit(self, value):
        value = value.lower()
        if not value or any(c not in '01xz' for c in value):
            return 'x'
        if len(value) > self.parent.size:
            if any(c != '0' for c in value[:-self.parent.size]):
                return 'x'
            value = value[-self.parent.size:]
        # VCD zero-extends short binary words and extends a leading X/Z with
        # that state. Known fields survive unknown bits in a different field.
        pad = value[0] if value[0] in 'xz' else '0'
        value = value.rjust(self.parent.size, pad)
        if self.field == 'value':
            if 'x' in value or 'z' in value:
                return 'x'
            bits = int(value, 2)
            if isinstance(self.fmt, CustomFixed):
                return self.fmt.format(bits)
            return str(ieee754_decode(bits, self.fmt)[0])
        stop = self.parent.size - self.bit_index
        return value[stop - self.size:stop]

    def changes_in_window(self, start, end):
        from .storage import SequenceView
        times = self._times
        if end < start or not times:
            return [], []
        lo = max(0, time_bisect_left(times, start) - 1)
        hi = time_bisect_right(times, end)
        return times[lo:hi], SequenceView(range(lo, hi), transform=self.value_str)

    def find_value(self, target, start_time=0):
        from decimal import Decimal, InvalidOperation
        from .streaming import check_read_cancel

        def matches(value):
            if self.field != 'value':
                return VCDSignal._match_value(value, target)
            if value.lower() == target.strip().lower():
                return True
            try:
                return Decimal(value) == Decimal(target)
            except (InvalidOperation, ValueError):
                return False

        check_read_cancel()
        before = time_bisect_right(self._times, start_time) - 1
        if before >= 0 and matches(self.value_str(before)):
            return start_time
        for i, (tick, value) in enumerate(self.iter_changes(start_time)):
            if i % 256 == 0:
                check_read_cancel()
            if matches(value):
                return tick
        return None

    def numeric_range_in_window(self, start, end):
        from .streaming import iter_window, check_read_cancel
        minimum = maximum = None
        for i, (_, raw) in enumerate(iter_window(self, start, end)):
            if i % 256 == 0:
                check_read_cancel()
            value = numeric_value(raw, self.signal_type)
            if value is not None:
                minimum = value if minimum is None else min(minimum, value)
                maximum = value if maximum is None else max(maximum, value)
        if minimum is None:
            return 0, 1
        return (minimum - 1, maximum + 1) if minimum == maximum else (minimum, maximum)

    def transition_count_in_window(self, start, end):
        from .streaming import iter_window, check_read_cancel
        previous, count = None, 0
        for i, (_, value) in enumerate(iter_window(self, start, end)):
            if i % 256 == 0:
                check_read_cancel()
            if previous is not None and value != previous:
                count += 1
            previous = value
        return count


def make_component_signals(parent, fmt):
    """Return named component views; defining them never reads trace values."""
    if parent.signal_type in ('real', 'realtime'):
        raise ValueError('Components require a binary signal, not a real-valued signal')
    if isinstance(fmt, CustomFixed):
        total = fmt.total_bits
        specs = [('sign', 'qsign', int(fmt.signed), total - 1),
                 ('integer', 'qint', fmt.integer_bits, fmt.fraction_bits),
                 ('fraction', 'qfrac', fmt.fraction_bits, 0),
                 ('value', 'qval', 1, 0)]
    else:
        total, exponent, mantissa, _ = float_format_spec(fmt)
        specs = [('sign', 'fsign', 1, total - 1),
                 ('exp', 'fexp', exponent, mantissa),
                 ('mant', 'fmant', mantissa, 0),
                 ('value', 'fval', 1, 0)]
    if parent.size != total:
        raise ValueError(f'Format needs {total} bits; signal has {parent.size}')
    return [(name, ComponentSignal(parent, name, suffix, size, offset, fmt))
            for name, suffix, size, offset in specs if size]
