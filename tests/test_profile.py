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


class PiecesOn:
    """Pieces kept switched off in candidate A (mech/profile.py) are tested switched on."""
    PIECES = ()

    def setUp(self):
        self._old = {k: PF.PROFILE[k] for k in self.PIECES}
        for k in self.PIECES:
            PF.PROFILE[k] = True

    def tearDown(self):
        PF.PROFILE.update(self._old)


class SquadPlayTests(PiecesOn, unittest.TestCase):
    PIECES = ("squad_no_break",)

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


class DamageTests(PiecesOn, unittest.TestCase):
    PIECES = ("gun_during_charge", "partial_release", "finish_close")

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


class GearFirstTests(unittest.TestCase):
    """Round table 12, candidate A: in squad mode, before 50 s and with at most one kill, a lone
    hunter (its torso on us for 1 s, or it hit us) is kept at 120-145 m and no mech is shot while
    we loot; never in a free-for-all."""

    def run_gear(self, d, t0=30.0, n=14, squad=True, others=(), cubes=0, weapons=None, passive=False):
        w = world()
        bot = Bot(hello(), world=w)
        if squad:
            bot.tactics.mode.mode = "squad"
        else:
            bot.tactics.mode.locked = True
        labels, fires = [], []
        for k in range(n):
            obs = zone_obs(self_obs(pos=(ME[0], ME[1], w.floor(*ME)), t=t0 + 0.1 * k, weapons=weapons))
            obs["self"]["cubes"] = cubes
            obs["mechs"] = [enemy(9, ME[0] + d, ME[1], w, math.pi, weapons=("gun",))] + \
                [enemy(20 + i, ME[0] + ox, ME[1] + oy, w, 0.0 if passive else math.atan2(-oy, -ox), weapons=("gun",))
                 for i, (ox, oy) in enumerate(others)]
            act = bot.act(obs)
            labels.append(bot.last_label or "")
            fires.append(any(act.get("fire") or []))
        return bot, labels, fires

    def test_a_lone_hunter_at_130_m_is_kept_at_range_and_not_shot(self):
        bot, labels, fires = self.run_gear(130.0)
        self.assertTrue(all("gear" in l for l in labels[1:]), labels)
        self.assertFalse(labels[-1].endswith("-"), labels)      # identified as the hunter by now
        self.assertFalse(any(fires))

    def test_never_in_a_free_for_all(self):
        bot, labels, _ = self.run_gear(130.0, squad=False)
        self.assertFalse(any("gear" in l for l in labels), labels)

    def test_a_second_mech_within_180_m_hands_back_the_fight(self):
        bot, labels, _ = self.run_gear(130.0, others=[(150.0, 40.0)])
        self.assertFalse(any("gear" in l for l in labels[11:]), labels)
        self.assertFalse(bot.tactics.gear_done)          # only stands aside meanwhile

    def test_a_passive_squad_member_close_by_neither_stops_it_nor_is_shot(self):
        bot, labels, fires = self.run_gear(130.0, others=[(60.0, 30.0)], passive=True)
        self.assertTrue(all("gear" in l for l in labels[1:]), labels)
        self.assertFalse(any(fires))

    def test_a_hunter_inside_55_m_is_fought(self):
        bot, labels, _ = self.run_gear(45.0)
        self.assertFalse(any("gear" in l for l in labels[11:]), labels)

    def test_ends_for_good_at_50_s(self):
        bot, labels, _ = self.run_gear(130.0, t0=49.75, n=6)
        self.assertTrue(any("gear" in l for l in labels[:3]), labels)
        self.assertFalse(any("gear" in l for l in labels[3:]), labels)
        self.assertTrue(bot.tactics.gear_done)

    def test_ends_once_geared(self):
        cfg = config()
        bot, labels, _ = self.run_gear(130.0, cubes=6, weapons=[weapon("gun"), weapon("laser")])
        self.assertFalse(any("gear" in l for l in labels), labels)
        self.assertTrue(bot.tactics.gear_done)

    def test_ends_after_the_second_kill(self):
        w = world()
        bot = Bot(hello(), world=w)
        bot.tactics.mode.mode = "squad"
        bot.tactics.th.kills = 2
        obs = zone_obs(self_obs(pos=(ME[0], ME[1], w.floor(*ME)), t=30.0))
        obs["mechs"] = [enemy(9, ME[0] + 130.0, ME[1], w, math.pi, weapons=("gun",))]
        bot.act(obs)
        self.assertNotIn("gear", bot.last_label or "")
        self.assertTrue(bot.tactics.gear_done)

    def test_inside_120_m_it_opens_the_range(self):
        bot, labels, _ = self.run_gear(95.0)
        self.assertTrue(all("gear" in l for l in labels[1:]), labels)
        self.assertTrue(any("kite" in l or "roam" in l or "cube" in l for l in labels[11:]), labels)


