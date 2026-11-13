"""Automated parts of the real-device regression.

Run with the analyzer attached:

    python3 tools/device_check.py --channels 32 --pwm-channel 0
    python3 tools/device_check.py --channels 16 --trigger-test

The script covers scan, PWM at several sample rates, trigger timeout recovery
and save/load. UART/SBUS/SPI checks still need a known signal and are listed
in docs/REGRESSION.md.
"""

import argparse
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from px_logic import LogicAnalyzer, load_capture  # noqa: E402


def _rates(text):
    return [float(part) for part in text.split(",") if part.strip()]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the automated device checks.")
    parser.add_argument("--channels", type=int, choices=(16, 32), default=32)
    parser.add_argument("--pwm-channel", type=int, default=0)
    parser.add_argument("--rates", type=_rates, default=_rates("5e6,20e6,100e6,250e6"))
    parser.add_argument("--time", type=int, default=200, help="capture length in ms")
    parser.add_argument("--expect-freq", type=float, default=400.0,
                        help="expected PWM frequency in Hz")
    parser.add_argument("--freq-tol", type=float, default=1.0,
                        help="allowed absolute frequency error in Hz")
    parser.add_argument("--trigger-test", action="store_true",
                        help="run the no-signal trigger timeout test (CH0 must be idle)")
    args = parser.parse_args(argv)

    la = LogicAnalyzer(channels=args.channels)
    failures = []

    devices = la.devices()
    print(f"[{'PASS' if devices else 'FAIL'}] scan: "
          f"{devices or la.last_error or 'no supported devices found'}")
    if not devices:
        return 1

    for rate in args.rates:
        data = la.capture(rate=rate, duration_ms=args.time)
        if not len(data):
            failures.append(f"capture at {rate:g} Hz returned no data: {la.last_error}")
            print(f"[FAIL] PWM {rate/1e6:g} MHz: {la.last_error}")
            continue
        stats = la.measure(data, channel=args.pwm_channel, sample_rate=rate)
        ok = abs(stats["freq_hz"] - args.expect_freq) <= args.freq_tol
        if not ok:
            failures.append(
                f"CH{args.pwm_channel} at {rate:g} Hz: {stats['freq_hz']:.3f} Hz, "
                f"expected {args.expect_freq:g} +/- {args.freq_tol:g}")
        print(f"[{'PASS' if ok else 'FAIL'}] PWM {rate/1e6:g} MHz: "
              f"{stats['freq_hz']:.3f} Hz, {stats['duty_pct']:.1f}% duty, "
              f"{stats['high_us']:.3f} us high")

    if args.trigger_test:
        empty = la.capture(rate=1_000_000, duration_ms=10, triggers="0=r", timeout_s=2)
        ok = not len(empty) and "timed out" in la.last_error
        after = la.capture(rate=1_000_000, duration_ms=10)
        ok = ok and len(after) > 0
        if not ok:
            failures.append("trigger timeout did not recover with a normal capture")
        print(f"[{'PASS' if ok else 'FAIL'}] trigger timeout: {la.last_error or 'no timeout'}, "
              f"next capture {len(after)} samples")

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "device-check.bin")
        data = la.capture(rate=1_000_000, duration_ms=10, save_path=path)
        loaded = load_capture(path, channels=args.channels) if len(data) else []
        ok = len(data) > 0 and len(loaded) == len(data)
        if not ok:
            failures.append("save/load roundtrip failed")
        print(f"[{'PASS' if ok else 'FAIL'}] save/load: {len(data)} -> {len(loaded)} samples")

    if failures:
        print("\nFailures:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("\nAll automated checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
