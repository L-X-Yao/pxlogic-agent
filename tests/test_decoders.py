"""Tests for the decoders and signal analysis on synthetic waveforms.

No hardware or pxlobster binary is needed. Run from the repo root:

    python -m unittest discover -s tests -v
"""

import contextlib
import io
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from px_logic import LogicAnalyzer, decode_i2c, decode_spi, decode_uart  # noqa: E402


def pack(channels):
    """{channel: sequence of 0/1} -> uint32 samples, bit N = channel N."""
    lengths = {len(v) for v in channels.values()}
    assert len(lengths) == 1, "all channels must have the same length"
    out = np.zeros(lengths.pop(), dtype=np.uint32)
    for ch, bits in channels.items():
        out |= np.asarray(bits, dtype=np.uint32) << np.uint32(ch)
    return out


def uart_wave(data, baud, sample_rate, gap_bits=2.0):
    """8N1, LSB first, idle high. Bit edges fall on fractional sample times."""
    segs = [(3.0, 1)]
    for byte in data:
        segs.append((1.0, 0))
        segs += [(1.0, (byte >> i) & 1) for i in range(8)]
        segs.append((1.0, 1))
        if gap_bits:
            segs.append((gap_bits, 1))
    segs.append((3.0, 1))
    ends = np.cumsum([d for d, _ in segs]) * (sample_rate / baud)
    levels = np.array([v for _, v in segs], dtype=np.uint32)
    t = np.arange(int(ends[-1]))
    return levels[np.searchsorted(ends, t, side="right")]


def spi_wave(frames, mode, half=6):
    """frames: one list of (mosi, miso) byte pairs per CS assertion. MSB first."""
    cpol, cpha = (mode >> 1) & 1, mode & 1
    idle, active = cpol, 1 - cpol
    sclk, mosi, miso, cs = [], [], [], []

    def emit(n, c, mo, mi, s):
        sclk.extend([c] * n)
        mosi.extend([mo] * n)
        miso.extend([mi] * n)
        cs.extend([s] * n)

    emit(4 * half, idle, 0, 0, 1)
    for frame in frames:
        emit(half, idle, 0, 0, 0)  # CS asserted, clock still idle
        for mo_byte, mi_byte in frame:
            for bit in range(7, -1, -1):
                mo, mi = (mo_byte >> bit) & 1, (mi_byte >> bit) & 1
                if cpha == 0:  # data valid before the leading edge
                    emit(half, idle, mo, mi, 0)
                    emit(half, active, mo, mi, 0)
                else:  # data changes on the leading edge
                    emit(half, active, mo, mi, 0)
                    emit(half, idle, mo, mi, 0)
        emit(half, idle, 0, 0, 0)
        emit(4 * half, idle, 0, 0, 1)  # CS released
    return sclk, mosi, miso, cs


def i2c_wave(messages, half=8):
    """messages: one list of (addr7, "R"|"W", data) parts per START..STOP.

    Parts after the first are sent with a repeated START. The slave ACKs the
    address and written bytes; the master NACKs the last byte it reads.
    """
    scl, sda = [], []
    q = half // 2

    def emit(n, c, d):
        scl.extend([c] * n)
        sda.extend([d] * n)

    def clock_bit(b):
        emit(q, 0, sda[-1])  # SCL falls, SDA held
        emit(q, 0, b)  # SDA changes while SCL is low
        emit(half, 1, b)  # SCL high: bit is sampled

    emit(4 * half, 1, 1)  # idle bus
    for parts in messages:
        for k, (addr, rw, data) in enumerate(parts):
            if k:  # repeated START: release SDA while SCL is low, then raise SCL
                emit(q, 0, sda[-1])
                emit(q, 0, 1)
                emit(half, 1, 1)
            emit(half, 1, 0)  # START: SDA falls while SCL is high
            frame = [(addr << 1) | (1 if rw == "R" else 0)] + list(data)
            for j, byte in enumerate(frame):
                for bit in range(7, -1, -1):
                    clock_bit((byte >> bit) & 1)
                nack = rw == "R" and j > 0 and j == len(frame) - 1
                clock_bit(1 if nack else 0)
        emit(q, 0, sda[-1])  # STOP: SDA low while SCL is low,
        emit(q, 0, 0)
        emit(half, 1, 0)  # SCL rises,
        emit(half, 1, 1)  # then SDA rises
        emit(4 * half, 1, 1)
    return scl, sda


