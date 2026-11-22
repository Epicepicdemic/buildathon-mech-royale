"""Candidate D, the squad-mode brawler (mech/brawler.py): each piece in squad mode, and the
free-for-all left exactly as it was."""
import math
import unittest

from tests.helpers import config, self_obs, stream_rows, weapon, world
from tests.test_controller import hello
from tests.test_counters import enemy, zone_obs
from mech import brain as B
from mech import brawler as BR
from mech import doctrine as D
from mech import profile as PF
from mech.brain import Bot
from mech.state import Me
from mech.threats import Track

ME = (300.0, -60.0)              # open lawn (tests/test_counters.py)
PIECES = ("brawl_loadout", "brawl_commit", "brawl_attack", "brawl_counter", "brawl_mortar")
GL = lambda: [weapon("gun"), weapon("lightning")]        # noqa: E731


class pieces:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        self.old = {k: PF.PROFILE[k] for k in PIECES}
        for k in PIECES:
            PF.PROFILE[k] = self.value

    def __exit__(self, *a):
        PF.PROFILE.update(self.old)


def make_bot(squad):
    bot = Bot(hello(), world=world())
    if squad:
        bot.tactics.mode.mode = "squad"
    else:
        bot.tactics.mode.locked = True
    return bot


def run(mechs_fn, n=12, squad=True, weapons=GL, energy=100, hp=1400, t0=100.0, bot=None, extra=None):
    w = world()
    bot = bot or make_bot(squad)
    labels, acts = [], []
    for k in range(n):
        obs = zone_obs(self_obs(pos=(ME[0], ME[1], w.floor(*ME)), t=t0 + 0.1 * k, weapons=weapons(),
                                energy=energy, hp=hp))
        obs["mechs"] = mechs_fn(k)
        if extra:
            extra(obs, k)
        acts.append(bot.act(obs))
        labels.append(bot.last_label or "")
    return bot, labels, acts


def at(eid, dx, dy, weapons=("gun",), facing_us=True):
    w = world()
    x, y = ME[0] + dx, ME[1] + dy
    yaw = math.atan2(-dy, -dx) if facing_us else math.atan2(dy, dx)
    return enemy(eid, x, y, w, yaw, weapons=weapons)


def trk(tid, x, y, t=10.0, weapons=("gun",), los=True, sees=True):
    tr = Track(tid, "mech", t)
    tr.pos = [x, y, 0.0]
    tr.in_obs = tr.los = los
    tr.sees_us = sees
    tr.t_seen = t
    tr.hp, tr.max_hp = 1400.0, 1400.0
    tr.weapons = list(weapons)
    return tr


def me_at(x=0.0, y=0.0, t=10.0, weapons=None, energy=100, hp=1400):
    obs = self_obs(pos=(x, y, 0.0), t=t, weapons=weapons or GL(), energy=energy, hp=hp)
    return Me(obs, config())


class LoadoutTests(unittest.TestCase):
    def stand_on(self, squad):
        w = world()
        bot = make_bot(squad)
        obs = zone_obs(self_obs(pos=(ME[0], ME[1], w.floor(*ME)), t=60.0,
                                weapons=[weapon("gun"), weapon("laser")]))
        obs["pickups"] = [{"id": 8001, "kind": "lightning", "pos": [ME[0] + 1.0, ME[1], w.floor(*ME)]}]
        return bot.act(obs)

    def test_squad_swaps_the_laser_for_lightning_and_keeps_the_gun(self):
        with pieces(True):
            a = self.stand_on(True)
        self.assertEqual(a.get("take"), 8001)
        self.assertEqual(a.get("drop"), 1)

    def test_ffa_keeps_the_laser(self):
        with pieces(True):
            a = self.stand_on(False)
        self.assertNotEqual(a.get("take"), 8001)

    def test_lightning_upgrades_first_in_squad_only(self):
        offer = {"choices": [{"id": "plating"}, {"id": "lightning_efficiency"}, {"id": "armor"}]}

        def ex(obs, k):
            obs["offer"] = offer
        with pieces(True):
            _, _, sq = run(lambda k: [], n=1, extra=ex)
            _, _, ffa = run(lambda k: [], n=1, squad=False, extra=ex)
        self.assertEqual(sq[0].get("pick"), 1)
        self.assertEqual(ffa[0].get("pick"), 0)

    def test_lightning_counts_as_geared(self):
        with pieces(True):
            bot, labels, _ = run(lambda k: [at(9, 130.0, 0.0)], n=6, t0=30.0)
        self.assertTrue(bot.tactics.gear_done)
        self.assertFalse(any("gear" in l for l in labels), labels)


