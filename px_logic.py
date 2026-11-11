# SPDX-License-Identifier: MIT
"""
PX Logic 32 — AI-callable Python wrapper around pxlobster CLI.

pxlobster handles USB/FPGA/hardware.
This layer handles capture orchestration + signal analysis + protocol decoding.

Quick start:
    from px_logic import LogicAnalyzer
    la = LogicAnalyzer()
    la.prime()
    data = la.capture(rate=20_000_000, duration_ms=100)
    la.measure_frequency(data, channel=0)
"""

import subprocess
import sys
import os
import shutil
import tempfile
import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_EXE_NAME = "pxlobster.exe" if sys.platform == "win32" else "pxlobster"

def _find_pxlobster():
    local = os.path.join(_HERE, _EXE_NAME)
    if os.path.isfile(local):
        return local
    found = shutil.which("pxlobster")
    if found:
        return found
    raise FileNotFoundError(
        f"pxlobster not found. Place {_EXE_NAME} next to this script or add to PATH.")


class LogicAnalyzer:
    SUPPORTED_RATES = [
        1_000_000_000, 500_000_000, 250_000_000, 125_000_000,
        800_000_000, 400_000_000, 200_000_000, 100_000_000,
        50_000_000, 25_000_000, 20_000_000, 10_000_000,
        5_000_000, 4_000_000, 2_000_000, 1_000_000,
        500_000, 400_000, 200_000, 100_000,
        50_000, 40_000, 20_000, 10_000, 5_000, 2_000,
    ]

    def __init__(self, exe_path=None, channels=32):
        self.exe = exe_path or _find_pxlobster()
        self.channels = channels
        self.bytes_per_sample = 4 if channels > 16 else 2

    def _run(self, args, timeout=60):
        r = subprocess.run([self.exe] + args,
                           capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0 and r.stderr.strip():
            print(f"[LA] stderr: {r.stderr.strip()}", file=sys.stderr)
        return r

    def scan(self):
        r = self._run(["--scan"])
        print(r.stdout.strip())
        return r.returncode == 0

    def prime(self):
        r = self._run(["--prime-fw"], timeout=30)
        return r.returncode == 0

    def stop(self):
        self._run(["--stop"], timeout=5)

    def nearest_rate(self, target_hz):
        return min(self.SUPPORTED_RATES, key=lambda r: abs(r - target_hz))

    def capture(self, rate=20_000_000, duration_ms=100, mode="buffer",
                triggers=None, decode_cross=True):
        """Capture logic data.

        Args:
            rate: sample rate in Hz (auto-snapped to nearest supported value)
            duration_ms: capture duration
            mode: "buffer", "stream", or "loop"
            triggers: trigger spec string, e.g. "0=r" (CH0 rising edge)
            decode_cross: decode DMA cross-lane format to standard packed samples

        Returns:
            numpy array shape (n_samples,) dtype uint32 — bit N = channel N value
        """
        rate = self.nearest_rate(rate)
        fd, path = tempfile.mkstemp(suffix=".bin")
        os.close(fd)

        cmd = ["-o", path, "--format", "bin",
               "--time", str(duration_ms),
               "--samplerate", str(rate),
               "--mode", mode]
        if decode_cross:
            cmd.append("--decode-cross")
        if triggers:
            cmd.extend(["-t", triggers])

        timeout_sec = max(30, duration_ms / 1000 * 5 + 10)
        try:
            r = self._run(cmd, timeout=timeout_sec)
            if r.returncode != 0:
                return np.array([], dtype=np.uint32)
            raw = np.fromfile(path, dtype=np.uint8)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

        if self.bytes_per_sample == 4:
            samples = np.frombuffer(raw, dtype=np.uint32)
        else:
            samples = np.frombuffer(raw, dtype=np.uint16).astype(np.uint32)

        print(f"[LA] {len(samples):,} samples @ {rate/1e6:g}MHz, {duration_ms}ms")
        return samples

    def channel_bits(self, samples, channel):
        return (samples >> channel) & 1

    def measure_frequency(self, samples, channel=0, sample_rate=20_000_000):
        ch = self.channel_bits(samples, channel)
        rising = np.where(np.diff(ch.astype(np.int8)) == 1)[0]
        if len(rising) < 2:
            print(f"[LA] CH{channel}: no periodic signal detected")
            return 0.0
        period = np.mean(np.diff(rising))
        freq = sample_rate / period
        duty = np.mean(ch) * 100
        jitter_pct = np.std(np.diff(rising)) / period * 100
        print(f"[LA] CH{channel}: {freq:,.1f} Hz, {duty:.1f}% duty, jitter {jitter_pct:.2f}%")
        return freq

    def summary(self, samples, sample_rate=20_000_000):
        """Print summary of all active channels."""
        for ch in range(self.channels):
            sig = self.channel_bits(samples, ch)
            edges = int(np.sum(np.abs(np.diff(sig.astype(np.int8)))))
            if edges > 2:
                rising = np.where(np.diff(sig.astype(np.int8)) == 1)[0]
                freq = sample_rate / np.mean(np.diff(rising)) if len(rising) >= 2 else 0
                print(f"  CH{ch:02d}: {freq:>10,.0f} Hz  {np.mean(sig)*100:5.1f}% duty  {edges:>8,} edges")


# ═══════════════════════════════════
# Protocol decoders
# ═══════════════════════════════════

def decode_uart(samples, channel, baudrate, sample_rate):
    """Decode UART (8N1, LSB first, idle high). Returns list of byte values."""
    sig = (samples >> channel) & 1
    spb = sample_rate / baudrate
    result = []
    i = 0
    while i < len(sig) - int(spb * 10):
        if sig[i] == 1 and sig[i + 1] == 0:
            val = 0
            for bit in range(8):
                # skip the start bit, sample each data bit at its centre
                pos = int(i + 1 + spb * (bit + 1.5))
                if pos < len(sig):
                    val |= int(sig[pos]) << bit
            result.append(val)
            # resume at the stop-bit centre so back-to-back frames tolerate baud error
            i += int(spb * 9.5)
        else:
            i += 1
    return result


def decode_spi(samples, sclk_ch, mosi_ch, miso_ch=None, cs_ch=None, cpol=0, cpha=0):
    """Decode SPI. Returns list of (mosi_byte, miso_byte) tuples."""
    sclk = (samples >> sclk_ch) & 1
    mosi = (samples >> mosi_ch) & 1
    miso = (samples >> miso_ch) & 1 if miso_ch is not None else np.zeros_like(mosi)
    edge = 1 if (cpol == 0) != (cpha == 1) else -1
    edges = np.where(np.diff(sclk.astype(np.int8)) == edge)[0] + 1
    result, mo, mi, bits = [], 0, 0, 0
    for idx in edges:
        if cs_ch is not None and ((samples[idx] >> cs_ch) & 1) == 1:
            if bits:
                result.append((mo, mi))
                mo = mi = bits = 0
            continue
        mo = (mo << 1) | int(mosi[idx])
        mi = (mi << 1) | int(miso[idx])
        bits += 1
        if bits == 8:
            result.append((mo, mi))
            mo = mi = bits = 0
    return result


def decode_i2c(samples, sda_ch, scl_ch):
    """Decode I2C. Returns list of transaction dicts."""
    sda = (samples >> sda_ch) & 1
    scl = (samples >> scl_ch) & 1
    txns, cur = [], None
    bval, bcnt = 0, 0
    for i in range(1, len(sda)):
        if scl[i] == 1 and sda[i - 1] == 1 and sda[i] == 0:  # START
            if cur and cur['data']:
                txns.append(cur)
            cur = {'addr': 0, 'rw': '?', 'data': [], 'acks': []}
            bval = bcnt = 0
        elif scl[i] == 1 and sda[i - 1] == 0 and sda[i] == 1:  # STOP
            if cur:
                txns.append(cur)
                cur = None
        elif cur is not None and scl[i - 1] == 0 and scl[i] == 1:  # SCL rising
            if bcnt < 8:
                bval = (bval << 1) | int(sda[i])
                bcnt += 1
            elif bcnt == 8:
                ack = int(sda[i]) == 0
                if not cur['data'] and cur['rw'] == '?':
                    cur['addr'] = bval >> 1
                    cur['rw'] = 'R' if (bval & 1) else 'W'
                else:
                    cur['data'].append(bval)
                cur['acks'].append(ack)
                bval = bcnt = 0
    if cur and cur['data']:
        txns.append(cur)
    return txns


if __name__ == "__main__":
    la = LogicAnalyzer()
    if "--scan" in sys.argv:
        la.scan()
    elif "--prime" in sys.argv:
        la.prime()
    else:
        rate = 20_000_000
        dur = 200
        for i, a in enumerate(sys.argv[1:], 1):
            if a == "--rate" and i < len(sys.argv) - 1:
                rate = int(float(sys.argv[i + 1]))
            elif a == "--time" and i < len(sys.argv) - 1:
                dur = int(sys.argv[i + 1])
        la.prime()
        data = la.capture(rate=rate, duration_ms=dur)
        if len(data) > 0:
            la.summary(data, sample_rate=la.nearest_rate(rate))
