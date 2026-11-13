"""Tests for the decoders and signal analysis on synthetic waveforms.

No hardware or pxlobster binary is needed. Run from the repo root:

    python -m unittest discover -s tests -v
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from px_logic import (LogicAnalyzer, decode_i2c, decode_spi, decode_uart,  # noqa: E402
                      load_capture, main)


def pack(channels):
    """{channel: sequence of 0/1} -> uint32 samples, bit N = channel N."""
    lengths = {len(v) for v in channels.values()}
    assert len(lengths) == 1, "all channels must have the same length"
    out = np.zeros(lengths.pop(), dtype=np.uint32)
    for ch, bits in channels.items():
        out |= np.asarray(bits, dtype=np.uint32) << np.uint32(ch)
    return out


def uart_signal(data, baud, sample_rate, gap_bits=2.0, data_bits=8, parity="none",
                stop_bits=1, invert=False, bad_parity=(), bad_stop=()):
    """LSB first, idle high unless invert. Bit edges fall on fractional sample times.

    bad_parity / bad_stop: indices of frames sent with a flipped parity bit or
    a low stop bit. Returns (wave, first sample of each start bit).
    """
    segs, start_segs = [(3.0, 1)], []
    for n, byte in enumerate(data):
        start_segs.append(len(segs))
        segs.append((1.0, 0))
        bits = [(byte >> i) & 1 for i in range(data_bits)]
        segs += [(1.0, b) for b in bits]
        if parity != "none":
            p = sum(bits) % 2 if parity == "even" else 1 - sum(bits) % 2
            segs.append((1.0, p ^ (n in bad_parity)))
        segs.append((1.0, 0 if n in bad_stop else 1))
        segs += [(1.0, 1)] * (stop_bits - 1)
        if gap_bits:
            segs.append((gap_bits, 1))
    segs.append((3.0, 1))
    ends = np.cumsum([d for d, _ in segs]) * (sample_rate / baud)
    levels = np.array([v for _, v in segs], dtype=np.uint32)
    if invert:
        levels ^= 1
    t = np.arange(int(ends[-1]))
    starts = [int(np.ceil(ends[k - 1])) for k in start_segs]
    return levels[np.searchsorted(ends, t, side="right")], starts


def uart_wave(data, baud, sample_rate, gap_bits=2.0, **options):
    return uart_signal(data, baud, sample_rate, gap_bits, **options)[0]


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

    def test_glitch_is_not_a_start_bit(self):
        line = np.ones(5_000, dtype=np.uint32)
        line[1_000] = 0  # one-sample spike on an idle line
        self.assertEqual(self.decode(line, 115_200, 10_000_000), b"")


class UartOptionsTest(unittest.TestCase):
    RATE = 10_000_000

    def decode(self, wave, baud, **options):
        return decode_uart(pack({5: wave}), channel=5, baudrate=baud,
                           sample_rate=self.RATE, **options)

    def test_sbus_inverted_8e2_back_to_back(self):
        data = bytes([0x0F] + list(range(1, 23)) + [0x00, 0x00])  # one 25-byte SBUS frame
        sbus = {"parity": "even", "stop_bits": 2, "invert": True}
        wave = uart_wave(data, 100_000, self.RATE, gap_bits=0, **sbus)
        frames = self.decode(wave, 100_000, detail=True, **sbus)
        self.assertEqual(bytes(f["value"] for f in frames), data)
        self.assertFalse(any(f["framing_error"] or f["parity_error"] for f in frames))

    def test_7_data_bits_odd_parity(self):
        data = b"PX Logic"
        wave = uart_wave(data, 115_200, self.RATE, data_bits=7, parity="odd")
        self.assertEqual(bytes(self.decode(wave, 115_200, data_bits=7, parity="odd")), data)

    def test_two_stop_bits_back_to_back_with_transmitter_2pct_fast(self):
        data = b"\x00\xff\x55\xaa"
        wave = uart_wave(data, 115_200 * 1.02, self.RATE, gap_bits=0, stop_bits=2)
        self.assertEqual(bytes(self.decode(wave, 115_200, stop_bits=2)), data)

    def test_parity_and_framing_errors_are_flagged(self):
        data = b"\x01\x02\x03"
        wave = uart_wave(data, 115_200, self.RATE, parity="even", bad_parity={1}, bad_stop={2})
        frames = self.decode(wave, 115_200, parity="even", detail=True)
        self.assertEqual([f["value"] for f in frames], list(data))
        self.assertEqual([f["parity_error"] for f in frames], [False, True, False])
        self.assertEqual([f["framing_error"] for f in frames], [False, False, True])

    def test_detail_positions_and_times(self):
        wave, starts = uart_signal(b"abc", 115_200, self.RATE)
        frames = self.decode(wave, 115_200, detail=True)
        self.assertEqual([f["start"] for f in frames], starts)
        span = int(self.RATE / 115_200 * 10)
        for f in frames:
            self.assertEqual(f["end"], f["start"] + span)
            self.assertAlmostEqual(f["time_s"], f["start"] / self.RATE)
        json.dumps(frames)  # plain Python types only

    def test_invalid_options(self):
        wave = uart_wave(b"x", 115_200, self.RATE)
        for options in ({"parity": "mark"}, {"stop_bits": 1.5}, {"data_bits": 4}):
            with self.subTest(**options), self.assertRaises(ValueError):
                self.decode(wave, 115_200, **options)


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

    def test_detail_positions_and_times(self):
        sclk, mosi, miso, cs = spi_wave(self.FRAMES, 0)
        samples = pack({2: sclk, 3: mosi, 4: miso, 5: cs})
        words = decode_spi(samples, sclk_ch=2, mosi_ch=3, miso_ch=4, cs_ch=5,
                           sample_rate=1_000_000, detail=True)
        self.assertEqual([(w["mosi"], w["miso"]) for w in words], self.EXPECTED)
        # mode 0, 12 samples per bit: the first rising edge follows 24 idle,
        # 6 CS-setup and 6 low samples; a word spans 7 bits between edges
        self.assertEqual([(w["start"], w["end"]) for w in words],
                         [(36, 120), (132, 216), (264, 348)])
        self.assertAlmostEqual(words[0]["time_s"], 36e-6)
        json.dumps(words)  # plain Python types only
        no_rate = decode_spi(samples, sclk_ch=2, mosi_ch=3, miso_ch=4, cs_ch=5, detail=True)
        self.assertIsNone(no_rate[0]["time_s"])

    def test_capture_starting_mid_word_resyncs_at_cs_release(self):
        sclk, mosi, miso, cs = spi_wave(self.FRAMES + [[(0x12, 0x34)]], 0)
        cut = 24 + 6 + 3 * 12  # idle, CS setup and the first 3 bits of byte 0
        samples = pack({2: sclk, 3: mosi, 4: miso, 5: cs})[cut:]
        words = decode_spi(samples, sclk_ch=2, mosi_ch=3, miso_ch=4, cs_ch=5, detail=True)
        # the cut frame is misaligned and ends in a 5-bit remainder; later frames are intact
        self.assertEqual([(w["mosi"], w["miso"], w["bits"]) for w in words],
                         [(0x28, 0xE7, 8), (0x00, 0x1F, 5), (0x81, 0x7E, 8), (0x12, 0x34, 8)])

    def test_16_bit_words(self):
        sclk, mosi, miso, cs = spi_wave([[(0xA5, 0x3C), (0x00, 0xFF)]], 0)
        samples = pack({2: sclk, 3: mosi, 4: miso, 5: cs})
        words = decode_spi(samples, sclk_ch=2, mosi_ch=3, miso_ch=4, cs_ch=5,
                           word_bits=16, detail=True)
        self.assertEqual([(w["mosi"], w["miso"], w["bits"]) for w in words],
                         [(0xA500, 0x3CFF, 16)])

    def test_lsb_first(self):
        sclk, mosi, miso, _ = spi_wave([[(0x01, 0x80)]], 0)
        samples = pack({2: sclk, 3: mosi, 4: miso})
        self.assertEqual(decode_spi(samples, sclk_ch=2, mosi_ch=3, miso_ch=4,
                                    lsb_first=True), [(0x80, 0x01)])

    def test_invalid_word_bits(self):
        sclk, mosi, miso, _ = spi_wave([[(0x00, 0x00)]], 0)
        samples = pack({2: sclk, 3: mosi, 4: miso})
        for bits in (0, 33):
            with self.subTest(word_bits=bits), self.assertRaises(ValueError):
                decode_spi(samples, sclk_ch=2, mosi_ch=3, miso_ch=4, word_bits=bits)


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

    def test_detail_positions_and_times(self):
        scl, sda = i2c_wave([[(0x68, "W", [0x75]), (0x68, "R", [0x71, 0x00])]])
        write, read = decode_i2c(pack({0: sda, 1: scl}), sda_ch=0, scl_ch=1,
                                 sample_rate=1_000_000, detail=True)
        # START after 32 idle samples; each bit takes 16 samples
        self.assertEqual((write["start"], write["end"]), (32, 344))
        self.assertEqual((read["start"], read["end"]), (344, 800))  # repeated START, then STOP
        self.assertAlmostEqual(write["time_s"], 32e-6)
        json.dumps([write, read])  # plain Python types only

    def test_address_only_part_before_repeated_start(self):
        got = self.decode([[(0x50, "W", []), (0x50, "R", [0x01])]])
        self.assertEqual(got, [
            (0x50, "W", [], [True]),
            (0x50, "R", [0x01], [True, False]),
        ])

    def test_capture_ending_after_the_address(self):
        scl, sda = i2c_wave([[(0x50, "W", [0x01])]])
        cut = 32 + 8 + 9 * 16  # idle, START, then the address byte and its ACK
        [txn] = decode_i2c(pack({0: sda[:cut], 1: scl[:cut]}), sda_ch=0, scl_ch=1, detail=True)
        self.assertEqual((txn["addr"], txn["rw"], txn["data"], txn["end"]), (0x50, "W", [], None))

    def test_start_stop_without_a_byte_is_ignored(self):
        scl = [1] * 40
        sda = [1] * 10 + [0] * 10 + [1] * 20  # START at 10, STOP at 20
        self.assertEqual(decode_i2c(pack({0: sda, 1: scl}), sda_ch=0, scl_ch=1), [])

    def test_plain_result_has_no_positions(self):
        scl, sda = i2c_wave([[(0x3C, "W", [0x00])]])
        [txn] = decode_i2c(pack({0: sda, 1: scl}), sda_ch=0, scl_ch=1)
        self.assertEqual(set(txn), {"addr", "rw", "data", "acks"})


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

    def test_measure_returns_stats(self):
        rate, period = 1_000_000, 2_500  # 400 Hz
        t = np.arange(rate // 20)  # 50 ms = 20 periods
        pwm = (t % period) < period // 4  # 25 % duty
        m = self.la.measure(pack({0: pwm}), channel=0, sample_rate=rate)
        self.assertEqual(m["channel"], 0)
        self.assertAlmostEqual(m["freq_hz"], 400.0, places=6)
        self.assertAlmostEqual(m["duty_pct"], 25.0, places=6)
        self.assertAlmostEqual(m["jitter_pct"], 0.0, places=6)
        self.assertEqual(m["edges"], 39)  # 20 falling + 19 rising
        # the capture starts inside a pulse, so 19 complete pulses remain
        self.assertEqual((m["high_us"], m["high_min_us"], m["high_max_us"]), (625.0, 625.0, 625.0))

    def test_servo_pulse_widths(self):
        rate = 1_000_000  # 1 us resolution
        pulses = [np.r_[np.ones(w), np.zeros(20_000 - w)] for w in (1_000, 1_500, 2_000) * 2]
        sig = np.concatenate([np.zeros(100)] + pulses)  # 50 Hz; starts low, so all pulses count
        m = self.la.measure(pack({0: sig}), channel=0, sample_rate=rate)
        self.assertAlmostEqual(m["freq_hz"], 50.0)
        self.assertAlmostEqual(m["high_us"], 1_500.0)
        self.assertAlmostEqual(m["high_min_us"], 1_000.0)
        self.assertAlmostEqual(m["high_max_us"], 2_000.0)

    def test_pulses_cut_off_by_the_capture_are_ignored(self):
        cut = np.r_[np.ones(300), np.zeros(700)]
        m = self.la.measure(pack({0: cut}), channel=0, sample_rate=1_000_000)
        self.assertEqual(m["high_us"], 0.0)
        sig = np.r_[cut, np.ones(500), np.zeros(500), np.ones(200)]
        m = self.la.measure(pack({0: sig}), channel=0, sample_rate=1_000_000)
        self.assertEqual((m["high_us"], m["high_min_us"], m["high_max_us"]), (500.0, 500.0, 500.0))

    def test_summary_returns_only_active_channels(self):
        rate = 1_000_000
        t = np.arange(rate // 20)
        samples = pack({0: (t % 2_500) < 625, 3: np.zeros_like(t), 7: (t % 1_000) < 500})
        with contextlib.redirect_stdout(io.StringIO()):
            active = self.la.summary(samples, sample_rate=rate)
        self.assertEqual([m["channel"] for m in active], [0, 7])
        self.assertAlmostEqual(active[1]["freq_hz"], 1000.0, places=6)

    def test_summary_min_edges_includes_one_shot_channels(self):
        sig = np.zeros(1_000, dtype=np.uint32)
        sig[500:] = 1  # a single rising edge
        samples = pack({2: sig})
        with contextlib.redirect_stdout(io.StringIO()):
            default = self.la.summary(samples, sample_rate=1_000_000)
            one_shot = self.la.summary(samples, sample_rate=1_000_000, min_edges=0)
        self.assertEqual(default, [])
        self.assertEqual([m["channel"] for m in one_shot], [2])
        self.assertEqual(one_shot[0]["edges"], 1)

    def test_nearest_rate(self):
        self.assertEqual(self.la.nearest_rate(19_000_000), 20_000_000)
        self.assertEqual(self.la.nearest_rate(1.2e9), 1_000_000_000)
        self.assertEqual(self.la.nearest_rate(2_100), 2_000)

    def test_analysis_needs_no_pxlobster(self):
        la = LogicAnalyzer()  # the binary is resolved only when a device is used
        t = np.arange(2_000)
        m = la.measure(pack({0: (t % 1_000) >= 500}), channel=0, sample_rate=1_000_000)
        self.assertAlmostEqual(m["freq_hz"], 1_000.0)


class CliDecodeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # Point PXLOBSTER at a missing file: --input must never need the binary.
        self.env = {"PXLOBSTER": os.path.join(self.tmp, "missing-pxlobster")}

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def write(self, samples, name="cap.bin"):
        path = os.path.join(self.tmp, name)
        samples.tofile(path)
        return path

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_uart_input_json(self):
        path = self.write(pack({5: uart_wave(b"Hi", 115_200, 20_000_000)}))
        code, out, err = self.run_main([
            "--json", "--input", path, "--rate", "20e6",
            "--decode", "uart", "--channel", "5", "--baud", "115200"])
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(result["protocol"], "uart")
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["frames"], [ord("H"), ord("i")])
        self.assertFalse(result["truncated"])

    def test_uart_detail_and_limit(self):
        path = self.write(pack({5: uart_wave(b"abcd", 115_200, 20_000_000)}))
        code, out, err = self.run_main([
            "--json", "--input", path, "--rate", "20e6", "--decode", "uart",
            "--channel", "5", "--baud", "115200", "--detail", "--limit", "2"])
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(result["count"], 4)
        self.assertTrue(result["truncated"])
        self.assertEqual([f["value"] for f in result["frames"]], [ord("a"), ord("b")])
        self.assertIn("framing_error", result["frames"][0])
        self.assertIsInstance(result["frames"][0]["time_s"], float)

    def test_missing_uart_args_is_json_error(self):
        path = self.write(pack({5: uart_wave(b"x", 115_200, 20_000_000)}))
        code, out, _ = self.run_main([
            "--json", "--input", path, "--decode", "uart", "--channel", "5"])
        self.assertEqual(code, 1)
        self.assertIn("--baud", json.loads(out)["error"])

    def test_spi_16_bit_input_json(self):
        sclk, mosi, miso, cs = spi_wave([[(0xA5, 0x3C), (0x00, 0xFF)]], 0)
        path = self.write(pack({2: sclk, 3: mosi, 4: miso, 5: cs}))
        code, out, err = self.run_main([
            "--json", "--input", path, "--rate", "1e6", "--decode", "spi",
            "--sclk", "2", "--mosi", "3", "--miso", "4", "--cs", "5",
            "--word-bits", "16"])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["frames"], [[0xA500, 0x3CFF]])

    def test_i2c_input_json(self):
        scl, sda = i2c_wave([[(0x3C, "W", [0x00, 0xAF])]])
        path = self.write(pack({0: sda, 1: scl}))
        code, out, err = self.run_main([
            "--json", "--input", path, "--rate", "1e6", "--decode", "i2c",
            "--sda", "0", "--scl", "1"])
        self.assertEqual(code, 0, err)
        [frame] = json.loads(out)["frames"]
        self.assertEqual((frame["addr"], frame["rw"], frame["data"]),
                         (0x3C, "W", [0x00, 0xAF]))

    def test_input_rejects_save_and_decode_rejects_scan(self):
        path = self.write(pack({5: uart_wave(b"x", 115_200, 20_000_000)}))
        code, out, _ = self.run_main(["--json", "--input", path, "--save", "x.bin"])
        self.assertEqual(code, 1)
        self.assertIn("--save", json.loads(out)["error"])
        code, out, _ = self.run_main(["--json", "--scan", "--decode", "uart"])
        self.assertEqual(code, 1)
        self.assertIn("--scan", json.loads(out)["error"])

    def test_load_capture_reads_16_channel_words(self):
        path = os.path.join(self.tmp, "cap16.bin")
        np.array([1, 0x8000], dtype=np.uint16).tofile(path)
        data = load_capture(path, channels=16)
        self.assertEqual(data.dtype, np.uint32)
        self.assertEqual(data.tolist(), [1, 0x8000])

    def test_min_edges_includes_one_shot_channel(self):
        sig = np.zeros(1_000, dtype=np.uint32)
        sig[500:] = 1
        path = self.write(pack({3: sig}))
        code, out, err = self.run_main(["--json", "--input", path, "--rate", "1e6"])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["active_channels"], [])
        code, out, err = self.run_main(["--json", "--input", path, "--rate", "1e6",
                                        "--min-edges", "0"])
        self.assertEqual(code, 0, err)
        [active] = json.loads(out)["active_channels"]
        self.assertEqual((active["channel"], active["edges"]), (3, 1))


if __name__ == "__main__":
    unittest.main()
