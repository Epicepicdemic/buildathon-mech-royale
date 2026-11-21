"""Replay the kit's recorded match through the bot and report the worst decision time.

The assertion uses the process's CPU time per decision (time.thread_time), which is what the
1-core match container limits; wall time is reported too but swings with load on a busy machine.
"""
import time
import unittest

from tests.helpers import stream_rows
from mech.brain import Bot


class ReplayTests(unittest.TestCase):
    def test_replay_worst_decision(self):
        rows = stream_rows()
        bot = Bot(rows[0])
        wall, cpu = [], []
        for r in rows[1:]:
            if r.get("type") != "obs":
                continue
            w0, c0 = time.perf_counter(), time.thread_time()
            a = bot.act(r)
            cpu.append((time.thread_time() - c0) * 1000)
            wall.append((time.perf_counter() - w0) * 1000)
            self.assertEqual(a["tick"], r["tick"])
            self.assertEqual(len(a["fire"]), 2)
        wall.sort()
        cpu.sort()
        print("\nreplay: %d decisions, cpu p50 %.2f p99 %.2f worst %.2f ms; wall worst %.2f ms"
              % (len(cpu), cpu[len(cpu) // 2], cpu[int(len(cpu) * 0.99)], cpu[-1], wall[-1]))
        self.assertEqual(len(cpu), 300)
        self.assertLess(cpu[-1], 20.0)


if __name__ == "__main__":
    unittest.main()
