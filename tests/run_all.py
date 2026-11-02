"""Run the offline test suite: python3 -I tests/run_all.py   (stub CLIs only; no network, no real agents)."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
sys.dont_write_bytecode = True
sys.path[:0] = [os.path.dirname(HERE), HERE]

if __name__ == "__main__":
    suite = unittest.defaultTestLoader.discover(HERE, pattern="test_*.py", top_level_dir=HERE)
    result = unittest.TextTestRunner(verbosity=2 if "-v" in sys.argv else 1).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
