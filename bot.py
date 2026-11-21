"""Mech Royale bot entry point (mechfight/1 over stdin/stdout)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mechfight as mf          # noqa: E402
from mech.brain import Bot      # noqa: E402

if __name__ == "__main__":
    mf.run(Bot)
