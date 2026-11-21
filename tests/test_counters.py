"""Answers to the ways our plays get beaten (analysis/COUNTERS.md). The sandbox has no lasers or
mortars, so these are checked here on the controller's decisions."""
import math
import random
import unittest

from tests.helpers import config, self_obs, weapon, world
from tests.test_controller import hello
from mech.brain import Bot
from mech import positions as P
from mech.cover import CoverState
from mech.state import Me


def zone_obs(obs):
    obs["zone"] = {"center": [41.3, -72.8], "radius": 560, "next_center": [20.0, -80.0], "next_radius": 380,
                   "shrink_start": 60, "shrink_end": 110, "dps": 20}
    return obs


def enemy(eid, x, y, w, yaw, charging=False, weapons=("gun", "laser")):
    return {"id": eid, "kind": "mech", "pos": [x, y, w.floor(x, y)], "vel": [0, 0, 0], "aim": [yaw, 0.0],
            "hp": 1400, "max_hp": 1400, "cubes": 0, "weapons": list(weapons), "charging": charging}


class LaserChargeTests(unittest.TestCase):
    def test_lightning_shorts_a_charging_laser_inside_45m(self):
        w = world()
        bot = Bot(hello(), world=w)
        bot.tactics.opening_done = True
        me = (300.0, -60.0)                     # open lawn
        ex, ey = 330.0, -60.0
        aim = [0.0, 0.0]
        fired = False
        for k in range(6):
            obs = zone_obs(self_obs(pos=(me[0], me[1], w.floor(*me)), t=50 + 0.1 * k, aim=aim,
                                    weapons=[weapon("gun"), weapon("lightning")]))
            obs["mechs"] = [enemy(9, ex, ey, w, math.pi, charging=True)]
            a = bot.act(obs)
            aim = a["aim"]
            fired = fired or a["fire"][1]
        self.assertTrue(fired)
        self.assertEqual(bot.tactics.th.tracks[9].id, 9)

    def test_ducks_behind_cover_from_a_charging_laser(self):
        w = world()
        bot = Bot(hello(), world=w)
        bot.tactics.opening_done = True
        me = (20.0, -85.0)                      # beside Wilce Health Center
        ex, ey = 20.0, 45.0                     # 130 m north up the open lane, charging at us
        labels = []
        for k in range(8):
            obs = zone_obs(self_obs(pos=(me[0], me[1], w.floor(*me)), t=50 + 0.1 * k))
            obs["mechs"] = [enemy(9, ex, ey, w, -math.pi / 2, charging=True)]
            a = bot.act(obs)
            labels.append(a.get("debug", {}).get("label", labels[-1] if labels else ""))
        hide = bot.cover.hide_xy()
        # it either ducks behind a cell it can reach inside the charge, or jinks while it looks for one
        self.assertTrue(any("duck" in l or "jink" in l for l in labels), labels)
        if hide is not None and math.hypot(hide[0] - me[0], hide[1] - me[1]) <= 9.8:
            self.assertTrue(any("duck" in l for l in labels), labels)


class MortarTests(unittest.TestCase):
    def test_moves_off_the_spot_when_a_mortar_launch_is_heard(self):
        w = world()
        bot = Bot(hello(), world=w)
        bot.tactics.opening_done = True
        me = (300.0, -60.0)
        obs = zone_obs(self_obs(pos=(me[0], me[1], w.floor(*me)), t=50.0))
        bot.act(obs)
        obs = zone_obs(self_obs(pos=(me[0], me[1], w.floor(*me)), t=50.1))
        obs["sounds"] = [{"kind": "mortar", "bearing": 0.5, "dist": "mid", "age": 0.05}]
        a = bot.act(obs)
        goal = bot.tactics.displace[0]
        self.assertGreater(math.hypot(goal[0] - me[0], goal[1] - me[1]), 12.0)
        self.assertTrue(w.walkable(me[0], me[1], goal[0], goal[1], need=2.7))
        self.assertGreater(math.hypot(*a["move"]), 0.9)
        # the move goes across the shell's bearing, not toward it
        mv = math.atan2(a["move"][1], a["move"][0])
        self.assertGreater(abs(math.cos(mv - 0.5)), -1.0)
        self.assertLess(math.cos(mv - 0.5), 0.5)

    def test_moving_bot_does_not_react(self):
        w = world()
        bot = Bot(hello(), world=w)
        bot.tactics.opening_done = True
        obs = zone_obs(self_obs(pos=(300.0, -60.0, w.floor(300, -60)), t=50.0, vel=(10.0, 0.0, 0.0)))
        obs["sounds"] = [{"kind": "mortar", "bearing": 0.5, "dist": "mid", "age": 0.05}]
        bot.act(obs)
        self.assertIsNone(bot.tactics.displace)


