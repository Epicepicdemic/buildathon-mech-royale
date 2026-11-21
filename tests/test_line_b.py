"""Tests for Line B's aggressive doctrine, spatial loot memory, weapon counters, and red-team fixes."""
import math
import random
import unittest

from tests.helpers import config, self_obs, track, weapon, world
from tests.test_controller import hello
from tests.test_counters import enemy, zone_obs
from mech import ballistics as bal
from mech import doctrine as D
from mech import positions as P
from mech.brain import Bot
from mech.gunner import Gunner, Target
from mech.loot import LootMemory
from mech.state import Me


class LineBCombatAndCanopyTests(unittest.TestCase):
    def test_shoots_through_tree_canopy_when_enemy_in_obs(self):
        """Trees block sight beyond 45 m when dense, but NEVER block bullets or lasers (protocol s.7)."""
        w = world()
        # Find a pair on the Oval where w.shot_clear is True (no building) even if w.sees is False (canopy)
        found = None
        for y in range(-90, 20, 10):
            p0 = (120.0, float(y), w.floor(120.0, y))
            p1 = (210.0, float(y), w.floor(210.0, y))
            if w.shot_clear((p0[0], p0[1], p0[2] + 7.0), (p1[0], p1[1], p1[2] + 4.5)) \
                    and not w.sees((p0[0], p0[1], p0[2] + 8.0), p1, 9.0, 45.0):
                found = (p0, p1)
                break
        self.assertIsNotNone(found, "expected a canopy-occluded clear-shot pair on the Oval")
        p0, p1 = found
        bot = Bot(hello(), world=w)
        obs = zone_obs(self_obs(pos=p0, t=0.0, aim=(0.0, 0.0)))
        obs["mechs"] = [{"id": 8, "kind": "mech", "pos": list(p1), "vel": [0, 0, 0], "aim": [math.pi, 0.0],
                         "hp": 1400, "max_hp": 1400, "cubes": 0, "weapons": ["gun"], "charging": False}]
        a = bot.act(obs)
        self.assertTrue(bot.threats.tracks[8].los)
        self.assertTrue(a["fire"][0])

    def test_fighting_retreat_fires_until_los_breaks_and_never_wallhops(self):
        """When leaving a losing fight near a climbable building, Line B shoots back at full speed
        until LOS is broken and NEVER initiates a suicidal WallHop while seen."""
        w = world()
        bot = Bot(hello(), world=w)
        me = (20.0, -85.0)                      # right beside Wilce Health Center (low roof available)
        ex, ey = 20.0, -42.0                    # 43 m north, healthier enemy with laser
        fired_in_break = False
        for k in range(8):
            t = 20.0 + 0.1 * k
            obs = zone_obs(self_obs(pos=(me[0], me[1], w.floor(*me)), t=t, hp=340,
                                    aim=(math.pi / 2, 0.0), weapons=[weapon("gun"), weapon("laser")]))
            obs["mechs"] = [enemy(9, ex, ey, w, -math.pi / 2, weapons=("gun", "laser"))]
            a = bot.act(obs)
            self.assertEqual(bot.tactics.last_play, D.BREAK)
            self.assertIsNone(bot.tactics.air)  # never WallHop while enemy sees us!
            fired_in_break = fired_in_break or a["fire"][0]
            self.assertFalse(a["fire"][1])      # does not start a 0.5x-speed laser charge while retreating
        self.assertTrue(fired_in_break)

        # Now step behind Wilce Health so LOS is broken: firing stops so 5 s heal timer begins
        hid = (-5.0, -100.0)
        obs = zone_obs(self_obs(pos=(hid[0], hid[1], w.floor(*hid)), t=21.0, hp=340,
                                aim=(math.pi / 2, 0.0), weapons=[weapon("gun"), weapon("laser")]))
        obs["mechs"] = [enemy(9, ex, ey, w, -math.pi / 2, weapons=("gun", "laser"))]
        a = bot.act(obs)
        self.assertFalse(bot.threats.tracks[9].los)
        self.assertFalse(any(a["fire"]))


