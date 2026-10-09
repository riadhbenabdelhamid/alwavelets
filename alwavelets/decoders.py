"""Decoders.

Stateless protocol decoders (SPI / I²C / UART), the RISC-V instruction
decoder + trace index, and the bus-transaction decoders (AXI / Wishbone /
AHB). Everything operates on duck-typed signal objects; only the standard
library is used beyond intra-module references.
"""
import re
from .core import ensure_trace_loaded


# ══════════════════════════════════════════════════════════════
#  PROTOCOL DECODERS
# ══════════════════════════════════════════════════════════════

class DecodedFrame:
    """A decoded protocol frame/transaction."""
    __slots__ = ('t_start', 't_end', 'label', 'data', 'color')
    def __init__(self, t_start, t_end, label, data="", color=0):
        self.t_start = t_start
        self.t_end = t_end
        self.label = label
        self.data = data
        self.color = color


def find_signal(vcd, name):
    """Signal by full name, then by bare name. Built once per trace (an
    index cached on the VCDFile, refreshed if its signal count changes) —
    every decoder used to rescan all signals for each role it resolved."""
    if not name:
        return None
    idx = getattr(vcd, '_name_index', None)
    if idx is None or idx[0] != len(vcd.signals):
        by_full, by_name = {}, {}
        for sig in vcd.signals.values():
            by_full.setdefault(sig.full_name, sig)
            by_name.setdefault(sig.name, sig)
        idx = (len(vcd.signals), by_full, by_name)
        try:
            vcd._name_index = idx
        except AttributeError:
            pass
    return idx[1].get(name) or idx[2].get(name)


def rising_edges(sig, t_start, t_end):
    """Times in [t_start, t_end] where `sig` goes to '1' from anything else."""
    times, values = sig.changes_in_window(t_start, t_end)
    out, prev = [], None
    for t, v in zip(times, values):
        if t >= t_start and v == '1' and prev is not None and prev != '1':
            out.append(t)
        prev = v
    return out


def _to_int(v, default=0):
    if not v or 'x' in v.lower() or 'z' in v.lower():
        return default
    try:
        return int(v, 2)
    except ValueError:
        return default


class SPIDecoder:
    """Decode SPI transactions from CLK, MOSI, MISO, CS signals."""
    def __init__(self, clk_sig, mosi_sig, miso_sig=None, cs_sig=None, cpol=0, cpha=0):
        self.clk = clk_sig
        self.mosi = mosi_sig
        self.miso = miso_sig
        self.cs = cs_sig
        self.cpol = cpol
        self.cpha = cpha

    def decode(self, t_start, t_end):
        frames = []
        if self.cs:
            regions = self._find_active_regions(self.cs, t_start, t_end, '0')
        else:
            regions = [(t_start, t_end)]
        # Data is sampled on the clock's first edge in mode 0 (CPOL0/CPHA0)
        # and mode 3 (CPOL1/CPHA1) — the rising edge — and on the second
        # edge, the falling one, in modes 1 and 2.
        rising = (self.cpol == 0) == (self.cpha == 0)
        for rs, r_end in regions:
            edges = self._find_edges(self.clk, rs, r_end, rising=rising)
            if not edges:
                continue
            mosi_v = miso_v = 0
            nbits = 0
            frame_start = edges[0]
            for t in edges:
                mosi_v = (mosi_v << 1) | (1 if self.mosi.value_at(t) == '1' else 0)
                if self.miso is not None:
                    miso_v = (miso_v << 1) | (1 if self.miso.value_at(t) == '1' else 0)
                nbits += 1
                if nbits == 8:
                    data = f"miso=0x{miso_v:02X}" if self.miso is not None else ""
                    frames.append(DecodedFrame(frame_start, t, f"0x{mosi_v:02X}", data, 6))
                    mosi_v = miso_v = 0
                    nbits = 0
                    frame_start = t
            if nbits:                                  # residual bits
                frames.append(DecodedFrame(frame_start, edges[-1],
                                           f"{mosi_v:0{nbits}b}", "", 6))
        return frames

    def _find_active_regions(self, sig, t_start, t_end, active_val):
        regions = []
        times, values = sig.changes_in_window(t_start, t_end)
        in_active = (sig.value_at(t_start) == active_val)
        start = t_start if in_active else None
        for t, v in zip(times, values):
            if t < t_start:
                in_active = (v == active_val)
                start = t_start if in_active else None
                continue
            if v == active_val and not in_active:
                start = t
                in_active = True
            elif v != active_val and in_active:
                regions.append((start, t))
                in_active = False
        if in_active and start is not None:
            regions.append((start, t_end))
        return regions

    def _find_edges(self, sig, t_start, t_end, rising=True):
        """Times where `sig` becomes '1' (rising) or '0' (falling)."""
        edges = []
        times, values = sig.changes_in_window(t_start, t_end)
        target = '1' if rising else '0'
        prev = None
        for t, v in zip(times, values):
            if t >= t_start and v == target and prev is not None and prev != target:
                edges.append(t)
            prev = v
        return edges


class I2CDecoder:
    """Decode I2C from SDA and SCL.

    START (SDA falls while SCL is high) and STOP (SDA rises while SCL high)
    frame the transfer; every byte is 8 bits sampled on SCL rising edges
    followed by a 9th clock carrying ACK (SDA low) or NAK. The first byte
    after START is the 7-bit address plus the R/W bit. Without a START the
    decoder still packs bits from the first clock it sees, so a capture
    that begins mid-transfer yields bytes rather than nothing.
    """
    def __init__(self, sda_sig, scl_sig):
        self.sda = sda_sig
        self.scl = scl_sig

    def decode(self, t_start, t_end):
        frames = []
        events = []                      # (time, order, kind): kind S/P/C
        for t in rising_edges(self.scl, t_start, t_end):
            events.append((t, 1, 'C'))
        sda_t, sda_v = self.sda.changes_in_window(t_start, t_end)
        prev = None
        for t, v in zip(sda_t, sda_v):
            if t >= t_start and prev is not None and self.scl.value_at(t) == '1':
                if prev == '1' and v == '0':
                    events.append((t, 0, 'S'))
                elif prev == '0' and v == '1':
                    events.append((t, 0, 'P'))
            prev = v
        events.sort()

        bits = []
        byte_start = None
        after_start = False
        awaiting_ack = None              # the byte frame whose ACK clock is next
        for t, _, kind in events:
            if kind == 'S':
                frames.append(DecodedFrame(t, t, 'S', 'START', 5))
                bits, awaiting_ack, after_start = [], None, True
            elif kind == 'P':
                frames.append(DecodedFrame(t, t, 'P', 'STOP', 5))
                bits, awaiting_ack, after_start = [], None, False
            else:
                bit = 1 if self.sda.value_at(t) == '1' else 0
                if awaiting_ack is not None:          # 9th clock
                    ack = 'ACK' if bit == 0 else 'NAK'
                    awaiting_ack.data = f"{awaiting_ack.data} {ack}".strip()
                    awaiting_ack.t_end = t
                    awaiting_ack = None
                    continue
                if not bits:
                    byte_start = t
                bits.append(bit)
                if len(bits) == 8:
                    val = 0
                    for b in bits:
                        val = (val << 1) | b
                    data = ""
                    if after_start:
                        data = f"addr 0x{val >> 1:02X} {'R' if val & 1 else 'W'}"
                        after_start = False
                    awaiting_ack = DecodedFrame(byte_start, t, f"0x{val:02X}", data, 6)
                    frames.append(awaiting_ack)
                    bits = []
        return frames


