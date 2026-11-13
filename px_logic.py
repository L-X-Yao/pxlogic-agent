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


def load_capture(path, channels=32):
    """Read a packed capture written by capture() or pxlobster --decode-cross.

    32-channel captures are uint32, 16-channel captures are uint16; both are
    returned as uint32 with bit N holding channel N.
    """
    dtype = np.uint32 if channels > 16 else np.uint16
    return np.fromfile(path, dtype=dtype).astype(np.uint32, copy=False)


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
        # Resolved lazily so offline analysis needs no pxlobster binary.
        self.exe = exe_path
        self.channels = channels
        self.bytes_per_sample = 4 if channels > 16 else 2
        self.last_output = ""  # stdout of the last pxlobster call
        self.last_error = ""   # why the last pxlobster call failed, "" on success

    def _exe_path(self):
        if not self.exe:
            self.exe = _find_pxlobster()
        return self.exe

    def _run(self, args, timeout=60):
        r = subprocess.run([self._exe_path()] + args,
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
                triggers=None, decode_cross=True, timeout_s=None, save_path=None):
        """Capture logic data.

        Args:
            rate: sample rate in Hz (auto-snapped to nearest supported value)
            duration_ms: capture duration
            mode: "buffer" or "stream". "loop" runs until stopped, so it is
                rejected; run `pxlobster --mode loop` directly instead
            triggers: trigger spec string, e.g. "0=r" (CH0 rising edge)
            decode_cross: decode DMA cross-lane format to standard packed samples
            timeout_s: give up after this many seconds, e.g. when a trigger
                never fires (default: max(30, 5 x duration + 10 s))
            save_path: keep a copy of the raw .bin capture at this path

        Returns:
            numpy array shape (n_samples,) dtype uint32 — bit N = channel N value.
            Empty on failure or timeout; the reason is in self.last_error.
        """
        if mode == "loop":
            raise ValueError("loop mode runs until stopped, so capture() could only time out "
                             "and return nothing; run `pxlobster --mode loop` directly and "
                             "end it with `pxlobster --stop`")
        if mode not in ("buffer", "stream"):
            raise ValueError(f'mode must be "buffer" or "stream", not {mode!r}')
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
            if save_path:
                shutil.copyfile(path, save_path)
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

    def summary(self, samples, sample_rate=20_000_000, min_edges=2):
        """Print and return measure() of every channel with more than min_edges edges.

        min_edges=0 includes one-shot signals that have a single edge.
        """
        active = []
        for ch in range(self.channels):
            m = self.measure(samples, ch, sample_rate)
            if m["edges"] > min_edges:
                print(f"  CH{ch:02d}: {m['freq_hz']:>10,.0f} Hz  {m['duty_pct']:5.1f}% duty  "
                      f"{m['high_us']:>10,.2f} us high  {m['edges']:>8,} edges")
                active.append(m)
        return active

    def identify(self, samples, sample_rate=20_000_000, max_buses=4):
        """Return deterministic channel stats and protocol/bus candidates.

        Each active channel has a ``candidates`` list (pwm, uart) sorted by
        confidence. ``buses`` holds i2c and spi candidates found across
        channels. Candidates carry evidence such as frame counts, errors and
        channel numbers; this method does not diagnose or prescribe.
        """
        channels = _identify_channels(self, samples, sample_rate)
        buses = _identify_buses(samples, channels, sample_rate)
        buses.sort(key=lambda b: b["confidence"], reverse=True)
        buses = buses[:max_buses]
        clock_channels, silent_channels = set(), set()
        for bus in buses:
            if bus["protocol"] == "i2c":
                clock_channels.add(bus["scl"])
                silent_channels.add(bus["sda"])
                involved = (bus["sda"], bus["scl"])
            else:
                clock_channels.add(bus["sclk"])
                silent_channels.update((bus["cs"], *bus["data"]))
                involved = (bus["sclk"], bus["cs"], *bus["data"])
            for ch in channels:
                if ch["channel"] in involved and "bus" not in ch:
                    ch["bus"] = bus["protocol"]
        for ch in channels:
            if ch["channel"] in silent_channels:
                ch["candidates"] = []
            elif ch["channel"] in clock_channels:
                # A clock can look like UART or PWM; the bus evidence wins.
                ch["candidates"] = [c for c in ch["candidates"] if c["protocol"] == "pwm"]
        return {"channels": channels, "buses": buses}


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
    # Only falling edges can begin a frame, so scan edges instead of samples.
    starts = (np.flatnonzero(np.diff(sig.astype(np.int8)) == -1) + 1).tolist()
    limit = len(sig) - span - 1
    resume = 0
    for start in starts:
        if start > limit:
            break
        if start <= resume:
            continue
        bits = [int(sig[start + c]) for c in centre]
        if bits[0]:  # high again at the start-bit centre: a glitch, not a frame
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
        resume = start - 1 + centre[-1]
    return result


