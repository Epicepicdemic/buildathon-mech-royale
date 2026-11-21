"""Candidate 1 for line B: what was ported from main (progress/BUILD-mech-line-b-v2.md).

Isolate and commit, return fire with long-leg dodging, reload discipline and reload cover, laser
and lightning held together, jump-and-jet route edges and jet gating. Line B's opening (fight
from the first second) and loot are kept; main's 30 s opening hold and _worth_starting gate are
not ported.
"""
import math
import unittest
from types import SimpleNamespace

from tests.helpers import config, self_obs, weapon, world
from tests.test_controller import hello
from tests.test_counters import enemy, zone_obs
from mech import doctrine as D
from mech.brain import Bot
from mech.geom import angdiff
from mech.state import Me


def run(bot, me_xy, t0, n, mechs_fn, hp=1400, events_fn=None, aim=None, weapons=None):
    w = bot.world
    out = []
    aim = aim or [0.0, 0.0]
    for k in range(n):
        t = t0 + 0.1 * k
        obs = zone_obs(self_obs(pos=(me_xy[0], me_xy[1], w.floor(*me_xy)), t=t, aim=aim, hp=hp, weapons=weapons))
        obs["mechs"] = mechs_fn(k)
        if events_fn is not None:
            obs["events"] = events_fn(k)
        a = bot.act(obs)
        aim = a["aim"]
        out.append(a)
    return out


def hit(by, bearing):
    return {"type": "damaged", "amount": 45, "weapon": "gun", "bearing": bearing, "by": by}


class ReturnFireTests(unittest.TestCase):
    def test_low_hp_keeps_shooting_while_seen(self):
        """Low HP and losing: whatever the play, the attacker can still hit us, so the torso stays
        on it and the gun fires."""
        w = world()
        bot = Bot(hello(), world=w)
        me = (300.0, -60.0)
        acts = run(bot, me, 120.0, 12, lambda k: [enemy(9, 360.0, -60.0, w, math.pi)], hp=300,
                   events_fn=lambda k: [hit(9, 0.0)] if k % 3 == 0 else [])
        self.assertGreater(sum(1 for a in acts if a["fire"][0]), 3)
        self.assertLess(angdiff(acts[-1]["aim"][0], 0.0), 0.3)

    def test_holds_fire_with_nobody_on_us(self):
        w = world()
        bot = Bot(hello(), world=w)
        acts = run(bot, (300.0, -60.0), 120.0, 6, lambda k: [], hp=500)
        self.assertFalse(any(a["fire"][0] for a in acts))

    def test_dodges_and_boosts_against_a_far_shooter(self):
        """A shooter 100 m away: long jink legs (evasive) and boost while energy lasts."""
        w = world()
        bot = Bot(hello(), world=w)
        me = (300.0, -60.0)
        acts = run(bot, me, 120.0, 6, lambda k: [enemy(9, 400.0, -60.0, w, math.pi)],
                   events_fn=lambda k: [hit(9, 0.0)])
        self.assertTrue(any(a["boost"] for a in acts))
        self.assertTrue(any(math.hypot(*a["move"]) > 0.5 for a in acts))

    def test_answers_the_close_attacker_not_a_far_passive_mech(self):
        w = world()
        bot = Bot(hello(), world=w)
        me = (300.0, -60.0)
        mechs = lambda k: [enemy(15, 300.0, 115.0, w, -math.pi / 2)] + \
            ([enemy(1, 339.0, -60.0, w, math.pi)] if k >= 4 else [])
        hits = lambda k: [hit(1, 0.0)] if k >= 4 and k % 2 == 0 else []
        acts = run(bot, me, 120.0, 14, mechs, events_fn=hits)
        self.assertLess(angdiff(acts[-1]["aim"][0], 0.0), 0.25)


class CommitTests(unittest.TestCase):
    def setUp(self):
        self.bot = Bot(hello(), world=world())
        self.tac = self.bot.tactics
        self.me = Me(zone_obs(self_obs(pos=(300.0, -60.0, 0.0), t=100.0)), self.bot.cfg)
        self.tac.last_play = D.ASSAULT
        self.tac.play_since = 99.0
        self.tac.play_hp0 = self.me.hp
        self.tac.play_others = 0
        self.tac.play_target = 9
        self.target = SimpleNamespace(id=9)
        self.calc = SimpleNamespace(ratio=0.8, finishable=False)
        self.sit = SimpleNamespace(unseen_hit=False)

    def test_a_fight_is_held_without_a_real_reason(self):
        self.assertEqual(self.tac._commit(self.me, D.BREAK, self.target, self.calc, [], self.sit), D.ASSAULT)

    def test_heavy_damage_a_newcomer_or_time_lets_it_break(self):
        tac, me = self.tac, self.me
        tac.play_hp0 = me.hp + 0.3 * me.max_hp
        self.assertEqual(tac._commit(me, D.BREAK, self.target, self.calc, [], self.sit), D.BREAK)
        tac.play_hp0 = me.hp
        self.assertEqual(tac._commit(me, D.BREAK, self.target, self.calc, [object()], self.sit), D.BREAK)
        tac.play_since = 96.0
        self.assertEqual(tac._commit(me, D.BREAK, self.target, self.calc, [], self.sit), D.BREAK)

    def test_a_break_is_held_unless_the_target_is_finishable(self):
        tac, me = self.tac, self.me
        tac.last_play = D.BREAK
        tac.play_since = 99.5
        self.assertEqual(tac._commit(me, D.ASSAULT, self.target, self.calc, [], self.sit), D.BREAK)
        fin = SimpleNamespace(ratio=0.8, finishable=True)
        self.assertEqual(tac._commit(me, D.ASSAULT, self.target, fin, [], self.sit), D.ASSAULT)


