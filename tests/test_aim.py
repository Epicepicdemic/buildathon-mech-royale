"""Aim pieces (aim branch): acceleration lead, laser release timing, gun fire gate."""
import math
import os
import unittest

from tests.helpers import config, self_obs, track, weapon
from mech import ballistics as bal
from mech import profile as PF
from mech.state import Me
from mech.gunner import Gunner, Target
from mech.doctrine import Intent


class Flag:
    def __init__(self, name, value):
        self.name, self.value = name, value

    def __enter__(self):
        self.old = PF.PROFILE.get(self.name)
        PF.PROFILE[self.name] = self.value

    def __exit__(self, *a):
        PF.PROFILE[self.name] = self.old


def fly(origin, yaw, pitch, speed, t, offset=bal.SHOT_OFFSET):
    d = (math.cos(pitch) * math.cos(yaw), math.cos(pitch) * math.sin(yaw), math.sin(pitch))
    return tuple(origin[i] + d[i] * (offset + speed * t) for i in range(3))


class PredictTests(unittest.TestCase):
    def test_starting_target_saturates_at_walk_speed(self):
        p = bal.predict((0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (58.5, 0.0), 1.0, 11.7, steps=40)
        # 0.2 s to reach 11.7 m/s (1.17 m), then 0.8 s at 11.7 m/s
        self.assertAlmostEqual(p[0], 1.17 + 0.8 * 11.7, delta=0.15)

    def test_braking_target_stops_rather_than_reverses(self):
        p = bal.predict((0.0, 0.0, 0.0), (11.7, 0.0, 0.0), (-58.5, 0.0), 1.0, 11.7, steps=40)
        self.assertAlmostEqual(p[0], 11.7 * 0.2 / 2, delta=0.15)
        p2 = bal.predict((0.0, 0.0, 0.0), (11.7, 0.0, 0.0), (-58.5, 0.0), 1.0, 11.7, steps=40, stop=False)
        self.assertLess(p2[0], 0.0)

    def test_gravity_and_floor(self):
        p = bal.predict((0.0, 0.0, 20.0), (0.0, 0.0, 0.0), (0.0, 0.0), 1.0, 11.7, steps=40, g=22.0, zmin=12.0)
        self.assertAlmostEqual(p[2], 12.0, places=6)
        p = bal.predict((0.0, 0.0, 20.0), (0.0, 0.0, 0.0), (0.0, 0.0), 0.5, 11.7, steps=40, g=22.0)
        self.assertAlmostEqual(p[2], 20.0 - 0.5 * 22.0 * 0.25, delta=0.01)

    def test_lead_accel_hits_an_accelerating_target(self):
        origin = (0.0, 0.0, 7.0)
        tgt, vel, acc = (150.0, 0.0, 4.5), (0.0, 0.0, 0.0), (0.0, 58.5)
        yaw, pitch, tf, aimp = bal.lead_accel(origin, tgt, vel, acc, 252.0, latency=0.0, vmax=11.7)
        bullet = fly(origin, yaw, pitch, 252.0, tf)
        where = bal.predict(tgt, vel, acc, tf, 11.7, steps=60)
        self.assertLess(math.dist(bullet, where), 0.1)
        # the constant-velocity lead misses the same target by over a metre
        y0, p0, tf0, _ = bal.lead(origin, tgt, vel, 252.0, latency=0.0)
        self.assertGreater(math.dist(fly(origin, y0, p0, 252.0, tf0), bal.predict(tgt, vel, acc, tf0, 11.7, steps=60)), 1.0)

    def test_lead_accel_zero_acc_equals_lead(self):
        origin = (0.0, 0.0, 7.0)
        for tgt, vel in [((100.0, 0.0, 4.5), (0.0, 11.7, 0.0)), ((60.0, 40.0, 6.0), (-19.5, 3.0, 0.0))]:
            a = bal.lead(origin, tgt, vel, 252.0, latency=0.05, ovel=(3.0, 1.0, 0.0))
            b = bal.lead_accel(origin, tgt, vel, (0.0, 0.0), 252.0, latency=0.05, ovel=(3.0, 1.0, 0.0), vmax=19.5)
            self.assertAlmostEqual(a[0], b[0], places=6)
            self.assertAlmostEqual(a[1], b[1], places=6)


class TrackAccelTests(unittest.TestCase):
    def test_accel_from_history(self):
        tr = track(pos=(100.0, 0.0, 0.0), vel=(0.0, 11.7, 0.0))
        tr.hist = [(9.8, 100.0, -0.6, 0.0, 0.0), (9.9, 100.0, -0.3, 0.0, 5.85), (10.0, 100.0, 0.0, 0.0, 11.7)]
        ax, ay = tr.accel()
        self.assertAlmostEqual(ax, 0.0)
        self.assertAlmostEqual(ay, 58.5, places=3)

    def test_no_accel_across_a_gap_or_out_of_view(self):
        tr = track()
        tr.hist = [(8.0, 0, 0, 0.0, 0.0), (10.0, 0, 0, 11.7, 0.0)]
        self.assertEqual(tr.accel(), (0.0, 0.0))
        tr.hist = [(9.9, 0, 0, 0.0, 0.0), (10.0, 0, 0, 11.7, 0.0)]
        tr.in_obs = False
        self.assertEqual(tr.accel(), (0.0, 0.0))


class GunAccelLeadTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config()

    def _aim(self, on):
        me = Me(self_obs(aim=(0.0, 0.0)), self.cfg)
        tr = track(pos=(150.0, 0.0, 0.0), vel=(0.0, 5.85, 0.0))
        tr.hist = [(9.8, 150.0, 0.0, 0.0, 0.0), (9.9, 150.0, 0.0, 0.0, 0.0), (10.0, 150.0, 0.0, 0.0, 5.85)]
        g = Gunner(self.cfg, None)
        with Flag("accel_lead", on):
            return g._gun_aim(me, Target.from_track(tr, me.t), me.by_kind["gun"])

    def test_accelerating_target_gets_more_lead(self):
        off = self._aim(False)
        on = self._aim(True)
        self.assertGreater(on[0], off[0] + math.radians(0.3))

    def test_off_is_unchanged(self):
        me = Me(self_obs(aim=(0.0, 0.0)), self.cfg)
        tr = track(pos=(150.0, 0.0, 0.0), vel=(0.0, 5.85, 0.0))
        g = Gunner(self.cfg, None)
        with Flag("accel_lead", False):
            a = g._gun_aim(me, Target.from_track(tr, me.t), me.by_kind["gun"])
        b = bal.lead(me.muzzle, Target.from_track(tr, me.t).center(), tr.vel, 252.0, latency=0.05, ovel=me.vel)
        self.assertAlmostEqual(a[0], b[0], places=3)


class LaserAimTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config()

    def test_laser_aims_one_tick_ahead_from_our_moving_muzzle(self):
        lw = weapon("laser", charge=0.4)
        me = Me(self_obs(aim=(0.0, 0.0), vel=(0.0, -6.0, 0.0), weapons=[weapon("gun"), lw]), self.cfg)
        tr = track(pos=(120.0, 0.0, 0.0), vel=(0.0, 11.7, 0.0))
        g = Gunner(self.cfg, None)
        with Flag("laser_aim", True), Flag("accel_lead", True):
            yaw, pitch, p = g._laser_aim(me, Target.from_track(tr, me.t))
        # target 0.585 m along +y, our muzzle 0.3 m along -y
        want = math.atan2(0.585 + 0.3, 120.0)
        self.assertAlmostEqual(yaw, want, places=4)


class EnvOverrideTests(unittest.TestCase):
    def test_env_override(self):
        old = dict(PF.PROFILE)
        try:
            os.environ["MECH_PROFILE"] = "accel_lead=0,laser_aim=1,nonsense=1"
            PF._env_overrides()
            self.assertFalse(PF.PROFILE["accel_lead"])
            self.assertTrue(PF.PROFILE["laser_aim"])
            self.assertNotIn("nonsense", PF.PROFILE)
        finally:
            os.environ.pop("MECH_PROFILE", None)
            PF.PROFILE.clear()
            PF.PROFILE.update(old)


if __name__ == "__main__":
    unittest.main()


class _WallWorld:
    """A wall at x = 50 up to z = 6 m: lines below that height are blocked."""

    def shot_clear(self, a, b):
        if (a[0] - 50.0) * (b[0] - 50.0) >= 0:
            return True
        u = (50.0 - a[0]) / (b[0] - a[0])
        return a[2] + (b[2] - a[2]) * u > 6.0


class ClearAimTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config()

    def _control(self, on, aim_pitch):
        me = Me(self_obs(aim=(0.0, aim_pitch)), self.cfg)
        tr = track(pos=(100.0, 0.0, 0.0), vel=(0.0, 0.0, 0.0))
        g = Gunner(self.cfg, _WallWorld())
        it = Intent("assault", tr)
        it.fire = "long"
        with Flag("clear_aim", on):
            return g.control(me, it, Target.from_track(tr, me.t))

    def test_aims_at_the_head_over_a_low_wall(self):
        aim, fire, _, _ = self._control(True, 0.0)
        # the centre (4.5 m) is behind the wall; the head (7.2 m) is reachable: aim up
        self.assertGreater(aim[1], math.atan2(7.2 - 7.0, 100.0) - 0.01)
        aim2, _, _, _ = self._control(True, aim[1])
        _, fire2, _, _ = self._control(True, aim[1])
        self.assertTrue(fire2[0])

    def test_off_fires_into_the_wall(self):
        aim, fire, _, _ = self._control(False, math.atan2(4.5 - 7.0, 100.0))
        self.assertTrue(fire[0])
        self.assertLess(aim[1], 0.0)


class LaserHoldTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config()

    def test_dropped_target_keeps_the_charge_on_it(self):
        lw = weapon("laser", charge=0.3)
        me = Me(self_obs(aim=(0.0, 0.0), weapons=[weapon("gun"), lw]), self.cfg)
        tr = track(pos=(100.0, 0.0, 0.0))
        g = Gunner(self.cfg, None)
        it = Intent("assault", tr)
        it.fire = "long"
        tgt = Target.from_track(tr, me.t)
        with Flag("laser_hold", True):
            g.control(me, it, tgt)                      # charging on it
            it2 = Intent("reposition", None)
            it2.fire = "none"
            tr.los = False
            aim, fire, _, info = g.control(me, it2, None, face_yaw=2.0)
        self.assertTrue(fire[1])                        # still holding
        self.assertAlmostEqual(aim[0], 0.0, places=2)   # on the last target, not the face yaw
        with Flag("laser_hold", False):
            g2 = Gunner(self.cfg, None)
            g2.control(me, it, tgt)
            aim, fire, _, _ = g2.control(me, it2, None, face_yaw=2.0)
        self.assertFalse(fire[1])                       # the old behaviour lets go

    def test_releases_when_it_comes_back_on_target(self):
        lw = weapon("laser", charge=0.6)
        me = Me(self_obs(aim=(0.0, math.atan2(4.5 - 7.0, 100.0)), weapons=[weapon("gun"), lw]), self.cfg)
        tr = track(pos=(100.0, 0.0, 0.0))
        g = Gunner(self.cfg, None)
        it = Intent("assault", tr)
        it.fire = "long"
        with Flag("laser_hold", True):
            g.control(me, it, Target.from_track(tr, me.t))
            it2 = Intent("reposition", None)
            it2.fire = "none"
            _, fire, _, info = g.control(me, it2, None, face_yaw=2.0)
        self.assertFalse(fire[1])
        self.assertEqual(info.get("laser"), "hold-release")


class ReengageTests(unittest.TestCase):
    def _setup(self):
        from tests.helpers import world
        from tests.test_controller import hello
        from mech.brain import Bot
        from mech import doctrine as D
        w = world()
        bot = Bot(hello(), world=w)
        p0 = (120.0, -40.0, w.floor(120.0, -40.0))
        lw = weapon("laser", cooldown=1.3)
        me = Me(self_obs(pos=p0, t=30.0, weapons=[weapon("gun"), lw]), config())
        tr = track(pos=(220.0, -40.0, w.floor(220.0, -40.0)), t=30.0)
        tr.hist = [(29.0 + 0.1 * k, 220.0, -40.0, 0.0, 0.0) for k in range(11)]
        tr.t_hit_us = 29.6
        tr.t_we_hit = 29.5
        return bot, me, tr, D

    def test_starts_after_a_hit_trade_while_the_laser_recovers(self):
        bot, me, tr, D = self._setup()
        tc = bot.tactics
        with Flag("reengage", True):
            self.assertIsNone(tc._reengage(me, D.CORNER, tr, None, [tr], 60.0))
            self.assertTrue(tc.reengaging)
            self.assertGreater(tc.reeng_until, me.t)
            self.assertEqual(tc.reeng_id, tr.id)
            # not again within REENG_EVERY_S once it is over
            tc.reeng_until = me.t
            self.assertIsNone(tc._reengage(me, D.CORNER, tr, None, [tr], 60.0))
            self.assertFalse(tc.reengaging)

    def test_no_trade_or_switched_off_does_nothing(self):
        bot, me, tr, D = self._setup()
        tr.t_hit_us = 20.0
        with Flag("reengage", True):
            bot.tactics._reengage(me, D.CORNER, tr, None, [tr], 60.0)
        self.assertFalse(bot.tactics.reengaging)
        bot2, me2, tr2, D = self._setup()
        with Flag("reengage", False):
            bot2.tactics._reengage(me2, D.CORNER, tr2, None, [tr2], 60.0)
        self.assertFalse(bot2.tactics.reengaging)

    def test_goal_is_hidden_and_far_enough(self):
        bot, me, tr, D = self._setup()
        tc = bot.tactics
        with Flag("reengage", True):
            tc._reengage(me, D.CORNER, tr, None, [tr], 60.0)
            tc.reeng.run(1e18)
            hx = tc.reeng.hide_xy()
            if hx is not None:          # open ground may have no such cell; then it stays put
                self.assertGreaterEqual(math.hypot(hx[0] - me.pos[0], hx[1] - me.pos[1]), 15.0 - 1.0)
                goal = tc._reengage(me, D.CORNER, tr, None, [tr], 60.0)
                self.assertEqual(tuple(goal), tuple(hx))
