"""Immutable, viewer-local definitions for the two configurable number formats."""
from dataclasses import asdict, dataclass
from decimal import Decimal, localcontext
from typing import Optional


def _integer(name, value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f'{name} must be an integer from {minimum} to {maximum}')


@dataclass(frozen=True)
class CustomFixed:
    # Integer bits exclude the optional two's-complement sign bit.
    integer_bits: int = 7
    fraction_bits: int = 8
    signed: bool = True

    value = 'fixed'

    def __post_init__(self):
        _integer('Integer bits', self.integer_bits, 0, 1024)
        _integer('Fraction bits', self.fraction_bits, 0, 1024)
        if type(self.signed) is not bool:
            raise ValueError('Signed must be true or false')
        _integer('Total width', self.total_bits, 1, 1024)

    @property
    def total_bits(self):
        return int(self.signed) + self.integer_bits + self.fraction_bits

    @property
    def description(self):
        return f'{"signed" if self.signed else "unsigned"} I{self.integer_bits} F{self.fraction_bits} ({self.total_bits}b)'

    def format(self, bits):
        if self.signed and bits & (1 << (self.total_bits - 1)):
            bits -= 1 << self.total_bits
        # Exact decimal text, including wide fixed-point words; never round
        # through a binary64 float or the process-wide Decimal context.
        whole, fraction = divmod(abs(bits), 1 << self.fraction_bits)
        text = str(whole)
        if fraction:
            digits = str(fraction * 5 ** self.fraction_bits)
            text += '.' + digits.zfill(self.fraction_bits).rstrip('0')
        return ('-' if bits < 0 else '') + text


@dataclass(frozen=True)
class CustomFloat:
    exponent_bits: int = 5
    mantissa_bits: int = 10
    bias: Optional[int] = None  # None means the conventional 2**(E-1)-1 bias.
    specials: str = 'ieee'

    value = 'float'

    def __post_init__(self):
        _integer('Exponent bits', self.exponent_bits, 1, 15)
        _integer('Mantissa bits', self.mantissa_bits, 0, 1022)
        _integer('Total width', self.total_bits, 2, 1024)
        if self.bias is not None:
            _integer('Bias', self.bias, -65535, 65535)
        if self.specials not in ('ieee', 'finite', 'nan'):
            raise ValueError('Special values must be ieee, finite, or nan')
        if self.specials == 'nan' and self.mantissa_bits == 0:
            raise ValueError('The nan policy needs at least one mantissa bit')

    @property
    def total_bits(self):
        return 1 + self.exponent_bits + self.mantissa_bits

    @property
    def exponent_bias(self):
        return (1 << (self.exponent_bits - 1)) - 1 if self.bias is None else self.bias

    @property
    def description(self):
        return f'E{self.exponent_bits} M{self.mantissa_bits} bias={self.exponent_bias} {self.specials} ({self.total_bits}b)'

    def decode(self, bits):
        """IEEE-style fields, with configurable top-exponent interpretation.

        Decimal keeps a finite custom value finite even outside binary64's
        exponent range. Field and zero handling are independent of rounding.
        """
        sign = (bits >> (self.total_bits - 1)) & 1
        exponent = (bits >> self.mantissa_bits) & ((1 << self.exponent_bits) - 1)
        mantissa = bits & ((1 << self.mantissa_bits) - 1)
        real_exponent = exponent - self.exponent_bias
        special = None
        if exponent == (1 << self.exponent_bits) - 1:
            if self.specials == 'ieee':
                special = 'nan' if mantissa else ('-inf' if sign else 'inf')
            elif self.specials == 'nan' and mantissa == (1 << self.mantissa_bits) - 1:
                special = 'nan'
        if special:
            number = Decimal(special)
        elif exponent == 0 and mantissa == 0:
            special = '-0' if sign else '+0'
            number = Decimal(special)
        else:
            if exponent == 0:
                special = 'denorm'
                coefficient = mantissa
                power = 1 - self.exponent_bias - self.mantissa_bits
            else:
                coefficient = (1 << self.mantissa_bits) + mantissa
                power = real_exponent - self.mantissa_bits
            with localcontext() as ctx:
                ctx.prec = max(28, self.mantissa_bits + 2)
                ctx.Emax, ctx.Emin = 999999, -999999
                number = Decimal(-coefficient if sign else coefficient) * Decimal(2) ** power
                number = number.normalize()
        return number, sign, exponent, real_exponent, mantissa, special


DEFAULT_CUSTOM_RADIXES = {'fixed': CustomFixed(), 'float': CustomFloat()}


def load_custom_radixes(data, previous=None):
    """Ignore malformed optional profiles without losing the other slot."""
    profiles = dict(DEFAULT_CUSTOM_RADIXES if previous is None else previous)
    if isinstance(data, dict):
        for key, cls in (('fixed', CustomFixed), ('float', CustomFloat)):
            if isinstance(data.get(key), dict):
                try:
                    profiles[key] = cls(**data[key])
                except (TypeError, ValueError):
                    pass
    return profiles


def custom_radix_state(profiles=None):
    return {key: asdict(value) for key, value in
            (DEFAULT_CUSTOM_RADIXES if profiles is None else profiles).items()}


def resolve_custom_radix(radix, profiles=None):
    if isinstance(radix, (CustomFixed, CustomFloat)):
        return radix
    return (DEFAULT_CUSTOM_RADIXES if profiles is None else profiles).get(radix.value, radix)


def custom_value_error(val, fmt, size):
    if any(c in val for c in 'xXzZ'):
        return val.upper()
    if size != fmt.total_bits:
        return f'width! (need {fmt.total_bits}b)'
    if not val or any(c not in '01' for c in val):
        return 'invalid bits'
    if len(val.lstrip('0')) > size:
        return f'width! (need {fmt.total_bits}b)'
    return None