class IsolateTests(unittest.TestCase):
    def test_isolate_spot_hides_from_the_others_and_sees_the_primary(self):
        from mech.cover import IsolateSearch, hidden_at, shot_from
        w = world()
        start = w.nearest_free(20.0, -85.0, 5)
        other = (20.0, -185.0, w.floor(20.0, -185.0) + 8.0)
        prim = (20.0, 15.0, w.floor(20.0, 15.0) + 4.5)
        srch = IsolateSearch(w, start, prim, [other])
        srch.run(1e18)
        self.assertIsNotNone(srch.result)
        n = srch.result[0]
        self.assertTrue(hidden_at(w, n, [other]))
        self.assertTrue(shot_from(w, n, prim))

    def test_two_attackers_from_opposite_sides_trigger_isolation(self):
        w = world()
        bot = Bot(hello(), world=w)
        me = (419.0, -230.0)
        a, b = (439.8, -152.7), (422.0, -309.9)
        ya = math.atan2(me[1] - a[1], me[0] - a[0])
        yb = math.atan2(me[1] - b[1], me[0] - b[0])
        mechs = lambda k: [enemy(8, a[0], a[1], w, ya), enemy(9, b[0], b[1], w, yb)]
        hits = lambda k: [hit(8, ya + math.pi), hit(9, yb + math.pi)]
        labels = []
        aim = [0.0, 0.0]
        for k in range(10):
            obs = zone_obs(self_obs(pos=(me[0], me[1], w.floor(*me)), t=120.0 + 0.1 * k, aim=aim))
            obs["mechs"] = mechs(k)
            obs["events"] = hits(k)
            act = bot.act(obs)
            aim = act["aim"]
            labels.append(bot.last_label)
        self.assertTrue(any(l.startswith("isolate") for l in labels), labels)


class ReloadTests(unittest.TestCase):
    def test_reloads_behind_cover_when_it_is_close(self):
        w = world()
        bot = Bot(hello(), world=w)
        me = (-135.0, -166.0)
        ex, ey = -146.4, -214.7
        yaw_to = math.atan2(ey - me[1], ex - me[0])
        aim = [yaw_to, 0.0]
        labels = []
        for k in range(20):
            obs = zone_obs(self_obs(pos=(me[0], me[1], w.floor(*me)), t=120.0 + 0.1 * k, aim=aim,
                                    weapons=[weapon("gun", ammo=0, reload=2.0), None]))
            obs["mechs"] = [enemy(9, ex, ey, w, yaw_to + math.pi)]
            if k % 3 == 0:
                obs["events"] = [hit(9, yaw_to)]
            a = bot.act(obs)
            aim = a["aim"]
            labels.append(bot.last_label)
        self.assertTrue(any(l.startswith("reload: cover") for l in labels), labels)

    def test_no_early_reload_under_fire(self):
        from mech.gunner import Gunner
        cfg = config()
        w = world()
        g = Gunner(cfg, w, 1)
        me = Me(zone_obs(self_obs(pos=(300.0, -60.0, w.floor(300.0, -60.0)), t=100.0,
                                  weapons=[weapon("gun", ammo=4), None])), cfg)
        it = D.Intent(D.REPOSITION)
        it.reload_ok = True
        it.under_fire = True
        self.assertFalse(g.control(me, it, None, [], 0.0, None, [])[2])
        it.under_fire = False
        self.assertTrue(Gunner(cfg, w, 1).control(me, it, None, [], 0.0, None, [])[2])


