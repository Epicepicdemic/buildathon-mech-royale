"""The jink pattern (mech/dodge.py) against led shots. The figures that chose it come from real
bullets (tools/replay_shooting.py); this checks the property on a synthetic constant-velocity
leading shooter, so it runs without the recordings."""
import math
import random
import unittest

from mech import dodge
from tools import replay_shooting as RS


def hit_rate(leg_s, speed, dist, n=300, seed=5):
    rng = random.Random(seed)
    hits = 0
    for _ in range(n):
        p0 = (0.0, 0.0, 0.0)
        ang = rng.uniform(-math.pi, math.pi)
        v0 = (math.cos(ang) * speed * 0.8, math.sin(ang) * speed * 0.8, 0.0)
        shooter = (dist, 0.0, 7.0)
        threat_yaw = 0.0
        pol = RS.jink_policy(rng, leg_s, threat_yaw, (0.0, 0.0), speed=speed)
        # the shooter leads the observed velocity (the bot's own predictor), fires one tick later
        pts = RS.simulate(p0, v0, pol, rng)
        track = RS.PathTrack(0.0, pts, 0.05)
        c = (p0[0], p0[1], p0[2] + 4.5)
        q = RS.pred_ours(shooter, c, v0, None)
        yaw, pitch = RS.bal.aim_angles(q[0] - shooter[0], q[1] - shooter[1], q[2] - shooter[2])
        d = RS.unit3(yaw, pitch)
        origin = (shooter[0] + d[0] * 3, shooter[1] + d[1] * 3, shooter[2] + d[2] * 3)
        hits += RS.sweep_hit(origin, d, track, RS.FIRE_DELAY, math.dist(origin, q) / RS.SPEED)[0]
    return hits / n


class DodgeTests(unittest.TestCase):
    def test_long_boosting_legs_beat_version_2_at_range(self):
        for dist in (110.0, 170.0):
            v2 = hit_rate(dodge.V2_LEG_S, 11.7, dist)
            v3 = hit_rate(dodge.LEG_S, 19.5, dist)
            self.assertLess(v3, v2 - 0.05, (dist, v2, v3))

    def test_jinker_reverses_and_stays_across_the_line(self):
        j = dodge.Jinker(random.Random(1))
        signs = []
        for k in range(60):
            u = j.direction(0.1 * k, (0.0, 0.0), 0.0)
            self.assertAlmostEqual(abs(u[1]), 1.0, places=6)      # threat due east: legs run north-south
            signs.append(u[1] > 0)
        flips = sum(1 for a, b in zip(signs, signs[1:]) if a != b)
        self.assertGreaterEqual(flips, 3)


if __name__ == "__main__":
    unittest.main()
