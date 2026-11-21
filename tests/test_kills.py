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


class FightingRetreatTests(unittest.TestCase):
    """Item 2 (done in abd7856, tests/test_redteam.py): breaking off keeps the torso and the gun on
    the pursuer, and never starts a wall hop while it can see us. End to end, beside a roof the
    version 2 wall hop would have jumped onto (Wilce Student Health Center, 11.9 m)."""

    def test_losing_retreat_fires_faces_and_stays_on_the_ground(self):
        w = world()
        bot = Bot(hello(), world=w)
        bot.tactics.opening_done = True
        me = (20.0, -85.0)
        px, py = 20.0, -35.0                      # pursuer 50 m north, looking at us, hitting us
        aim = [0.0, 0.0]
        acts = []
        for k in range(14):
            obs = zone_obs(self_obs(pos=(me[0], me[1], w.floor(*me)), t=150.0 + 0.1 * k, aim=aim, hp=700))
            p = enemy(9, px, py, w, -math.pi / 2, weapons=("gun", "laser"))
            obs["mechs"] = [p]
            if k % 2 == 0:
                obs["events"] = [{"type": "damaged", "amount": 45, "weapon": "gun", "bearing": math.pi / 2, "by": 9}]
            a = bot.act(obs)
            aim = a["aim"]
            acts.append(a)
            self.assertIsNone(bot.tactics.air)            # no wall hop, no air play of any kind
            self.assertFalse(a["boost"] and a["jump"])
        self.assertTrue(any(a["fire"][0] for a in acts))
        to_p = math.atan2(py - me[1], px - me[0])
        self.assertLess(abs(math.atan2(math.sin(acts[-1]["aim"][0] - to_p), math.cos(acts[-1]["aim"][0] - to_p))), 0.3)


if __name__ == "__main__":
    unittest.main()


class LoadoutTests(unittest.TestCase):
    """Item 3: laser and lightning together; the gun is what goes when both slots are full."""

    def take_at_feet(self, weapons, kind):
        w = world()
        bot = Bot(hello(), world=w)
        bot.tactics.opening_done = True
        me = (300.0, -60.0)
        obs = zone_obs(self_obs(pos=(me[0], me[1], w.floor(*me)), t=100.0, weapons=weapons))
        obs["pickups"] = [{"id": 8001, "kind": kind, "pos": [me[0] + 1.0, me[1], w.floor(*me)]}]
        return bot.act(obs)

    def test_laser_with_gun_and_lightning_drops_the_gun(self):
        a = self.take_at_feet([weapon("gun"), weapon("lightning")], "laser")
        self.assertEqual(a.get("take"), 8001)
        self.assertEqual(a.get("drop"), 0)          # the gun's slot, not lightning's

    def test_lightning_with_gun_and_laser_drops_the_gun(self):
        a = self.take_at_feet([weapon("gun"), weapon("laser")], "lightning")
        self.assertEqual(a.get("take"), 8001)
        self.assertEqual(a.get("drop"), 0)

    def test_never_drops_one_of_the_pair_for_the_other(self):
        a = self.take_at_feet([weapon("laser"), weapon("lightning")], "laser")
        self.assertNotIn("take", a)

    def test_mortar_still_goes_first(self):
        a = self.take_at_feet([weapon("gun"), weapon("mortar")], "lightning")
        self.assertEqual(a.get("take"), 8001)
        self.assertEqual(a.get("drop"), 1)

    def test_fights_without_a_gun(self):
        """Laser and lightning, no gun: the bot still fights a close attacker (lightning fires)."""
        w = world()
        bot = Bot(hello(), world=w)
        bot.tactics.opening_done = True
        me = (300.0, -60.0)
        aim = [0.0, 0.0]
        fired = False
        for k in range(10):
            obs = zone_obs(self_obs(pos=(me[0], me[1], w.floor(*me)), t=120.0 + 0.1 * k, aim=aim,
                                    weapons=[weapon("laser"), weapon("lightning")]))
            obs["mechs"] = [enemy(9, 330.0, -60.0, w, math.pi, weapons=("gun",))]
            obs["events"] = [{"type": "damaged", "amount": 45, "weapon": "gun", "bearing": 0.0, "by": 9}] if k % 2 == 0 else []
            a = bot.act(obs)
            aim = a["aim"]
            fired = fired or a["fire"][1] or a["fire"][0]
        self.assertTrue(fired)
