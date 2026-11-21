"""End-to-end decisions through the whole controller on synthetic observations."""
import math
import unittest

from tests.helpers import config, self_obs, stream_rows, world
from mech.brain import Bot
from mech import doctrine as D


def hello():
    return {"type": "hello", "config": config(), "seed": 7, "slot": 3}


class SingleHunterTests(unittest.TestCase):
    """The team lead's case: one hunter walks at a healthy bot that is in cover. The bot fights."""

    def test_fights_an_approaching_hunter_from_cover(self):
        w = world()
        bot = Bot(hello(), world=w)
        bot.tactics.opening_done = True
        bot.tactics.mode.locked = True      # a free-for-all (squad mode paces a lone hunter: test_profile)
        # strongpoint S1 on the east side of Wilce Health Center; the hunter walks down the open lane
        # to the north, in sight the whole way
        me = (20.0, -85.0)
        hx, hy = 20.0, 45.0
        plays = []
        fired = False
        aim = [0.0, 0.0]
        for k in range(40):
            t = 40.0 + 0.1 * k
            obs = self_obs(pos=(me[0], me[1], w.floor(*me)), t=t, aim=aim)
            obs["zone"] = {"center": [41.3, -72.8], "radius": 560, "next_center": [20.0, -80.0], "next_radius": 380,
                           "shrink_start": 60, "shrink_end": 110, "dps": 20}
            hy -= 1.17
            obs["mechs"] = [{"id": 9, "kind": "mech", "pos": [hx, hy, w.floor(hx, hy)], "vel": [0.0, -11.7, 0.0],
                             "aim": [math.atan2(me[1] - hy, me[0] - hx), 0.0], "hp": 1400, "max_hp": 1400, "cubes": 0,
                             "weapons": ["gun"], "charging": False}]
            a = bot.act(obs)
            aim = a["aim"]          # the torso arrives where we pointed it
            plays.append(bot.tactics.last_play)
            fired = fired or a["fire"][0]
        late = plays[10:]
        self.assertNotIn(D.BREAK, late, plays)
        self.assertTrue(all(p in (D.CORNER, D.ASSAULT) for p in late), plays)
        self.assertTrue(fired)


class ZoneOverrideTests(unittest.TestCase):
    def test_leaves_for_the_zone_with_little_slack(self):
        w = world()
        bot = Bot(hello(), world=w)
        bot.tactics.opening_done = True
        me = (300.0, -60.0)
        for k in range(30):
            t = 100.0 + 0.1 * k
            obs = self_obs(pos=(me[0], me[1], w.floor(*me)), t=t)
            # next circle 330 m west, 10 s before it closes: walking there is impossible, so run
            obs["zone"] = {"center": [41.3, -72.8], "radius": 560, "next_center": [-60.0, -60.0], "next_radius": 30,
                           "shrink_start": 60, "shrink_end": 110, "dps": 20}
            a = bot.act(obs)
        self.assertLess(a["move"][0], -0.5)       # heading west
        self.assertTrue(a["boost"])


if __name__ == "__main__":
    unittest.main()


class PopUpIntegrationTests(unittest.TestCase):
    def test_laser_pop_up_over_a_wall(self):
        """Laser held, the enemy is behind Townshend Hall and cannot see us: the controller starts
        the pop-up, charges the laser and jets up."""
        from tests.helpers import weapon
        w = world()
        bot = Bot(hello(), world=w)
        bot.tactics.opening_done = True
        me = (41.0, -124.0)
        ex, ey = 93.0, -112.0
        started = False
        jetted = False
        charging = False
        for k in range(12):
            t = 60.0 + 0.1 * k
            obs = self_obs(pos=(me[0], me[1], w.floor(*me)), t=t,
                           weapons=[weapon("gun"), weapon("laser", charge=min(0.75, 0.1 * k))])
            obs["zone"] = {"center": [41.3, -72.8], "radius": 380, "next_center": [41.0, -100.0], "next_radius": 240,
                           "shrink_start": 105, "shrink_end": 145, "dps": 35}
            obs["mechs"] = [{"id": 9, "kind": "mech", "pos": [ex, ey, w.floor(ex, ey)], "vel": [0, 0, 0],
                             "aim": [0.0, 0.0], "hp": 1400, "max_hp": 1400, "cubes": 0, "weapons": ["gun"],
                             "charging": False}]
            if k > 3:
                obs["self"]["grounded"] = False
                obs["self"]["pos"][2] += 2.0
            a = bot.act(obs)
            air = bot.tactics.air
            started = started or (air is not None and air.name == "popup")
            jetted = jetted or a["boost"] or a["jump"]
            charging = charging or a["fire"][1]
        self.assertTrue(started)
        self.assertTrue(jetted)
        self.assertTrue(charging)