def decode_spi(samples, sclk_ch, mosi_ch, miso_ch=None, cs_ch=None, cpol=0, cpha=0,
               *, word_bits=8, lsb_first=False, sample_rate=None, detail=False):
    """Decode SPI, CS active low.

    Releasing CS ends the current word, so every frame starts aligned even
    when the capture began in the middle of a word. A word cut short this
    way is still returned, with fewer than word_bits bits.

    Args:
        word_bits: bits per word, 1 to 32 (default 8)
        lsb_first: shift in LSB first instead of MSB first

    Returns:
        List of (mosi_word, miso_word) tuples. With detail=True, dicts with
        keys mosi, miso, bits (word_bits unless cut short), start and end
        (sample indices of the word's first and last sampling edge) and
        time_s (None without sample_rate).
    """
    if not 1 <= word_bits <= 32:
        raise ValueError(f"word_bits must be 1 to 32, not {word_bits!r}")
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
        if lsb_first:
            mo |= int(mosi[idx]) << bits
            mi |= int(miso[idx]) << bits
        else:
            mo = (mo << 1) | int(mosi[idx])
            mi = (mi << 1) | int(miso[idx])
        bits += 1
        if bits == word_bits:
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
    # START, STOP and data bits all happen when SCL or SDA changes; skip idle samples.
    events = np.flatnonzero((scl[1:] != scl[:-1]) | (sda[1:] != sda[:-1])) + 1
    for i in events.tolist():
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
# Protocol identification
# ═══════════════════════════════════

_UART_BAUDS = sorted({
    300, 600, 1200, 2400, 4800, 9600, 14400, 19200, 28800, 38400,
    57600, 76800, 115200, 230400, 250000, 460800, 500000, 921600,
    1_000_000, 1_500_000, 2_000_000, 3_000_000, 31_250, 100_000, 420_000,
})

_UART_CONFIGS = [
    (8, "none", 1, False),
    (8, "even", 2, True),   # SBUS
    (8, "even", 1, False),
    (8, "odd", 1, False),
    (8, "none", 2, False),
    (8, "none", 1, True),
    (7, "none", 1, False),
]


def _signal_runs(sig):
    """Lengths of consecutive equal-level runs."""
    diff = np.diff(sig.astype(np.int8))
    edges = np.flatnonzero(diff != 0)
    starts = np.r_[0, edges + 1]
    ends = np.r_[edges + 1, len(sig)]
    return ends - starts


def _uart_baud_candidates(sig, sample_rate):
    """Baud rates worth trying, starting near the shortest run length."""
    runs = _signal_runs(sig)
    runs = runs[runs >= 2]
    estimate = sample_rate / float(runs.min()) if len(runs) else None
    candidates = set()
    if estimate:
        nearest = min(_UART_BAUDS, key=lambda b: abs(b - estimate))
        if abs(nearest - estimate) / estimate <= 0.02:
            estimate = float(nearest)
        for factor in (1.0, 0.5, 2.0, 3.0):
            candidates.add(estimate * factor)
        for baud in _UART_BAUDS:
            if abs(baud - estimate) / estimate <= 0.25:
                candidates.add(float(baud))
    else:
        candidates.update(float(b) for b in _UART_BAUDS)
    valid = [b for b in candidates if b > 0 and sample_rate / b >= 4]
    valid.sort(key=lambda b: abs(b - (estimate or b)))
    return valid[:5]