class UARTDecoder:
    """Decode asynchronous serial (8N1 by default) from one data line.

    A start bit must still be low at its centre (a glitch is not a start
    bit), data bits are sampled at their centres LSB first, the parity bit
    is checked when `parity` is 'even'/'odd', and every stop bit must be
    high — otherwise the frame is reported as FRAME ERR and the search
    resumes right after the false start instead of swallowing the next
    real frame.
    """
    def __init__(self, data_sig, baud_period, data_bits=8, parity=None, stop_bits=1):
        self.data = data_sig
        self.baud_period = baud_period  # in timescale ticks
        self.data_bits = data_bits
        self.parity = parity  # None, 'even', 'odd'
        self.stop_bits = max(1, int(stop_bits))

    def decode(self, t_start, t_end):
        frames = []
        baud = int(self.baud_period)
        if baud <= 0:
            return frames
        half = baud // 2
        n_par = 1 if self.parity else 0
        frame_len = (1 + self.data_bits + n_par + self.stop_bits) * baud
        t = t_start - 1
        while t < t_end:
            nxt = self.data.next_edge_after(t)
            if nxt is None or nxt >= t_end:
                break
            if self.data.value_at(nxt) != '0' or self.data.value_at(nxt + half) != '0':
                t = nxt
                continue
            val = ones = 0
            sample = nxt + baud + half               # centre of data bit 0
            for i in range(self.data_bits):
                if self.data.value_at(sample) == '1':
                    val |= 1 << i
                    ones += 1
                sample += baud
            errs = []
            if self.parity:
                pbit = self.data.value_at(sample) == '1'
                sample += baud
                expect = (ones % 2 == 1) if self.parity == 'even' else (ones % 2 == 0)
                if pbit != expect:
                    errs.append('PARITY')
            stop_ok = all(self.data.value_at(sample + k * baud) == '1'
                          for k in range(self.stop_bits))
            if not stop_ok:
                frames.append(DecodedFrame(nxt, min(nxt + baud, t_end), 'FRAME ERR', '', 1))
                t = nxt
                continue
            if 32 <= val < 127:
                label = f"'{chr(val)}' 0x{val:02X}"
            else:
                label = f"0x{val:02X}"
            frame_end = nxt + frame_len
            frames.append(DecodedFrame(nxt, min(frame_end, t_end), label, ' '.join(errs), 6))
            # The next start bit may coincide with the end of this stop bit.
            t = frame_end - 1
        return frames


# ══════════════════════════════════════════════════════════════
#  STAGE 2: RISC-V INSTRUCTION DECODER
# ══════════════════════════════════════════════════════════════

