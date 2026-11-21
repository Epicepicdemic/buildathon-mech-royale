import math
import unittest

from tests.helpers import config, self_obs, world
from mech.state import Me
from mech.threats import Threats


def mech_entry(tid, pos, vel=(0, 0, 0), hp=1400, weapons=("gun",), aim=(0.0, 0.0), charging=False):
    return {"id": tid, "kind": "mech", "pos": list(pos), "vel": list(vel), "aim": list(aim), "hp": hp,
            "max_hp": 1400, "cubes": 0, "weapons": list(weapons), "charging": charging}


class ThreatMemoryTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config()
        self.th = Threats(self.cfg, None, 3)

    def step(self, t, mechs=(), sounds=(), events=(), beams=(), pos=(0.0, 0.0, 0.0)):
        obs = self_obs(t=t, pos=pos)
        obs["mechs"] = list(mechs)
        obs["sounds"] = list(sounds)
        obs["events"] = list(events)
        obs["beams"] = list(beams)
        me = Me(obs, self.cfg)
        self.th.update(obs, me)
        return me

    def test_remembers_position_velocity_weapons_hp(self):
        self.step(10.0, [mech_entry(5, (100, 0, 0), (0, 10, 0), hp=900, weapons=("gun", "laser"))])
        tr = self.th.tracks[5]
        self.assertEqual(tr.hp, 900)
        self.assertIn("laser", tr.weapons)
        self.assertTrue(tr.in_obs)
        # gone from the observation: extrapolated briefly, uncertainty grows
        self.step(11.0)
        p = tr.est_pos(11.0)
        self.assertFalse(tr.in_obs)
        self.assertAlmostEqual(p[1], 10.0, delta=0.01)
        self.assertGreater(tr.uncertainty(11.0), 10.0)
        self.assertIn(tr, self.th.active_mechs(11.0))
        self.step(30.0)
        self.assertNotIn(tr, self.th.active_mechs(30.0))

    def test_sound_attaches_to_known_track(self):
        self.step(10.0, [mech_entry(5, (100, 0, 0))])
        self.step(11.0, sounds=[{"kind": "gun", "bearing": 0.05, "dist": "mid", "age": 0.05}])
        self.assertAlmostEqual(self.th.tracks[5].t_heard, 11.0)
        self.assertEqual(self.th.contacts, [])

    def test_unknown_sound_becomes_sector_contact(self):
        self.step(10.0, sounds=[{"kind": "step", "bearing": 1.0, "dist": "near", "age": 0.1}])
        self.assertEqual(len(self.th.contacts), 1)
        c = self.th.contacts[0]
        d = math.hypot(*c.pos)
        self.assertLess(d, 60.0)
        self.assertAlmostEqual(math.atan2(c.pos[1], c.pos[0]), 1.0, places=3)
        # a second sound on nearly the same bearing merges
        self.step(10.5, sounds=[{"kind": "step", "bearing": 1.05, "dist": "near", "age": 0.1}])
        self.assertEqual(len(self.th.contacts), 1)
        self.assertEqual(self.th.contacts[0].count, 2)
        # far gunfire uses the gun's carry range for the band
        self.step(11.0, sounds=[{"kind": "gun", "bearing": -2.0, "dist": "far", "age": 0.1}])
        far = [c for c in self.th.contacts if c.band == "far"][0]
        self.assertTrue(160 < math.hypot(*far.pos) < 320)

    def test_damage_from_unseen_attacker(self):
        self.step(10.0, events=[{"type": "damaged", "amount": 45, "weapon": "gun", "bearing": 2.0, "by": 7}])
        tr = self.th.tracks[7]
        self.assertTrue(tr.phantom)
        self.assertAlmostEqual(math.atan2(tr.pos[1], tr.pos[0]), 2.0, places=3)
        self.assertIsNotNone(self.th.unseen_attacker(10.0))
        self.assertEqual(self.th.t_taken, 10.0)

    def test_zone_damage_is_not_an_attacker(self):
        self.step(10.0, events=[{"type": "damaged", "amount": 20, "weapon": "zone", "bearing": None, "by": None}])
        self.assertEqual(self.th.tracks, {})
        self.assertEqual(self.th.t_zone_dmg, 10.0)

    def test_kill_and_engaged_elsewhere(self):
        self.step(10.0, [mech_entry(5, (100, 0, 0), hp=1000), mech_entry(6, (100, 50, 0))])
        # 5 loses HP we did not deal
        self.step(10.1, [mech_entry(5, (100, 0, 0), hp=900), mech_entry(6, (100, 50, 0))])
        self.assertTrue(self.th.tracks[5].engaged_elsewhere(10.1))
        # 6 fires a laser that ends near 5
        self.step(10.2, [mech_entry(5, (100, 0, 0), hp=900), mech_entry(6, (100, 50, 0))],
                  beams=[{"by": 6, "kind": "laser", "path": [[100, 50, 7], [100, 0, 4]], "age": 0.05}])
        self.assertTrue(self.th.tracks[6].engaged_elsewhere(10.2))
        self.step(10.3, events=[{"type": "kill", "target": 5}])
        self.assertTrue(self.th.tracks[5].dead)
        self.assertEqual(self.th.kills, 1)


class SightEstimateTests(unittest.TestCase):
    def test_building_blocks_and_open_ground_sees(self):
        w = world()
        # Thompson library sits around (138, -139); look across it east-west
        a = (60.0, -140.0)
        b = (215.0, -140.0)
        fa, fb = w.floor(*a), w.floor(*b)
        self.assertFalse(w.sees((a[0], a[1], fa + 8), (b[0], b[1], fb), 9.0))
        # two points on the open Oval
        c, d = (300.0, -60.0), (330.0, -40.0)
        self.assertTrue(w.sees((c[0], c[1], w.floor(*c) + 8), (d[0], d[1], w.floor(*d)), 9.0))


if __name__ == "__main__":
    unittest.main()
