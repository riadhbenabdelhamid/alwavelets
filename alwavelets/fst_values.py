"""Bounded FST bus batches, using the existing two-bit logic page encoding."""
from .core import _B4Q, numeric_value


# Each hexadecimal digit represents four binary bits. Translating its ASCII
# code to four two-bit logic digits expands a binary FST payload entirely in C.
_BINARY_QUADS = bytearray(256)
for _digit, _char in enumerate(b'0123456789abcdef'):
    _BINARY_QUADS[_char] = sum(((_digit >> bit) & 1) << (bit * 2) for bit in range(4))
_BINARY_QUADS = bytes(_BINARY_QUADS)
_LOGIC_DIGITS = bytes.maketrans(b'xz', b'23')


class LogicBatch:
    """Packed bus values and extrema; uncommon logic states retain their text.

    Widths need not be byte-aligned. FST binary bytes are left-aligned, whereas
    stored two-bit digits are right-aligned within each value's stride. The
    public iterator still yields full-width strings for ordinary consumers.
    """
    def __init__(self, width):
        self.width, self.stride = width, (width + 3) // 4
        self.padding = (-width) % 8
        self.data = bytearray()
        self.strings = None
        self.minimum = self.maximum = None

    def __len__(self):
        return len(self.strings) if self.strings is not None else len(self.data) // self.stride

    def __iter__(self):
        if self.strings is not None:
            yield from self.strings
        else:
            for offset in range(0, len(self.data), self.stride):
                yield ''.join(map(_B4Q.__getitem__, self.data[offset:offset + self.stride]))[-self.width:]

    def _number(self, value):
        if value is not None:
            if self.minimum is None or value < self.minimum:
                self.minimum = value
            if self.maximum is None or value > self.maximum:
                self.maximum = value

    def append_binary(self, raw):
        number = int.from_bytes(raw, 'big') >> self.padding
        self._number(number)
        if self.strings is not None:
            self.strings.append(format(number, '0%db' % self.width))
            return
        packed = raw.hex().encode('ascii').translate(_BINARY_QUADS)
        if self.padding:
            packed = (int.from_bytes(packed, 'big') >> (self.padding * 2)).to_bytes(self.stride, 'big')
        self.data.extend(packed)

    def append_text(self, raw, *, allow_fallback=True):
        """Return False, without mutation, if a new text fallback is disallowed."""
        if self.strings is None and not raw.translate(None, b'01xz'):
            self.data.extend(int(raw.translate(_LOGIC_DIGITS), 4).to_bytes(self.stride, 'big'))
            # Unknown/high-impedance values have no numeric extrema.
            if b'x' not in raw and b'z' not in raw:
                self._number(int(raw, 2))
        else:
            if self.strings is None:
                if not allow_fallback:
                    return False
                self.strings = list(self)
                self.data.clear()
            value = raw.decode('ascii', 'replace')
            self.strings.append(value)
            self._number(numeric_value(value))
        return True