class CommitTests(unittest.TestCase):
    def test_a_hunter_inside_45_m_gets_everything(self):
        with pieces(True):
            bot, labels, acts = run(lambda k: [at(9, 34.0, 0.0)], n=14)
        self.assertTrue(any("brawl: commit" in l for l in labels[2:]), labels)
        self.assertTrue(any(a["fire"][1] for a in acts), "lightning never fired")
        self.assertTrue(any(a["fire"][0] for a in acts), "gun never fired")

    def test_never_in_a_free_for_all(self):
        with pieces(True):
            bot, labels, _ = run(lambda k: [at(9, 34.0, 0.0)], n=14, squad=False)
        self.assertFalse(any("brawl" in l for l in labels), labels)
        self.assertIsNone(bot.tactics.bmode)

    def test_needs_lightning(self):
        with pieces(True):
            _, labels, _ = run(lambda k: [at(9, 34.0, 0.0)], n=8, weapons=lambda: [weapon("gun"), None])
        self.assertFalse(any("brawl" in l for l in labels), labels)

    def test_the_target_is_the_one_the_arc_chains_through(self):
        br = BR.Brawler(world())
        me = me_at()
        a = trk(5, 28.0, 0.0)
        b = trk(6, 0.0, 38.0)
        c = trk(7, 6.0, 44.0, sees=False)          # 8.5 m from b: chained
        got = br.commit_target(me, [a, b, c], [a, b], True)
        self.assertIs(got[0], b)
        self.assertEqual(got[1], 1)

    def test_a_mortar_holder_comes_last_without_a_laser(self):
        br = BR.Brawler(world())
        me = me_at()
        m = trk(5, 25.0, 0.0, weapons=("gun", "mortar"))
        g = trk(6, 0.0, 40.0)
        with pieces(True):
            self.assertIs(br.commit_target(me, [m, g], [m, g], True)[0], g)
            self.assertIsNone(br.commit_target(me, [m, g], [m, g], False))


class AttackTests(unittest.TestCase):
    def test_closes_on_a_lone_hunter(self):
        with pieces(True):
            _, labels, _ = run(lambda k: [at(9, 110.0, 0.0)], n=14)
        self.assertTrue(any("brawl: close" in l for l in labels[2:]), labels)

    def test_not_with_a_second_hunter_beside_it(self):
        br = BR.Brawler(world())
        me = me_at()
        a = trk(5, 110.0, 0.0)
        b = trk(6, 110.0, 40.0)
        self.assertIsNone(br.attack_target(me, [a, b], [a, b], True))
        self.assertIs(br.attack_target(me, [a], [a], True), a)

    def test_a_tight_cluster_is_attacked(self):
        br = BR.Brawler(world())
        me = me_at()
        a = trk(5, 110.0, 0.0)
        b = trk(6, 110.0, 9.0)                      # inside the 14 m chain
        self.assertIs(br.attack_target(me, [a, b], [a, b], True).id in (5, 6), True)

    def test_not_low_on_energy_or_hp(self):
        br = BR.Brawler(world())
        a = trk(5, 110.0, 0.0)
        self.assertIsNone(br.attack_target(me_at(energy=30), [a], [a], True))
        self.assertIsNone(br.attack_target(me_at(hp=600), [a], [a], True))

    def test_mortar_lane_without_a_laser(self):
        br = BR.Brawler(world())
        me = me_at()
        a = trk(5, 110.0, 0.0, weapons=("gun", "mortar"))
        b = trk(6, 0.0, 110.0)
        m = trk(7, 30.0, 120.0, weapons=("gun", "mortar"), sees=False)
        with pieces(True):
            self.assertIsNone(br.attack_target(me, [a], [a], True))
            self.assertIsNone(br.attack_target(me, [b, m], [b], True))


