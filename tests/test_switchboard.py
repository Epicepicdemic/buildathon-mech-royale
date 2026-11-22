"""The switchboard (mech/squad_settings.py): B2's values by default, read only in squad mode,
refusing unknown or mistyped overrides, and actually changing squad play when set."""
import json
import math
import os
import tempfile
import unittest

from tests.helpers import config, self_obs, world
from tests.test_controller import hello
from tests.test_counters import enemy, zone_obs
from mech import doctrine as D
from mech import squad_settings as SS
from mech.brain import Bot

ME = (300.0, -60.0)


def scenario(bot, t0, n=60):
    """A varied fight: a hunter closing from 160 m to 40 m with a laser, a crate, a drone, an
    upgrade offer, damage taken and a reload heard."""
    w = world()
    labels = []
    for k in range(n):
        t = t0 + 0.1 * k
        obs = zone_obs(self_obs(pos=(ME[0], ME[1], w.floor(*ME)), t=t, hp=1400 - 8 * k))
        d = 160.0 - 2.0 * k
        e = enemy(9, ME[0] + d, ME[1], w, math.pi, charging=(k % 20 > 14), weapons=("gun", "laser"))
        e["hp"] = 1400 - 15 * k
        other = enemy(10, ME[0] + 90.0, ME[1] + 80.0, w, math.pi * 1.25, weapons=("gun", "mortar"))
        obs["mechs"] = [e, other, {"id": 104, "kind": "npc", "pos": [ME[0] - 60.0, ME[1] + 20.0, w.floor(ME[0] - 60, ME[1] + 20)],
                                   "vel": [0, 0, 0], "aim": [0.0, 0.0], "hp": 350, "max_hp": 350}]
        obs["crates"] = [{"id": 1009, "pos": [ME[0] - 30.0, ME[1] - 10.0, w.floor(ME[0] - 30, ME[1] - 10)], "hp": 300.0}]
        ev = []
        if k % 5 == 0:
            ev.append({"type": "damaged", "amount": 45, "weapon": "gun", "bearing": 0.0, "by": 9})
        obs["events"] = ev
        obs["sounds"] = [{"kind": "reload", "bearing": 0.0, "dist": "mid", "age": 0.1}] if k == 30 else []
        if k == 10:
            obs["offer"] = {"choices": [{"id": "armor"}, {"id": "gun_rate"}, {"id": "jets"}]}
        bot.act(obs)
        labels.append(bot.last_label or "")
    return labels


class SwitchboardTests(unittest.TestCase):
    def setUp(self):
        self.saved = dict(SS.S)
        SS.READS.clear()

    def tearDown(self):
        SS.S.clear()
        SS.S.update(self.saved)
        SS.TRACE = False
        SS.READS.clear()

    def test_defaults_are_b2_values(self):
        b2 = {"lose_s": 1.5, "lose_near_m": 160.0, "lose_hp_two": 0.55, "lose_hp_three": 0.80, "lose_hp_alone": 0.35,
              "finish_hp": 0.30, "isolate_m": 24.0, "assault_ratio": D.ASSAULT_RATIO, "even_ratio": D.EVEN_RATIO,
              "corner_take": D.CORNER_TAKE, "their_dps_mult": 1.0, "gear_until_s": 65.0, "gear_kills": 1,
              "gear_cubes": 6, "gear_cubes_alone": 10, "gear_defend_m": 55.0, "gear_defend_hp": 0.5,
              "gear_near_m": 180.0, "gear_min_m": 120.0, "gear_max_m": 145.0, "hunt_aim_rad": 0.2,
              "hunt_aim_s": 1.0, "hunt_memory_s": 10.0, "want_lightning_m": 38.0, "want_gun_m": 62.0,
              "want_laser_m": 85.0, "floor_lightning_m": D.HOUSE_LIGHTNING_M, "floor_mortar_m": D.HOUSE_MORTAR_M,
              "finish_on": 0, "finish_m": D.FINISH_M, "finish_target_hp": D.FINISH_TARGET_HP,
              "finish_own_hp": D.FINISH_OWN_HP, "sticky": D.STICKY, "sticky_weak": D.STICKY_WEAK,
              "reload_s": D.RELOAD_S, "reload_discount": 0.25, "partial_frac": 0.7, "mortar_mult": 1.6,
              "facing_away_mult": 1.0, "boost_low": 8.0, "boost_resume": 40.0, "upgrade_order": []}
        for k, v in b2.items():
            self.assertEqual(SS.DEFAULTS[k], v, k)
        self.assertFalse(os.path.exists(SS.PATH), "the default build carries no override file")

    def test_free_for_all_reads_no_knob(self):
        bot = Bot(hello(), world=world())
        bot.tactics.mode.locked = True          # a free-for-all
        SS.TRACE = True
        scenario(bot, 40.0)
        self.assertEqual(SS.READS, [], sorted(set(SS.READS)))

    def test_squad_mode_reads_them(self):
        bot = Bot(hello(), world=world())
        bot.tactics.mode.mode = "squad"
        SS.TRACE = True
        scenario(bot, 40.0)
        self.assertGreater(len(set(SS.READS)), 20, sorted(set(SS.READS)))

    def test_a_variant_changes_squad_play_and_not_free_for_all(self):
        base_sq = scenario(self._squad(), 40.0, n=30)
        base_ffa = scenario(self._ffa(), 40.0, n=30)
        SS.S["gear_on"] = 0
        SS.S["assault_ratio"] = 0.5
        self.assertNotEqual(scenario(self._squad(), 40.0, n=30), base_sq)
        self.assertEqual(scenario(self._ffa(), 40.0, n=30), base_ffa)

    def _squad(self):
        bot = Bot(hello(), world=world())
        bot.tactics.mode.mode = "squad"
        return bot

    def _ffa(self):
        bot = Bot(hello(), world=world())
        bot.tactics.mode.locked = True
        return bot

    def test_unknown_or_mistyped_overrides_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.json")
            json.dump({"lose_hp_tow": 0.4}, open(p, "w"))
            with self.assertRaises(KeyError):
                SS.load(p)
            json.dump({"lose_hp_two": "low"}, open(p, "w"))
            with self.assertRaises(TypeError):
                SS.load(p)
            json.dump({"lose_hp_two": 0.4, "upgrade_order": ["armor"]}, open(p, "w"))
            s = SS.load(p)
            self.assertEqual((s["lose_hp_two"], s["upgrade_order"], s["lose_s"]), (0.4, ["armor"], 1.5))


if __name__ == "__main__":
    unittest.main()
