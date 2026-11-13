"""Synthetic waveforms used by the offline evaluation cases.

These deliberately mirror the shapes used in tests/test_decoders.py, but live
in a separate module so the eval runner can build cases from JSON without
importing test code.
"""

import numpy as np


def pack(channels):
    """{channel: sequence of 0/1} -> uint32 samples, bit N = channel N."""
    lengths = {len(v) for v in channels.values()}
    assert len(lengths) == 1, "all channels must have the same length"
    out = np.zeros(lengths.pop(), dtype=np.uint32)
    for ch, bits in channels.items():
        out |= np.asarray(bits, dtype=np.uint32) << np.uint32(ch)
    return out


def uart(data, baud, sample_rate, *, gap_bits=2.0, data_bits=8, parity="none",
         stop_bits=1, invert=False, bad_parity=(), bad_stop=()):
    """LSB-first UART waveform; bit edges may fall on fractional samples."""
    if isinstance(data, str):
        data = data.encode()
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
    return levels[np.searchsorted(ends, t, side="right")]


def spi(frames, mode, half=6):
    """One list of (mosi, miso) pairs per CS assertion, MSB first."""
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
        emit(half, idle, 0, 0, 0)
        for mo_byte, mi_byte in frame:
            for bit in range(7, -1, -1):
                mo, mi = (mo_byte >> bit) & 1, (mi_byte >> bit) & 1
                if cpha == 0:
                    emit(half, idle, mo, mi, 0)
                    emit(half, active, mo, mi, 0)
                else:
                    emit(half, active, mo, mi, 0)
                    emit(half, idle, mo, mi, 0)
        emit(half, idle, 0, 0, 0)
        emit(4 * half, idle, 0, 0, 1)
    return sclk, mosi, miso, cs


def i2c(messages, half=8, nack_last=False):
    """One list of (addr7, "R"|"W", data) parts per START..STOP."""
    scl, sda = [], []
    q = half // 2

    def emit(n, c, d):
        scl.extend([c] * n)
        sda.extend([d] * n)

    def clock_bit(b):
        emit(q, 0, sda[-1])
        emit(q, 0, b)
        emit(half, 1, b)

    emit(4 * half, 1, 1)
    for parts in messages:
        for k, (addr, rw, data) in enumerate(parts):
            if k:
                emit(q, 0, sda[-1])
                emit(q, 0, 1)
                emit(half, 1, 1)
            emit(half, 1, 0)
            frame = [(addr << 1) | (1 if rw == "R" else 0)] + list(data)
            for j, byte in enumerate(frame):
                for bit in range(7, -1, -1):
                    clock_bit((byte >> bit) & 1)
                nack = rw == "R" and j > 0 and j == len(frame) - 1
                if nack_last and rw == "W" and j == len(frame) - 1:
                    nack = True
                clock_bit(1 if nack else 0)
        emit(q, 0, sda[-1])
        emit(q, 0, 0)
        emit(half, 1, 0)
        emit(half, 1, 1)
        emit(4 * half, 1, 1)
    return scl, sda


def pwm(sample_rate, duration_ms, freq_hz, duty_pct, jitter_pct=0.0, seed=0):
    n = int(sample_rate * duration_ms / 1000)
    period = sample_rate / float(freq_hz)
    if not jitter_pct:
        t = np.arange(n)
        return ((t % period) < period * duty_pct / 100).astype(np.uint32)
    rng = np.random.default_rng(seed)
    wave = np.zeros(n, dtype=np.uint32)
    jitter = period * jitter_pct / 100
    t = 0
    while t < n:
        period_i = max(1.0, period + rng.uniform(-jitter, jitter))
        high = max(1, min(n - t, int(period_i * duty_pct / 100)))
        low = max(1, int(period_i - high))
        wave[t:t + high] = 1
        t += high + low
    return wave


def build(case):
    """Build (samples, sample_rate) for a JSON case."""
    rate = int(case["sample_rate"])
    kind = case["kind"]
    if kind == "uart":
        raw = case.get("data")
        data = raw.encode() if isinstance(raw, str) else bytes.fromhex(case["data_hex"])
        wave = uart(data, case["baud"], rate,
                    gap_bits=case.get("gap_bits", 2.0),
                    data_bits=case.get("data_bits", 8),
                    parity=case.get("parity", "none"),
                    stop_bits=case.get("stop_bits", 1),
                    invert=case.get("invert", False),
                    bad_parity=set(case.get("bad_parity", [])),
                    bad_stop=set(case.get("bad_stop", [])))
        return pack({case["channel"]: wave}), rate
    if kind == "spi":
        sclk, mosi, miso, cs = spi(case["frames"], case["mode"],
                                   half=case.get("half", 6))
        channels = {case["sclk"]: sclk, case["mosi"]: mosi, case["cs"]: cs}
        if case.get("miso") is not None:
            channels[case["miso"]] = miso
        return pack(channels), rate
    if kind == "i2c":
        scl, sda = i2c(case["messages"], half=case.get("half", 8),
                       nack_last=case.get("nack_last", False))
        return pack({case["sda"]: sda, case["scl"]: scl}), rate
    if kind == "pwm":
        wave = pwm(rate, case["duration_ms"], case["freq_hz"], case["duty_pct"],
                   jitter_pct=case.get("jitter_pct", 0.0), seed=case.get("seed", 0))
        return pack({case["channel"]: wave}), rate
    if kind == "idle":
        n = int(rate * case["duration_ms"] / 1000)
        return np.ones(n, dtype=np.uint32), rate
    if kind == "noise":
        n = int(rate * case["duration_ms"] / 1000)
        rng = np.random.default_rng(case.get("seed", 0))
        bits = rng.integers(0, 2, size=n).astype(np.uint32)
        return bits, rate
    raise ValueError(f"unknown case kind: {kind!r}")
