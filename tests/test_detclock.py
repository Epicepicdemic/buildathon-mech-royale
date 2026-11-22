"""The search budget is counted in work units, not wall-clock time (mech/nav.py WorkBudget), so the
bot's actions do not depend on how fast the machine is: the same observations give the same actions
whether a clock read takes 0.05 ms or 0.3 ms (a fast and a slow machine, as far as the bot can tell).
The only clock use left is the 30 ms safety stop (brain.SAFETY_S); with 0.4 ms per read it trips here,
which in a real match means a decision that has really run 30 ms."""
import itertools
import json
import time
import unittest

from tests.helpers import stream_rows
from mech.brain import Bot


def actions(tick):
    real = time.perf_counter
    c = itertools.count()
    time.perf_counter = lambda: next(c) * tick
    try:
        rows = stream_rows()
        bot = Bot(rows[0])
        out = []
        for r in rows[1:]:
            if r.get("type") == "obs":
                a = dict(bot.act(r))
                a.pop("debug", None)
                out.append(json.dumps(a, sort_keys=True))
        return out
    finally:
        time.perf_counter = real


class DeterministicBudgetTests(unittest.TestCase):
    def test_actions_do_not_depend_on_machine_speed(self):
        fast, slow = actions(0.00005), actions(0.0003)
        self.assertEqual(len(fast), 300)
        self.assertEqual(fast, slow)


if __name__ == "__main__":
    unittest.main()