class RV32Decoder:
    """RISC-V RV32IMAC instruction decoder."""

    REG_NAMES = [
        'zero','ra','sp','gp','tp','t0','t1','t2',
        's0','s1','a0','a1','a2','a3','a4','a5',
        'a6','a7','s2','s3','s4','s5','s6','s7',
        's8','s9','s10','s11','t3','t4','t5','t6',
    ]

    @staticmethod
    def reg(n):
        return RV32Decoder.REG_NAMES[n & 0x1F]

    @staticmethod
    def sext(val, bits):
        if val & (1 << (bits - 1)):
            return val - (1 << bits)
        return val

    def decode(self, bits, pc=0):
        """Decode a 32-bit RISC-V instruction. Returns dict with fields."""
        if bits & 3 != 3:
            return self._decode_compressed(bits, pc)
        return self._decode_32(bits, pc)

    def _decode_32(self, b, pc):
        opcode = b & 0x7F
        rd = (b >> 7) & 0x1F
        funct3 = (b >> 12) & 0x7
        rs1 = (b >> 15) & 0x1F
        rs2 = (b >> 20) & 0x1F
        funct7 = (b >> 25) & 0x7F

        r = {
            'bits': b, 'pc': pc, 'rd': rd, 'rs1': rs1, 'rs2': rs2,
            'funct3': funct3, 'funct7': funct7, 'opcode': opcode,
            'mnemonic': '???', 'asm': f'??? 0x{b:08x}',
            'imm': 0, 'is_branch': False, 'is_jal': False, 'is_jalr': False,
            'is_store': False, 'is_load': False, 'is_csr': False,
            'target_addr': None, 'is_compressed': False, 'length': 4,
        }

        # I-immediate
        imm_i = self.sext((b >> 20) & 0xFFF, 12)
        # S-immediate
        imm_s = self.sext(((b >> 25) << 5) | ((b >> 7) & 0x1F), 12)
        # B-immediate
        imm_b = self.sext(
            ((b >> 31) << 12) | (((b >> 7) & 1) << 11) |
            (((b >> 25) & 0x3F) << 5) | (((b >> 8) & 0xF) << 1), 13)
        # U-immediate
        imm_u = b & 0xFFFFF000
        # J-immediate
        imm_j = self.sext(
            ((b >> 31) << 20) | (((b >> 12) & 0xFF) << 12) |
            (((b >> 20) & 1) << 11) | (((b >> 21) & 0x3FF) << 1), 21)

        R = self.reg

        if opcode == 0x37:  # LUI
            r['mnemonic'] = 'lui'; r['imm'] = imm_u
            r['asm'] = f"lui {R(rd)}, 0x{(imm_u >> 12) & 0xFFFFF:x}"
        elif opcode == 0x17:  # AUIPC
            r['mnemonic'] = 'auipc'; r['imm'] = imm_u
            r['asm'] = f"auipc {R(rd)}, 0x{(imm_u >> 12) & 0xFFFFF:x}"
        elif opcode == 0x6F:  # JAL
            r['mnemonic'] = 'jal'; r['imm'] = imm_j; r['is_jal'] = True
            r['target_addr'] = (pc + imm_j) & 0xFFFFFFFF
            r['asm'] = f"jal {R(rd)}, {imm_j}"
        elif opcode == 0x67:  # JALR
            r['mnemonic'] = 'jalr'; r['imm'] = imm_i; r['is_jalr'] = True
            r['asm'] = f"jalr {R(rd)}, {imm_i}({R(rs1)})"
        elif opcode == 0x63:  # Branch
            r['is_branch'] = True; r['imm'] = imm_b
            r['target_addr'] = (pc + imm_b) & 0xFFFFFFFF
            mn = {0:'beq',1:'bne',4:'blt',5:'bge',6:'bltu',7:'bgeu'}.get(funct3, 'b?')
            r['mnemonic'] = mn
            r['asm'] = f"{mn} {R(rs1)}, {R(rs2)}, {imm_b}"
        elif opcode == 0x03:  # Load
            r['is_load'] = True; r['imm'] = imm_i
            mn = {0:'lb',1:'lh',2:'lw',4:'lbu',5:'lhu'}.get(funct3, 'l?')
            r['mnemonic'] = mn
            r['asm'] = f"{mn} {R(rd)}, {imm_i}({R(rs1)})"
        elif opcode == 0x23:  # Store
            r['is_store'] = True; r['imm'] = imm_s
            mn = {0:'sb',1:'sh',2:'sw'}.get(funct3, 's?')
            r['mnemonic'] = mn
            r['asm'] = f"{mn} {R(rs2)}, {imm_s}({R(rs1)})"
        elif opcode == 0x13:  # ALU immediate
            r['imm'] = imm_i
            if funct3 == 0: mn = 'addi'
            elif funct3 == 1: mn = 'slli'
            elif funct3 == 2: mn = 'slti'
            elif funct3 == 3: mn = 'sltiu'
            elif funct3 == 4: mn = 'xori'
            elif funct3 == 5: mn = 'srai' if funct7 == 0x20 else 'srli'
            elif funct3 == 6: mn = 'ori'
            elif funct3 == 7: mn = 'andi'
            else: mn = 'alui?'
            r['mnemonic'] = mn
            if mn in ('slli', 'srli', 'srai'):
                r['imm'] = rs2                     # shamt: imm[4:0]; bit 30 is the funct7 sra flag
                r['asm'] = f"{mn} {R(rd)}, {R(rs1)}, {rs2}"
            elif mn == 'addi' and rd == 0 and rs1 == 0 and imm_i == 0: r['asm'] = 'nop'
            elif mn == 'addi' and rs1 == 0: r['asm'] = f"li {R(rd)}, {imm_i}"
            elif mn == 'addi' and imm_i == 0: r['asm'] = f"mv {R(rd)}, {R(rs1)}"
            else: r['asm'] = f"{mn} {R(rd)}, {R(rs1)}, {imm_i}"
        elif opcode == 0x33:  # ALU register
            if funct7 == 1:  # M extension
                mn = {0:'mul',1:'mulh',2:'mulhsu',3:'mulhu',4:'div',5:'divu',6:'rem',7:'remu'}.get(funct3,'m?')
            elif funct7 == 0x20:
                mn = {0:'sub',5:'sra'}.get(funct3,'?')
            else:
                mn = {0:'add',1:'sll',2:'slt',3:'sltu',4:'xor',5:'srl',6:'or',7:'and'}.get(funct3,'?')
            r['mnemonic'] = mn
            r['asm'] = f"{mn} {R(rd)}, {R(rs1)}, {R(rs2)}"
        elif opcode == 0x0F:  # Fence
            r['mnemonic'] = 'fence'; r['asm'] = 'fence'
        elif opcode == 0x73:  # System
            r['is_csr'] = True
            csr = (b >> 20) & 0xFFF
            if funct3 == 0:
                if b == 0x00000073: r['mnemonic'] = 'ecall'; r['asm'] = 'ecall'
                elif b == 0x00100073: r['mnemonic'] = 'ebreak'; r['asm'] = 'ebreak'
                elif b == 0x10200073: r['mnemonic'] = 'sret'; r['asm'] = 'sret'
                elif b == 0x30200073: r['mnemonic'] = 'mret'; r['asm'] = 'mret'
                elif b == 0x10500073: r['mnemonic'] = 'wfi'; r['asm'] = 'wfi'
                else: r['asm'] = f'sys 0x{b:08x}'
            else:
                mn = {1:'csrrw',2:'csrrs',3:'csrrc',5:'csrrwi',6:'csrrsi',7:'csrrci'}.get(funct3,'csr?')
                r['mnemonic'] = mn; r['imm'] = csr
                csr_name = CSR_NAMES.get(csr, f'0x{csr:03x}')
                if funct3 >= 5:
                    r['asm'] = f"{mn} {R(rd)}, {csr_name}, {rs1}"
                else:
                    r['asm'] = f"{mn} {R(rd)}, {csr_name}, {R(rs1)}"
        elif opcode == 0x2F:  # Atomic (A extension)
            r['mnemonic'] = 'amo'; r['asm'] = f"amo 0x{b:08x}"
        else:
            r['mnemonic'] = '???'; r['asm'] = f"??? 0x{b:08x}"

        return r

    def _decode_compressed(self, b, pc):
        """Decode 16-bit compressed (C extension) instruction."""
        b = b & 0xFFFF
        r = {
            'bits': b, 'pc': pc, 'rd': 0, 'rs1': 0, 'rs2': 0,
            'funct3': 0, 'funct7': 0, 'opcode': b & 3,
            'mnemonic': 'c.???', 'asm': f'c.??? 0x{b:04x}',
            'imm': 0, 'is_branch': False, 'is_jal': False, 'is_jalr': False,
            'is_store': False, 'is_load': False, 'is_csr': False,
            'target_addr': None, 'is_compressed': True, 'length': 2,
        }
        op = b & 3
        funct3 = (b >> 13) & 7
        R = self.reg

        if op == 0:
            if funct3 == 0:  # c.addi4spn
                rd = 8 + ((b >> 2) & 7)
                r['mnemonic'] = 'c.addi4spn'
                r['rd'] = rd
                r['asm'] = f"c.addi4spn {R(rd)}, sp, ..."
            elif funct3 == 2:  # c.lw
                r['mnemonic'] = 'c.lw'; r['is_load'] = True
            elif funct3 == 6:  # c.sw
                r['mnemonic'] = 'c.sw'; r['is_store'] = True
        elif op == 1:
            if funct3 == 0:  # c.addi / c.nop
                rd = (b >> 7) & 0x1F
                r['rd'] = rd; r['mnemonic'] = 'c.nop' if rd == 0 else 'c.addi'
                r['asm'] = 'c.nop' if rd == 0 else f"c.addi {R(rd)}, ..."
            elif funct3 == 1:  # c.jal (RV32 only)
                r['mnemonic'] = 'c.jal'; r['is_jal'] = True
            elif funct3 == 5:  # c.j
                r['mnemonic'] = 'c.j'; r['is_jal'] = True
            elif funct3 == 6:  # c.beqz
                r['mnemonic'] = 'c.beqz'; r['is_branch'] = True
            elif funct3 == 7:  # c.bnez
                r['mnemonic'] = 'c.bnez'; r['is_branch'] = True
            elif funct3 == 2:  # c.li
                rd = (b >> 7) & 0x1F
                r['rd'] = rd; r['mnemonic'] = 'c.li'
                r['asm'] = f"c.li {R(rd)}, ..."
            elif funct3 == 3:  # c.lui / c.addi16sp
                rd = (b >> 7) & 0x1F
                r['mnemonic'] = 'c.addi16sp' if rd == 2 else 'c.lui'
        elif op == 2:
            if funct3 == 0:  # c.slli
                r['mnemonic'] = 'c.slli'
            elif funct3 == 2:  # c.lwsp
                r['mnemonic'] = 'c.lwsp'; r['is_load'] = True
            elif funct3 == 4:  # c.mv / c.add / c.jr / c.jalr
                rs2 = (b >> 2) & 0x1F
                rd = (b >> 7) & 0x1F
                if (b >> 12) & 1:
                    if rs2 == 0 and rd == 0:
                        r['mnemonic'] = 'c.ebreak'; r['asm'] = 'c.ebreak'
                    elif rs2 == 0:
                        r['mnemonic'] = 'c.jalr'; r['is_jalr'] = True
                        r['asm'] = f"c.jalr {R(rd)}"
                    else:
                        r['mnemonic'] = 'c.add'
                        r['asm'] = f"c.add {R(rd)}, {R(rs2)}"
                else:
                    if rs2 == 0:
                        r['mnemonic'] = 'c.jr'; r['is_jalr'] = True
                        r['asm'] = f"c.jr {R(rd)}"
                    else:
                        r['mnemonic'] = 'c.mv'
                        r['asm'] = f"c.mv {R(rd)}, {R(rs2)}"
                r['rd'] = rd; r['rs2'] = rs2
            elif funct3 == 6:  # c.swsp
                r['mnemonic'] = 'c.swsp'; r['is_store'] = True

        return r