def _pwm_candidate(stats):
    if stats["edges"] < 4 or stats["freq_hz"] <= 0 or stats["jitter_pct"] > 10:
        return None
    confidence = 0.5
    if stats["edges"] >= 10:
        confidence += 0.2
    if stats["jitter_pct"] <= 1:
        confidence += 0.2
    if stats["high_us"] > 0:
        confidence += 0.1
    return {
        "protocol": "pwm",
        "confidence": round(min(1.0, confidence), 3),
        "freq_hz": round(stats["freq_hz"], 3),
        "duty_pct": round(stats["duty_pct"], 3),
        "high_us": round(stats["high_us"], 3),
        "jitter_pct": round(stats["jitter_pct"], 3),
    }


def _uart_candidate(samples, channel, sample_rate):
    bauds = _uart_baud_candidates((samples >> channel) & 1, sample_rate)
    duration = len(samples) / sample_rate if sample_rate else 0.0
    # Cheap pass: count frames and require more than one distinct value. This
    # rejects stable clocks and PWM without running the detailed decoder.
    rough = []
    for rank, baud in enumerate(bauds[:3]):
        for data_bits, parity, stop_bits, invert in _UART_CONFIGS[:3]:
            values = decode_uart(samples, channel, baud, sample_rate,
                                 data_bits=data_bits, parity=parity,
                                 stop_bits=stop_bits, invert=invert)
            n = len(values)
            if n < 2 or len(set(values)) < 2:
                continue
            rough.append((min(1.0, n / 5), rank, baud,
                          (data_bits, parity, stop_bits, invert)))
    if not rough:
        return None
    rough.sort(key=lambda item: (-item[0], item[1]))
    best = None
    for _, _, baud, config in rough[:2]:
        data_bits, parity, stop_bits, invert = config
        frames = decode_uart(samples, channel, baud, sample_rate,
                             data_bits=data_bits, parity=parity,
                             stop_bits=stop_bits, invert=invert, detail=True)
        n = len(frames)
        if n < 2 or len({f["value"] for f in frames}) < 2:
            continue
        framing = sum(1 for f in frames if f["framing_error"])
        parity_errors = sum(1 for f in frames if f["parity_error"])
        error_rate = (framing + parity_errors) / n
        bits = 1 + data_bits + (0 if parity == "none" else 1) + stop_bits
        expected = (baud / bits) * duration if duration else 0.0
        coverage = n / expected if expected else 0.0
        score = min(1.0, n / 5) * max(0.0, 1 - error_rate * 8)
        if coverage > 1.05:
            score *= 0.5
        if score < 0.2:
            continue
        candidate = {
            "protocol": "uart",
            "confidence": round(score, 3),
            "baudrate": int(baud) if float(baud).is_integer() else round(baud, 1),
            "data_bits": data_bits,
            "parity": parity,
            "stop_bits": stop_bits,
            "invert": invert,
            "frames": n,
            "framing_errors": framing,
            "parity_errors": parity_errors,
            "bytes_per_second": round(n / duration, 1) if duration else 0.0,
        }
        if best is None or candidate["confidence"] > best["confidence"]:
            best = candidate
    return best


def _identify_channels(la, samples, sample_rate):
    channels = []
    if len(samples) > 1:
        active_mask = int(np.bitwise_or.reduce(samples[1:] ^ samples[:-1]))
    else:
        active_mask = 0
    for ch in range(la.channels):
        if not (active_mask >> ch) & 1:
            continue
        stats = la.measure(samples, ch, sample_rate)
        candidates = []
        pwm = _pwm_candidate(stats)
        if pwm:
            candidates.append(pwm)
        uart = _uart_candidate(samples, ch, sample_rate)
        if uart:
            candidates.append(uart)
        candidates.sort(key=lambda c: c["confidence"], reverse=True)
        stats["candidates"] = candidates
        channels.append(stats)
    return channels


