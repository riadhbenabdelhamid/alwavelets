"""Protocol and bus decoders on synthetic traces: SPI in all four modes,
I2C START/ACK/STOP framing, UART start/stop/parity checks, RISC-V shift
and c.ebreak decode, instruction-trace retirements, and clock-sampled AXI,
Wishbone and AHB transactions."""
import pytest


def _sig(w, name, size, changes, stype="wire", scope="top"):
    s = w.VCDSignal(name, name, size, stype, scope)
    for t, v in changes:
        s.add_change(t, v)
    return s


class _VCD:
    """Duck-typed trace for the decoders: signals by full name + max_time."""
    timescale, timescale_number, timescale_unit = "1ns", 1, "ns"

    def __init__(self, *sigs):
        self.signals = {s.full_name: s for s in sigs}
        self.signal_order = list(self.signals)
        self.max_time = max((s._times[-1] for s in sigs if len(s._times)), default=0)

    def time_to_str(self, t):
        return "%dns" % t




# ── decoders ─────────────────────────────────────────────────────────

def _bits_msb(byte):
    return [(byte >> i) & 1 for i in range(7, -1, -1)]


def _clock(w, rises, period=20):
    clk = w.VCDSignal("clk", "clk", 1, scope="top")
    clk.add_change(0, "0")
    for e in rises:
        clk.add_change(e, "1")
        clk.add_change(e + period // 2, "0")
    return clk


def _line(w, name, byte, sample_edges):
    sig = w.VCDSignal(name, name, 1)
    sig.add_change(0, "1")
    for e, bit in zip(sample_edges, _bits_msb(byte)):
        sig.add_change(e - 3, "1" if bit else "0")
    return sig


@pytest.mark.parametrize("cpol,cpha,on_rising", [(0, 0, True), (0, 1, False),
                                                 (1, 0, False), (1, 1, True)])
def test_spi_all_modes(w, cpol, cpha, on_rising):
    rises = [20 + 20 * i for i in range(8)]
    sample = rises if on_rising else [r + 10 for r in rises]
    spi = w.SPIDecoder(_clock(w, rises), _line(w, "mosi", 0xA5, sample),
                       miso_sig=_line(w, "miso", 0x3C, sample), cpol=cpol, cpha=cpha)
    frames = spi.decode(0, 300)
    assert [f.label for f in frames] == ["0xA5"]
    assert frames[0].data == "miso=0x3C"


def test_i2c_start_ack_stop(w):
    # 9 SCL pulses per byte: 8 data + ACK. Two bytes, START before, STOP after.
    rises = [100 + 20 * i for i in range(18)]
    last = rises[-1] + 10
    # SCL: high around START (t=50) and STOP (t=last+25), clocking in between.
    scl_ch = [(0, "0"), (40, "1"), (60, "0")]
    for r in rises:
        scl_ch += [(r, "1"), (r + 10, "0")]
    scl_ch.append((last + 20, "1"))
    scl = _sig(w, "scl", 1, scl_ch)
    bits = _bits_msb(0xA3) + [0] + _bits_msb(0x3C) + [1]   # addr 0x51 R, ACK; data, NAK
    sda_ch = [(0, "1"), (50, "0")]                   # START: SDA falls while SCL high
    sda_ch += [(e - 3, "1" if bit else "0") for e, bit in zip(rises, bits)]
    sda_ch.append((last + 15, "0"))                  # (SCL low: not a START)
    sda_ch.append((last + 25, "1"))                  # STOP: SDA rises while SCL high
    sda = _sig(w, "sda", 1, sda_ch)
    frames = w.I2CDecoder(sda, scl).decode(0, last + 100)
    assert [f.label for f in frames] == ["S", "0xA3", "0x3C", "P"]
    assert frames[1].data == "addr 0x51 R ACK"
    assert frames[2].data == "NAK"


def _uart_line(w, frames_at, baud=10, parity=None, bad_stop=False):
    sig = w.VCDSignal("rx", "rx", 1)
    sig.add_change(0, "1")
    for start, byte in frames_at:
        sig.add_change(start, "0")
        t = start + baud
        ones = 0
        for i in range(8):
            bit = (byte >> i) & 1
            ones += bit
            sig.add_change(t, "1" if bit else "0")
            t += baud
        if parity:
            pbit = (ones % 2 == 1) if parity == "even" else (ones % 2 == 0)
            sig.add_change(t, "1" if pbit else "0")
            t += baud
        sig.add_change(t, "0" if bad_stop else "1")
        if bad_stop:
            sig.add_change(t + baud, "1")
    return sig


def test_uart_glitch_does_not_eat_next_frame(w):
    sig = _uart_line(w, [(100, 0x55)])
    sig.add_change(50, "0")                           # 1-tick glitch on the idle line
    sig.add_change(51, "1")
    frames = w.UARTDecoder(sig, 10).decode(0, 400)
    assert [f.label for f in frames] == ["'U' 0x55"]


def test_uart_parity_and_framing(w):
    ok = _uart_line(w, [(100, 0x55)], parity="even")
    frames = w.UARTDecoder(ok, 10, parity="even").decode(0, 400)
    assert [(f.label, f.data) for f in frames] == [("'U' 0x55", "")]
    wrong = _uart_line(w, [(100, 0x55)], parity="odd")      # sender used odd
    frames = w.UARTDecoder(wrong, 10, parity="even").decode(0, 400)
    assert frames[0].data == "PARITY"
    bad = _uart_line(w, [(100, 0x55), (300, 0x41)], bad_stop=True)
    labels = [f.label for f in w.UARTDecoder(bad, 10).decode(0, 600)]
    assert labels[0] == "FRAME ERR" and labels.count("FRAME ERR") == 2


def test_rv32_shift_amount_and_c_ebreak(w):
    d = w.RV32Decoder()
    assert d.decode(0x40515093)["asm"] == "srai ra, sp, 5"
    assert d.decode(0x00515093)["asm"] == "srli ra, sp, 5"
    assert d.decode(0x9002)["mnemonic"] == "c.ebreak"
    assert d.decode(0x9082)["mnemonic"] == "c.jalr"


def test_instruction_trace_counts_repeated_retirements(w):
    instr = _sig(w, "instr", 32, [(0, format(0x00100093, "032b"))])
    valid = _sig(w, "valid", 1, [(0, "0"), (10, "1"), (20, "0"), (30, "1"), (40, "0"), (50, "1")])
    vcd = _VCD(instr, valid)
    tags = w.SignalTagStore()
    tags.set_tag("isa.instr.writeback", "top.instr")
    tags.set_tag("isa.valid.writeback", "top.valid")
    tr = w.InstructionTrace()
    assert tr.build(vcd, tags) == 3
    assert [e["time"] for e in tr.entries] == [10, 30, 50]
    # with a clock: one entry per cycle where valid is high
    clk = _clock(w, [5 + 10 * i for i in range(6)], period=10)   # edges 5..55
    vcd2 = _VCD(instr, valid, clk)
    tags.set_tag("clock", "top.clk")
    tr2 = w.InstructionTrace()
    assert tr2.build(vcd2, tags) == 3
    assert [e["time"] for e in tr2.entries] == [15, 35, 55]


def test_axi_burst_beats_and_outstanding_writes(w):
    clk = _clock(w, [10 * i + 5 for i in range(20)], period=10)
    hi, lo = "1", "0"
    aw_v = _sig(w, "awvalid", 1, [(0, lo), (10, hi), (30, lo)])          # AW at cycles 15, 25
    aw_r = _sig(w, "awready", 1, [(0, hi)])
    aw_a = _sig(w, "awaddr", 8, [(0, "00000000"), (10, "00010000"), (20, "00100000")])
    aw_len = _sig(w, "awlen", 4, [(0, "0011"), (20, "0000")])           # 4 beats, then 1
    w_v = _sig(w, "wvalid", 1, [(0, lo), (30, hi), (80, lo)])            # beats at 35..75
    w_r = _sig(w, "wready", 1, [(0, hi)])
    w_d = _sig(w, "wdata", 8, [(0, "00000000")] + [(30 + 10 * i, format(i + 1, "08b")) for i in range(5)])
    b_v = _sig(w, "bvalid", 1, [(0, lo), (80, hi), (100, lo)])
    b_r = _sig(w, "bready", 1, [(0, hi)])
    b_resp = _sig(w, "bresp", 2, [(0, "00"), (90, "10")])
    sigs = [clk, aw_v, aw_r, aw_a, aw_len, w_v, w_r, w_d, b_v, b_r, b_resp]
    vcd = _VCD(*sigs)
    tags = {"clock": "top.clk", "axi.aw.valid": "top.awvalid", "axi.aw.ready": "top.awready",
            "axi.aw.addr": "top.awaddr", "axi.aw.len": "top.awlen", "axi.w.valid": "top.wvalid",
            "axi.w.ready": "top.wready", "axi.w.data": "top.wdata", "axi.b.valid": "top.bvalid",
            "axi.b.ready": "top.bready", "axi.b.resp": "top.bresp"}
    txns = w.AXIDecoder(vcd, tags).decode_writes()
    assert [(t.addr, t.length, t.data, t.resp) for t in txns] == [
        (0x10, 4, [1, 2, 3, 4], "OKAY"), (0x20, 1, [5], "SLVERR")]
    assert txns[0].t_end == 85 and txns[1].t_end == 95


def test_wishbone_pipelined_with_clock(w):
    clk = _clock(w, [10 * i + 5 for i in range(10)], period=10)
    cyc = _sig(w, "cyc", 1, [(0, "0"), (10, "1"), (50, "0")])
    stb = _sig(w, "stb", 1, [(0, "0"), (10, "1"), (40, "0")])            # 3 beats: 15,25,35
    ack = _sig(w, "ack", 1, [(0, "0"), (20, "1"), (50, "0")])            # acks at 25,35,45
    we = _sig(w, "we", 1, [(0, "1")])
    addr = _sig(w, "addr", 8, [(0, "00000000"), (10, "00000001"), (20, "00000010"), (30, "00000011")])
    vcd = _VCD(clk, cyc, stb, ack, we, addr)
    tags = {"clock": "top.clk", "wb.cyc": "top.cyc", "wb.stb": "top.stb", "wb.ack": "top.ack",
            "wb.we": "top.we", "wb.addr": "top.addr"}
    txns = w.WishboneDecoder(vcd, tags).decode_all()
    # beat 1 presented at 15, acked at 25; beat 2 presented at 35 (addr 3 by
    # then) and acked the same cycle; stb is low at 45 so no third beat.
    assert [(t.addr, t.t_start, t.t_end) for t in txns] == [(1, 15, 25), (3, 35, 35)]


def test_ahb_incr_burst(w):
    clk = _clock(w, [10 * i + 5 for i in range(12)], period=10)
    hready = _sig(w, "hready", 1, [(0, "1")])
    htrans = _sig(w, "htrans", 2, [(0, "00"), (10, "10"), (20, "11"), (50, "00")])   # NONSEQ + 3 SEQ
    haddr = _sig(w, "haddr", 8, [(0, "00000000")] + [(10 + 10 * i, format(0x40 + 4 * i, "08b")) for i in range(4)])
    hwrite = _sig(w, "hwrite", 1, [(0, "1")])
    hwdata = _sig(w, "hwdata", 8, [(0, "00000000")] + [(20 + 10 * i, format(i + 1, "08b")) for i in range(4)])
    vcd = _VCD(clk, hready, htrans, haddr, hwrite, hwdata)
    tags = {"clock": "top.clk", "ahb.hready": "top.hready", "ahb.htrans": "top.htrans",
            "ahb.haddr": "top.haddr", "ahb.hwrite": "top.hwrite", "ahb.hwdata": "top.hwdata"}
    txns = w.AHBDecoder(vcd, tags).decode_all()
    assert len(txns) == 1
    assert (txns[0].addr, txns[0].length, txns[0].data) == (0x40, 4, [1, 2, 3, 4])
