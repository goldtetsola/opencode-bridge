import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "tests", "fixtures"))

from a4_live_string_utils import normalize_label


class NormalizeLabelTests(unittest.TestCase):
    def test_strips_and_lowercases(self):
        self.assertEqual(normalize_label("  Hello  "), "hello")


if __name__ == "__main__":
    unittest.main()