class GhostTargetTests(unittest.TestCase):
    def test_shoots_the_visible_attacker_not_a_vanished_dying_target(self):
        """A nearly dead enemy drops out of view as someone dies; another enemy is shooting us from
        40 m. The bot must not chase the ghost."""
        w = world()
        bot = Bot(hello(), world=w)
        bot.tactics.opening_done = True
        me = (300.0, -60.0)
        aim = [0.0, 0.0]
        fired_at = set()
        for k in range(20):
            t = 50.0 + 0.1 * k
            obs = self_obs(pos=(me[0], me[1], w.floor(*me)), t=t, aim=aim)
            obs["zone"] = {"center": [41.3, -72.8], "radius": 560, "next_center": [300.0, -60.0], "next_radius": 380,
                           "shrink_start": 60, "shrink_end": 110, "dps": 20}
            attacker = {"id": 13, "kind": "mech", "pos": [340.0, -60.0, w.floor(340, -60)], "vel": [0, 0, 0],
                        "aim": [math.pi, 0.0], "hp": 900, "max_hp": 1400, "cubes": 0, "weapons": ["gun"],
                        "charging": False}
            obs["mechs"] = [attacker]
            obs["alive"] = 16
            if k < 5:
                obs["mechs"].append({"id": 6, "kind": "mech", "pos": [300.0, -10.0, w.floor(300, -10)],
                                     "vel": [0, 0, 0], "aim": [0.0, 0.0], "hp": 60, "max_hp": 1400, "cubes": 0,
                                     "weapons": ["gun"], "charging": False})
            else:
                obs["alive"] = 15
            if k % 3 == 0:
                obs["events"] = [{"type": "damaged", "amount": 45, "weapon": "gun", "bearing": 0.0, "by": 13}]
            a = bot.act(obs)
            aim = a["aim"]
            if a["fire"][0] and k >= 6:
                fired_at.add(round(math.degrees(aim[0])))
        self.assertTrue(bot.threats.tracks[6].dead)
        self.assertTrue(fired_at)
        self.assertTrue(all(abs(d) < 10 for d in fired_at), fired_at)      # east, at the attacker


class LootTests(unittest.TestCase):
    def _take(self, pickups, weapons=None):
        w = world()
        bot = Bot(hello(), world=w)
        bot.tactics.opening_done = True
        me = (300.0, -60.0)
        f = w.floor(*me)
        obs = self_obs(pos=(me[0], me[1], f), t=40.0, weapons=weapons)
        obs["pickups"] = [{"id": 8000 + i, "kind": k, "pos": [me[0] + 1.0 + i, me[1], f]} for i, k in enumerate(pickups)]
        return bot.act(obs)

    def test_takes_a_laser_first_and_never_a_mortar(self):
        a = self._take(["mortar", "lightning", "laser"])
        self.assertEqual(a.get("take"), 8002)
        a = self._take(["mortar"])
        self.assertNotIn("take", a)

    def test_swaps_lightning_for_a_laser(self):
        from tests.helpers import weapon
        a = self._take(["laser"], weapons=[weapon("gun"), weapon("lightning")])
        self.assertEqual(a.get("take"), 8000)
        self.assertEqual(a.get("drop"), 1)
