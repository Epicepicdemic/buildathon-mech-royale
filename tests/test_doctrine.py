"""Each play of the fighting doctrine, fed a situation, and the choice between them."""
import math
import unittest

from tests.helpers import config, track
from mech import doctrine as D


def weapons(*kinds):
    w = config()["weapons"]
    return {k: dict(w[k]) for k in kinds}


def sit(enemy=None, others=(), hp=1400, kinds=("gun",), corner=False, unseen=False, energy=100.0, ammo=12,
        long_sight=True, t=10.0):
    return D.Situation(t, (0.0, 0.0), 0.0, hp, 1400, weapons(*kinds), enemy, others, 0, energy, 0.0, corner,
                       unseen, long_sight, config(), False, False, ammo)


class ChoosePlayTests(unittest.TestCase):
    def test_no_threat_repositions(self):
        self.assertEqual(D.choose_play(sit())[0], D.REPOSITION)

    def test_hit_by_unseen_breaks(self):
        self.assertEqual(D.choose_play(sit(unseen=True))[0], D.BREAK)
        hidden = track(pos=(120, 0, 0), los=False, sees_us=True)
        hidden.in_obs = False           # its position is not being reported: we cannot answer it
        self.assertEqual(D.choose_play(sit(hidden, unseen=True))[0], D.BREAK)

    def test_hit_from_behind_trees_is_answered(self):
        # the server still reports it (in_obs) but a crown hides it from our eye: shots pass
        # through trees, so this is a fight, not a break
        behind_trees = track(pos=(120, 0, 0), los=False, sees_us=False)
        behind_trees.fire_line = True
        self.assertNotEqual(D.choose_play(sit(behind_trees, unseen=True))[0], D.BREAK)

    def test_weak_enemy_is_assaulted(self):
        e = track(pos=(50, 0, 0), hp=300)
        play, calc = D.choose_play(sit(e))
        self.assertTrue(calc.finishable)
        self.assertEqual(play, D.ASSAULT)

    def test_even_duel_is_a_corner_fight(self):
        e = track(pos=(60, 0, 0))
        play, calc = D.choose_play(sit(e))
        self.assertTrue(0.8 <= calc.ratio < 1.5, calc.ratio)
        self.assertEqual(play, D.CORNER)

    def test_lightning_up_close_is_an_assault(self):
        e = track(pos=(30, 0, 0))
        play, calc = D.choose_play(sit(e, kinds=("gun", "lightning")))
        self.assertGreaterEqual(calc.ratio, 1.5)
        self.assertEqual(play, D.ASSAULT)

    def test_rear_arc_counts(self):
        facing = track(pos=(60, 0, 0), yaw=math.pi, sees_us=True)        # looking at us
        away = track(pos=(60, 0, 0), yaw=0.0, sees_us=False)             # showing its back
        a = D.FightCalc(sit(facing), facing)
        b = D.FightCalc(sit(away), away)
        self.assertFalse(a.rear)
        self.assertTrue(b.rear)
        self.assertGreater(b.our_dps, a.our_dps * 1.4)

    def test_third_party_turns_an_assault_into_holding_ground(self):
        # version 3: others nearby stop the charge but not the fight; their fire is in the ratio
        e = track(5, pos=(40, 0, 0), hp=1100)
        play, _ = D.choose_play(sit(e, kinds=("gun", "lightning")))
        self.assertEqual(play, D.ASSAULT)
        o1 = track(6, pos=(60, 60, 0), weapons=("gun", "laser"))
        o2 = track(7, pos=(-50, 70, 0), weapons=("gun",))
        play, calc = D.choose_play(sit(e, others=[o1, o2], kinds=("gun", "lightning")))
        self.assertEqual(play, D.CORNER)
        self.assertGreater(calc.ratio, 1.0)

    def test_near_others_alone_is_not_a_break(self):
        e = track(5, pos=(70, 0, 0), hp=1400)
        o1 = track(6, pos=(150, 20, 0), sees_us=False)
        play, calc = D.choose_play(sit(e, others=[o1]))
        self.assertNotEqual(play, D.BREAK)

    def test_outgunned_breaks(self):
        e = track(5, pos=(120, 0, 0), hp=1400, weapons=("gun", "laser"))
        others = [track(6 + k, pos=(100, 40 * (k - 1), 0), weapons=("gun", "laser")) for k in range(3)]
        play, calc = D.choose_play(sit(e, others=others, hp=600))
        self.assertLess(calc.ratio, D.EVEN_RATIO)
        self.assertEqual(play, D.BREAK)

    def test_third_party_with_corner_keeps_a_corner_fight(self):
        e = track(5, pos=(60, 0, 0), hp=900)
        o1 = track(6, pos=(120, 60, 0), sees_us=False)
        play, calc = D.choose_play(sit(e, others=[o1], corner=True))
        self.assertEqual(play, D.CORNER)

    def test_low_hp_breaks(self):
        e = track(pos=(60, 0, 0))
        self.assertEqual(D.choose_play(sit(e, hp=350))[0], D.BREAK)

    def test_outranged_breaks_and_retreating_enemy_is_harassed(self):
        laser = track(pos=(170, 0, 0), weapons=("gun", "laser"), vel=(8, 0, 0))
        laser.hist = [(9.0 + 0.1 * k, 170.0, 0.0, 0.0, 11.0 if k % 4 < 2 else -11.0) for k in range(10)]
        self.assertEqual(D.choose_play(sit(laser))[0], D.BREAK)
        laser.hp = 600.0                  # hurt and moving away: keep it from healing
        play, calc = D.choose_play(sit(laser))
        self.assertTrue(D.retreating(sit(laser), laser))
        self.assertEqual(play, D.HARASS)

    def test_no_usable_weapon_breaks(self):
        e = track(pos=(260, 0, 0), weapons=("laser",))
        self.assertEqual(D.choose_play(sit(e))[0], D.BREAK)

    def test_hysteresis_holds_the_current_play(self):
        e = track(pos=(60, 0, 0), hp=1200)
        s = sit(e)
        calc = D.FightCalc(s, e)
        self.assertTrue(1.3 <= calc.ratio < 1.7, calc.ratio)
        self.assertEqual(D.choose_play(s, calc, prev=D.ASSAULT)[0], D.ASSAULT)
        self.assertEqual(D.choose_play(s, calc, prev=D.CORNER)[0], D.CORNER)