def _i2c_candidates(samples, channels, sample_rate):
    active = [m for m in channels if m["edges"] > 0]
    clock_like = [m for m in active
                  if m["edges"] >= 6 and 10 <= m["duty_pct"] <= 90]
    best = {}
    for scl in clock_like:
        for sda in active:
            if sda["channel"] == scl["channel"]:
                continue
            txns = decode_i2c(samples, sda["channel"], scl["channel"], detail=True)
            if not txns:
                continue
            total_acks = sum(len(t["acks"]) for t in txns)
            if not total_acks:
                continue
            acks = sum(1 for t in txns for a in t["acks"] if a)
            ack_ratio = acks / total_acks
            if ack_ratio < 0.5:
                continue
            data_bytes = sum(len(t["data"]) for t in txns)
            score = ((0.4 + 0.6 * ack_ratio) *
                     (0.5 + 0.5 * min(1.0, (len(txns) + data_bytes) / 4)))
            if score < 0.5:
                continue
            key = frozenset((sda["channel"], scl["channel"]))
            candidate = {
                "protocol": "i2c",
                "confidence": round(score, 3),
                "sda": sda["channel"],
                "scl": scl["channel"],
                "transactions": len(txns),
                "acks": acks,
                "nacks": total_acks - acks,
                "addresses": sorted({t["addr"] for t in txns}),
            }
            if key not in best or score > best[key]["confidence"]:
                best[key] = candidate
    return sorted(best.values(), key=lambda b: b["confidence"], reverse=True)


def _spi_candidates(samples, channels, sample_rate):
    active = [m for m in channels if m["edges"] > 0]
    clock_like = [m for m in active
                  if m["edges"] >= 6 and 10 <= m["duty_pct"] <= 90]
    best = {}
    for clk in clock_like:
        clk_ch = clk["channel"]
        sclk = (samples >> clk_ch) & 1
        clk_diff = np.diff(sclk.astype(np.int8))
        all_edges = np.flatnonzero(clk_diff != 0) + 1
        for cs in active:
            cs_ch = cs["channel"]
            if cs_ch == clk_ch:
                continue
            cs_sig = (samples >> cs_ch) & 1
            cs_frames = int(np.count_nonzero(np.diff(cs_sig.astype(np.int8)) == -1))
            if cs_frames < 1 or np.count_nonzero(cs_sig[all_edges] == 0) < 8:
                continue
            for mosi in active:
                mosi_ch = mosi["channel"]
                if mosi_ch in (clk_ch, cs_ch):
                    continue
                mosi_sig = (samples >> mosi_ch) & 1
                mosi_diff = np.diff(mosi_sig.astype(np.int8))
                if np.count_nonzero((mosi_diff != 0) & (cs_sig[1:] == 0)) < 4:
                    continue
                for mode in range(4):
                    cpol, cpha = mode >> 1, mode & 1
                    edge = 1 if (cpol == 0) != (cpha == 1) else -1
                    sample_edges = np.flatnonzero(clk_diff == edge) + 1
                    active_edges = sample_edges[cs_sig[sample_edges] == 0]
                    if len(active_edges) < 8:
                        continue
                    words = decode_spi(samples, clk_ch, mosi_ch, None, cs_ch,
                                       cpol=cpol, cpha=cpha, detail=True)
                    complete = sum(1 for w in words if w["bits"] == 8)
                    if complete < 2:
                        continue
                    violations = np.count_nonzero(
                        mosi_sig[active_edges] != mosi_sig[active_edges - 1]) / len(active_edges)
                    score = (min(1.0, complete / 4) *
                             (0.6 + 0.4 * min(1.0, cs_frames / 2)) *
                             max(0.0, 1 - violations * 4))
                    if score < 0.4:
                        continue
                    key = (clk_ch, cs_ch, mosi_ch)
                    candidate = {
                        "protocol": "spi",
                        "confidence": float(round(score, 3)),
                        "sclk": clk_ch,
                        "mosi": mosi_ch,
                        "miso": None,
                        "cs": cs_ch,
                        "mode": mode,
                        "words": complete,
                        "frames": cs_frames,
                    }
                    if key not in best or score > best[key]["confidence"]:
                        best[key] = candidate
    grouped = {}
    for candidate in best.values():
        grouped.setdefault((candidate["sclk"], candidate["cs"], candidate["mode"]),
                           []).append(candidate)
    result = []
    for candidates in grouped.values():
        data = sorted({c["mosi"] for c in candidates})
        top = max(candidates, key=lambda c: c["confidence"])
        item = {k: v for k, v in top.items() if k != "mosi"}
        item["data"] = data
        item["mosi"] = data[0] if len(data) == 1 else None
        item["miso"] = None
        if len(data) > 1:
            item["ambiguous_data"] = True
        result.append(item)
    return sorted(result, key=lambda b: b["confidence"], reverse=True)


