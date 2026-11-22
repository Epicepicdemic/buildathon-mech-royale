"""The clean-room pieces (mech/cleanroom.py): each switch changes play the way the spec says, and
each counter holds (analysis/mech/CLEANROOM-SPEC.md; implemented clean-room from our behaviour spec)."""
import math
import time
import unittest

from tests.helpers import self_obs, weapon, world
from tests.test_controller import hello
from tests.test_counters import enemy, zone_obs
from mech import cleanroom_settings as CRS
from mech.brain import Bot

ME = (300.0, -60.0)          # open lawn (tests/test_counters.py)


def obs_at(w, t, hp=1400, energy=100, weapons=None, pos=ME):
    return zone_obs(self_obs(pos=(pos[0], pos[1], w.floor(*pos)), t=t, hp=hp, energy=energy, weapons=weapons))


class CleanroomTests(unittest.TestCase):
    def setUp(self):
        self.saved = dict(CRS.S)

    def tearDown(self):
        CRS.S.clear()
        CRS.S.update(self.saved)

    def run_bot(self, n, make, switches):
        CRS.S.update(switches)
        w = world()
        bot = Bot(hello(), world=w)
        acts = []
        aim = [0.0, 0.0]
        for k in range(n):
            obs = make(w, 20.0 + 0.1 * k, k)
            obs["self"]["aim"] = list(aim)          # the torso turns as we ask
            a = bot.act(obs)
            aim = a["aim"]
            acts.append((a, bot.last_label or ""))
        return bot, acts

    # ---------------------------------------------------------------- 1. gear first
    def test_gear_leaves_a_distant_passive_mech_alone(self):
        def make(w, t, k):
            o = obs_at(w, t)
            o["mechs"] = [enemy(9, ME[0] + 150.0, ME[1], w, math.pi / 2)]      # looking away, never fires
            return o
        _, off = self.run_bot(20, make, {"gear": 0})
        _, on = self.run_bot(20, make, {"gear": 1})
        self.assertTrue(any(a["fire"][0] for a, _ in off[5:]), "B2 shoots it")
        self.assertFalse(any(a["fire"][0] for a, _ in on[5:]), "gear first leaves it alone")

    def test_gear_phase_ends_at_gear_until_s(self):
        def make(w, t, k):
            o = obs_at(w, t)
            o["mechs"] = [enemy(9, ME[0] + 150.0, ME[1], w, math.pi / 2)]
            return o
        _, ended = self.run_bot(20, make, {"gear": 1, "gear_until_s": 15.0})     # the run starts at 20 s
        self.assertTrue(any(a["fire"][0] for a, _ in ended[5:]), "after the phase B2 shoots it again")

    def test_gear_fights_a_rusher_that_hits_us(self):
        """Counter: a rival rushing us from spawn is fought at once."""
        def make(w, t, k):
            o = obs_at(w, t, hp=1400 - 10 * k)
            o["mechs"] = [enemy(9, ME[0] + 150.0 - 3 * k, ME[1], w, math.pi)]
            o["events"] = [{"type": "damaged", "amount": 10, "weapon": "gun", "bearing": 0.0, "by": 9}]
            return o
        _, on = self.run_bot(20, make, {"gear": 1})
        self.assertTrue(any(a["fire"][0] for a, _ in on[5:]))

    def test_gear_fights_a_close_mech(self):
        def make(w, t, k):
            o = obs_at(w, t)
            o["mechs"] = [enemy(9, ME[0] + 40.0, ME[1], w, math.pi / 2)]
            return o
        _, on = self.run_bot(15, make, {"gear": 1})
        self.assertTrue(any(a["fire"][0] for a, _ in on[5:]))

    # ---------------------------------------------------------------- 6. hunt
    def test_hunt_geared_and_healthy_fights_a_passive_mech(self):
        def make(w, t, k):
            o = obs_at(w, t, weapons=[weapon("gun"), weapon("laser")])
            o["self"]["cubes"] = 4
            o["mechs"] = [enemy(9, ME[0] + 150.0, ME[1], w, math.pi / 2)]
            return o
        _, nohunt = self.run_bot(20, make, {"gear": 1, "hunt": 0})
        _, hunt = self.run_bot(20, make, {"gear": 1, "hunt": 1})
        self.assertFalse(any(a["fire"][0] or a["fire"][1] for a, _ in nohunt[5:]), "gear leaves it alone")
        self.assertTrue(any(a["fire"][0] or a["fire"][1] for a, _ in hunt[5:]), "hunting, it is fought")

    def test_hunt_not_while_hurt(self):
        def make(w, t, k):
            o = obs_at(w, t, hp=600, weapons=[weapon("gun"), weapon("laser")])
            o["self"]["cubes"] = 4
            return o
        bot, _ = self.run_bot(5, make, {"gear": 1, "hunt": 1})
        self.assertFalse(bot.tactics.cr.hunting)

    def test_hunt_walks_to_a_remembered_mech(self):
        def make(w, t, k):
            o = obs_at(w, t, weapons=[weapon("gun"), weapon("laser")])
            o["self"]["cubes"] = 4
            if k < 3:
                o["mechs"] = [enemy(9, ME[0] + 150.0, ME[1] + 60.0, w, math.pi / 2)]
            return o
        _, acts = self.run_bot(100, make, {"gear": 1, "hunt": 1})     # out of the 6 s fight memory after ~9 s
        self.assertTrue(any(lab.startswith("cr hunt") for _, lab in acts[80:]), [lab for _, lab in acts[80:]])

    # ---------------------------------------------------------------- 2. energy
    def test_energy_never_boosts_to_loot(self):
        def make(w, t, k):
            o = obs_at(w, t)
            o["pickups"] = [{"id": 501, "kind": "laser", "pos": [ME[0] - 60.0, ME[1], w.floor(ME[0] - 60, ME[1])]}]
            return o
        _, off = self.run_bot(25, make, {"energy": 0})
        _, on = self.run_bot(25, make, {"energy": 1})
        self.assertTrue(any(a["boost"] for a, _ in off), "B2 boosts to a laser 60 m away")
        self.assertFalse(any(a["boost"] for a, _ in on))

    def test_energy_still_dodges_a_laser_charge(self):
        """Counter: the energy we keep is spent on the dodge."""
        def make(w, t, k):
            o = obs_at(w, t)
            o["mechs"] = [enemy(9, ME[0] + 150.0, ME[1], w, math.pi, charging=True)]
            return o
        _, on = self.run_bot(15, make, {"energy": 1})
        self.assertTrue(any(a["boost"] or "laser" in lab or "jink" in lab for a, lab in on))

    # ---------------------------------------------------------------- 3. heal
    def test_heal_holds_still_and_does_not_shoot_crates(self):
        def make(w, t, k):
            o = obs_at(w, t, hp=500)
            o["crates"] = [{"id": 1009, "pos": [ME[0] - 30.0, ME[1] - 10.0, w.floor(ME[0] - 30, ME[1] - 10)], "hp": 300.0}]
            return o
        _, off = self.run_bot(20, make, {"heal": 0})
        _, on = self.run_bot(20, make, {"heal": 1})
        self.assertFalse(any(a["fire"][0] for a, _ in on), "no crate fire while hurt")
        self.assertTrue(any(lab.startswith("cr heal") for _, lab in on))

    def test_heal_shoots_back_at_a_drone_on_us(self):
        """Counter: a drone hitting us stops regeneration anyway, so it is answered."""
        def make(w, t, k):
            o = obs_at(w, t, hp=500)
            o["mechs"] = [{"id": 104, "kind": "npc", "pos": [ME[0] - 40.0, ME[1], w.floor(ME[0] - 40, ME[1]) + 3.0],
                           "vel": [0, 0, 0], "aim": [0.0, 0.0], "hp": 350, "max_hp": 350}]
            o["events"] = [{"type": "damaged", "amount": 25, "weapon": "npc", "bearing": math.pi, "by": 104}]
            return o
        _, on = self.run_bot(20, make, {"heal": 1})
        self.assertTrue(any(a["fire"][0] for a, _ in on[3:]))

    def test_heal_does_not_stand_still_while_hit(self):
        def make(w, t, k):
            o = obs_at(w, t, hp=600 - 5 * k)
            o["events"] = [{"type": "damaged", "amount": 5, "weapon": "gun", "bearing": 0.0, "by": 77}]
            return o
        _, on = self.run_bot(20, make, {"heal": 1})
        self.assertTrue(any(abs(a["move"][0]) + abs(a["move"][1]) > 0.2 for a, _ in on[5:]))

    # ---------------------------------------------------------------- 4. cells
    def test_cells_choose_a_ring_cell_in_a_fight(self):
        def make(w, t, k):
            o = obs_at(w, t, weapons=[weapon("gun"), weapon("laser")])
            o["mechs"] = [enemy(9, ME[0] + 80.0, ME[1], w, math.pi)]
            return o
        _, on = self.run_bot(20, make, {"cells": 1})
        self.assertTrue(any("cell" in lab for _, lab in on))

    # ---------------------------------------------------------------- 5. home spot
    def test_home_keeps_clear_of_a_supply_drop(self):
        from mech.state import Me
        w = world()
        goals = {}
        for home in (0, 1):
            CRS.S.update({"home": home})
            bot = Bot(hello(), world=w)
            o = obs_at(w, 20.0)
            bot.act(o)
            g0 = bot.tactics.hold_spot(Me(o, bot.cfg))
            o = obs_at(w, 20.1)
            o["drops"] = [{"id": 1, "pos": [g0[0], g0[1], w.floor(*g0)], "lands_in": 20.0}]
            bot.act(o)
            bot.tactics.hold_goal = None             # force a fresh pick
            goals[home] = (g0, bot.tactics.hold_spot(Me(o, bot.cfg)))
        g0, g1 = goals[1]
        self.assertGreater(math.hypot(g1[0] - g0[0], g1[1] - g0[1]), 20.0)

    # ---------------------------------------------------------------- all on: still fast
    def test_all_on_decisions_stay_fast(self):
        def make(w, t, k):
            o = obs_at(w, t, hp=1400 - 20 * k, weapons=[weapon("gun"), weapon("laser")])
            o["mechs"] = [enemy(9, ME[0] + 160.0 - 2.0 * k, ME[1], w, math.pi, charging=(k % 20 > 14)),
                          enemy(10, ME[0] + 90.0, ME[1] + 80.0, w, math.pi * 1.25, weapons=("gun", "mortar"))]
            if k % 5 == 0:
                o["events"] = [{"type": "damaged", "amount": 45, "weapon": "gun", "bearing": 0.0, "by": 9}]
            return o
        CRS.S.update({"gear": 1, "energy": 1, "silent": 1, "heal": 1, "cells": 1})
        w = world()
        bot = Bot(hello(), world=w)
        worst = 0.0
        for k in range(60):
            o = make(w, 20.0 + 0.1 * k, k)
            c0 = time.process_time()
            bot.act(o)
            worst = max(worst, (time.process_time() - c0) * 1000)
        self.assertLess(worst, 30.0)


if __name__ == "__main__":
    unittest.main()