class LootAndFarmingTests(unittest.TestCase):
    def test_loot_memory_keeps_seen_laser_and_infers_kill_drop(self):
        w = world()
        cfg = config()
        lm = LootMemory(cfg)
        p = (300.0, -60.0, w.floor(300, -60))
        obs1 = self_obs(pos=p, t=1.0, aim=(0.0, 0.0))
        obs1["pickups"] = [{"id": 77, "kind": "laser", "pos": [370.0, -60.0, w.floor(370, -60)]}]
        lm.update(obs1, Me(obs1, cfg), w)
        self.assertIn(77, lm.pickups)

        # Turn 180 deg west so the laser at +70 m east is outside our 120 deg FOV (beyond 45 m near_range)
        obs2 = self_obs(pos=p, t=2.0, aim=(math.pi, 0.0))
        obs2["pickups"] = []
        kill_drop = [([250.0, -120.0, w.floor(250, -120)], 5, ["lightning"], 2.0)]
        lm.update(obs2, Me(obs2, cfg), w, new_kill_drops=kill_drop)
        self.assertIn(77, lm.pickups)           # still remembered!
        self.assertEqual(len(lm.kill_drops), 1)

        # Bot routes to kill drops emitted on a kill event
        bot = Bot(hello(), world=w)
        obs_fight = zone_obs(self_obs(pos=p, t=10.0, aim=(math.pi, 0.0)))
        obs_fight["mechs"] = [{"id": 4, "kind": "mech", "pos": [235.0, -60.0, w.floor(235, -60)],
                               "vel": [0, 0, 0], "aim": [0.0, 0.0], "hp": 40, "max_hp": 1400,
                               "cubes": 4, "weapons": ["gun", "laser"], "charging": False}]
        bot.act(obs_fight)
        obs_kill = zone_obs(self_obs(pos=p, t=10.1, aim=(0.0, 0.0)))  # turned away as shot lands
        obs_kill["events"] = [{"type": "kill", "target": 4}]
        a = bot.act(obs_kill)
        self.assertEqual(bot.tactics.loot_note, "killdrop")
        self.assertLess(a["move"][0], -0.5)     # heads west toward the dead mech's 6-cube + laser drop!

    def test_walks_onto_crate_while_shooting_it(self):
        w = world()
        bot = Bot(hello(), world=w)
        me = (300.0, -60.0)
        cx, cy = 342.0, -60.0
        aim = [0.0, 0.0]
        moved_east = False
        shot_crate = False
        for k in range(6):
            obs = zone_obs(self_obs(pos=(me[0], me[1], w.floor(*me)), t=5.0 + 0.1 * k, aim=aim))
            # Even with distant gunfire heard, Line B still farms nearby crates!
            obs["sounds"] = [{"kind": "gun", "bearing": -2.0, "dist": "far", "age": 0.2}]
            obs["crates"] = [{"id": 301, "pos": [cx, cy, w.floor(cx, cy)], "hp": 300}]
            a = bot.act(obs)
            aim = a["aim"]
            moved_east = moved_east or (a["move"][0] > 0.6)
            shot_crate = shot_crate or a["fire"][0]
        self.assertTrue(moved_east)
        self.assertTrue(shot_crate)