def _identify_buses(samples, channels, sample_rate):
    active = sorted((m for m in channels if m["edges"] > 0),
                    key=lambda m: m["edges"], reverse=True)[:8]
    buses = _i2c_candidates(samples, active, sample_rate)
    buses.extend(_spi_candidates(samples, active, sample_rate))
    return buses


# ═══════════════════════════════════
# Command line
# ═══════════════════════════════════

def _check_channel(option, value, channels):
    if value is not None and not 0 <= value < channels:
        raise ValueError(f"{option} must be between 0 and {channels - 1}")


def _validate_decode_args(args):
    if not args.decode:
        return
    if args.limit < 0:
        raise ValueError("--limit must be 0 or more")
    if args.decode == "uart":
        if args.channel is None:
            raise ValueError("--decode uart requires --channel")
        if args.baud is None:
            raise ValueError("--decode uart requires --baud")
        if args.baud <= 0:
            raise ValueError("--baud must be positive")
        if not 5 <= args.data_bits <= 9:
            raise ValueError("--data-bits must be 5 to 9")
        _check_channel("--channel", args.channel, args.channels)
    elif args.decode == "spi":
        if args.sclk is None or args.mosi is None:
            raise ValueError("--decode spi requires --sclk and --mosi")
        if not 1 <= args.word_bits <= 32:
            raise ValueError("--word-bits must be 1 to 32")
        for option, value in (("--sclk", args.sclk), ("--mosi", args.mosi),
                              ("--miso", args.miso), ("--cs", args.cs)):
            _check_channel(option, value, args.channels)
    else:
        if args.sda is None or args.scl is None:
            raise ValueError("--decode i2c requires --sda and --scl")
        _check_channel("--sda", args.sda, args.channels)
        _check_channel("--scl", args.scl, args.channels)


def _decode_cli(args, samples, sample_rate):
    if args.decode == "uart":
        frames = decode_uart(samples, args.channel, args.baud, sample_rate,
                             data_bits=args.data_bits, parity=args.parity,
                             stop_bits=args.stop_bits, invert=args.invert,
                             detail=args.detail)
    elif args.decode == "spi":
        frames = decode_spi(samples, args.sclk, args.mosi, args.miso, args.cs,
                            cpol=args.cpol, cpha=args.cpha, word_bits=args.word_bits,
                            lsb_first=args.lsb_first, sample_rate=sample_rate,
                            detail=args.detail)
    else:
        frames = decode_i2c(samples, args.sda, args.scl, sample_rate=sample_rate,
                            detail=args.detail)
    total = len(frames)
    truncated = bool(args.limit and total > args.limit)
    if truncated:
        frames = frames[:args.limit]
    return {"ok": True, "protocol": args.decode, "sample_rate": sample_rate,
            "count": total, "truncated": truncated, "frames": frames}


def _print_decoded(protocol, frames):
    for f in frames:
        if protocol == "uart":
            if isinstance(f, dict):
                print(f"{f['time_s']:9.6f}s  0x{f['value']:02X}"
                      f"{' framing' if f['framing_error'] else ''}"
                      f"{' parity' if f['parity_error'] else ''}")
            else:
                print(f"0x{f:02X}")
        elif protocol == "spi":
            if isinstance(f, dict):
                ts = f"{f['time_s']:9.6f}s" if f["time_s"] is not None else " " * 10
                print(f"{ts}  MOSI 0x{f['mosi']:X}  MISO 0x{f['miso']:X}  {f['bits']} bits")
            else:
                print(f"MOSI 0x{f[0]:02X}  MISO 0x{f[1]:02X}")
        else:
            ts = f"{f['time_s']:9.6f}s" if f.get("time_s") is not None else " " * 10
            data = " ".join(f"{b:02X}" for b in f["data"])
            acks = " ".join("A" if a else "N" for a in f["acks"])
            print(f"{ts}  {f['rw']} 0x{f['addr']:02X}: {data}  [{acks}]")


