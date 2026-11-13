"""The offline smoke cases must keep passing as detectors change."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eval.runner import DEFAULT_CASES, load_cases, run_all  # noqa: E402


class EvalSmokeTest(unittest.TestCase):
    def test_smoke_cases_pass(self):
        report = run_all(load_cases(DEFAULT_CASES))
        self.assertEqual(report["failed"], 0, report)
        self.assertGreaterEqual(report["total"], 10)


if __name__ == "__main__":
    unittest.main()