class CounterTests(unittest.TestCase):
    def test_where_we_were_seen_is_burned_for_5_s(self):
        br = BR.Brawler(world())
        me = me_at(10.0, 20.0, t=10.0)
        br.note_seen(me, [trk(5, 100.0, 0.0)])
        self.assertTrue(br.spot_burned((12.0, 22.0), 12.0))
        self.assertFalse(br.spot_burned((30.0, 22.0), 12.0))
        self.assertFalse(br.spot_burned((12.0, 22.0), 15.5))

    def test_ambush_cell_is_hidden_has_two_exits_and_is_not_burned(self):
        w = world()
        burned = [ME]
        start = w.nearest_free(ME[0], ME[1], 5)
        watcher = (ME[0] + 120.0, ME[1], w.floor(ME[0] + 120.0, ME[1]) + 8.0)
        s = BR.AmbushSearch(w, start, [watcher], burned, max_cost=120.0)
        s.run(1e18)
        if s.result is None:
            self.skipTest("no ambush cell within 120 m of the test lawn")
        n = s.result[0]
        x, y = w.node_xy(n)
        self.assertGreaterEqual(math.hypot(x - ME[0], y - ME[1]), BR.SEEN_RADIUS_M)
        self.assertTrue(BR.has_exits(w, n))
        from mech.cover import hidden_at
        self.assertTrue(hidden_at(w, n, [watcher]))

    def test_corner_peeks_avoid_burned_spots_in_squad_only(self):
        with pieces(True):
            sq, _, _ = run(lambda k: [at(9, 34.0, 0.0)], n=3)
            ffa, _, _ = run(lambda k: [at(9, 34.0, 0.0)], n=3, squad=False)
        self.assertTrue(len(sq.tactics.cov.extra_avoid) >= 1)
        self.assertEqual(tuple(ffa.tactics.cov.extra_avoid), ())


class MortarTargetTests(unittest.TestCase):
    def test_with_a_laser_the_mortar_holder_comes_first_in_squad(self):
        cfg = config()
        ws = {"gun": dict(cfg["weapons"]["gun"]), "laser": dict(cfg["weapons"]["laser"])}
        m = trk(5, 240.0, 0.0, t=100.0, weapons=("gun", "mortar"))
        g = trk(6, 40.0, 0.0, t=100.0, weapons=("gun",))
        fac = lambda e: D.Situation(100.0, (0.0, 0.0), 0.0, 1400, 1400, ws, e, [], cfg=cfg)  # noqa: E731
        with pieces(True):
            self.assertIs(D.pick_target(fac, [m, g], 100.0, (0.0, 0.0), squad=True)[0], m)
        with pieces(False):
            self.assertIs(D.pick_target(fac, [m, g], 100.0, (0.0, 0.0), squad=True)[0], g)


class FreeForAllUnchangedTests(unittest.TestCase):
    """The pieces switched on or off give the same actions in a free-for-all (searches run to the
    end each decision so both runs see the same results)."""

    def setUp(self):
        self.old = B.WORK_BUDGET_S
        B.WORK_BUDGET_S = 5.0

    def tearDown(self):
        B.WORK_BUDGET_S = self.old

    def scenario(self, value):
        def mechs(k):
            return [at(9, 34.0 + k, 0.0), at(10, 110.0, 30.0, weapons=("gun", "mortar"))]
        with pieces(value):
            _, _, acts = run(mechs, n=20, squad=False)
        return acts

    def test_close_fight_identical(self):
        self.assertEqual(self.scenario(True), self.scenario(False))

    def test_recorded_stream_identical(self):
        rows = stream_rows()

        def replay(value):
            out = []
            with pieces(value):
                bot = Bot(rows[0])
                bot.tactics.mode.locked = True
                for r in rows[1:]:
                    if r.get("type") == "obs":
                        out.append(bot.act(r))
            return out
        self.assertEqual(replay(True), replay(False))


if __name__ == "__main__":
    unittest.main()
