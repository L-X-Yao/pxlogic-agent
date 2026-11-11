# SPDX-License-Identifier: MIT
"""
PX Logic logic-analyzer toolkit for AI agents, built on the pxlobster CLI.

pxlobster handles USB/FPGA/hardware.
This layer handles capture orchestration + signal analysis + protocol decoding.

Quick start:
    from px_logic import LogicAnalyzer
    la = LogicAnalyzer()
    data = la.capture(rate=20_000_000, duration_ms=100)
    la.measure(data, channel=0, sample_rate=20_000_000)  # -> dict

Command line (with --json, stdout carries exactly one JSON object):
    pxlogic-agent --json --rate 20e6 --time 200
"""

import re
import subprocess
import sys
import os
import shutil
import tempfile
import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_EXE_NAME = "pxlobster.exe" if sys.platform == "win32" else "pxlobster"
_DEVICE_LINE = re.compile(r"([0-9A-Fa-f]{4}:[0-9A-Fa-f]{4})\s+(\S.*)")


def _find_pxlobster():
    """Locate pxlobster: $PXLOBSTER, then next to this file, then PATH."""
    env = os.environ.get("PXLOBSTER")
    if env:
        if os.path.isfile(env):
            return env
        raise FileNotFoundError(f"PXLOBSTER points to a missing file: {env}")
    local = os.path.join(_HERE, _EXE_NAME)
    if os.path.isfile(local):
        return local
    found = shutil.which("pxlobster")
    if found:
        return found
    raise FileNotFoundError(
        f"pxlobster not found. Set PXLOBSTER, place {_EXE_NAME} next to px_logic.py, "
        "or add it to PATH.")