class UnmodeledWeaponAndDodgeTests(unittest.TestCase):
    def test_dodges_incoming_gun_bullet_ray(self):
        w = world()
        bot = Bot(hello(), world=w)
        me = (300.0, -60.0)
        f = w.floor(*me)
        obs = zone_obs(self_obs(pos=(me[0], me[1], f), t=12.0))
        # Bullet 100 m east flying due west straight at our chest (t_hit ~ 0.40 s)
        obs["projectiles"] = [{"kind": "gun", "pos": [400.0, -60.0, f + 4.5], "vel": [-252.0, 0.0, 0.0]}]
        a = bot.act(obs)
        # Steps north or south perpendicular to the east-west bullet ray
        self.assertGreater(abs(a["move"][1]), 0.65, a["move"])

    def test_falling_mortar_shell_near_wall_picks_walkable_escape(self):
        """Red-team probe 1c: standing beside Wilce Health's west wall with a shell landing 2 m east,
        mover must NEVER steer west into the building wall."""
        w = world()
        bot = Bot(hello(), world=w)
        # Exact probe 1c setup at (20, -85) (wall on west at x<=19) + normal clearance (23, -85)
        for sx in (20.0, 23.0):
            sy = -85.0
            sz = w.floor(sx, sy)
            obs = zone_obs(self_obs(pos=(sx, sy, sz), t=30.0))
            obs["projectiles"] = [{"kind": "mortar", "pos": [sx - 8.0, sy, sz + 28.0], "vel": [11.0, 0.0, -22.0]}]
            a = bot.act(obs)
            mx, my = a["move"]
            self.assertGreater(math.hypot(mx, my), 0.8)
            # Must not push west into Wilce Health (sight=11.9, clearance=0)
            self.assertGreater(w.clearance(sx + mx * 4.0, sy + my * 4.0), 2.0, (sx, a["move"]))
            self.assertLessEqual(w.sight(sx + mx * 3.0, sy + my * 3.0), sz + 1.5, (sx, a["move"]))
            self.assertTrue(a["boost"])

    def test_off_axis_laser_charge_and_220m_unseen_laser_hit(self):
        """Red-team probes 2a (off-axis pre-snap charge) and 2b (220 m phantom laser hit)."""
        w = world()
        bot = Bot(hello(), world=w)
        me = (300.0, -60.0)
        ex, ey = 430.0, -60.0
        # Enemy aims 0.35 rad off-axis while charging (will snap on release)
        obs = zone_obs(self_obs(pos=(me[0], me[1], w.floor(*me)), t=40.0))
        obs["mechs"] = [enemy(9, ex, ey, w, math.pi + 0.35, charging=True)]
        a = bot.act(obs)
        self.assertIsNotNone(bot.tactics.laser_threat(Me(obs, config())))
        self.assertTrue(a["boost"])

        # 220 m unseen laser hit: triggers BREAK + boost + evasive movement
        bot2 = Bot(hello(), world=w)
        for k in range(3):
            obs2 = zone_obs(self_obs(pos=(me[0], me[1], w.floor(*me)), t=45.0 + 0.1 * k))
            if k == 0:
                obs2["events"] = [{"type": "damaged", "amount": 315, "weapon": "laser", "bearing": 0.4, "by": 12}]
            a2 = bot2.act(obs2)
        self.assertEqual(bot2.tactics.last_play, D.BREAK)
        self.assertTrue(a2["boost"])
        self.assertGreater(math.hypot(*a2["move"]), 0.7)

    def test_lightning_short_zeroes_laser_dps_kites_and_chains_past_close_crate(self):
        """Red-team probe 3: short effect zeroes laser DPS in FightCalc, kites outside 45 m while
        shooting gun, and Gunner fires lightning when a crate is within chain range of the enemy."""
        cfg = config()
        e = track(9, pos=(32.0, 0.0, 0.0), weapons=("gun", "lightning"))
        ws = {k: dict(cfg["weapons"][k]) for k in ("gun", "laser")}
        s_normal = D.Situation(10.0, (0.0, 0.0), 0.0, 1400, 1400, ws, e, cfg=cfg, shorted=False)
        s_short = D.Situation(10.0, (0.0, 0.0), 0.0, 1400, 1400, ws, e, cfg=cfg, shorted=True)
        self.assertLess(D.FightCalc(s_short, e).our_dps, D.FightCalc(s_normal, e).our_dps - 80.0)

        # Kites away from a 32 m lightning enemy while firing gun
        w = world()
        bot = Bot(hello(), world=w)
        me = (300.0, -60.0)
        ex, ey = 332.0, -60.0
        aim = [0.0, 0.0]
        for k in range(4):
            obs = zone_obs(self_obs(pos=(me[0], me[1], w.floor(*me)), t=25.0 + 0.1 * k, aim=aim,
                                    weapons=[weapon("gun"), weapon("laser")]))
            obs["mechs"] = [enemy(9, ex, ey, w, math.pi, weapons=("gun", "lightning"))]
            if k == 0:
                obs["events"] = [{"type": "effect", "effect": "short", "by": 9}]
            a = bot.act(obs)
            aim = a["aim"]
        self.assertLess(a["move"][0], -0.3, a["move"])  # kiting west away from lightning at +32 m east!
        self.assertTrue(a["fire"][0])                   # shooting gun while kiting!
        self.assertFalse(a["fire"][1])                  # shorted laser not held

        # Crate 8 m in front of enemy (within 14 m chain range) does NOT block our lightning
        g = Gunner(cfg, w)
        me_st = Me(self_obs(pos=(300.0, -60.0, w.floor(300, -60)), t=30.0, aim=(0.0, 0.0),
                            weapons=[weapon("gun"), weapon("lightning")]), cfg)
        tr_e = track(9, pos=(336.0, -60.0, w.floor(336, -60)))
        tgt = Target.from_track(tr_e, 30.0)
        it = D.Intent(D.ASSAULT, tr_e)
        it.fire = "long"
        crate_near_enemy = type("C", (), {"id": 501, "kind": "crate", "pos": [328.0, -60.0, w.floor(328, -60)]})()
        _, fire, _, _ = g.control(me_st, it, tgt, obstacles=[crate_near_enemy])
        self.assertTrue(fire[1])

    def test_smooth_weave_detected_and_damps_lead(self):
        cfg = config()
        tr = track(5, pos=(140.0, 0.0, 0.0), vel=(0.0, 11.7, 0.0))
        # Smooth sinusoidal lateral velocity: adjacent 0.1 s ticks barely turn (< 25 deg),
        # which v2's cos < 0.3 missed, while over 0.5 s it reverses completely.
        tr.hist = []
        for k in range(16):
            t = 10.0 + 0.1 * k
            vy = 11.7 * math.sin(2.0 * math.pi * (0.1 * k) / 1.4)
            vx = 2.0
            tr.hist.append((t, 140.0 + vx * 0.1 * k, 0.0, vx, vy))
        tr.vel = [tr.hist[-1][3], tr.hist[-1][4], 0.0]
        self.assertTrue(tr.dodging())

        g = Gunner(cfg, None)
        me = Me(self_obs(pos=(0.0, 0.0, 0.0), t=11.5, weapons=[weapon("gun")]), cfg)
        yaw_damped, _, _, _ = g._gun_aim(me, Target.from_track(tr, 11.5), me.by_kind["gun"])
        yaw_raw, _, _, _ = bal.lead(me.muzzle, Target.from_track(tr, 11.5).center(), tr.vel, 252.0, latency=0.05)
        self.assertLess(abs(yaw_damped), abs(yaw_raw))


