"""Candidate 1's stuck fix (94e49f9), carried onto 90ffa8a: back off from a wall the tables miss,
then give the goal up the second time at the same wall."""
import unittest

from tests.helpers import self_obs, world
from tests.test_controller import hello
from tests.test_counters import zone_obs
from mech.brain import Bot
from mech.state import Me


class BackOffTests(unittest.TestCase):
    """Sandbox seeds 6, 17 and 20 (candidate 1's first squad batch): stopped by a wall the tables
    do not show, the mover replanned the same route into it for 20-50 s, once hanging in the air."""

    def drive(self, n, grounded=True, z_off=0.0):
        import time
        w = world()
        bot = Bot(hello(), world=w)
        mv = bot.mover
        x, y = 300.0, -60.0
        goal = (360.0, -60.0)
        notes = []
        for k in range(n):
            obs = zone_obs(self_obs(pos=(x, y, w.floor(x, y) + z_off), t=100.0 + 0.1 * k))
            obs["self"]["grounded"] = grounded
            me = Me(obs, bot.cfg)
            bot.zone.update(obs["zone"], me.t)
            mv.go(goal, me, bot.zone, tag="test")
            mv.work(me, bot.zone, time.perf_counter() + 0.02)
            mv.steer(me, bot.zone)
            notes.append(mv.note)
        return mv, goal, notes

    def test_backs_off_then_gives_the_goal_up_at_the_same_wall(self):
        mv, goal, notes = self.drive(120)
        self.assertIn("back off", notes)
        self.assertGreaterEqual(mv.backoffs, 2)
        self.assertTrue(mv.goal_bad(goal, 111.0))
        self.assertFalse(mv.goal_bad(goal, 145.0))       # forgiven after 30 s

    def test_hanging_in_the_air_against_a_wall_backs_off_at_once(self):
        mv, goal, notes = self.drive(25, grounded=False, z_off=0.4)
        self.assertIn("back off", notes[:22])


if __name__ == "__main__":
    unittest.main()