class CrateStallTests(unittest.TestCase):
    def test_a_point_blank_crate_that_loses_no_hp_is_left_after_3_s(self):
        w = world()
        bot = Bot(hello(), world=w)
        f = w.floor(*ME)
        picks = []
        for k in range(40):
            obs = zone_obs(self_obs(pos=(ME[0], ME[1], f), t=30.0 + 0.1 * k))
            obs["crates"] = [{"id": 1009, "pos": [ME[0] + 1.4, ME[1] + 0.5, f], "hp": 300.0}]
            me = Me(obs, config())
            sc = bot.tactics.shoot_choice(me, obs)
            picks.append(sc is not None and sc.id == 1009)
        self.assertTrue(all(picks[:25]), picks)
        self.assertFalse(any(picks[33:]), picks)

    def test_a_crate_that_takes_damage_is_kept(self):
        w = world()
        bot = Bot(hello(), world=w)
        f = w.floor(*ME)
        picks = []
        for k in range(40):
            obs = zone_obs(self_obs(pos=(ME[0], ME[1], f), t=30.0 + 0.1 * k))
            obs["crates"] = [{"id": 1009, "pos": [ME[0] + 6.0, ME[1], f], "hp": 300.0 - 6.0 * k}]
            sc = bot.tactics.shoot_choice(Me(obs, config()), obs)
            picks.append(sc is not None and sc.id == 1009)
        self.assertTrue(all(picks), picks)