class UartTest(unittest.TestCase):
    DATA = b"Hello, PX\x00\xff\x55\xaa"

    def decode(self, wave, baud, rate):
        return bytes(decode_uart(pack({5: wave}), channel=5, baudrate=baud, sample_rate=rate))

    def test_115200_with_idle_gaps(self):
        wave = uart_wave(self.DATA, 115_200, 20_000_000)
        self.assertEqual(self.decode(wave, 115_200, 20_000_000), self.DATA)

    def test_115200_back_to_back(self):
        wave = uart_wave(self.DATA, 115_200, 20_000_000, gap_bits=0)
        self.assertEqual(self.decode(wave, 115_200, 20_000_000), self.DATA)

    def test_back_to_back_with_transmitter_2pct_fast(self):
        wave = uart_wave(self.DATA, 115_200 * 1.02, 20_000_000, gap_bits=0)
        self.assertEqual(self.decode(wave, 115_200, 20_000_000), self.DATA)

    def test_1mbaud_at_20msps(self):
        wave = uart_wave(self.DATA, 1_000_000, 20_000_000, gap_bits=0.5)
        self.assertEqual(self.decode(wave, 1_000_000, 20_000_000), self.DATA)


class SpiTest(unittest.TestCase):
    FRAMES = [[(0xA5, 0x3C), (0x00, 0xFF)], [(0x81, 0x7E)]]
    EXPECTED = [(0xA5, 0x3C), (0x00, 0xFF), (0x81, 0x7E)]

    def test_all_modes_with_cs(self):
        for mode in range(4):
            with self.subTest(mode=mode):
                sclk, mosi, miso, cs = spi_wave(self.FRAMES, mode)
                samples = pack({2: sclk, 3: mosi, 4: miso, 5: cs})
                got = decode_spi(samples, sclk_ch=2, mosi_ch=3, miso_ch=4, cs_ch=5,
                                 cpol=mode >> 1, cpha=mode & 1)
                self.assertEqual(got, self.EXPECTED)

    def test_mode0_without_cs(self):
        sclk, mosi, miso, _ = spi_wave(self.FRAMES, 0)
        samples = pack({2: sclk, 3: mosi, 4: miso})
        self.assertEqual(decode_spi(samples, sclk_ch=2, mosi_ch=3, miso_ch=4), self.EXPECTED)


class I2cTest(unittest.TestCase):
    def decode(self, messages):
        scl, sda = i2c_wave(messages)
        txns = decode_i2c(pack({0: sda, 1: scl}), sda_ch=0, scl_ch=1)
        return [(t["addr"], t["rw"], t["data"], t["acks"]) for t in txns]

    def test_write(self):
        got = self.decode([[(0x3C, "W", [0x00, 0xAF])]])
        self.assertEqual(got, [(0x3C, "W", [0x00, 0xAF], [True, True, True])])

    def test_register_read_with_repeated_start(self):
        got = self.decode([[(0x68, "W", [0x75]), (0x68, "R", [0x71, 0x00])]])
        self.assertEqual(got, [
            (0x68, "W", [0x75], [True, True]),
            (0x68, "R", [0x71, 0x00], [True, True, False]),
        ])

    def test_two_separate_transactions(self):
        got = self.decode([[(0x50, "W", [0x01])], [(0x51, "W", [0x02, 0x03])]])
        self.assertEqual(got, [
            (0x50, "W", [0x01], [True, True]),
            (0x51, "W", [0x02, 0x03], [True, True, True]),
        ])


class AnalysisTest(unittest.TestCase):
    def setUp(self):
        # Analysis helpers only; the pxlobster CLI is never started.
        self.la = LogicAnalyzer(exe_path="pxlobster")

    def test_measure_frequency_400hz_pwm(self):
        rate, period = 1_000_000, 2_500  # 400 Hz
        t = np.arange(rate // 20)  # 50 ms
        pwm = (t % period) < period // 4  # 25 % duty
        with contextlib.redirect_stdout(io.StringIO()):
            freq = self.la.measure_frequency(pack({0: pwm}), channel=0, sample_rate=rate)
        self.assertAlmostEqual(freq, 400.0, places=6)

    def test_measure_frequency_no_signal(self):
        with contextlib.redirect_stdout(io.StringIO()):
            freq = self.la.measure_frequency(np.zeros(1000, dtype=np.uint32), channel=0)
        self.assertEqual(freq, 0.0)

    def test_nearest_rate(self):
        self.assertEqual(self.la.nearest_rate(19_000_000), 20_000_000)
        self.assertEqual(self.la.nearest_rate(1.2e9), 1_000_000_000)
        self.assertEqual(self.la.nearest_rate(2_100), 2_000)


if __name__ == "__main__":
    unittest.main()