def _print_check(result):
    for ch in result["channels"]:
        if ch.get("bus"):
            continue
        candidates = ch["candidates"]
        if not candidates:
            print(f"CH{ch['channel']:02d}: {ch['edges']} edges, no protocol candidate")
            continue
        best = candidates[0]
        if best["protocol"] == "uart":
            parity = "N" if best["parity"] == "none" else best["parity"][0].upper()
            inverted = " inverted" if best["invert"] else ""
            errors = best["framing_errors"] + best["parity_errors"]
            print(f"CH{ch['channel']:02d}: UART {best['baudrate']:g} "
                  f"{best['data_bits']}{parity}{best['stop_bits']}{inverted}  "
                  f"conf {best['confidence']:.2f}  frames {best['frames']}  errors {errors}")
        else:
            print(f"CH{ch['channel']:02d}: PWM {best['freq_hz']:,.1f} Hz  "
                  f"{best['duty_pct']:.1f}%  high {best['high_us']:.2f} us  "
                  f"conf {best['confidence']:.2f}")
    for bus in result["buses"]:
        if bus["protocol"] == "i2c":
            print(f"I2C: SDA=CH{bus['sda']} SCL=CH{bus['scl']}  "
                  f"{bus['transactions']} txns  {bus['acks']} ACK  "
                  f"conf {bus['confidence']:.2f}")
        elif bus["mosi"] is not None:
            print(f"SPI: SCLK=CH{bus['sclk']} MOSI=CH{bus['mosi']} CS=CH{bus['cs']}  "
                  f"mode {bus['mode']}  {bus['words']} words  "
                  f"conf {bus['confidence']:.2f}")
        else:
            data = ",".join(f"CH{ch}" for ch in bus["data"])
            print(f"SPI: SCLK=CH{bus['sclk']} DATA={data} CS=CH{bus['cs']}  "
                  f"mode {bus['mode']}  {bus['words']} words  "
                  f"conf {bus['confidence']:.2f}")