class LoadoutTests(unittest.TestCase):
    def take_at_feet(self, weapons, kind):
        w = world()
        bot = Bot(hello(), world=w)
        me = (300.0, -60.0)
        obs = zone_obs(self_obs(pos=(me[0], me[1], w.floor(*me)), t=100.0, weapons=weapons))
        obs["pickups"] = [{"id": 8001, "kind": kind, "pos": [me[0] + 1.0, me[1], w.floor(*me)]}]
        return bot.act(obs)

    def test_laser_with_gun_and_lightning_drops_the_gun(self):
        a = self.take_at_feet([weapon("gun"), weapon("lightning")], "laser")
        self.assertEqual((a.get("take"), a.get("drop")), (8001, 0))

    def test_lightning_with_gun_and_laser_drops_the_gun(self):
        a = self.take_at_feet([weapon("gun"), weapon("laser")], "lightning")
        self.assertEqual((a.get("take"), a.get("drop")), (8001, 0))

    def test_never_drops_one_of_the_pair_for_the_other(self):
        a = self.take_at_feet([weapon("laser"), weapon("lightning")], "laser")
        self.assertNotIn("take", a)

    def test_mortar_still_goes_first(self):
        a = self.take_at_feet([weapon("gun"), weapon("mortar")], "lightning")
        self.assertEqual((a.get("take"), a.get("drop")), (8001, 1))

    def test_fights_without_a_gun(self):
        w = world()
        bot = Bot(hello(), world=w)
        acts = run(bot, (300.0, -60.0), 120.0, 10, lambda k: [enemy(9, 330.0, -60.0, w, math.pi, weapons=("gun",))],
                   events_fn=lambda k: [hit(9, 0.0)] if k % 2 == 0 else [],
                   weapons=[weapon("laser"), weapon("lightning")])
        self.assertTrue(any(a["fire"][0] or a["fire"][1] for a in acts))


class BackOffTests(unittest.TestCase):
    """Sandbox seeds 6, 17 and 20 (candidate 1's first squad batch): stopped by a wall the tables
    do not show, the mover replanned the same route into it for 20-50 s, once hanging in the air."""

    def drive(self, n, grounded=True, z_off=0.0):
        import time
        w = world()
        bot = Bot(hello(), world=w)
        mv = bot.mover
        x, y = 300.0, -60.0
        goal = (360.0, -60.0)
        notes = []
        for k in range(n):
            obs = zone_obs(self_obs(pos=(x, y, w.floor(x, y) + z_off), t=100.0 + 0.1 * k))
            obs["self"]["grounded"] = grounded
            me = Me(obs, bot.cfg)
            bot.zone.update(obs["zone"], me.t)
            mv.go(goal, me, bot.zone, tag="test")
            mv.work(me, bot.zone, time.perf_counter() + 0.02)
            mv.steer(me, bot.zone)
            notes.append(mv.note)
        return mv, goal, notes

    def test_backs_off_then_gives_the_goal_up_at_the_same_wall(self):
        mv, goal, notes = self.drive(120)
        self.assertIn("back off", notes)
        self.assertGreaterEqual(mv.backoffs, 2)
        self.assertTrue(mv.goal_bad(goal, 111.0))
        self.assertFalse(mv.goal_bad(goal, 145.0))       # forgiven after 30 s

    def test_hanging_in_the_air_against_a_wall_backs_off_at_once(self):
        mv, goal, notes = self.drive(25, grounded=False, z_off=0.4)
        self.assertIn("back off", notes[:22])


class JetTests(unittest.TestCase):
    def me(self, z, grounded=False, energy=80.0):
        return SimpleNamespace(pos=(300.0, -60.0, z), yaw=0.0, radius=2.0, energy=energy, grounded=grounded, t=100.0)

    def test_no_jets_in_the_air_over_open_ground(self):
        bot = Bot(hello(), world=world())
        f = bot.world.floor(300.0, -60.0)
        self.assertFalse(bot.mover._jet_needed(self.me(f + 3.0), (1.0, 0.0)))

    def test_stuck_in_the_air_while_seen_does_not_jet(self):
        bot = Bot(hello(), world=world())
        mv = bot.mover
        mv.exposed = True
        mv.last_move = (1.0, 0.0)
        f = bot.world.floor(300.0, -60.0)
        self.assertEqual(mv._unstick(2, self.me(f + 3.0), False, False), (False, False))

    def test_a_failed_climb_jumps_again_then_gives_up(self):
        from mech import routing
        info = routing.HopInfo(0, top=8.0, energy=10, land_floor=0.0, launch_floor=0.0)
        h = routing.Hop(0.0, (0.0, 0.0), (6.0, 0.0), info)
        self.assertTrue(h.step(0.0, 0.0, 0.0, 0.0, True, 100.0)[2])
        h.step(0.3, 0.2, 0.0, 9.0, False, 90.0)
        self.assertTrue(h.cleared)
        self.assertTrue(h.step(1.0, 1.0, 0.0, 0.0, True, 80.0)[2])
        self.assertFalse(h.failed)
        h.step(1.3, 1.0, 0.0, 3.0, False, 75.0)
        h.step(2.0, 1.0, 0.0, 0.0, True, 70.0)
        h.step(2.3, 1.0, 0.0, 3.0, False, 65.0)
        h.step(3.0, 1.0, 0.0, 0.0, True, 60.0)
        self.assertTrue(h.failed)


if __name__ == "__main__":
    unittest.main()
