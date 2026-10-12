#!/usr/bin/env python3
"""Generate the deterministic RV32 writeback demo for the `p` register panel."""
import argparse
from pathlib import Path


def addi(rd, rs1, imm):
    return ((imm & 0xfff) << 20) | (rs1 << 15) | (rd << 7) | 0x13


def add(rd, rs1, rs2):
    return (rs2 << 20) | (rs1 << 15) | (rd << 7) | 0x33


def write_demo(path):
    """Emit payloads 50 ns before the rising edge that retires each instruction."""
    slots = []
    pcs = [0x80000000, 0x80001000]

    def retire(hart, bits, value, note):
        slots.append((hart, pcs[hart], bits, (bits >> 7) & 31, value, note))
        pcs[hart] += 4

    # Every register becomes known; each hart has easily recognizable values.
    for rd in range(1, 32):
        for hart in (0, 1):
            value = (hart + 1) * 0x100 + rd
            retire(hart, addi(rd, 0, value), value, 'Initialize x%d' % rd)

    retire(0, addi(10, 0, 5), 5, 'a0 = 5')                     # 6300 ns
    retire(0, addi(11, 0, 7), 7, 'a1 = 7')                     # 6400 ns
    retire(0, add(12, 10, 11), 12, 'a2 = a0 + a1 = 12')        # 6500 ns
    retire(0, addi(10, 10, 1), 6, 'a0 = 6')                    # 6600 ns
    retire(0, addi(10, 10, 1), 7, 'Same instruction, a0 = 7')   # 6700 ns
    slots.append(None)                                        # 6800 ns: bubble
    retire(0, addi(5, 0, -1), 0xffffffff, 't0 = -1')            # 6900 ns
    retire(0, 0x80000337, 0x80000000, 'lui t1, 0x80000')        # 7000 ns
    retire(0, add(7, 5, 6), 0x7fffffff, 't2 = t0 + t1')        # 7100 ns
    retire(0, addi(0, 0, 0), 0, 'nop: no register write')       # 7200 ns
    retire(1, addi(10, 0, 100), 100, 'Hart 1: a0 = 100')       # 7300 ns
    retire(1, addi(11, 0, 200), 200, 'Hart 1: a1 = 200')       # 7400 ns
    retire(1, add(12, 10, 11), 300, 'Hart 1: a2 = 300')        # 7500 ns
    retire(0, add(13, 10, 12), 19, 'Hart 0: a3 = 7 + 12')      # 7600 ns
    retire(1, add(13, 10, 12), 400, 'Hart 1: a3 = 100 + 300')  # 7700 ns
    slots.extend((None, None))

    signals = [('!', 1, 'clk'), ('"', 1, 'reset'), ('#', 1, 'valid_wb'),
               ('$', 1, 'hart_id'), ('%', 32, 'instr_wb'), ('&', 5, 'rd_addr'),
               ("'", 32, 'rd_data'), ('(', 1, 'rd_wen')]
    lines = [
        '$version Alwavelets register panel demo $end',
        '$comment Synthetic RV32 retirements from two harts; not a CPU simulation. $end',
        '$comment Open this file normally (without --demo), then press p. $end',
        '$comment Try ;goto 6500ns, ;goto 7100ns, ;goto 7500ns, ;goto 7600ns. $end',
        '$timescale 1ns $end',
        '$scope module top $end',
    ]
    for code, width, name in signals:
        lines.append('$var wire %d %s %s $end' % (width, code, name))
    # The nested name top.pc.wb lets the PC role be auto-detected too.
    lines.extend(('$scope module pc $end', '$var wire 32 ) wb $end',
                  '$upscope $end', '$upscope $end', '$enddefinitions $end',
                  '#0', '$dumpvars'))

    def value(code, width, number):
        bits = format(number, '0%db' % width)
        lines.append(bits + code if width == 1 else 'b%s %s' % (bits, code))

    for code, width, name in signals:
        value(code, width, int(name == 'reset'))
    value(')', 32, 0)
    lines.append('$end')
    for index, slot in enumerate(slots, 1):
        time = index * 100
        lines.extend(('#%d' % (time - 50), '0!', '0"'))
        if slot is None:
            lines.extend(('$comment Idle cycle: register state is preserved. $end', '0#', '0('))
        else:
            hart, pc, bits, rd, data, note = slot
            lines.extend(('$comment %d ns: hart %d, %s $end' % (time, hart, note), '1#'))
            for code, width, number in (('$', 1, hart), ('%', 32, bits), ('&', 5, rd),
                                        ("'", 32, data), ('(', 1, int(rd != 0)), (')', 32, pc)):
                value(code, width, number)
        lines.extend(('#%d' % time, '1!'))
    lines.extend(('#%d' % (time + 50), '0!', '#%d' % (time + 100), '1!'))
    Path(path).write_text('\n'.join(lines) + '\n', encoding='ascii')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', nargs='?', type=Path,
                        default=Path(__file__).resolve().parents[1] / 'demo_register_panel.vcd')
    args = parser.parse_args()
    write_demo(args.output)
    print('Wrote %s' % args.output)


if __name__ == '__main__':
    main()