class RearWatchAndMidgameTests(unittest.TestCase):
    def test_patrol_yaw_glances_behind_and_rear_hit_snaps_torso(self):
        w = world()
        bot = Bot(hello(), world=w)
        me = (300.0, -60.0)
        yaws = [bot.tactics.patrol_yaw(Me(self_obs(pos=(me[0], me[1], w.floor(*me)), t=0.1 * k), config()), 0.0)
                for k in range(30)]
        # At least several ticks glance > 95 deg away from 0.0 into the rear open sector
        self.assertTrue(any(abs(y) > 1.7 for y in yaws), yaws)

        # Taking a rear hit from bearing -2.8 rad immediately turns danger_yaw to face that rear attacker
        obs = zone_obs(self_obs(pos=(me[0], me[1], w.floor(*me)), t=15.0, aim=(0.0, 0.0)))
        obs["events"] = [{"type": "damaged", "amount": 67.5, "weapon": "gun", "bearing": -2.8, "by": 14}]
        a = bot.act(obs)
        self.assertLess(abs(((a["aim"][0] - (-2.8) + math.pi) % (2 * math.pi)) - math.pi), 0.15)

    def test_midgame_third_party_does_not_flee_busy_enemies_and_avoids_sandwich_cells(self):
        e = track(5, pos=(75.0, 0.0, 0.0), hp=650)
        e.t_hp_drop_other = 99.5
        o1 = track(6, pos=(95.0, 45.0, 0.0), hp=900)
        o1.t_fired_other = 99.6
        s = D.Situation(100.0, (0.0, 0.0), 0.0, 1400, 1400,
                        {k: dict(config()["weapons"][k]) for k in ("gun", "laser")},
                        e, others=[o1], corner=True, cfg=config())
        play, _ = D.choose_play(s)
        self.assertIn(play, (D.ASSAULT, D.CORNER))

        w = world()
        circle = (41.3, -72.8, 220.0)
        n = P.choose_hold(w, (41.3, -72.8), circle, [], random.Random(11), alive=7)
        hx, hy = w.node_xy(n)
        frac = math.hypot(hx - circle[0], hy - circle[1]) / circle[2]
        self.assertGreater(frac, 0.25, frac)    # does not camp the exposed dead-center bowl in 7-bot midgame


if __name__ == "__main__":
    unittest.main()
