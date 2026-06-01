"""Make the repo root importable in tests (so ``import clanklib`` works)."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
