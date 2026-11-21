"""Squad-practice profile (mech/profile.py, recon/rea-mech-royale.md sections 2 and 6): the mode
detector and its counter, the squad play choice, and the damage pieces used in both modes."""
import math
import unittest
from types import SimpleNamespace as N

from tests.helpers import config, self_obs, weapon, world
from tests.test_controller import hello
from tests.test_counters import enemy, zone_obs
from mech import doctrine as D
from mech import profile as PF
from mech.brain import Bot
from mech.gunner import Gunner, Target
from mech.state import Me
from mech.threats import Track

ME = (300.0, -60.0)


class switch:
    def __init__(self, name, value):
        self.name, self.value = name, value

    def __enter__(self):
        self.old = PF.PROFILE[self.name]
        PF.PROFILE[self.name] = self.value

    def __exit__(self, *a):
        PF.PROFILE[self.name] = self.old


def hunter(w, k, d0=180.0, speed=8.0, eid=9):
    """A mech walking straight at us from the east."""
    x = ME[0] + d0 - speed * 0.1 * k
    e = enemy(eid, x, ME[1], w, math.pi)
    e["vel"] = [-speed, 0.0, 0.0]
    return e


def drive(n, mechs_fn=lambda k: [], t0=0.0, step=0.1, extra=None, bot=None):
    w = world()
    bot = bot or Bot(hello(), world=w)
    labels = []
    for k in range(n):
        obs = zone_obs(self_obs(pos=(ME[0], ME[1], w.floor(*ME)), t=t0 + step * k))
        obs["mechs"] = mechs_fn(k)
        if extra:
            extra(obs, k)
        bot.act(obs)
        labels.append(bot.last_label)
    return bot, labels