# Well-known CSR names
CSR_NAMES = {
    0x000: 'ustatus', 0x001: 'fflags', 0x002: 'frm', 0x003: 'fcsr',
    0x100: 'sstatus', 0x104: 'sie', 0x105: 'stvec', 0x106: 'scounteren',
    0x140: 'sscratch', 0x141: 'sepc', 0x142: 'scause', 0x143: 'stval',
    0x144: 'sip', 0x180: 'satp',
    0x300: 'mstatus', 0x301: 'misa', 0x302: 'medeleg', 0x303: 'mideleg',
    0x304: 'mie', 0x305: 'mtvec', 0x306: 'mcounteren',
    0x340: 'mscratch', 0x341: 'mepc', 0x342: 'mcause', 0x343: 'mtval',
    0x344: 'mip',
    0xB00: 'mcycle', 0xB02: 'minstret', 0xB80: 'mcycleh', 0xB82: 'minstreth',
    0xC00: 'cycle', 0xC01: 'time', 0xC02: 'instret',
    0xF11: 'mvendorid', 0xF12: 'marchid', 0xF13: 'mimpid', 0xF14: 'mhartid',
}


# ══════════════════════════════════════════════════════════════
#  STAGE 3: INSTRUCTION TRACE INDEX
# ══════════════════════════════════════════════════════════════

