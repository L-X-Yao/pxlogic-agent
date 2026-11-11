"""Tests for the command line (px_logic.main) without hardware.

On POSIX a small fake pxlobster script stands in for the real binary.

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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from px_logic import LogicAnalyzer, main  # noqa: E402

# Mimics the stdout format of pxlobster v0.2.4 (src/main.zig).
FAKE_PXLOBSTER = """#!{python}
import os
import sys
import time
import numpy as np

args = sys.argv[1:]
here = os.path.dirname(os.path.abspath(sys.argv[0]))
if "--scan" in args:
    print("16C0:05DC  PX-Logic U2 channel 32")
elif "--prime-fw" in args:
    print("16C0:05DC  PX-Logic U2 channel 32  [fw loaded]")
elif "--stop" in args:
    open(os.path.join(here, "stopped"), "w").close()
elif "-t" in args:
    time.sleep(30)  # the trigger never fires
elif "-o" in args:
    rate = int(args[args.index("--samplerate") + 1])
    ms = int(args[args.index("--time") + 1])
    t = np.arange(rate * ms // 1000)
    period = rate // 400
    pwm = (t % period) < period // 4  # 400 Hz, 25 % duty on CH0
    pwm.astype(np.uint32).tofile(args[args.index("-o") + 1])
else:
    sys.exit(2)
"""


def run_main(argv, env):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, env), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


class MissingBinaryTest(unittest.TestCase):
    def test_json_error_and_exit_code(self):
        missing = os.path.join(tempfile.gettempdir(), "no-such-pxlobster")
        code, out, _ = run_main(["--json", "--scan"], {"PXLOBSTER": missing})
        self.assertEqual(code, 1)
        result = json.loads(out)
        self.assertFalse(result["ok"])
        self.assertIn("PXLOBSTER", result["error"])


class CaptureModeTest(unittest.TestCase):
    def test_loop_and_unknown_modes_are_rejected(self):
        la = LogicAnalyzer(exe_path="pxlobster")  # never started: the check comes first
        with self.assertRaisesRegex(ValueError, "pxlobster --stop"):
            la.capture(mode="loop")
        with self.assertRaises(ValueError):
            la.capture(mode="bogus")


@unittest.skipIf(os.name == "nt", "the fake pxlobster is a POSIX script")
class FakeDeviceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        fake = os.path.join(self.tmp, "pxlobster")
        with open(fake, "w") as f:
            f.write(FAKE_PXLOBSTER.format(python=sys.executable))
        os.chmod(fake, 0o755)
        self.env = {"PXLOBSTER": fake}

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_scan_json(self):
        code, out, _ = run_main(["--json", "--scan"], self.env)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {
            "ok": True,
            "devices": [{"usb_id": "16c0:05dc", "label": "PX-Logic U2 channel 32"}],
        })

    def test_prime_json(self):
        code, out, _ = run_main(["--json", "--prime"], self.env)
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["ok"])

    def test_capture_json(self):
        code, out, err = run_main(["--json", "--rate", "1e6", "--time", "50"], self.env)
        self.assertEqual(code, 0, err)
        result = json.loads(out)  # stdout holds nothing but the JSON object
        self.assertEqual(result["sample_rate"], 1_000_000)
        self.assertEqual(result["samples"], 50_000)
        [ch0] = result["active_channels"]
        self.assertEqual(ch0["channel"], 0)
        self.assertAlmostEqual(ch0["freq_hz"], 400.0)
        self.assertAlmostEqual(ch0["duty_pct"], 25.0)
        self.assertAlmostEqual(ch0["high_us"], 625.0)
        self.assertIn("samples @", err)  # progress logs went to stderr

    def test_trigger_timeout_sends_stop(self):
        code, out, _ = run_main(["--json", "--trigger", "0=r", "--timeout", "1"], self.env)
        self.assertEqual(code, 1)
        result = json.loads(out)
        self.assertFalse(result["ok"])
        self.assertIn("timed out", result["error"])
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "stopped")))

    def test_human_output_without_json(self):
        code, out, _ = run_main(["--rate", "1e6", "--time", "50"], self.env)
        self.assertEqual(code, 0)
        self.assertIn("CH00:", out)


if __name__ == "__main__":
    unittest.main()