class DetectorTests(unittest.TestCase):
    def test_squad_after_20_s_with_a_hunter_converging(self):
        bot, labels = drive(40, lambda k: [hunter(world(), k)], t0=18.0)
        self.assertEqual(bot.tactics.mode.mode, "squad")
        self.assertTrue(any(l.startswith("S ") for l in labels))
        self.assertGreaterEqual(bot.tactics.mode.t_squad, 20.0)

    def test_not_before_20_s(self):
        bot, labels = drive(30, lambda k: [hunter(world(), k)], t0=5.0)
        self.assertEqual(bot.tactics.mode.mode, "ffa")

    def test_counter_a_quiet_opening_with_nobody_coming_stays_ffa(self):
        """The standing rule's counter: a quiet free-for-all opening must not trip it."""
        far = lambda k: [enemy(9, ME[0] + 150.0, ME[1] + 100.0, world(), 0.0)]      # looking away, still
        bot, labels = drive(60, far, t0=15.0, step=0.5)
        self.assertEqual(bot.tactics.mode.mode, "ffa")
        self.assertFalse(any(l.startswith("S ") for l in labels))

    def test_fire_between_two_other_mechs_locks_ffa_for_good(self):
        w = world()
        a, b = (ME[0] + 100.0, ME[1] + 20.0), (ME[0] + 100.0, ME[1] - 40.0)

        def extra(obs, k):
            if k == 5:
                obs["beams"] = [{"by": 8, "kind": "laser", "path": [[a[0], a[1], 7.0], [b[0], b[1], 4.0]]}]
        mechs = lambda k: [enemy(8, a[0], a[1], w, -math.pi / 2), enemy(10, b[0], b[1], w, math.pi / 2), hunter(w, k)]
        bot, labels = drive(40, mechs, t0=10.0, extra=extra)
        self.assertTrue(bot.tactics.mode.locked)
        self.assertEqual(bot.tactics.mode.mode, "ffa")

    def test_a_death_we_did_not_cause_before_the_shrink_locks_ffa(self):
        def extra(obs, k):
            obs["alive"] = 16 if k < 3 else 15
        bot, labels = drive(30, lambda k: [hunter(world(), k)], t0=15.0, extra=extra)
        self.assertTrue(bot.tactics.mode.locked)

    def test_our_own_kill_is_not_evidence(self):
        def extra(obs, k):
            obs["alive"] = 16 if k < 3 else 15
            if k == 3:
                obs["events"] = [{"type": "kill", "target": 77}]
        bot, labels = drive(40, lambda k: [hunter(world(), k)], t0=18.0, extra=extra)
        self.assertFalse(bot.tactics.mode.locked)
        self.assertEqual(bot.tactics.mode.mode, "squad")

    def test_our_own_hit_is_not_evidence(self):
        """5537640 locked free-for-all in 27 of 40 squad matches on 'mech hurt by another' that was
        our own hit: the HP drop is seen before the tick's hit event."""
        w = world()

        def mechs(k):
            e = hunter(w, k)
            e["hp"] = 1400 - 45 * k
            return [e]

        def extra(obs, k):
            obs["events"] = [{"type": "hit", "target": 9, "amount": 45, "weapon": "gun"}]
        bot, labels = drive(40, mechs, t0=18.0, extra=extra)
        self.assertFalse(bot.tactics.mode.locked, bot.tactics.mode.why)
        self.assertEqual(bot.tactics.mode.mode, "squad")

    def test_a_mech_hurt_while_another_aims_and_fires_at_it_is_evidence(self):
        w = world()
        a, b = (ME[0] + 100.0, ME[1] + 30.0), (ME[0] + 100.0, ME[1] - 30.0)

        def mechs(k):
            ea = enemy(8, a[0], a[1], w, -math.pi / 2)            # torso on b
            eb = enemy(10, b[0], b[1], w, 0.0)
            eb["hp"] = 1400 - 40 * k
            return [ea, eb]

        def extra(obs, k):
            obs["beams"] = [{"by": 8, "kind": "laser", "path": [[a[0], a[1], 7.0], [b[0] + 30.0, b[1] - 30.0, 4.0]]}]
        bot, labels = drive(10, mechs, t0=10.0, extra=extra)
        self.assertTrue(bot.tactics.mode.locked)

    def test_a_death_in_a_later_phase_is_not_evidence(self):
        """zone.shrink_start moves to each later phase; deaths then may be the zone."""
        def extra(obs, k):
            obs["zone"]["shrink_start"] = 60.0 if k < 5 else 200.0      # the next phase announced
            obs["alive"] = 12 if k < 20 else 11
        bot, labels = drive(40, lambda k: [hunter(world(), k)], t0=95.0, extra=extra)
        self.assertFalse(bot.tactics.mode.locked, bot.tactics.mode.why)

    def test_a_shot_passing_near_another_mech_is_not_evidence(self):
        w = world()
        a, b = (ME[0] + 100.0, ME[1] + 30.0), (ME[0] + 100.0, ME[1] - 30.0)

        def extra(obs, k):
            obs["projectiles"] = [{"id": 900 + k, "kind": "gun", "pos": [a[0], a[1] - 10.0, 8.0], "vel": [0.0, -250.0, 0.0]}]
        mechs = lambda k: [enemy(8, a[0], a[1], w, -math.pi / 2), enemy(10, b[0], b[1], w, 0.0), hunter(w, k)]
        bot, labels = drive(40, mechs, t0=18.0, extra=extra)
        self.assertFalse(bot.tactics.mode.locked, bot.tactics.mode.why)

    def test_off_switch_means_always_ffa(self):
        with switch("mode_detector", False):
            bot, labels = drive(40, lambda k: [hunter(world(), k)], t0=18.0)
            self.assertFalse(bot.tactics.mode.squad())


