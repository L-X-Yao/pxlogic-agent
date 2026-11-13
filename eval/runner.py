"""Score protocol identification against offline cases.

Run from the repository root:

    python3 eval/runner.py
    python3 eval/runner.py --json

Each case builds a synthetic capture, runs LogicAnalyzer.identify(), and
checks the expected channels/buses and their evidence. No hardware is used.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from px_logic import LogicAnalyzer  # noqa: E402
from eval import waveforms  # noqa: E402

DEFAULT_CASES = Path(__file__).resolve().parent / "cases"


def load_cases(path=DEFAULT_CASES):
    path = Path(path)
    files = sorted(path.glob("*.json")) if path.is_dir() else [path]
    cases = []
    for file in files:
        data = json.loads(file.read_text())
        cases.extend(data["cases"] if isinstance(data, dict) else data)
    return cases


def _close(actual, expected, tolerance):
    return abs(float(actual) - float(expected)) <= float(tolerance)


def _check_channel(result, expected, failures):
    channel = expected["channel"]
    actual_channel = next((c for c in result["channels"] if c["channel"] == channel), None)
    if actual_channel is None:
        failures.append(f"CH{channel}: no activity")
        return
    protocol = expected["protocol"]
    candidate = next(
        (c for c in actual_channel["candidates"] if c["protocol"] == protocol), None)
    if candidate is None:
        failures.append(f"CH{channel}: no {protocol} candidate")
        return
    for key, expected_value in expected.items():
        if key in ("channel", "protocol", "min_confidence", "min_frames",
                   "max_framing_errors", "max_parity_errors",
                   "min_framing_errors", "min_parity_errors",
                   "freq_tol", "duty_tol", "high_tol"):
            continue
        actual_value = candidate.get(key)
        if isinstance(expected_value, (int, float)) and isinstance(actual_value, (int, float)):
            tolerance = expected.get(
                "freq_tol" if key == "freq_hz" else
                "duty_tol" if key == "duty_pct" else
                "high_tol" if key == "high_us" else "tol", 0.0)
            if not _close(actual_value, expected_value, tolerance):
                failures.append(
                    f"CH{channel}: {key}={actual_value!r}, expected {expected_value!r}")
        elif actual_value != expected_value:
            failures.append(
                f"CH{channel}: {key}={actual_value!r}, expected {expected_value!r}")
    if "min_confidence" in expected and candidate["confidence"] < expected["min_confidence"]:
        failures.append(
            f"CH{channel}: confidence {candidate['confidence']} < {expected['min_confidence']}")
    if "min_frames" in expected and candidate.get("frames", 0) < expected["min_frames"]:
        failures.append(f"CH{channel}: frames {candidate.get('frames', 0)} < {expected['min_frames']}")
    if "max_framing_errors" in expected:
        if candidate.get("framing_errors", 0) > expected["max_framing_errors"]:
            failures.append(
                f"CH{channel}: framing_errors {candidate.get('framing_errors')} > "
                f"{expected['max_framing_errors']}")
    if "max_parity_errors" in expected:
        if candidate.get("parity_errors", 0) > expected["max_parity_errors"]:
            failures.append(
                f"CH{channel}: parity_errors {candidate.get('parity_errors')} > "
                f"{expected['max_parity_errors']}")
    if "min_framing_errors" in expected:
        if candidate.get("framing_errors", 0) < expected["min_framing_errors"]:
            failures.append(
                f"CH{channel}: framing_errors {candidate.get('framing_errors')} < "
                f"{expected['min_framing_errors']}")
    if "min_parity_errors" in expected:
        if candidate.get("parity_errors", 0) < expected["min_parity_errors"]:
            failures.append(
                f"CH{channel}: parity_errors {candidate.get('parity_errors')} < "
                f"{expected['min_parity_errors']}")


def _matches_bus(actual, expected):
    if actual["protocol"] != expected["protocol"]:
        return False
    if expected["protocol"] == "i2c":
        return (actual.get("sda"), actual.get("scl")) == (expected.get("sda"),
                                                          expected.get("scl"))
    if expected["protocol"] == "spi":
        if (actual.get("sclk"), actual.get("cs")) != (expected.get("sclk"),
                                                      expected.get("cs")):
            return False
        if "mode" in expected and actual.get("mode") != expected["mode"]:
            return False
        if "data" in expected and sorted(actual.get("data", [])) != sorted(expected["data"]):
            return False
        return True
    return False


def _check_bus(result, expected, failures):
    matches = [b for b in result["buses"] if _matches_bus(b, expected)]
    if not matches:
        failures.append(f"no matching {expected['protocol']} bus")
        return
    bus = matches[0]
    protocol = expected["protocol"]
    prefix = "I2C" if protocol == "i2c" else "SPI"
    if "min_confidence" in expected and bus["confidence"] < expected["min_confidence"]:
        failures.append(f"{prefix}: confidence {bus['confidence']} < {expected['min_confidence']}")
    if "min_words" in expected and bus.get("words", 0) < expected["min_words"]:
        failures.append(f"{prefix}: words {bus.get('words', 0)} < {expected['min_words']}")
    if "min_acks" in expected and bus.get("acks", 0) < expected["min_acks"]:
        failures.append(f"{prefix}: acks {bus.get('acks', 0)} < {expected['min_acks']}")
    if "min_nacks" in expected and bus.get("nacks", 0) < expected["min_nacks"]:
        failures.append(f"{prefix}: nacks {bus.get('nacks', 0)} < {expected['min_nacks']}")
    if "addresses" in expected and not set(expected["addresses"]).issubset(
            set(bus.get("addresses", []))):
        failures.append(f"{prefix}: addresses {bus.get('addresses')} missing "
                        f"{expected['addresses']}")


def run_case(case):
    failures = []
    samples, sample_rate = waveforms.build(case)
    analyzer = LogicAnalyzer(channels=case.get("device_channels", 32))
    result = analyzer.identify(samples, sample_rate=sample_rate,
                               max_buses=case.get("max_buses", 4))
    expected = case.get("expected", {})
    for channel in expected.get("channels", []):
        _check_channel(result, channel, failures)
    for bus in expected.get("buses", []):
        _check_bus(result, bus, failures)
    if expected.get("no_buses") and result["buses"]:
        failures.append(f"expected no buses, got {[b['protocol'] for b in result['buses']]}")
    if expected.get("no_channels") and result["channels"]:
        failures.append(f"expected no channels, got {[c['channel'] for c in result['channels']]}")
    return {"name": case["name"], "passed": not failures, "failures": failures}


def run_all(cases=None):
    cases = load_cases() if cases is None else cases
    results = [run_case(case) for case in cases]
    passed = sum(1 for item in results if item["passed"])
    total = len(results)
    return {
        "ok": passed == total,
        "passed": passed,
        "failed": total - passed,
        "total": total,
        "score": round(passed / total, 3) if total else 0.0,
        "cases": results,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Score pxlogic-agent identification offline.")
    parser.add_argument("--cases", default=str(DEFAULT_CASES),
                        help="case file or directory (default: eval/cases)")
    parser.add_argument("--case", help="run only cases whose name contains this text")
    parser.add_argument("--json", action="store_true", help="print one JSON object")
    args = parser.parse_args(argv)
    cases = load_cases(args.cases)
    if args.case:
        cases = [c for c in cases if args.case in c["name"]]
    report = run_all(cases)
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        for item in report["cases"]:
            status = "PASS" if item["passed"] else "FAIL"
            print(f"{status}  {item['name']}")
            for failure in item["failures"]:
                print(f"      {failure}")
        print(f"{report['passed']}/{report['total']} passed, score {report['score']}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
