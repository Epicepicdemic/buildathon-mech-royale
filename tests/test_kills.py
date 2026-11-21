"""Kills first (the chair, Sat 07:20): practice points are about kills a match against the hunting
squad, and a kill's drop is what makes the next kill likelier."""
import math
import unittest

from tests.helpers import self_obs, weapon, world
from tests.test_controller import hello
from tests.test_counters import enemy, zone_obs
from mech.brain import Bot


def kill_then(bot, me, victim, cubes, extra_mechs=(), hp=1000, n=8):
    """A fight ends with our kill of `victim` (id 9 at `victim`); then `cubes` lie around it."""
    w = bot.world
    acts = []
    for k in range(3):
        obs = zone_obs(self_obs(pos=(me[0], me[1], w.floor(*me)), t=100.0 + 0.1 * k, hp=hp))
        e = enemy(9, victim[0], victim[1], w, math.pi)
        e["hp"] = 40
        obs["mechs"] = [e] + list(extra_mechs)
        obs["sounds"] = [{"kind": "gun", "bearing": 2.0, "dist": "mid", "age": 0.1}]
        bot.act(obs)
    for k in range(n):
        obs = zone_obs(self_obs(pos=(me[0], me[1], w.floor(*me)), t=100.3 + 0.1 * k, hp=hp))
        obs["mechs"] = list(extra_mechs)
        if k == 0:
            obs["events"] = [{"type": "kill", "target": 9}]
            obs["alive"] = 15
        obs["cubes"] = [{"id": 5000 + i, "pos": [x, y, w.floor(x, y)]} for i, (x, y) in enumerate(cubes)]
        acts.append(bot.act(obs))
    return acts


class KillDropTests(unittest.TestCase):
    def test_collects_the_cubes_of_a_kill_despite_noise(self):
        """Version 2 walked 30 m away from them because a gun had been heard and HP was under 93%."""
        w = world()
        bot = Bot(hello(), world=w)
        bot.tactics.opening_done = True
        me = (300.0, -60.0)
        victim = (330.0, -60.0)
        cubes = [(330.0, -60.0), (331.0, -58.0), (329.0, -62.0)]
        acts = kill_then(bot, me, victim, cubes)
        labels = [a.get("debug", {}).get("label", "") for a in acts]
        self.assertTrue(any(l.startswith("kill drop") for l in labels), labels)
        # heading for the drop (the route may still be planning on a loaded machine)
        mv = bot.mover
        goal = mv.goal if mv.goal is not None else mv.plan_goal
        self.assertIsNotNone(goal)
        self.assertLess(min(math.hypot(goal[0] - x, goal[1] - y) for x, y in cubes), 3.0)

    def test_leaves_a_drop_another_mech_covers(self):
        w = world()
        bot = Bot(hello(), world=w)
        bot.tactics.opening_done = True
        me = (300.0, -60.0)
        victim = (330.0, -60.0)
        watcher = enemy(8, 330.0, 20.0, w, -math.pi / 2)     # 80 m north, open line onto the drop
        acts = kill_then(bot, me, victim, [(330.0, -60.0)], extra_mechs=[watcher])
        labels = [a.get("debug", {}).get("label", "") for a in acts]
        self.assertFalse(any(l.startswith("kill drop") for l in labels), labels)


if __name__ == "__main__":
    unittest.main()