class SquadPlayTests(unittest.TestCase):
    """recon/rea-mech-royale.md section 4 item 1: with two hunters about, squad mode stays in the
    fight where free-for-all mode breaks."""

    def sit(self, hp=980, target_hp=1000, others=1):
        cfg = config()
        e = Track(9, "mech", 100.0)
        e.pos = [90.0, 0.0, 0.0]
        e.in_obs, e.los, e.sees_us = True, True, True
        e.hp, e.max_hp, e.yaw = float(target_hp), 1400.0, math.pi
        e.weapons = ["gun"]
        os_ = []
        for i in range(others):
            o = Track(20 + i, "mech", 100.0)
            o.pos = [100.0, 60.0 + 10 * i, 0.0]
            o.in_obs, o.los, o.sees_us = True, True, True
            o.hp, o.max_hp, o.yaw = 1000.0, 1400.0, math.pi
            o.weapons = ["gun"]
            os_.append(o)
        ws = {"gun": dict(cfg["weapons"]["gun"])}
        return D.Situation(100.0, (0.0, 0.0), 0.0, hp, 1400, ws, e, os_, cfg=cfg)

    def test_two_hunters_at_70_percent_break_in_ffa_fight_in_squad(self):
        s = self.sit()
        ffa, _ = D.choose_play(s)
        squad, _ = D.choose_play(s, squad=True)
        self.assertEqual(ffa, D.BREAK)                   # a second mech within 160 m
        self.assertNotEqual(squad, D.BREAK)

    def lose_run(self, hp, target_hp, n=25):
        w = world()
        bot = Bot(hello(), world=w)
        bot.tactics.mode.mode = "squad"
        plays = []
        a = (ME[0] + 60.0, ME[1])
        for k in range(n):
            obs = zone_obs(self_obs(pos=(ME[0], ME[1], w.floor(*ME)), t=100.0 + 0.1 * k, hp=hp))
            e = enemy(9, a[0], a[1], w, math.pi)
            e["hp"] = target_hp
            obs["mechs"] = [e]
            obs["events"] = [{"type": "damaged", "amount": 45, "weapon": "gun", "bearing": 0.0, "by": 9}]
            bot.act(obs)
            plays.append(bot.tactics.last_play)
        return plays

    def test_low_hp_against_a_healthier_target_breaks_only_after_1_5_s(self):
        plays = self.lose_run(hp=420, target_hp=1300)
        self.assertNotIn(D.BREAK, plays[:14])            # the first 1.4 s: keep trading
        self.assertIn(D.BREAK, plays[16:])

    def test_never_breaks_while_finishing(self):
        plays = self.lose_run(hp=420, target_hp=300)
        self.assertNotIn(D.BREAK, plays)

    def test_ffa_still_breaks_at_once(self):
        w = world()
        bot = Bot(hello(), world=w)
        bot.tactics.mode.locked = True
        a = (ME[0] + 60.0, ME[1])
        plays = []
        for k in range(6):
            obs = zone_obs(self_obs(pos=(ME[0], ME[1], w.floor(*ME)), t=100.0 + 0.1 * k, hp=300))
            e = enemy(9, a[0], a[1], w, math.pi)
            e["hp"] = 1300
            obs["mechs"] = [e]
            obs["events"] = [{"type": "damaged", "amount": 45, "weapon": "gun", "bearing": 0.0, "by": 9}]
            bot.act(obs)
            plays.append(bot.tactics.last_play)
        self.assertIn(D.BREAK, plays[:3])


class DamageTests(unittest.TestCase):
    def control(self, charge, target_hp=1400.0, dist=60.0):
        cfg = config()
        w = world()
        g = Gunner(cfg, w, 1)
        f = w.floor(*ME)
        me = Me(zone_obs(self_obs(pos=(ME[0], ME[1], f), t=100.0, aim=[0.0, 0.0],
                                  weapons=[weapon("gun"), weapon("laser", charge=charge)])), cfg)
        tr = Track(9, "mech", 100.0)
        tr.pos = [ME[0] + dist, ME[1], w.floor(ME[0] + dist, ME[1])]
        tr.vel = [0.0, 0.0, 0.0]
        tr.hp, tr.max_hp = target_hp, 1400.0
        tr.in_obs = tr.los = True
        tgt = Target(9, "mech", tuple(tr.pos), (0.0, 0.0, 0.0), 2.5, 9.0, True, track=tr)
        it = D.Intent(D.ASSAULT)
        it.fire = "long"
        return g.control(me, it, tgt, [], 0.0, None, [])[1]

    def test_the_gun_fires_while_the_laser_charges(self):
        fire = self.control(0.2)
        self.assertTrue(fire[0])           # gun
        self.assertTrue(fire[1])           # laser still charging
        with switch("gun_during_charge", False):
            fire = self.control(0.2)
        self.assertFalse(fire[0])

    def test_a_partial_charge_that_kills_is_released(self):
        """At 0.6 s a release does 315 x (0.35 + 0.65 x 0.75) = 264; x0.7 = 185 kills a 150 HP mech."""
        self.assertFalse(self.control(0.6, target_hp=150.0)[1])        # released
        self.assertTrue(self.control(0.6, target_hp=400.0)[1])         # keeps charging
        with switch("partial_release", False):
            self.assertTrue(self.control(0.6, target_hp=150.0)[1])

    def test_finish_close(self):
        cfg = config()
        e = Track(9, "mech", 100.0)
        e.pos = [60.0, 0.0, 0.0]
        e.in_obs, e.los = True, True
        e.hp, e.max_hp, e.weapons = 450.0, 1400.0, ["gun"]
        s = D.Situation(100.0, (0.0, 0.0), 0.0, 1000, 1400, {"gun": dict(cfg["weapons"]["gun"])}, e, [], cfg=cfg)
        it = D.Assault().act(s, {"calc": None})
        self.assertEqual(it.spot, "approach")            # 60 m > 20 m: close in to finish
        with switch("finish_close", False):
            it = D.Assault().act(s, {"calc": None})
        self.assertNotEqual(it.spot, "approach")         # 60 m is the gun's usual range


if __name__ == "__main__":
    unittest.main()