def _cli(args):
    if (args.scan or args.prime) and (args.decode or args.save or args.check):
        raise ValueError("--decode, --save and --check cannot be used with --scan or --prime")
    if args.check and args.decode:
        raise ValueError("--check cannot be combined with --decode")
    if args.min_edges < 0:
        raise ValueError("--min-edges must be 0 or more")
    _validate_decode_args(args)
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
        if args.input:
            if args.save:
                raise ValueError("--input cannot be combined with --save")
            if args.trigger or args.timeout is not None:
                raise ValueError("--input cannot be combined with --trigger or --timeout")
            rate = la.nearest_rate(args.rate)
            data = load_capture(args.input, args.channels)
            fallback = "no data in the input file"
        else:
            la.prime()
            rate = la.nearest_rate(args.rate)
            data = la.capture(rate=rate, duration_ms=args.time,
                              triggers=args.trigger, timeout_s=args.timeout,
                              save_path=args.save)
            fallback = "capture returned no data"
        if not len(data):
            result = {"ok": False}
        elif args.decode:
            result = _decode_cli(args, data, rate)
            result["samples"] = int(len(data))
            result["duration_ms"] = round(len(data) * 1000 / rate, 3)
            if args.input:
                result["input"] = args.input
            if args.save:
                result["saved"] = args.save
            if not args.json:
                _print_decoded(args.decode, result["frames"])
                if result["truncated"]:
                    print(f"[LA] showing first {len(result['frames'])} of "
                          f"{result['count']} frames", file=sys.stderr)
        elif args.check:
            result = la.identify(data, sample_rate=rate)
            result["ok"] = True
            result["sample_rate"] = rate
            result["samples"] = int(len(data))
            result["duration_ms"] = round(len(data) * 1000 / rate, 3)
            if args.input:
                result["input"] = args.input
            if args.save:
                result["saved"] = args.save
            if not args.json:
                _print_check(result)
        else:
            duration_ms = args.time if not args.input else round(len(data) * 1000 / rate, 3)
            result = {"ok": True, "sample_rate": rate, "duration_ms": duration_ms,
                      "samples": int(len(data)),
                      "active_channels": la.summary(data, sample_rate=rate,
                                                    min_edges=args.min_edges)}
            if args.input:
                result["input"] = args.input
            if args.save:
                result["saved"] = args.save
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
        description="Capture with a PX Logic analyzer through pxlobster, "
                    "summarise channels, decode or identify protocols.")
    action = p.add_mutually_exclusive_group()
    action.add_argument("--scan", action="store_true", help="list supported devices and exit")
    action.add_argument("--prime", action="store_true", help="load the FPGA bitstream and exit")
    action.add_argument("--input", metavar="FILE",
                        help="analyze a saved .bin capture instead of capturing")
    p.add_argument("--rate", type=float, default=20e6,
                   help="sample rate in Hz, snapped to the nearest supported rate (default: 20e6)")
    p.add_argument("--time", type=int, default=200,
                   help="capture length in ms (default: 200; ignored with --input)")
    p.add_argument("--channels", type=int, choices=(16, 32), default=32,
                   help="channel count of the device (default: 32)")
    p.add_argument("--min-edges", type=int, default=2, metavar="N",
                   help="summarise channels with more than N edges "
                        "(default: 2; 0 includes one-shot signals)")
    p.add_argument("--trigger", metavar="SPEC",
                   help='pxlobster trigger spec, e.g. "0=r" for a CH0 rising edge')
    p.add_argument("--timeout", type=float, metavar="S",
                   help="give up after S seconds and send --stop "
                        "(default: max(30, 5 x time + 10))")
    p.add_argument("--save", metavar="FILE",
                   help="save the raw capture to FILE (packed .bin)")
    p.add_argument("--decode", choices=("uart", "spi", "i2c"),
                   help="decode a protocol instead of only summarising channels")
    p.add_argument("--check", action="store_true",
                   help="identify protocols and buses on active channels")
    p.add_argument("--detail", action="store_true",
                   help="include positions, times and error flags in decoded frames")
    p.add_argument("--limit", type=int, default=10000, metavar="N",
                   help="max decoded frames to return, 0 for all (default: 10000)")
    p.add_argument("--json", action="store_true",
                   help="print exactly one JSON object on stdout; logs go to stderr")

    uart = p.add_argument_group("UART decode options")
    uart.add_argument("--channel", type=int, metavar="CH", help="UART channel")
    uart.add_argument("--baud", type=float, metavar="HZ", help="UART baud rate")
    uart.add_argument("--data-bits", type=int, default=8,
                      help="UART data bits, 5 to 9 (default: 8)")
    uart.add_argument("--parity", choices=("none", "even", "odd"), default="none",
                      help="UART parity (default: none)")
    uart.add_argument("--stop-bits", type=int, choices=(1, 2), default=1,
                      help="UART stop bits (default: 1)")
    uart.add_argument("--invert", action="store_true",
                      help="decode a line that idles low, e.g. SBUS")

    spi = p.add_argument_group("SPI decode options")
    spi.add_argument("--sclk", type=int, metavar="CH", help="SPI clock channel")
    spi.add_argument("--mosi", type=int, metavar="CH", help="SPI MOSI channel")
    spi.add_argument("--miso", type=int, metavar="CH", help="SPI MISO channel")
    spi.add_argument("--cs", type=int, metavar="CH", help="SPI CS channel")
    spi.add_argument("--cpol", type=int, choices=(0, 1), default=0,
                     help="SPI clock polarity (default: 0)")
    spi.add_argument("--cpha", type=int, choices=(0, 1), default=0,
                     help="SPI clock phase (default: 0)")
    spi.add_argument("--word-bits", type=int, default=8, metavar="N",
                     help="SPI bits per word, 1 to 32 (default: 8)")
    spi.add_argument("--lsb-first", action="store_true",
                     help="SPI words are sent LSB first")

    i2c = p.add_argument_group("I2C decode options")
    i2c.add_argument("--sda", type=int, metavar="CH", help="I2C SDA channel")
    i2c.add_argument("--scl", type=int, metavar="CH", help="I2C SCL channel")

    args = p.parse_args(argv)

    try:
        with contextlib.redirect_stdout(sys.stderr if args.json else sys.stdout):
            result = _cli(args)
    except (ValueError, OSError) as e:
        if not args.json:
            print(f"error: {e}", file=sys.stderr)
            return 2
        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    except Exception as e:  # agents get failures as JSON, humans get the traceback
        if not args.json:
            raise
        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
