"""Configuration of the fixed-point and floating-point radix slots."""
from .custom_radix import CustomFixed, CustomFloat
from .ui_editing import remember_entries


class CustomRadixMixin:
    def configure_custom_radix(self, kind, args=''):
        current = self.custom_radixes[kind]
        if not args:
            if kind == 'fixed':
                initial = f'{current.integer_bits} {current.fraction_bits} {"signed" if current.signed else "unsigned"}'
                prompt = 'Fixed I F signed|unsigned: '
            else:
                bias = 'auto' if current.bias is None else current.bias
                initial = f'{current.exponent_bits} {current.mantissa_bits} {bias} {current.specials}'
                prompt = 'Float E M bias ieee|finite|nan: '
            args = self.prompt_input(prompt, initial=initial)
            if not args:
                return
        fields = args.lower().split()
        try:
            if kind == 'fixed':
                if len(fields) not in (2, 3):
                    raise ValueError('Use: fixed <integer bits> <fraction bits> [signed|unsigned]')
                signed = fields[2] if len(fields) == 3 else 'signed'
                if signed not in ('signed', 'unsigned'):
                    raise ValueError('Choose signed or unsigned; integer bits exclude the sign')
                profile = CustomFixed(int(fields[0]), int(fields[1]), signed == 'signed')
            else:
                if len(fields) not in (2, 3, 4):
                    raise ValueError('Use: float <exponent bits> <mantissa bits> [bias|auto] [ieee|finite|nan]')
                bias = None if len(fields) < 3 or fields[2] == 'auto' else int(fields[2])
                profile = CustomFloat(int(fields[0]), int(fields[1]), bias,
                                      fields[3] if len(fields) == 4 else 'ieee')
        except ValueError as exc:
            self._show_message(str(exc))
            return
        if profile != current:
            self.push_undo(f'custom {kind} settings', remember_entries(self.custom_radixes, [kind]))
            self.custom_radixes[kind] = profile
            self._needs_layout = self._dirty = True
        self._show_message(f'Custom {kind}: {profile.description}')
