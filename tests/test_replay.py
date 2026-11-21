"""Replay the kit's recorded match through the bot and report the worst decision time."""
import time
import unittest

from tests.helpers import stream_rows
from mech.brain import Bot


class ReplayTests(unittest.TestCase):
    def test_replay_worst_decision(self):
        rows = stream_rows()
        bot = Bot(rows[0])
        times = []
        for r in rows[1:]:
            if r.get("type") != "obs":
                continue
            t0 = time.perf_counter()
            a = bot.act(r)
            times.append((time.perf_counter() - t0) * 1000)
            self.assertEqual(a["tick"], r["tick"])
            self.assertEqual(len(a["fire"]), 2)
        times.sort()
        print("\nreplay: %d decisions, p50 %.2f ms, p99 %.2f ms, worst %.2f ms"
              % (len(times), times[len(times) // 2], times[int(len(times) * 0.99)], times[-1]))
        self.assertEqual(len(times), 300)
        self.assertLess(times[-1], 20.0)


if __name__ == "__main__":
    unittest.main()
