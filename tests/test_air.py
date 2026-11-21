"""The air plays, fed situations on the real map; the wall hop is flown in tools/kinesim."""
import math
import unittest

from tests.helpers import config, self_obs, world
from mech import air
from mech.state import Me
from tools.kinesim import KMech


def me_at(x, y, z=None, energy=100.0, grounded=True, t=10.0, weapons=None):
    w = world()
    if z is None:
        z = w.floor(x, y)
    obs = self_obs(pos=(x, y, z), energy=energy, t=t, weapons=weapons)
    obs["self"]["grounded"] = grounded
    return Me(obs, config())


def me_from_kmech(m, t):
    obs = self_obs(pos=(m.x, m.y, m.z), energy=m.energy, t=t, vel=(m.vx, m.vy, m.vz))
    obs["self"]["grounded"] = m.grounded
    return Me(obs, config())


class WallHopTests(unittest.TestCase):
    START = (41.0, -124.0)          # ground beside Townshend Hall (roof about 17 m above us)

    def test_finds_a_roof_with_enough_energy(self):
        w = world()
        me = me_at(*self.START)
        tgt = air.WallHop.find(w, me, cfg_mech=config()["mech"])
        self.assertIsNotNone(tgt)
        self.assertGreater(tgt[2] - me.pos[2], 8.0)
        self.assertIsNone(air.WallHop.find(w, me_at(*self.START, energy=15.0), cfg_mech=config()["mech"]))

    def test_prefers_the_side_away_from_the_pursuer(self):
        w = world()
        me = me_at(*self.START)
        pursuer = (me.pos[0] + 60.0, me.pos[1])
        tgt = air.WallHop.find(w, me, away_from=pursuer, cfg_mech=config()["mech"])
        tgt2 = air.WallHop.find(w, me, away_from=(me.pos[0] - 60.0, me.pos[1]), cfg_mech=config()["mech"])
        self.assertIsNotNone(tgt)
        self.assertLessEqual(math.hypot(tgt[0] - pursuer[0], tgt[1] - pursuer[1]) -
                             math.hypot(me.pos[0] - pursuer[0], me.pos[1] - pursuer[1]), 6.0)
        self.assertIsNotNone(tgt2)

    def test_flies_onto_the_roof(self):
        w = world()
        x, y = self.START
        m = KMech(w, x, y)
        t = 10.0
        me = me_from_kmech(m, t)
        tgt = air.WallHop.find(w, me, cfg_mech=config()["mech"])
        hop = air.WallHop()
        hop.start(t, tgt)
        airborne = 0
        for _ in range(80):
            me = me_from_kmech(m, t)
            mv, boost, jump, done = hop.step(me)
            if done:
                break
            m.decide(mv, boost, jump)
            airborne += 0 if m.grounded else 1
            t += 0.1
        self.assertTrue(hop.done)
        self.assertIsNotNone(w.roof_at(m.x, m.y))
        self.assertGreater(m.z, tgt[2] - 1.3)
        self.assertLess(airborne * 0.1, 4.0)          # in the air briefly, not hovering


class PopUpTests(unittest.TestCase):
    def test_rises_just_enough_to_see_over_the_wall(self):
        w = world()
        me = me_at(41.0, -124.0)
        # a target on the far side of Townshend Hall
        tx, ty = 93.0, -112.0
        tgt = (tx, ty, w.floor(tx, ty) + 4.5)
        rise = air.PopUp.find(w, me, tgt, max_rise=20.0, cfg_mech=config()["mech"])
        self.assertIsNotNone(rise)
        self.assertTrue(5.0 < rise <= 21.0, rise)
        # already in sight: nothing to do
        open_me = me_at(300.0, -60.0)
        self.assertIsNone(air.PopUp.find(w, open_me, (330.0, -40.0, w.floor(330, -40) + 4.5)))

    def test_jets_up_then_drops_after_release(self):
        pu = air.PopUp()
        pu.start(10.0, 6.0)
        me = me_at(41.0, -124.0)
        mv, boost, jump, done = pu.step(me)
        self.assertTrue(jump)
        up = me_at(41.0, -124.0, z=me.pos[2] + 3.0, grounded=False, t=10.5)
        mv, boost, jump, done = pu.step(up)
        self.assertTrue(boost)
        high = me_at(41.0, -124.0, z=me.pos[2] + 6.5, grounded=False, t=10.9)
        self.assertTrue(pu.at_height(high))
        mv, boost, jump, done = pu.step(high, laser_released=True)
        self.assertFalse(boost)


class RoofPlayTests(unittest.TestCase):
    ROOF = (47.0, -100.0)        # on Townshend Hall

    def roof_me(self):
        w = world()
        h = w.roof_at(*self.ROOF)
        self.assertIsNotNone(h)
        return me_at(self.ROOF[0], self.ROOF[1], z=h)

    def test_roof_edge_has_a_peek_and_a_hide(self):
        w = world()
        me = self.roof_me()
        tx, ty = 39.0, -138.0          # on the ground 39 m away, near the wall
        res = air.RoofEdge.find(w, me, (tx, ty, w.floor(tx, ty) + 4.5), radius=25.0)
        self.assertIsNotNone(res)
        peek, hide = res
        self.assertNotEqual(peek, hide)

    def test_drop_attack_lands_behind_an_enemy_below(self):
        w = world()
        me = self.roof_me()
        ex, ey = 20.0, -80.0
        land = air.DropAttack.find(w, me, (ex, ey, w.floor(ex, ey)), enemy_yaw=math.pi)   # facing west
        self.assertIsNotNone(land)
        self.assertGreater(land[0], ex)     # east of it, behind its facing
        self.assertIsNone(air.DropAttack.find(w, me_at(300.0, -60.0), (310.0, -60.0, 0.0), 0.0))

    def test_last_circle_roof(self):
        w = world()
        me = me_at(41.0, -124.0)
        spot = air.LastCircleRoof.find(w, me, self.ROOF, 12.0)
        self.assertIsNotNone(spot)
        self.assertIsNone(air.LastCircleRoof.find(w, me, (300.0, -60.0), 12.0))
        self.assertIsNone(air.LastCircleRoof.find(w, me, self.ROOF, 80.0))


if __name__ == "__main__":
    unittest.main()
