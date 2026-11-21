import math
import unittest

from tests.helpers import config, self_obs, track, weapon
from mech import ballistics as bal
from mech.state import Me
from mech.gunner import Gunner, Target
from mech.doctrine import Intent


class LaserSequenceTests(unittest.TestCase):
    """Hold to charge, keep the torso on the target, release when full and aligned."""

    def run_sequence(self, aim_err_fn, steps=14, t0=10.0):
        cfg = config()
        tr = track(pos=(120.0, 0.0, 0.0))
        g = Gunner(cfg, None)
        it = Intent("corner", tr)
        it.fire = "long"
        charge = 0.0
        out = []
        for k in range(steps):
            t = t0 + 0.1 * k
            ws = [weapon("gun"), weapon("laser", charge=charge)]
            tgt = Target.from_track(tr, t)
            me = Me(self_obs(t=t, weapons=ws), cfg)
            yaw, pitch = bal.aim_angles(tgt.center()[0] - me.muzzle[0], tgt.center()[1] - me.muzzle[1],
                                        tgt.center()[2] - me.muzzle[2])
            me = Me(self_obs(t=t, weapons=ws, aim=(yaw + aim_err_fn(k), pitch)), cfg)
            aim, fire, _, info = g.control(me, it, tgt)
            out.append((charge, fire[1], info.get("laser")))
            # the server: holding builds charge (0.1 s per decision), letting go fires it
            if fire[1]:
                charge = min(0.75, charge + 0.1) if charge > 0 or k == 0 or out[-1][2] == "start" else charge
                if charge == 0.0:
                    charge = 0.1
            elif charge > 0:
                out.append(("fired", charge))
                break
        return out

    def test_charges_then_releases_when_full_and_aligned(self):
        seq = self.run_sequence(lambda k: 0.0)
        self.assertEqual(seq[0][2], "start")
        fired = [s for s in seq if s[0] == "fired"]
        self.assertEqual(len(fired), 1)
        self.assertGreaterEqual(fired[0][1], 0.75 - 1e-6)

    def test_holds_while_off_target(self):
        # aim stays 0.3 rad off for the whole sequence: never released by us
        seq = self.run_sequence(lambda k: 0.3, steps=12)
        self.assertFalse(any(s[0] == "fired" for s in seq))
        self.assertTrue(all(s[1] for s in seq if s[0] != "fired"))

    def test_releases_once_aim_arrives(self):
        seq = self.run_sequence(lambda k: 0.3 if k < 10 else 0.0, steps=16)
        fired = [s for s in seq if s[0] == "fired"]
        self.assertEqual(len(fired), 1)

    def test_shorted_laser_does_not_start(self):
        cfg = config()
        tr = track(pos=(80.0, 0.0, 0.0))
        g = Gunner(cfg, None)
        it = Intent("assault", tr)
        it.fire = "long"
        ws = [weapon("gun"), weapon("laser")]
        me = Me(self_obs(t=10.0, weapons=ws), cfg)
        aim, fire, _, _ = g.control(me, it, Target.from_track(tr, 10.0),
                                    events=[{"type": "effect", "effect": "short", "by": 5}])
        self.assertFalse(fire[1])


def shell_landing(tube, yaw, pitch, v, g, target_z, offset=bal.SHOT_OFFSET):
    cp, sp = math.cos(pitch), math.sin(pitch)
    x0 = tube[0] + offset * cp * math.cos(yaw)
    y0 = tube[1] + offset * cp * math.sin(yaw)
    z0 = tube[2] + offset * sp
    vz = v * sp
    # z0 + vz t - g t^2 / 2 = target_z, descending root
    disc = vz * vz + 2 * g * (z0 - target_z)
    t = (vz + math.sqrt(disc)) / g
    return (x0 + v * cp * math.cos(yaw) * t, y0 + v * cp * math.sin(yaw) * t, t)


class MortarTests(unittest.TestCase):
    def test_low_and_high_arcs_land_on_target(self):
        tube = (0.0, 0.0, 9.5)
        # high arcs need pitch <= max_pitch (1.3 rad), which puts them beyond ~125 m
        for tgt in [(140.0, 30.0, 1.0), (100.0, -130.0, 15.0), (200.0, 0.0, -4.0), (30.0, 10.0, 0.0)]:
            for high in (False, True):
                sol = bal.mortar_solution(tube, tgt, (0, 0, 0), 72.0, 22.0, high, -0.7, 1.3)
                if high and math.hypot(tgt[0], tgt[1]) < 125:
                    self.assertIsNone(sol)
                    continue
                self.assertIsNotNone(sol, (tgt, high))
                yaw, pitch, tf, _ = sol
                x, y, t = shell_landing(tube, yaw, pitch, 72.0, 22.0, tgt[2])
                self.assertLess(math.hypot(x - tgt[0], y - tgt[1]), 0.5, (tgt, high))
                self.assertAlmostEqual(t, tf, delta=0.1)
            lo = bal.mortar_solution(tube, tgt, (0, 0, 0), 72.0, 22.0, False)
            hi = bal.mortar_solution(tube, tgt, (0, 0, 0), 72.0, 22.0, True)
            if hi is not None:
                self.assertLess(lo[1], hi[1])

    def test_leads_a_moving_target(self):
        tube = (0.0, 0.0, 9.5)
        tgt, vel = (150.0, 0.0, 1.0), (0.0, 5.0, 0.0)
        yaw, pitch, tf, aimp = bal.mortar_solution(tube, tgt, vel, 72.0, 22.0, False)
        x, y, t = shell_landing(tube, yaw, pitch, 72.0, 22.0, tgt[2])
        self.assertLess(math.hypot(x - (tgt[0] + vel[0] * t), y - (tgt[1] + vel[1] * t)), 1.0)

    def test_out_of_range(self):
        self.assertIsNone(bal.mortar_solution((0, 0, 9.5), (300.0, 0.0, 0.0), (0, 0, 0), 72.0, 22.0))

    def test_lob_over_a_building(self):
        """The gunner picks the high arc when the low one would hit a wall."""
        from tests.helpers import world
        w = world()
        cfg = config()
        g = Gunner(cfg, w)
        # a tall block between us and a target 160 m north: the low arc hits it, the high one clears
        me_pos = (-60.0, -170.0)
        tgt_pos = (-60.0, -10.0)
        f0 = w.floor(*me_pos)
        f1 = w.floor(*tgt_pos)
        ws = [weapon("gun"), weapon("mortar")]
        me = Me(self_obs(pos=(me_pos[0], me_pos[1], f0), weapons=ws), cfg)
        tgt = Target(9, "mech", (tgt_pos[0], tgt_pos[1], f1), (0, 0, 0), 2.5, 9.0, False)
        sol = g.mortar_aim(me, me.by_kind["mortar"], tgt)
        self.assertIsNotNone(sol)
        tube = (me_pos[0], me_pos[1], f0 + 9.5)
        low = bal.mortar_solution(tube, (tgt_pos[0], tgt_pos[1], f1 + 1.0), (0, 0, 0), 72.0, 22.0, False)
        self.assertFalse(g.arc_clear(tube, low[0], low[1], 72.0, 22.0, low[2]))
        self.assertGreater(sol[1], 0.8)        # a high lob


if __name__ == "__main__":
    unittest.main()