class InstructionTrace:
    """Pre-computed index of all retired instructions for fast queries."""

    def __init__(self):
        self.entries = []    # list of dicts
        self.decoder = None
        self.tags = None
        self.hart_id = 0
        self.thread_id = 0
        self.built = False

    def build(self, vcd, tags, decoder=None):
        """Build trace from VCD + tag store. Returns number of entries."""
        ensure_trace_loaded(vcd)
        self.tags = tags
        self.decoder = decoder or RV32Decoder()
        self.entries = []

        # Resolve tagged signals
        instr_name = tags.get_signal('isa.instr.writeback')
        valid_name = tags.get_signal('isa.valid.writeback')
        pc_name = tags.get_signal('isa.pc.writeback')

        if not instr_name:
            return 0

        def find_sig(name):
            return find_signal(vcd, name)

        instr_sig = find_sig(instr_name)
        valid_sig = find_sig(valid_name)
        pc_sig = find_sig(pc_name)
        rd_addr_sig = find_sig(tags.get_signal('isa.rd.addr'))
        rd_data_sig = find_sig(tags.get_signal('isa.rd.data'))
        rd_wen_sig = find_sig(tags.get_signal('isa.rd.wen'))
        mem_addr_sig = find_sig(tags.get_signal('isa.mem.addr'))
        mem_wdata_sig = find_sig(tags.get_signal('isa.mem.wdata'))
        mem_wen_sig = find_sig(tags.get_signal('isa.mem.wen'))
        csr_addr_sig = find_sig(tags.get_signal('isa.csr.addr'))
        csr_wdata_sig = find_sig(tags.get_signal('isa.csr.wdata'))
        csr_wen_sig = find_sig(tags.get_signal('isa.csr.wen'))
        stall_sig = find_sig(tags.get_signal('isa.pipeline.stall'))
        hart_id_sig = find_sig(tags.get_signal('isa.hart_id'))
        thread_id_sig = find_sig(tags.get_signal('isa.thread_id'))

        if not instr_sig:
            return 0

        def safe_int(sig, t, default=None):
            if not sig:
                return default
            v = sig.value_at(t)
            if not v or 'x' in v.lower() or 'z' in v.lower():
                return default
            try:
                return int(v, 2)
            except ValueError:
                try:
                    return int(float(v))
                except (ValueError, OverflowError):
                    return default

        # Retirement instants. With a tagged clock: every rising edge where
        # valid is high (so back-to-back identical instructions each count).
        # Otherwise: each instruction-value change plus each rise of valid —
        # walking only the instruction's changes missed a repeated
        # instruction retired again on a later valid pulse.
        clk_sig = find_sig(tags.get_signal('clock'))
        cands = rising_edges(clk_sig, 0, vcd.max_time) if clk_sig is not None else []
        if not cands:
            cand_set = set(instr_sig._times)
            if valid_sig is not None:
                cand_set.update(rising_edges(valid_sig, 0, vcd.max_time))
            cands = sorted(cand_set)
        for t in cands:
            if valid_sig is not None and valid_sig.value_at(t) != '1':
                continue

            instr_bits = safe_int(instr_sig, t)
            if instr_bits is None or instr_bits == 0:
                continue

            pc_val = safe_int(pc_sig, t, 0)
            decoded = self.decoder.decode(instr_bits, pc_val)

            entry = {
                'time': t,
                'pc': pc_val,
                'bits': instr_bits,
                'decoded': decoded,
                'rd_addr': safe_int(rd_addr_sig, t) if rd_wen_sig and rd_wen_sig.value_at(t) == '1' else None,
                'rd_data': safe_int(rd_data_sig, t) if rd_wen_sig and rd_wen_sig.value_at(t) == '1' else None,
                'mem_addr': safe_int(mem_addr_sig, t) if mem_wen_sig and mem_wen_sig.value_at(t) == '1' else None,
                'mem_wdata': safe_int(mem_wdata_sig, t) if mem_wen_sig and mem_wen_sig.value_at(t) == '1' else None,
                'csr_addr': safe_int(csr_addr_sig, t),
                'csr_wdata': safe_int(csr_wdata_sig, t),
                'stall': stall_sig.value_at(t) == '1' if stall_sig else False,
                'hart_id': safe_int(hart_id_sig, t, tags.hart_id),
                'thread_id': safe_int(thread_id_sig, t, tags.thread_id),
            }
            self.entries.append(entry)

        self.built = True
        return len(self.entries)

    def query(self, filter_fn):
        """Apply a filter function to all entries. Returns matching entries."""
        return [e for e in self.entries if filter_fn(e)]

    # Names available to a query expression. __builtins__ is blanked so a
    # typo cannot reach open()/__import__ by accident — but this is a
    # convenience, not a sandbox: Python's eval cannot be made safe against a
    # hostile expression (object introspection reaches everything). The
    # prompt runs with the user's own authority, like a Tcl script would,
    # and is not reachable from the agent control channel.
    _QUERY_NAMESPACE = {
        "__builtins__": {},
        "len": len, "int": int, "str": str, "abs": abs, "min": min, "max": max,
        "any": any, "all": all, "sum": sum, "range": range, "sorted": sorted,
        "hex": hex, "bin": bin, "oct": oct, "ord": ord, "bool": bool,
        "round": round, "float": float,
    }

    def query_str(self, expr_str):
        """Apply a string expression as filter. Returns (matches, error_str).

        The expression is a Python lambda evaluated with the user's
        authority (see _QUERY_NAMESPACE): treat it like a script, not a
        sandbox.
        """
        try:
            fn = eval(expr_str, dict(self._QUERY_NAMESPACE), {})
            if callable(fn):
                return self.query(fn), None
            return [], "Expression must be callable (use lambda e: ...)"
        except Exception as ex:
            return [], str(ex)

    def find_mnemonic(self, pattern):
        """Find all instructions matching mnemonic pattern (regex)."""
        pat = re.compile(pattern, re.I)
        return [e for e in self.entries if pat.search(e['decoded']['mnemonic'])]

    def find_pc_range(self, pc_lo, pc_hi):
        """Find all instructions with PC in [pc_lo, pc_hi]."""
        return [e for e in self.entries if pc_lo <= e['pc'] <= pc_hi]

    def find_reg_writes(self, reg_num, pc_lo=0, pc_hi=0xFFFFFFFF):
        """Find all writes to register reg_num in PC range."""
        return [e for e in self.entries
                if e['rd_addr'] == reg_num and pc_lo <= e['pc'] <= pc_hi]

    def find_stores(self, addr_lo=0, addr_hi=0xFFFFFFFF):
        """Find all stores to address range."""
        return [e for e in self.entries
                if e['decoded']['is_store'] and
                e['mem_addr'] is not None and
                addr_lo <= e['mem_addr'] <= addr_hi]

    def find_csr_changes(self, csr_addr):
        """Find all CSR writes to a specific CSR."""
        return [e for e in self.entries
                if e['decoded']['is_csr'] and e.get('csr_addr') == csr_addr]

    def find_stalled(self):
        """Find all instructions retired during stall."""
        return [e for e in self.entries if e.get('stall')]



    # ══ MULTI-HART / BARREL PROCESSOR DEBUG ══

    def extract_thread_trace(self, hart_id):
        """Extract a clean single-thread trace from the interleaved stream."""
        return [e for e in self.entries if e.get('hart_id') == hart_id]

    def extract_thread_trace_range(self, hart_id, t_start=0, t_end=None):
        """Extract thread trace within a time range."""
        if t_end is None:
            t_end = float('inf')
        return [e for e in self.entries
                if e.get('hart_id') == hart_id and t_start <= e['time'] <= t_end]

    def get_active_harts(self):
        """Get sorted list of all hart IDs seen in the trace."""
        return sorted(set(e.get('hart_id', 0) for e in self.entries))

    def get_hart_count(self):
        """Number of distinct harts in the trace."""
        return len(self.get_active_harts())

    def hart_ipc(self, hart_id, t_start=0, t_end=None):
        """Compute IPC for a specific hart in a time window."""
        thread_entries = self.extract_thread_trace_range(hart_id, t_start, t_end)
        if len(thread_entries) < 2:
            return 0.0
        span = thread_entries[-1]['time'] - thread_entries[0]['time']
        if span <= 0:
            return 0.0
        return len(thread_entries) / span

    def all_hart_ipc(self, t_start=0, t_end=None):
        """Compute IPC for all harts. Returns dict {hart_id: ipc}."""
        result = {}
        for h in self.get_active_harts():
            result[h] = self.hart_ipc(h, t_start, t_end)
        return result

    def fairness_analysis(self):
        """Analyze scheduling fairness. Returns per-hart stats."""
        harts = self.get_active_harts()
        if not harts or not self.entries:
            return {}

        results = {}
        for h in harts:
            times = [e['time'] for e in self.entries if e.get('hart_id') == h]
            if len(times) < 2:
                results[h] = {'count': len(times), 'avg_gap': 0, 'max_gap': 0,
                              'max_gap_time': 0, 'starved': False}
                continue

            gaps = [times[i+1] - times[i] for i in range(len(times)-1)]
            avg_gap = sum(gaps) / len(gaps)
            max_gap = max(gaps)
            max_gap_idx = gaps.index(max_gap)
            max_gap_time = times[max_gap_idx]

            # Starvation = max gap > 3× average (heuristic)
            starved = max_gap > avg_gap * 3 if avg_gap > 0 else False

            results[h] = {
                'count': len(times),
                'avg_gap': avg_gap,
                'max_gap': max_gap,
                'max_gap_time': max_gap_time,
                'starved': starved,
            }
        return results

    def find_starvation_events(self, threshold_multiplier=3.0):
        """Find all times where a hart was starved (gap > threshold × avg)."""
        harts = self.get_active_harts()
        events = []
        for h in harts:
            times = [e['time'] for e in self.entries if e.get('hart_id') == h]
            if len(times) < 3:
                continue
            gaps = [times[i+1] - times[i] for i in range(len(times)-1)]
            avg = sum(gaps) / len(gaps)
            threshold = avg * threshold_multiplier
            for i, g in enumerate(gaps):
                if g > threshold:
                    events.append({
                        'hart_id': h, 'time': times[i],
                        'gap': g, 'avg_gap': avg,
                        'ratio': g / avg if avg > 0 else 0,
                    })
        return sorted(events, key=lambda e: e['time'])

    def thread_divergence(self, hart_a, hart_b, after_time=0):
        """Compare two hart traces and find where they diverge."""
        trace_a = self.extract_thread_trace_range(hart_a, after_time)
        trace_b = self.extract_thread_trace_range(hart_b, after_time)

        if not trace_a or not trace_b:
            return {'identical': False, 'reason': 'Empty trace(s)',
                    'diverge_time': after_time, 'details': []}

        min_len = min(len(trace_a), len(trace_b))
        for i in range(min_len):
            ea, eb = trace_a[i], trace_b[i]
            da, db = ea['decoded'], eb['decoded']

            if da['mnemonic'] != db['mnemonic']:
                return {
                    'identical': False,
                    'diverge_time': ea['time'],
                    'diverge_index': i,
                    'hart_a_entry': ea,
                    'hart_b_entry': eb,
                    'reason': f"Mnemonic: {da['asm']} vs {db['asm']}",
                    'details': self._divergence_details(trace_a, trace_b, i, 5),
                }
            if ea['pc'] != eb['pc']:
                return {
                    'identical': False,
                    'diverge_time': ea['time'],
                    'diverge_index': i,
                    'hart_a_entry': ea,
                    'hart_b_entry': eb,
                    'reason': f"PC: 0x{ea['pc']:x} vs 0x{eb['pc']:x}",
                    'details': self._divergence_details(trace_a, trace_b, i, 5),
                }

        return {'identical': True, 'compared': min_len,
                'reason': f'Identical for {min_len} instructions', 'details': []}

    def _divergence_details(self, trace_a, trace_b, start_idx, count):
        """Get side-by-side details around divergence point."""
        details = []
        for i in range(start_idx, min(start_idx + count, len(trace_a), len(trace_b))):
            ea, eb = trace_a[i], trace_b[i]
            details.append({
                'index': i,
                'a_time': ea['time'], 'a_pc': ea['pc'], 'a_asm': ea['decoded']['asm'],
                'b_time': eb['time'], 'b_pc': eb['pc'], 'b_asm': eb['decoded']['asm'],
                'match': ea['decoded']['mnemonic'] == eb['decoded']['mnemonic'] and ea['pc'] == eb['pc'],
            })
        return details

    def shared_resource_contention(self, addr_lo=0, addr_hi=0xFFFFFFFF):
        """Find memory accesses to same address from different harts."""
        # Group by address
        addr_map = {}  # addr -> [(time, hart, is_write, data)]
        for e in self.entries:
            if e['mem_addr'] is not None and addr_lo <= e['mem_addr'] <= addr_hi:
                addr = e['mem_addr']
                is_write = e['decoded']['is_store']
                data = e.get('mem_wdata') if is_write else e.get('mem_rdata')
                if addr not in addr_map:
                    addr_map[addr] = []
                addr_map[addr].append({
                    'time': e['time'], 'hart_id': e.get('hart_id', 0),
                    'is_write': is_write, 'data': data, 'pc': e['pc'],
                    'asm': e['decoded']['asm'],
                })

        # Find contention: same address accessed by different harts
        contention = []
        for addr, accesses in addr_map.items():
            harts = set(a['hart_id'] for a in accesses)
            if len(harts) > 1:
                # Sort by time
                accesses.sort(key=lambda a: a['time'])
                contention.append({
                    'addr': addr,
                    'n_harts': len(harts),
                    'harts': sorted(harts),
                    'accesses': accesses,
                    'has_waw': self._has_waw(accesses),
                    'has_raw': self._has_raw(accesses),
                    'has_war': self._has_war(accesses),
                })

        return sorted(contention, key=lambda c: -c['n_harts'])

    def _has_waw(self, accesses):
        for i in range(len(accesses)-1):
            if accesses[i]['is_write'] and accesses[i+1]['is_write'] and \
               accesses[i]['hart_id'] != accesses[i+1]['hart_id']:
                return True
        return False

    def _has_raw(self, accesses):
        last_writer = {}
        for a in accesses:
            if a['is_write']:
                last_writer[a['hart_id']] = a
            elif not a['is_write']:
                for wh, wa in last_writer.items():
                    if wh != a['hart_id'] and wa['time'] < a['time']:
                        return True
        return False

    def _has_war(self, accesses):
        last_reader = {}
        for a in accesses:
            if not a['is_write']:
                last_reader[a['hart_id']] = a
            elif a['is_write']:
                for rh, ra in last_reader.items():
                    if rh != a['hart_id'] and ra['time'] < a['time']:
                        return True
        return False

    def scheduling_heatmap(self, t_start, t_end, n_buckets=80):
        """Build a heatmap: which hart is active in each time bucket."""
        if not self.entries:
            return []
        bucket_size = max(1, (t_end - t_start) // n_buckets)
        harts = self.get_active_harts()
        hart_to_idx = {h: i for i, h in enumerate(harts)}
        n_harts = len(harts)

        # heatmap[bucket][hart_idx] = count
        heatmap = [[0] * n_harts for _ in range(n_buckets)]

        for e in self.entries:
            if t_start <= e['time'] <= t_end:
                bucket = min(n_buckets - 1, (e['time'] - t_start) // bucket_size)
                h_idx = hart_to_idx.get(e.get('hart_id', 0), 0)
                heatmap[bucket][h_idx] += 1

        return {'heatmap': heatmap, 'harts': harts, 'bucket_size': bucket_size,
                't_start': t_start, 't_end': t_end}

    def register_snapshot(self, hart_id, time, n_regs=32):
        """Reconstruct register file state for a hart at a given time.
        Walks backward through the trace to find the last write to each register."""
        regs = [None] * n_regs
        regs[0] = 0  # x0 is always 0

        thread_entries = [e for e in self.entries
                         if e.get('hart_id') == hart_id and e['time'] <= time]

        for e in thread_entries:
            if e['rd_addr'] is not None and 0 < e['rd_addr'] < n_regs:
                regs[e['rd_addr']] = e['rd_data']

        return regs

    def summary(self):
        """Return a summary string."""
        if not self.entries:
            return "No trace built"
        n = len(self.entries)
        mnemonics = {}
        for e in self.entries:
            mn = e['decoded']['mnemonic']
            mnemonics[mn] = mnemonics.get(mn, 0) + 1
        top5 = sorted(mnemonics.items(), key=lambda x: -x[1])[:5]
        t0 = self.entries[0]['time']
        t1 = self.entries[-1]['time']
        return (f"{n} instructions [{t0}..{t1}] "
                f"Top: {', '.join(f'{m}:{c}' for m,c in top5)}")


# ══════════════════════════════════════════════════════════════
#  FEATURE 4: BUS TRANSACTION DECODE
# ══════════════════════════════════════════════════════════════

class BusTransaction:
    __slots__ = ('protocol', 'type', 't_start', 't_end', 'addr', 'data',
                 'length', 'burst', 'size', 'resp', 'id', 'details')

    def __init__(self, protocol, type, t_start):
        self.protocol = protocol
        self.type = type
        self.t_start = t_start
        self.t_end = t_start
        self.addr = None
        self.data = []
        self.length = 0
        self.burst = None
        self.size = None
        self.resp = None
        self.id = None
        self.details = {}


class _BusDecoder:
    """Shared plumbing: role lookup, sample points, handshakes.

    Transactions are sampled per *clock cycle* when the tag dict names a
    'clock' (every rising edge), which is what the protocols define. Without
    a clock the decoders fall back to the union of the control signals'
    change times — adequate for single-beat traffic, but valid/ready held
    high across several cycles then looks like one beat.
    """
    def __init__(self, vcd, bus_tags):
        self.vcd = vcd
        self.tags = bus_tags

    def _find_sig(self, role):
        return find_signal(self.vcd, self.tags.get(role))

    _to_int = staticmethod(_to_int)

    def _clock(self):
        for role in ('clock', 'clk'):
            sig = self._find_sig(role)
            if sig is not None:
                return sig
        return None

    def _sample_points(self, sigs, clock=None):
        ensure_trace_loaded(self.vcd)
        clock = clock or self._clock()
        if clock is not None:
            edges = rising_edges(clock, 0, self.vcd.max_time)
            if edges:
                return edges
        times = set()
        for sg in sigs:
            if sg is not None:
                times.update(sg._times)
        return sorted(times)

    @staticmethod
    def _handshake_times(valid_sig, ready_sig, clock=None, max_time=None):
        """Times at which valid and ready are both high: each clock rising
        edge with a clock, else each change event of either signal."""
        if not valid_sig or not ready_sig:
            return []
        if clock is not None:
            end = max_time if max_time is not None else max(
                valid_sig._times[-1] if len(valid_sig._times) else 0,
                ready_sig._times[-1] if len(ready_sig._times) else 0)
            pts = rising_edges(clock, 0, end)
        else:
            pts = sorted(set(valid_sig._times) | set(ready_sig._times))
        return [t for t in pts
                if valid_sig.value_at(t) == '1' and ready_sig.value_at(t) == '1']


class AXIDecoder(_BusDecoder):
    BURST_NAMES = {0: 'FIXED', 1: 'INCR', 2: 'WRAP'}
    RESP_NAMES = {0: 'OKAY', 1: 'EXOKAY', 2: 'SLVERR', 3: 'DECERR'}

    def _hs(self, v, r, clk):
        ensure_trace_loaded(self.vcd)
        return self._handshake_times(v, r, clk, self.vcd.max_time) if v and r else []

    def decode_writes(self):
        aw_v, aw_r, aw_a = (self._find_sig(k) for k in ('axi.aw.valid', 'axi.aw.ready', 'axi.aw.addr'))
        aw_id, aw_len, aw_burst = (self._find_sig(k) for k in ('axi.aw.id', 'axi.aw.len', 'axi.aw.burst'))
        w_v, w_r, w_d = (self._find_sig(k) for k in ('axi.w.valid', 'axi.w.ready', 'axi.w.data'))
        b_v, b_r, b_resp, b_id = (self._find_sig(k) for k in ('axi.b.valid', 'axi.b.ready', 'axi.b.resp', 'axi.b.id'))
        if not (aw_v and aw_r and aw_a):
            return []
        clk = self._clock()
        aw_times = self._hs(aw_v, aw_r, clk)
        w_times = self._hs(w_v, w_r, clk)
        b_times = self._hs(b_v, b_r, clk)

        # W beats arrive in AW order (AXI4 has no WID), so they are consumed
        # from one cursor; the write-data channel may even lead AW. B
        # responses are matched by BID when it is tagged, else in order.
        txns = []
        wi = 0
        b_left = list(b_times)
        for t_aw in aw_times:
            txn = BusTransaction('AXI4', 'write', t_aw)
            txn.addr = self._to_int(aw_a.value_at(t_aw))
            if aw_id: txn.id = self._to_int(aw_id.value_at(t_aw))
            txn.length = (self._to_int(aw_len.value_at(t_aw)) + 1) if aw_len else 1
            if aw_burst:
                txn.burst = self.BURST_NAMES.get(self._to_int(aw_burst.value_at(t_aw)), '?')
            for _ in range(txn.length):
                if wi >= len(w_times):
                    break
                t_w = w_times[wi]
                wi += 1
                if w_d:
                    txn.data.append(self._to_int(w_d.value_at(t_w)))
                txn.t_end = max(txn.t_end, t_w)
            for i, t_b in enumerate(b_left):
                if t_b < txn.t_end:
                    continue
                if b_id and aw_id and self._to_int(b_id.value_at(t_b)) != txn.id:
                    continue
                if b_resp:
                    txn.resp = self.RESP_NAMES.get(self._to_int(b_resp.value_at(t_b)), '?')
                txn.t_end = t_b
                del b_left[i]
                break
            txns.append(txn)
        return txns

    def decode_reads(self):
        ar_v, ar_r, ar_a = (self._find_sig(k) for k in ('axi.ar.valid', 'axi.ar.ready', 'axi.ar.addr'))
        ar_id, ar_len = (self._find_sig(k) for k in ('axi.ar.id', 'axi.ar.len'))
        r_v, r_r, r_d, r_resp, r_id = (self._find_sig(k) for k in
                                       ('axi.r.valid', 'axi.r.ready', 'axi.r.data', 'axi.r.resp', 'axi.r.id'))
        if not (ar_v and ar_r and ar_a):
            return []
        clk = self._clock()
        ar_times = self._hs(ar_v, ar_r, clk)
        r_times = self._hs(r_v, r_r, clk)
        # R beats can interleave between IDs; with RID tagged each ID has its
        # own queue, otherwise beats are consumed in order.
        by_id = {}
        for t_r in r_times:
            key = self._to_int(r_id.value_at(t_r)) if (r_id and ar_id) else None
            by_id.setdefault(key, []).append(t_r)
        txns = []
        for t_ar in ar_times:
            txn = BusTransaction('AXI4', 'read', t_ar)
            txn.addr = self._to_int(ar_a.value_at(t_ar))
            if ar_id: txn.id = self._to_int(ar_id.value_at(t_ar))
            txn.length = (self._to_int(ar_len.value_at(t_ar)) + 1) if ar_len else 1
            queue = by_id.get(txn.id if (r_id and ar_id) else None, [])
            for _ in range(txn.length):
                while queue and queue[0] < t_ar:
                    queue.pop(0)
                if not queue:
                    break
                t_r = queue.pop(0)
                if r_d:
                    txn.data.append(self._to_int(r_d.value_at(t_r)))
                if r_resp:
                    txn.resp = self.RESP_NAMES.get(self._to_int(r_resp.value_at(t_r)), '?')
                txn.t_end = t_r
            txns.append(txn)
        return txns

    def decode_all(self):
        all_txns = self.decode_writes() + self.decode_reads()
        all_txns.sort(key=lambda t: t.t_start)
        return all_txns


class WishboneDecoder(_BusDecoder):
    def decode_all(self):
        cyc, stb, we, ack = (self._find_sig(k) for k in ('wb.cyc', 'wb.stb', 'wb.we', 'wb.ack'))
        addr, dat_i, dat_o = (self._find_sig(k) for k in ('wb.addr', 'wb.dat_i', 'wb.dat_o'))
        if not (cyc and stb and ack):
            return []
        txns = []
        cur = None
        for t in self._sample_points([cyc, stb, ack]):
            c = cyc.value_at(t) == '1'
            s_ = stb.value_at(t) == '1'
            a = ack.value_at(t) == '1'
            if c and s_ and cur is None:
                is_write = we.value_at(t) == '1' if we else False
                cur = BusTransaction('Wishbone', 'write' if is_write else 'read', t)
                if addr: cur.addr = self._to_int(addr.value_at(t))
                if is_write and dat_i: cur.data.append(self._to_int(dat_i.value_at(t)))
                cur.length = 1
            if a and cur is not None:
                if cur.type == 'read' and dat_o:
                    cur.data.append(self._to_int(dat_o.value_at(t)))
                cur.t_end = t
                cur.resp = 'OK'
                txns.append(cur)
                cur = None
        return txns


class AHBDecoder(_BusDecoder):
    HTRANS = {0: 'IDLE', 1: 'BUSY', 2: 'NONSEQ', 3: 'SEQ'}
    HRESP = {0: 'OKAY', 1: 'ERROR'}

    def decode_all(self):
        hready, hwrite, haddr = (self._find_sig(k) for k in ('ahb.hready', 'ahb.hwrite', 'ahb.haddr'))
        hwdata, hrdata, hresp, htrans = (self._find_sig(k) for k in
                                         ('ahb.hwdata', 'ahb.hrdata', 'ahb.hresp', 'ahb.htrans'))
        if not (hready and haddr and htrans):
            return []
        txns = []
        cur = None          # open burst (NONSEQ + following SEQ beats)
        pending = None      # beat whose data phase completes at the next ready
        for t in self._sample_points([haddr, htrans, hready]):
            if hready.value_at(t) != '1':
                continue
            if pending is not None:
                if pending.type == 'write' and hwdata:
                    pending.data.append(self._to_int(hwdata.value_at(t)))
                elif pending.type == 'read' and hrdata:
                    pending.data.append(self._to_int(hrdata.value_at(t)))
                if hresp:
                    pending.resp = self.HRESP.get(self._to_int(hresp.value_at(t)), '?')
                pending.t_end = t
                pending = None
            trans = self._to_int(htrans.value_at(t))
            if trans == 2:                                   # NONSEQ: new transfer
                if cur is not None:
                    txns.append(cur)
                is_w = hwrite.value_at(t) == '1' if hwrite else False
                cur = BusTransaction('AHB', 'write' if is_w else 'read', t)
                cur.addr = self._to_int(haddr.value_at(t))
                cur.length = 1
                pending = cur
            elif trans == 3 and cur is not None:             # SEQ: next burst beat
                cur.length += 1
                pending = cur
            elif trans == 0 and cur is not None:             # IDLE ends the burst
                txns.append(cur)
                cur = None
        if cur is not None:
            txns.append(cur)
        return txns