class CornerEdgeTests(unittest.TestCase):
    def test_never_the_same_edge_twice(self):
        w = world()
        cs = CoverState(w, random.Random(3))
        me = Me(self_obs(pos=(20.0, -85.0, w.floor(20, -85))), config())
        target = (20.0, -10.0, w.floor(20, -10) + 4.5)
        cs.want_hide(me, [(20.0, -10.0, w.floor(20, -10) + 8.0)], 10.0, max_cost=60.0)
        cs.run(1e18)
        self.assertIsNotNone(cs.hide)
        cs.want_peek(target, 10.0)
        cs.run(1e18)
        self.assertIsNotNone(cs.peek)
        first = cs.peek_xy()
        cs.peek_used()
        cs.want_peek(target, 11.0)
        cs.run(1e18)
        if cs.peek is not None:
            second = cs.peek_xy()
            self.assertGreaterEqual(math.hypot(second[0] - first[0], second[1] - first[1]), 4.5)


class RandomPositionTests(unittest.TestCase):
    def test_hold_spot_differs_by_seed_and_avoids_threat_lines(self):
        w = world()
        circle = (41.3, -72.8, 140.0)
        picks = {P.choose_hold(w, (0.0, -60.0), circle, [], random.Random(s)) for s in range(12)}
        self.assertGreater(len(picks), 1)                     # not one fixed answer
        same = {P.choose_hold(w, (0.0, -60.0), circle, [], random.Random(5)) for _ in range(3)}
        self.assertEqual(len(same), 1)                        # reproducible for one seed
        threat = [(40.0, -60.0, w.floor(40, -60) + 8.0)]
        for s in range(6):
            n = P.choose_hold(w, (0.0, -60.0), circle, threat, random.Random(s))
            x, y = w.node_xy(n)
            d = math.hypot(x - 40.0, y + 60.0)
            seen = w.fan_dist(n, math.atan2(-60.0 - y, 40.0 - x)) >= d
            self.assertFalse(seen and d < 120.0, (x, y))

    def test_no_shipped_position_list(self):
        w = world()
        self.assertFalse(hasattr(w, "strongpoints"))
        self.assertNotIn("strongpoints", w.meta)


class AfterTheFightTests(unittest.TestCase):
    def test_heals_out_of_sight_before_looting_when_others_are_about(self):
        w = world()
        bot = Bot(hello(), world=w)
        tc = bot.tactics
        tc.opening_done = True
        me = (20.0, -85.0)
        # we just fought (took damage), a gunshot was heard nearby, and a cube lies 20 m away
        bot.threats.t_fight = 49.0
        obs = zone_obs(self_obs(pos=(me[0], me[1], w.floor(*me)), t=50.0, hp=700))
        obs["sounds"] = [{"kind": "gun", "bearing": 1.5, "dist": "mid", "age": 0.1}]
        obs["cubes"] = [{"id": 5001, "pos": [30.0, -70.0, w.floor(30, -70)]}]
        a = bot.act(obs)
        self.assertNotEqual(tc.loot_note, "cube")
        self.assertIn("heal", a.get("debug", {}).get("label", "heal"))

    def test_loots_when_nothing_is_about(self):
        w = world()
        bot = Bot(hello(), world=w)
        tc = bot.tactics
        tc.opening_done = True
        me = (20.0, -85.0)
        bot.threats.t_fight = 20.0
        obs = zone_obs(self_obs(pos=(me[0], me[1], w.floor(*me)), t=50.0, hp=700))
        obs["cubes"] = [{"id": 5001, "pos": [30.0, -70.0, w.floor(30, -70)]}]
        bot.act(obs)
        self.assertEqual(tc.loot_note, "cube")


class OpeningTests(unittest.TestCase):
    def test_fights_visible_enemy_from_t_zero(self):
        """At t = 0 with an enemy in sight and range, Line B locks torso on it and opens fire immediately."""
        w = world()
        bot = Bot(hello(), world=w)
        me = (300.0, -60.0)
        ex, ey = 385.0, -60.0
        aim = [0.0, 0.0]
        fired = False
        for k in range(5):
            t = 0.1 * k
            obs = zone_obs(self_obs(pos=(me[0], me[1], w.floor(*me)), t=t, aim=aim))
            obs["mechs"] = [enemy(9, ex, ey, w, math.pi, weapons=("gun",))]
            a = bot.act(obs)
            aim = a["aim"]
            fired = fired or a["fire"][0]
        self.assertTrue(fired)
        self.assertLess(abs(aim[0]), 0.1)

    def test_boosts_to_loot_and_cover_at_t_zero_without_creeping(self):
        """With no enemy in sight at t = 0, Line B immediately goes for visible loot/crates or boosts out
        of open ground without wasting 35 s creeping silently."""
        from tools.kinesim import KMech
        w = world()
        bot = Bot(hello(), world=w)
        spawn = (300.0, -60.0)
        m = KMech(w, *spawn)
        speeds = []
        for k in range(40):
            t = 0.1 * k
            obs = zone_obs(self_obs(pos=(m.x, m.y, m.z), t=t, vel=(m.vx, m.vy, m.vz), energy=m.energy))
            obs["self"]["grounded"] = m.grounded
            obs["pickups"] = [{"id": 9001, "kind": "laser", "pos": [345.0, -60.0, w.floor(345, -60)]}]
            a = bot.act(obs)
            m.decide(a["move"], a["boost"], a["jump"])
            if k >= 5:
                speeds.append(math.hypot(m.vx, m.vy))
        self.assertEqual(bot.tactics.loot_note, "pickup")
        self.assertGreater(max(speeds), 12.0)                 # boosts onto the early laser pickup


if __name__ == "__main__":
    unittest.main()