class SymmetryTests(unittest.TestCase):
    def test_equal_mechs_close_up_is_not_an_assault(self):
        """Two equal gun mechs at 40 m are an even fight (both sides hit about 9 shots in 10): fight
        from cover, do not charge. Version 3's first build scored this 1.25 in our favour and lost
        three early duels in the sandbox by assaulting."""
        e = track(pos=(40, 0, 0), hp=1400, sees_us=True)
        play, calc = D.choose_play(sit(e))
        self.assertLess(calc.ratio, D.ASSAULT_RATIO)
        self.assertGreater(calc.ratio, 0.8)
        self.assertEqual(play, D.CORNER)

    def test_both_sides_use_the_same_table(self):
        for d in (30.0, 60.0, 120.0, 190.0):
            self.assertEqual(D.gun_hit_prob(d, True), D.their_gun_hit_prob(d, True))


class HunterTests(unittest.TestCase):
    """A single hunter coming at a healthy bot in cover is fought, not fled (team lead's case)."""

    def approaching(self, weapons=("gun",), dist=90.0):
        # coming straight at us from the east, looking at us
        return track(pos=(dist, 0.0, 0.0), vel=(-11.7, 0.0, 0.0), yaw=math.pi, weapons=weapons, sees_us=True)

    def test_single_gun_hunter_is_fought_from_cover(self):
        for d in (150.0, 90.0, 60.0, 30.0):
            play, calc = D.choose_play(sit(self.approaching(dist=d), corner=True))
            self.assertIn(play, (D.CORNER, D.ASSAULT), (d, play, calc.ratio, calc.ratio_corner))

    def test_laser_hunter_is_fought_from_cover(self):
        play, calc = D.choose_play(sit(self.approaching(("gun", "laser"), 110.0), corner=True))
        self.assertIn(play, (D.CORNER, D.ASSAULT), (play, calc.ratio, calc.ratio_corner))

    def test_hunter_with_our_laser_is_assaulted(self):
        play, calc = D.choose_play(sit(self.approaching(dist=100.0), kinds=("gun", "laser"), corner=True))
        self.assertEqual(play, D.ASSAULT, calc.ratio)

    def test_hurt_bot_still_breaks_from_a_losing_fight(self):
        play, calc = D.choose_play(sit(self.approaching(("gun", "laser"), 110.0), hp=380, corner=True))
        self.assertEqual(play, D.BREAK)