class CarbonFixTests(unittest.TestCase):
    """Round table 12, candidate B (brainstorm/r12/mech-carbon.md and mech-neon.md)."""

    def weak(self, dist, weapons):
        cfg = config()
        e = Track(9, "mech", 100.0)
        e.pos = [dist, 0.0, 0.0]
        e.in_obs, e.los = True, True
        e.hp, e.max_hp, e.weapons = 450.0, 1400.0, list(weapons)
        return D.Situation(100.0, (0.0, 0.0), 0.0, 1000, 1400, {"gun": dict(cfg["weapons"]["gun"])}, e, [], cfg=cfg)

    def test_finish_close_never_walks_into_lightning_or_mortar(self):
        with switch("finish_close", True):
            self._finish_clamps()

    def test_finish_close_is_off(self):
        self.assertFalse(PF.on("finish_close"))           # HOUSE-BOTS.md: 84% of expert kills under 78 m
        self.assertNotEqual(D.Assault().act(self.weak(55.0, ["gun"]), {"calc": None}).spot, "approach")

    def _finish_clamps(self):
        self.assertNotEqual(D.Assault().act(self.weak(55.0, ["gun", "lightning"]), {"calc": None}).spot, "approach")
        self.assertNotEqual(D.Assault().act(self.weak(72.0, ["gun", "mortar"]), {"calc": None}).spot, "approach")
        self.assertEqual(D.Assault().act(self.weak(55.0, ["gun"]), {"calc": None}).spot, "approach")

    def tr(self, tid, x, hp, hit_us=-1e9):
        e = Track(tid, "mech", 100.0)
        e.pos = [x, 0.0, 0.0]
        e.in_obs, e.los, e.sees_us = True, True, True
        e.hp, e.max_hp, e.weapons, e.yaw = float(hp), 1400.0, ["gun"], math.pi
        e.t_hit_us = hit_us
        return e

    def pick(self, a, b, prev, squad):
        cfg = config()
        ws = {"gun": dict(cfg["weapons"]["gun"])}
        fac = lambda e: D.Situation(100.0, (0.0, 0.0), 0.0, 1400, 1400, ws, e, [o for o in (a, b) if o is not e], cfg=cfg)
        return D.pick_target(fac, [a, b], 100.0, (0.0, 0.0), prev_id=prev, squad=squad)[0]

    def test_sticky_target_survives_a_second_hunters_hit(self):
        a = self.tr(9, 80.0, 700)                       # the one we are on
        b = self.tr(10, 70.0, 600, hit_us=99.5)         # a fresh hit from another
        self.assertIs(self.pick(a, b, prev=None, squad=True), b)
        self.assertIs(self.pick(a, b, prev=9, squad=True), a)
        with switch("sticky_target", False):
            self.assertIs(self.pick(a, b, prev=9, squad=True), b)

    def test_counter_no_stickiness_in_a_free_for_all_crossfire(self):
        a = self.tr(9, 80.0, 700)
        b = self.tr(10, 70.0, 600, hit_us=99.5)
        self.assertIs(self.pick(a, b, prev=9, squad=False), b)     # two shooters in sight

    def test_airborne_enemy_raises_our_gun_dps_in_fightcalc(self):
        cfg = config()
        e = self.tr(9, 125.0, 1400)
        ws = {"gun": dict(cfg["weapons"]["gun"])}
        s = D.Situation(100.0, (0.0, 0.0), 0.0, 1400, 1400, ws, e, [], cfg=cfg)
        ground = D.FightCalc(s, e).our_dps
        e.airborne = True
        air = D.FightCalc(s, e).our_dps
        self.assertGreater(air, ground * 1.25)
        with switch("airborne_aim", False):
            self.assertAlmostEqual(D.FightCalc(s, e).our_dps, ground)

    def gun_pitch(self, airborne, vz=0.0):
        cfg = config()
        w = world()
        g = Gunner(cfg, w, 1)
        f = w.floor(*ME)
        me = Me(zone_obs(self_obs(pos=(ME[0], ME[1], f), t=100.0)), cfg)
        tr = Track(9, "mech", 100.0)
        gx = ME[0] + 140.0
        gf = w.floor(gx, ME[1])
        tr.pos = [gx, ME[1], gf + 14.0]
        tr.vel = [0.0, 0.0, vz]
        tr.airborne, tr.ground = airborne, gf
        tgt = Target(9, "mech", tuple(tr.pos), tuple(tr.vel), 2.5, 9.0, True, track=tr)
        return g._gun_aim(me, tgt, me.by_kind["gun"])[1]

    def test_gravity_in_the_lead_of_a_falling_airborne_mech(self):
        self.assertLess(self.gun_pitch(True), self.gun_pitch(False) - 0.005)    # aims under it
        self.assertAlmostEqual(self.gun_pitch(True, vz=8.0), self.gun_pitch(False, vz=8.0))  # still jetting up

    def charge_fire(self, vel):
        cfg = config()
        w = world()
        g = Gunner(cfg, w, 1)
        f = w.floor(*ME)
        me = Me(zone_obs(self_obs(pos=(ME[0], ME[1], f), t=100.0, aim=[0.0, 0.0],
                                  weapons=[weapon("gun"), weapon("laser", charge=0.2)])), cfg)
        tr = Track(9, "mech", 100.0)
        tr.pos = [ME[0] + 150.0, ME[1], w.floor(ME[0] + 150.0, ME[1])]
        tr.vel = list(vel)
        tr.hp, tr.max_hp = 1400.0, 1400.0
        tr.in_obs = tr.los = True
        tgt = Target(9, "mech", tuple(tr.pos), tuple(vel), 2.5, 9.0, True, track=tr)
        it = D.Intent(D.ASSAULT)
        it.fire = "long"
        aim, fire, _, _ = g.control(me, it, tgt, [], 0.0, None, [])
        return aim, fire

    def test_the_torso_stays_on_the_laser_line_and_the_gun_waits_off_its_lead(self):
        aim, fire = self.charge_fire((0.0, 22.0, 0.0))  # 22 m/s across at 150 m: the lead is ~0.13 rad off
        self.assertLess(abs(aim[0]), 0.02)              # on the laser's line, not the gun's lead
        self.assertFalse(fire[0])
        self.assertTrue(fire[1])

    def test_isolation_hides_from_all_shooters_but_the_target(self):
        w = world()
        bot = Bot(hello(), world=w)
        bot.tactics.mode.mode = "squad"
        bot.tactics.gear_done = True
        for k in range(4):
            obs = zone_obs(self_obs(pos=(ME[0], ME[1], w.floor(*ME)), t=100.0 + 0.1 * k, hp=900))
            obs["mechs"] = [enemy(9, ME[0] + 70.0, ME[1], w, math.pi, weapons=("gun",)),
                            enemy(10, ME[0] + 60.0, ME[1] - 40.0, w, math.atan2(40.0, -60.0), weapons=("gun",))]
            obs["events"] = [{"type": "damaged", "amount": 45, "weapon": "gun", "bearing": 0.0, "by": 9}]
            bot.act(obs)
        self.assertTrue(bot.tactics.isolating)
        self.assertIsNotNone(bot.tactics.iso.hide_key)
        self.assertEqual(len(bot.tactics.iso.hide_key), 1)      # one watcher: the target is not hidden from


