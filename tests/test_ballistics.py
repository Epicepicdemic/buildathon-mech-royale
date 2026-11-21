import math
import unittest

from tests.helpers import config, self_obs, track, weapon
from mech import ballistics as bal
from mech.state import Me
from mech.gunner import Gunner, Target
from mech.doctrine import Intent


def fly(origin, yaw, pitch, speed, t, offset=bal.SHOT_OFFSET):
    d = (math.cos(pitch) * math.cos(yaw), math.cos(pitch) * math.sin(yaw), math.sin(pitch))
    return tuple(origin[i] + d[i] * (offset + speed * t) for i in range(3))


class LeadTests(unittest.TestCase):
    def test_lead_hits_crossing_target(self):
        origin = (0.0, 0.0, 7.0)
        for tgt, vel in [((100.0, 0.0, 4.5), (0.0, 11.7, 0.0)), ((60.0, 40.0, 6.0), (-19.5, 3.0, 0.0)),
                         ((200.0, -30.0, 2.0), (5.0, 11.0, 0.0))]:
            yaw, pitch, tf, aimp = bal.lead(origin, tgt, vel, 252.0, latency=0.0)
            bullet = fly(origin, yaw, pitch, 252.0, tf)
            where = tuple(tgt[i] + vel[i] * tf for i in range(3))
            self.assertLess(math.dist(bullet, where), 0.05)

    def test_lead_includes_latency_and_own_motion(self):
        origin = (0.0, 0.0, 7.0)
        tgt, vel, ovel, lat = (80.0, 0.0, 4.5), (0.0, -10.0, 0.0), (0.0, 8.0, 0.0), 0.1
        yaw, pitch, tf, aimp = bal.lead(origin, tgt, vel, 252.0, latency=lat, ovel=ovel)
        o2 = (origin[0] + ovel[0] * lat, origin[1] + ovel[1] * lat, origin[2])
        bullet = fly(o2, yaw, pitch, 252.0, tf)
        where = tuple(tgt[i] + vel[i] * (lat + tf) for i in range(3))
        self.assertLess(math.dist(bullet, where), 0.05)

    def test_reversal_range_matches_the_numbers(self):
        r = bal.reversal_range(252.0)
        self.assertTrue(74.0 <= r <= 80.0, r)
        self.assertGreater(bal.reversal_range(252.0, accel=12.0), 150.0)


class FireWhenAlignedTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config()

    def _fire(self, aim_yaw):
        obs = self_obs(aim=(aim_yaw, 0.0))
        me = Me(obs, self.cfg)
        tr = track(pos=(60.0, 0.0, 0.0), vel=(0.0, 0.0, 0.0))
        g = Gunner(self.cfg, None)
        it = Intent("assault", tr)
        it.fire = "long"
        aim, fire, reload, info = g.control(me, it, Target.from_track(tr, me.t))
        return aim, fire

    def test_no_fire_until_torso_arrives(self):
        aim, fire = self._fire(1.0)          # torso pointing 57 degrees off
        self.assertFalse(fire[0])
        self.assertAlmostEqual(aim[0], 0.0, places=2)

    def test_fires_when_on_target(self):
        aim, fire = self._fire(0.0)
        self.assertTrue(fire[0])

    def test_lead_allows_for_turn_time(self):
        """With the torso far off, the lead point is computed for when it gets there."""
        cfg = self.cfg
        tr = track(pos=(100.0, 0.0, 0.0), vel=(0.0, 11.0, 0.0))
        me_off = Me(self_obs(aim=(-1.2, 0.0)), cfg)
        me_on = Me(self_obs(aim=(0.11, 0.0)), cfg)
        g = Gunner(cfg, None)
        it = Intent("assault", tr)
        it.fire = "long"
        a_off, _, _, _ = g.control(me_off, it, Target.from_track(tr, me_off.t))
        a_on, _, _, _ = g.control(me_on, it, Target.from_track(tr, me_on.t))
        # 1.3 rad of slewing at 4 rad/s is ~0.33 s: the target moves ~3.6 m more, so the lead is larger
        self.assertGreater(a_off[0], a_on[0] + 0.02)


class BurstTests(unittest.TestCase):
    def test_short_bursts_beyond_reversal_range(self):
        cfg = config()
        tr = track(pos=(150.0, 0.0, 0.0), vel=(0.0, 11.0, 0.0))
        tr.hist = [(9.0 + 0.1 * k, 150.0, 0.0, 0.0, 11.0 if k % 4 < 2 else -11.0) for k in range(10)]
        g = Gunner(cfg, None, seed=1)
        it = Intent("assault", tr)
        it.fire = "long"
        fired = 0
        ammo = 12
        rests = 0
        for k in range(30):
            t = 10.0 + 0.1 * k
            obs = self_obs(aim=(0.0, 0.0), t=t)
            obs["self"]["weapons"][0]["ammo"] = ammo
            me = Me(obs, cfg)
            tgt = Target.from_track(tr, t)
            yaw, pitch, _, _ = bal.lead(me.muzzle, tgt.center(), tgt.vel, 252.0, 0.05)
            obs["self"]["aim"] = [yaw, pitch]
            me = Me(obs, cfg)
            aim, fire, _, info = g.control(me, it, tgt)
            if fire[0]:
                fired += 1
                ammo -= 1
            elif info.get("burst") == "short":
                rests += 1
        self.assertGreater(rests, 0)            # it paused between bursts
        self.assertLess(fired, 25)

    def test_long_burst_on_still_target(self):
        cfg = config()
        tr = track(pos=(150.0, 0.0, 0.0), vel=(0.0, 0.0, 0.0))
        g = Gunner(cfg, None)
        it = Intent("assault", tr)
        it.fire = "long"
        for k in range(8):
            t = 10.0 + 0.1 * k
            me = Me(self_obs(t=t), cfg)
            tgt = Target.from_track(tr, t)
            yaw, pitch, _, _ = bal.lead(me.muzzle, tgt.center(), tgt.vel, 252.0, 0.05)
            obs = self_obs(aim=(yaw, pitch), t=t)
            obs["self"]["weapons"][0]["ammo"] = 12 - k
            me = Me(obs, cfg)
            aim, fire, _, info = g.control(me, it, tgt)
            self.assertTrue(fire[0])


if __name__ == "__main__":
    unittest.main()


class TimerResidueTests(unittest.TestCase):
    def test_reload_residue_does_not_block_the_gun(self):
        cfg = config()
        obs = self_obs(aim=(0.0, 0.0))
        obs["self"]["weapons"][0]["reload"] = 6.938893903907228e-17
        me = Me(obs, cfg)
        self.assertEqual(me.by_kind["gun"].reload, 0.0)
        tr = track(pos=(60.0, 0.0, 0.0))
        it = Intent("assault", tr)
        it.fire = "long"
        aim, fire, _, _ = Gunner(cfg, None).control(me, it, Target.from_track(tr, me.t))
        self.assertTrue(fire[0])
