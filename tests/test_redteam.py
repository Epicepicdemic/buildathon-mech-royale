"""The red team's findings (analysis/mech/redteam/REPORT.md), as decisions the controller must make.

1. A break never means holding fire while something can still hit us; cornered, the bot fights.
2. No wall hop while a mech that can see us is close enough to hit us in the air; airborne under fire,
   the torso is on the attacker.
3. Other mechs merely being near is not a reason to break (tests/test_doctrine.py).
4. The rear is watched: the torso does not just look along the legs; held spots are not held for a
   fixed time.
"""
import math
import unittest

from tests.helpers import self_obs, weapon, world
from tests.test_controller import hello
from tests.test_counters import enemy, zone_obs
from mech.brain import Bot
from mech.geom import angdiff
from mech import tactics as T


def run(bot, me_xy, t0, n, mechs_fn, hp=1400, events_fn=None, aim=None):
    w = bot.world
    out = []
    aim = aim or [0.0, 0.0]
    for k in range(n):
        t = t0 + 0.1 * k
        obs = zone_obs(self_obs(pos=(me_xy[0], me_xy[1], w.floor(*me_xy)), t=t, aim=aim, hp=hp))
        obs["mechs"] = mechs_fn(k)
        if events_fn is not None:
            obs["events"] = events_fn(k)
        a = bot.act(obs)
        aim = a["aim"]
        out.append(a)
    return out


class ReturnFireTests(unittest.TestCase):
    def test_low_hp_break_keeps_shooting_while_seen(self):
        """Below a quarter of our HP and losing we break, but the attacker can still hit us, so the
        torso stays on it and the gun fires (version 2 held fire: the vulture finished it)."""
        w = world()
        bot = Bot(hello(), world=w)
        bot.tactics.opening_done = True
        me = (300.0, -60.0)                     # open lawn
        ex, ey = 360.0, -60.0                   # 60 m east, looking at us
        hits = lambda k: [{"type": "damaged", "amount": 45, "weapon": "gun", "bearing": 0.0, "by": 9}] if k % 3 == 0 else []
        acts = run(bot, me, 120.0, 12, lambda k: [enemy(9, ex, ey, w, math.pi, weapons=("gun", "laser"))],
                   hp=300, events_fn=hits)
        fired = sum(1 for a in acts if a["fire"][0])
        self.assertGreater(fired, 3)
        # the torso is on the attacker, not on the way out
        self.assertLess(angdiff(acts[-1]["aim"][0], 0.0), 0.3)

    def test_holds_fire_once_sight_is_broken(self):
        """Healing needs 5 s without damage: with nobody able to hit us the gun stays quiet."""
        w = world()
        bot = Bot(hello(), world=w)
        bot.tactics.opening_done = True
        me = (300.0, -60.0)
        acts = run(bot, me, 120.0, 6, lambda k: [], hp=500)
        self.assertFalse(any(a["fire"][0] for a in acts))

    def test_shooters_count_mechs_behind_trees(self):
        """In the observation with no building between us: a shooter even if a crown hides it."""
        w = world()
        bot = Bot(hello(), world=w)
        bot.tactics.opening_done = True
        me = (300.0, -60.0)
        run(bot, me, 120.0, 2, lambda k: [enemy(9, 360.0, -60.0, w, math.pi)],
            events_fn=lambda k: [{"type": "damaged", "amount": 45, "weapon": "gun", "bearing": 0.0, "by": 9}])
        tr = bot.threats.tracks[9]
        tr.los = False                         # pretend a crown is in the way of our eye
        from mech.state import Me
        m = Me(zone_obs(self_obs(pos=(me[0], me[1], w.floor(*me)), t=120.2)), bot.cfg)
        self.assertIn(tr, bot.tactics.shooters(m))


class AirTests(unittest.TestCase):
    def test_no_wall_hop_with_a_close_watcher(self):
        w = world()
        bot = Bot(hello(), world=w)
        tac = bot.tactics
        from mech.state import Me
        m = Me(zone_obs(self_obs(pos=(20.0, -85.0, w.floor(20.0, -85.0)), t=100.0)), bot.cfg)
        tr = bot.threats.tracks.setdefault(9, __import__("mech.threats", fromlist=["Track"]).Track(9, "mech", 99.0))
        tr.pos = [20.0, 15.0, w.floor(20.0, 15.0)]
        tr.in_obs = True
        tr.sees_us = True
        self.assertFalse(tac._air_safe(m, [tr]))
        tr.sees_us = False
        tr.t_hit_us = -1e9
        self.assertTrue(tac._air_safe(m, [tr]))
        tr.pos = [20.0, -85.0 + T.AIR_SAFE_M + 20.0, 0.0]
        tr.sees_us = True
        self.assertTrue(tac._air_safe(m, [tr]))

    def test_airborne_under_fire_faces_the_attacker(self):
        w = world()
        bot = Bot(hello(), world=w)
        bot.tactics.opening_done = True
        from mech import air as A
        hop = A.WallHop()
        hop.start(119.9, (25.0, -85.0, 20.0))
        bot.tactics.air = hop
        me = (20.0, -85.0)
        ex, ey = 20.0, 15.0                     # 100 m north, shooting at us
        acts = run(bot, me, 120.0, 8, lambda k: [enemy(9, ex, ey, w, -math.pi / 2)],
                   events_fn=lambda k: [{"type": "damaged", "amount": 45, "weapon": "gun", "bearing": math.pi / 2, "by": 9}])
        self.assertLess(angdiff(acts[-1]["aim"][0], math.pi / 2), 0.3)
        self.assertTrue(any(a["fire"][0] for a in acts))


class RearWatchTests(unittest.TestCase):
    def test_torso_sweeps_when_nothing_is_known(self):
        """Walking with no threat known, the torso turns through every direction within a few
        seconds instead of looking along the legs."""
        w = world()
        bot = Bot(hello(), world=w)
        bot.tactics.opening_done = True
        me = (300.0, -60.0)
        acts = run(bot, me, 100.0, 30, lambda k: [])
        yaws = [a["aim"][0] for a in acts]
        sectors = {int(((y + math.pi) % (2 * math.pi)) / (math.pi / 4)) for y in yaws}
        self.assertGreaterEqual(len(sectors), 7)

    def test_hold_time_varies(self):
        w = world()
        durs = set()
        for seed in range(4):
            h = hello()
            h["seed"] = 1000 + seed
            bot = Bot(h, world=w)
            bot.tactics.opening_done = True
            run(bot, (300.0, -60.0), 100.0, 2, lambda k: [])
            durs.add(round(bot.tactics.hold_dur, 2))
        self.assertGreater(len(durs), 1)


if __name__ == "__main__":
    unittest.main()