def _parse_devices(text):
    """Parse pxlobster device lines such as "16C0:05DC  PX-Logic U2 channel 32"."""
    devices = []
    for line in text.splitlines():
        m = _DEVICE_LINE.fullmatch(line.strip())
        if m:
            devices.append({"usb_id": m.group(1).lower(), "label": m.group(2).strip()})
    return devices


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
        self.last_output = ""  # stdout of the last pxlobster call
        self.last_error = ""   # why the last pxlobster call failed, "" on success

    def _run(self, args, timeout=60):
        r = subprocess.run([self.exe] + args,
                           capture_output=True, text=True, timeout=timeout)
        err = r.stderr.strip()
        self.last_output = r.stdout
        self.last_error = (err or f"pxlobster exited with code {r.returncode}") if r.returncode else ""
        if self.last_error:
            print(f"[LA] pxlobster error: {self.last_error}", file=sys.stderr)
        return r

    def scan(self):
        r = self._run(["--scan"])
        print(r.stdout.strip())
        return r.returncode == 0

    def devices(self):
        """Supported devices as [{"usb_id": "16c0:05dc", "label": "..."}]."""
        self._run(["--scan"])
        return _parse_devices(self.last_output)

    def prime(self):
        """Load the FPGA bitstream. True only if a device reports it loaded."""
        r = self._run(["--prime-fw"], timeout=30)
        # pxlobster exits 0 even when a device is busy or the load fails
        return r.returncode == 0 and "[fw loaded]" in r.stdout

    def stop(self):
        self._run(["--stop"], timeout=5)

    def nearest_rate(self, target_hz):
        return min(self.SUPPORTED_RATES, key=lambda r: abs(r - target_hz))

    def capture(self, rate=20_000_000, duration_ms=100, mode="buffer",
                triggers=None, decode_cross=True, timeout_s=None):
        """Capture logic data.

        Args:
            rate: sample rate in Hz (auto-snapped to nearest supported value)
            duration_ms: capture duration
            mode: "buffer", "stream", or "loop"
            triggers: trigger spec string, e.g. "0=r" (CH0 rising edge)
            decode_cross: decode DMA cross-lane format to standard packed samples
            timeout_s: give up after this many seconds, e.g. when a trigger
                never fires (default: max(30, 5 x duration + 10 s))

        Returns:
            numpy array shape (n_samples,) dtype uint32 — bit N = channel N value.
            Empty on failure or timeout; the reason is in self.last_error.
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

        if timeout_s is None:
            timeout_s = max(30, duration_ms / 1000 * 5 + 10)
        try:
            try:
                r = self._run(cmd, timeout=timeout_s)
            except subprocess.TimeoutExpired:
                # the killed pxlobster can leave the device armed; disarm it
                try:
                    self.stop()
                    note = "sent --stop"
                except (OSError, subprocess.SubprocessError):
                    note = "--stop failed as well"
                self.last_error = f"capture timed out after {timeout_s:g} s (trigger never fired?); {note}"
                print(f"[LA] {self.last_error}", file=sys.stderr)
                return np.array([], dtype=np.uint32)
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

    def measure(self, samples, channel=0, sample_rate=20_000_000):
        """Stats of one channel as a dict.

        Keys: channel, freq_hz (0.0 with fewer than two rising edges),
        duty_pct, jitter_pct (std of the period as % of its mean), edges,
        high_us, high_min_us and high_max_us (mean, shortest and longest
        complete high pulse in microseconds, 0.0 without one; resolution is
        one sample period).
        """
        sig = self.channel_bits(samples, channel)
        diff = np.diff(sig.astype(np.int8))
        rising = np.flatnonzero(diff == 1)
        falling = np.flatnonzero(diff == -1)
        stats = {
            "channel": channel,
            "freq_hz": 0.0,
            "duty_pct": float(np.mean(sig) * 100) if len(sig) else 0.0,
            "jitter_pct": 0.0,
            "edges": int(np.count_nonzero(diff)),
            "high_us": 0.0,
            "high_min_us": 0.0,
            "high_max_us": 0.0,
        }
        if len(rising) >= 2:
            periods = np.diff(rising)
            period = float(np.mean(periods))
            stats["freq_hz"] = sample_rate / period
            stats["jitter_pct"] = float(np.std(periods) / period * 100)
        if len(rising):
            # a high pulse runs from a rising edge to the next falling edge;
            # pulses cut off at either end of the capture are left out
            falling = falling[falling > rising[0]]
            n = min(len(rising), len(falling))
            if n:
                widths = (falling[:n] - rising[:n]) * (1e6 / sample_rate)
                stats["high_us"] = float(widths.mean())
                stats["high_min_us"] = float(widths.min())
                stats["high_max_us"] = float(widths.max())
        return stats

    def measure_frequency(self, samples, channel=0, sample_rate=20_000_000):
        """Print a one-line report and return the frequency in Hz (0.0 if none)."""
        m = self.measure(samples, channel, sample_rate)
        if m["freq_hz"] == 0.0:
            print(f"[LA] CH{channel}: no periodic signal detected")
            return 0.0
        print(f"[LA] CH{channel}: {m['freq_hz']:,.1f} Hz, {m['duty_pct']:.1f}% duty, "
              f"high {m['high_us']:,.2f} us, jitter {m['jitter_pct']:.2f}%")
        return m["freq_hz"]

    def summary(self, samples, sample_rate=20_000_000):
        """Print and return measure() of every channel with more than 2 edges."""
        active = []
        for ch in range(self.channels):
            m = self.measure(samples, ch, sample_rate)
            if m["edges"] > 2:
                print(f"  CH{ch:02d}: {m['freq_hz']:>10,.0f} Hz  {m['duty_pct']:5.1f}% duty  "
                      f"{m['high_us']:>10,.2f} us high  {m['edges']:>8,} edges")
                active.append(m)
        return active


# ═══════════════════════════════════
# Protocol decoders
# ═══════════════════════════════════

def _seconds(index, sample_rate):
    return index / sample_rate if sample_rate else None


def decode_uart(samples, channel, baudrate, sample_rate, *, data_bits=8,
                parity="none", stop_bits=1, invert=False, detail=False):
    """Decode UART, LSB first. The defaults are 8N1 on a line that idles high.

    Args:
        data_bits: 5 to 9
        parity: "none", "even" or "odd"
        stop_bits: 1 or 2
        invert: True for a line that idles low, e.g. SBUS
            (100000 baud, parity="even", stop_bits=2)
        detail: return one dict per frame instead of the bare value

    Returns:
        List of data values. With detail=True, dicts with keys value, start
        and end (sample indices of the frame), time_s, framing_error (a stop
        bit was low) and parity_error.
    """
    parity = str(parity).lower()
    if parity not in ("none", "even", "odd"):
        raise ValueError(f'parity must be "none", "even" or "odd", not {parity!r}')
    if stop_bits not in (1, 2):
        raise ValueError(f"stop_bits must be 1 or 2, not {stop_bits!r}")
    if data_bits not in range(5, 10):
        raise ValueError(f"data_bits must be 5 to 9, not {data_bits!r}")
    sig = (samples >> channel) & 1
    if invert:
        sig = sig ^ 1
    spb = sample_rate / baudrate
    parity_bits = 0 if parity == "none" else 1
    n_bits = 1 + data_bits + parity_bits + stop_bits  # start bit included
    centre = [int(spb * (k + 0.5)) for k in range(n_bits)]  # bit centres from the start edge
    span = int(spb * n_bits)
    result = []
    i = 0
    while i < len(sig) - span - 1:
        if sig[i] == 1 and sig[i + 1] == 0:
            start = i + 1
            bits = [int(sig[start + c]) for c in centre]
            if bits[0]:  # high again at the start-bit centre: a glitch, not a frame
                i += 1
                continue
            data = bits[1:1 + data_bits]
            value = sum(b << k for k, b in enumerate(data))
            parity_error = False
            if parity_bits:
                expected = 1 if parity == "odd" else 0
                parity_error = (sum(data) + bits[1 + data_bits]) % 2 != expected
            if detail:
                result.append({"value": value, "start": start, "end": start + span,
                               "time_s": _seconds(start, sample_rate),
                               "framing_error": not all(bits[-stop_bits:]),
                               "parity_error": parity_error})
            else:
                result.append(value)
            # resume at the last stop-bit centre so back-to-back frames tolerate baud error
            i = start - 1 + centre[-1]
        else:
            i += 1
    return result


def decode_spi(samples, sclk_ch, mosi_ch, miso_ch=None, cs_ch=None, cpol=0, cpha=0,
               *, sample_rate=None, detail=False):
    """Decode SPI, MSB first, 8-bit words, CS active low.

    Releasing CS ends the current word, so every frame starts aligned even
    when the capture began in the middle of a word. A word cut short this
    way is still returned, with fewer than 8 bits.

    Returns:
        List of (mosi_byte, miso_byte) tuples. With detail=True, dicts with
        keys mosi, miso, bits (8 unless cut short), start and end (sample
        indices of the word's first and last sampling edge) and time_s
        (None without sample_rate).
    """
    sclk = (samples >> sclk_ch) & 1
    mosi = (samples >> mosi_ch) & 1
    miso = (samples >> miso_ch) & 1 if miso_ch is not None else np.zeros_like(mosi)
    edge = 1 if (cpol == 0) != (cpha == 1) else -1
    edges = np.where(np.diff(sclk.astype(np.int8)) == edge)[0] + 1
    releases = []  # first high sample of each CS release
    if cs_ch is not None:
        cs = (samples >> cs_ch) & 1
        releases = (np.flatnonzero(np.diff(cs.astype(np.int8)) == 1) + 1).tolist()
    result, mo, mi, bits, first, last = [], 0, 0, 0, 0, 0

    def flush():
        if detail:
            result.append({"mosi": mo, "miso": mi, "bits": bits, "start": first, "end": last,
                           "time_s": _seconds(first, sample_rate)})
        else:
            result.append((mo, mi))

    r = 0
    for idx in edges.tolist():
        while r < len(releases) and releases[r] <= idx:  # CS released since the last edge
            r += 1
            if bits:
                flush()
                mo = mi = bits = 0
        if cs_ch is not None and cs[idx] == 1:
            continue
        if not bits:
            first = idx
        last = idx
        mo = (mo << 1) | int(mosi[idx])
        mi = (mi << 1) | int(miso[idx])
        bits += 1
        if bits == 8:
            flush()
            mo = mi = bits = 0
    if bits and r < len(releases):  # CS released after the last edge
        flush()
    return result


def decode_i2c(samples, sda_ch, scl_ch, *, sample_rate=None, detail=False):
    """Decode I2C with 7-bit addresses.

    Each part from a START to the next STOP or repeated START is listed once
    its address byte has arrived, address-only parts included.

    Returns:
        List of transaction dicts with keys addr, rw, data and acks. With
        detail=True they also have start and end (sample indices of the
        START and of the STOP or repeated START; end is None if the capture
        ends first) and time_s (None without sample_rate).
    """
    sda = (samples >> sda_ch) & 1
    scl = (samples >> scl_ch) & 1
    txns, cur = [], None
    bval, bcnt = 0, 0
    for i in range(1, len(sda)):
        if scl[i] == 1 and sda[i - 1] == 1 and sda[i] == 0:  # START
            if cur and cur['rw'] != '?':
                cur['end'] = i  # ended by a repeated START
                txns.append(cur)
            cur = {'addr': 0, 'rw': '?', 'data': [], 'acks': [], 'start': i, 'end': None}
            bval = bcnt = 0
        elif scl[i] == 1 and sda[i - 1] == 0 and sda[i] == 1:  # STOP
            if cur and cur['rw'] != '?':
                cur['end'] = i
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
    if cur and cur['rw'] != '?':
        txns.append(cur)
    for t in txns:
        if detail:
            t['time_s'] = _seconds(t['start'], sample_rate)
        else:
            del t['start'], t['end']
    return txns


# ═══════════════════════════════════
# Command line
# ═══════════════════════════════════

def _cli(args):
    la = LogicAnalyzer(channels=args.channels)
    if args.scan:
        la.scan()  # prints pxlobster's device list
        devices = _parse_devices(la.last_output)
        result = {"ok": bool(devices), "devices": devices}
        fallback = "no supported devices found"
    elif args.prime:
        ok = la.prime()
        if la.last_output.strip():
            print(la.last_output.strip())
        result = {"ok": ok, "devices": _parse_devices(la.last_output)}
        fallback = "no device reported the bitstream as loaded"
    else:
        la.prime()
        rate = la.nearest_rate(args.rate)
        data = la.capture(rate=rate, duration_ms=args.time,
                          triggers=args.trigger, timeout_s=args.timeout)
        if len(data):
            result = {"ok": True, "sample_rate": rate, "duration_ms": args.time,
                      "samples": int(len(data)),
                      "active_channels": la.summary(data, sample_rate=rate)}
        else:
            result = {"ok": False}
        fallback = "capture returned no data"
    if not result["ok"]:
        result["error"] = la.last_error or fallback
        if not la.last_error and not la.last_output.strip():  # nothing was shown yet
            print(f"[LA] {fallback}", file=sys.stderr)
    return result


def main(argv=None):
    """Entry point of `pxlogic-agent` and `python px_logic.py`; returns the exit code."""
    import argparse
    import contextlib
    import json

    p = argparse.ArgumentParser(
        description="Capture with a PX Logic analyzer through pxlobster and "
                    "summarise every active channel.")
    action = p.add_mutually_exclusive_group()
    action.add_argument("--scan", action="store_true", help="list supported devices and exit")
    action.add_argument("--prime", action="store_true", help="load the FPGA bitstream and exit")
    p.add_argument("--rate", type=float, default=20e6,
                   help="sample rate in Hz, snapped to the nearest supported rate (default: 20e6)")
    p.add_argument("--time", type=int, default=200, help="capture length in ms (default: 200)")
    p.add_argument("--channels", type=int, choices=(16, 32), default=32,
                   help="channel count of the device (default: 32)")
    p.add_argument("--trigger", metavar="SPEC",
                   help='pxlobster trigger spec, e.g. "0=r" for a CH0 rising edge')
    p.add_argument("--timeout", type=float, metavar="S",
                   help="give up after S seconds and send --stop "
                        "(default: max(30, 5 x time + 10))")
    p.add_argument("--json", action="store_true",
                   help="print exactly one JSON object on stdout; logs go to stderr")
    args = p.parse_args(argv)

    try:
        with contextlib.redirect_stdout(sys.stderr if args.json else sys.stdout):
            result = _cli(args)
    except Exception as e:  # agents get failures as JSON, humans get the traceback
        if not args.json:
            raise
        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