class HouseBotTests(unittest.TestCase):
    """analysis/mech/HOUSE-BOTS.md folded into candidate B: measured house accuracy (squad only),
    floors of 70 m from lightning and 95 m from mortar holders (squad only), the x1.6 laser
    cook-off on mortar holders and punishing open reloads (both modes)."""

    def e(self, x, weapons, hp=1400.0, tid=9):
        e = Track(tid, "mech", 100.0)
        e.pos = [x, 0.0, 0.0]
        e.in_obs, e.los, e.sees_us = True, True, True
        e.hp, e.max_hp, e.weapons, e.yaw = hp, 1400.0, list(weapons), math.pi
        return e

    def sit(self, e, house, weapons=("gun",), others=()):
        cfg = config()
        ws = {k: dict(cfg["weapons"][k]) for k in weapons}
        return D.Situation(100.0, (0.0, 0.0), 0.0, 1400, 1400, ws, e, list(others), cfg=cfg, house=house)

    def test_house_lasers_and_guns_land_little_at_120_m(self):
        e = self.e(120.0, ["gun", "laser"])
        house = D.FightCalc(self.sit(e, True), e).their_dps_on_us
        other = D.FightCalc(self.sit(e, False), e).their_dps_on_us
        self.assertLess(house, other * 0.5)
        near = self.e(40.0, ["gun", "laser"])
        self.assertGreater(D.FightCalc(self.sit(near, True), near).their_dps_on_us, house * 3.0)

    def test_squad_floor_of_70_m_from_a_lightning_holder(self):
        e = self.e(62.0, ["gun", "lightning"])
        self.assertEqual(D.Assault().act(self.sit(e, True), {"calc": None}).spot, "retreat")
        self.assertNotEqual(D.Assault().act(self.sit(e, False), {"calc": None}).spot, "retreat")

    def test_squad_floor_of_95_m_from_a_mortar_holder(self):
        e = self.e(88.0, ["gun", "mortar"])
        it = D.Assault().act(self.sit(e, True, weapons=("gun", "laser")), {"calc": None})
        self.assertIsNone(it.goal)                       # 88 m < 95 m: do not close
        it = D.Assault().act(self.sit(e, False, weapons=("gun", "laser")), {"calc": None})
        self.assertIsNotNone(it.goal)                    # free-for-all: laser range 85 m... 75 m floor

    def test_laser_cooks_off_a_mortar_holder(self):
        plain = self.e(100.0, ["gun"])
        mort = self.e(100.0, ["gun", "mortar"])
        a = D.FightCalc(self.sit(plain, False, weapons=("gun", "laser")), plain)
        b = D.FightCalc(self.sit(mort, False, weapons=("gun", "laser")), mort)
        self.assertGreater(b.our_dps, a.our_dps * 1.15)
        with switch("mortar_priority", False):
            self.assertAlmostEqual(D.FightCalc(self.sit(mort, False, weapons=("gun", "laser")), mort).our_dps, a.our_dps)

    def pick(self, a, b, weapons=("gun",)):
        cfg = config()
        ws = {k: dict(cfg["weapons"][k]) for k in weapons}
        fac = lambda e: D.Situation(100.0, (0.0, 0.0), 0.0, 1400, 1400, ws, e, [o for o in (a, b) if o is not e], cfg=cfg)
        return D.pick_target(fac, [a, b], 100.0, (0.0, 0.0), squad=True)[0]

    def test_mortar_holders_come_first_with_a_laser(self):
        a = self.e(80.0, ["gun"], tid=9)
        b = self.e(90.0, ["gun", "mortar"], tid=10)
        self.assertIs(self.pick(a, b, weapons=("gun", "laser")), b)

    def test_a_target_reloading_in_the_open_comes_first(self):
        a = self.e(80.0, ["gun"], tid=9)
        b = self.e(90.0, ["gun"], tid=10)
        self.assertIs(self.pick(a, b), a)
        b.t_reload_heard = 99.5
        self.assertIs(self.pick(a, b), b)
        e = self.e(60.0, ["gun"])
        fresh = D.FightCalc(self.sit(e, False), e).their_dps_on_us
        e.t_reload_heard = 99.5
        self.assertLess(D.FightCalc(self.sit(e, False), e).their_dps_on_us, fresh * 0.5)


if __name__ == "__main__":
    unittest.main()