class TargetChoiceTests(unittest.TestCase):
    def test_prefers_weak_close_and_busy(self):
        strong_far = track(5, pos=(180, 0, 0), hp=1400)
        weak_close = track(6, pos=(60, 20, 0), hp=400)
        tgt, calc = D.pick_target(lambda e: sit(e), [strong_far, weak_close], 10.0, (0, 0))
        self.assertIs(tgt, weak_close)
        busy = track(7, pos=(90, -10, 0), hp=1000)
        idle = track(8, pos=(80, 10, 0), hp=1000)
        busy.t_hp_drop_other = 9.5
        tgt, _ = D.pick_target(lambda e: sit(e), [idle, busy], 10.0, (0, 0))
        self.assertIs(tgt, busy)

    def test_prefers_the_one_shooting_us(self):
        a = track(5, pos=(80, 0, 0))
        b = track(6, pos=(80, 20, 0))
        b.t_hit_us = 9.8
        tgt, _ = D.pick_target(lambda e: sit(e), [a, b], 10.0, (0, 0))
        self.assertIs(tgt, b)


class PlayActTests(unittest.TestCase):
    def test_assault_closes_and_fires_long(self):
        e = track(pos=(150, 0, 0), hp=500)
        s = sit(e, kinds=("gun", "lightning"))
        it = D.PLAYS[D.ASSAULT].act(s, {"calc": D.FightCalc(s, e)})
        self.assertEqual(it.fire, "long")
        self.assertIsNotNone(it.goal)
        self.assertEqual(it.spot, "approach")
        it2 = D.PLAYS[D.ASSAULT].act(sit(track(pos=(30, 0, 0)), kinds=("gun", "lightning")), {})
        self.assertIsNone(it2.goal)
        self.assertEqual(it2.spot, "strafe")

    def test_assault_goes_for_the_rear_of_an_unaware_enemy(self):
        e = track(pos=(150, 0, 0), yaw=0.0, sees_us=False)     # facing east, away from us
        it = D.PLAYS[D.ASSAULT].act(sit(e), {})
        # the approach point lies behind it (west of it), on our side
        self.assertLess(it.goal[0], 150.0)

    def test_corner_steps_out_when_loaded_and_back_to_reload(self):
        e = track(pos=(70, 0, 0))
        ctx = {"hide": (0.0, 5.0), "peek": (3.0, 0.0), "reloading": False, "weapon_ready": True}
        it = D.PLAYS[D.CORNER].act(sit(e, corner=True), ctx)
        self.assertEqual((it.spot, it.goal, it.fire), ("peek", (3.0, 0.0), "long"))
        ctx["reloading"] = True
        it = D.PLAYS[D.CORNER].act(sit(e, corner=True, ammo=0), ctx)
        self.assertEqual((it.spot, it.goal, it.fire), ("hide", (0.0, 5.0), "none"))
        self.assertTrue(it.reload_ok)
        ctx = {"hide": (0.0, 5.0), "peek": (3.0, 0.0), "hurt_in_exposure": True}
        it = D.PLAYS[D.CORNER].act(sit(e, corner=True), ctx)
        self.assertEqual(it.spot, "hide")

    def test_harass_fires_once_every_four_seconds(self):
        e = track(pos=(150, 0, 0), hp=600)
        it = D.PLAYS[D.HARASS].act(sit(e, t=20.0), {"last_harass_hit": 15.0})
        self.assertEqual(it.fire, "single")
        it = D.PLAYS[D.HARASS].act(sit(e, t=20.0), {"last_harass_hit": 18.5})
        self.assertEqual(it.fire, "none")

    def test_break_runs_to_cover_facing_the_danger(self):
        e = track(pos=(100, 0, 0))
        it = D.PLAYS[D.BREAK].act(sit(e), {"hide": (-20.0, 10.0), "danger_yaw": 0.3})
        self.assertEqual((it.goal, it.speed, it.fire, it.face), ((-20.0, 10.0), "boost", "none", 0.3))
        self.assertTrue(it.evasive)
        it = D.PLAYS[D.BREAK].act(sit(e, energy=10.0), {"hide": None})
        self.assertEqual((it.spot, it.speed), ("retreat", "walk"))

    def test_reposition_holds_fire(self):
        it = D.PLAYS[D.REPOSITION].act(sit(), {})
        self.assertEqual(it.fire, "none")


if __name__ == "__main__":
    unittest.main()
