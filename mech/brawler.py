"""Candidate D: a squad-mode close-quarters brawler and counter-attacker (mech/profile.py switches).

The practice squad shares vision and hunts us. House experts land 87.5% of gun shots and 83% of
zaps inside 45 m (analysis/mech/HOUSE-BOTS.md), but a lone hunter there meets gun + lightning
(about 190 + 240 HP/s, chained onto anyone within 14 m of it) and dies in 3-4 s. So with lightning
in hand we do not keep range: we hold a doorway and commit when a hunter walks into 45 m, or we
close through cover on a hunter that is alone (or on a tight cluster the arc chains through).

    brawl_loadout   lightning before the laser (a laser is swapped out for it, the gun stays);
                    lightning upgrades first
    brawl_commit    a hunter inside lightning range: everything on the chain target (lightning on
                    the nearest in the cone, the gun on the same mech), close to 28 m, no break
    brawl_attack    lightning, energy and HP in hand: close through cover on an isolated or
                    clustered hunter within 160 m
    brawl_counter   otherwise, with a hunter coming: wait silent in a cell hidden from every known
                    threat, with a wall beside it and a second way out, never where we were seen
                    in the last 5 s (the squad tracks a mech it saw for 5 s); the commit fires
                    when it walks in. The same 5 s rule keeps corner peeks off burned spots.
    brawl_mortar    mortar beats lightning (16 m/s throw, 45 energy drained): with a laser, mortar
                    holders come first at range; without one we neither attack them nor wait in
                    their lane, and hide from them as from any other watcher

Nothing here runs in a free-for-all: every entry point takes `squad` and returns early without it.
"""
import math

from . import nav
from . import profile as PF
from .cover import hidden_at
from .geom import angdiff, bearing

SEEN_MEMORY_S = 5.0     # protocol section 8: a mech that saw us tracks our real position this long
SEEN_RADIUS_M = 7.0     # ... so a cell this close to where we were seen is burned meanwhile
SEEN_EVERY_S = 0.5      # a seen position is noted at most this often
COMMIT_MARGIN_M = 2.0   # commit inside lightning range minus this
COMMIT_WANT_M = 28.0    # ... and close to this, so a strafing target cannot step out of the arc
COMMIT_ENERGY = 12.0    # lightning needs energy (3 a zap, 4 a second): at least this
COMMIT_QUIT_HP = 0.2    # under this share of HP against a target over half HP the lose rule stands
ATTACK_MAX_M = 160.0    # attack a hunter this close ...
ATTACK_ENERGY = 55.0    # ... with this much energy (a 5 s zap run costs 60, boosting 17 a second) ...
ATTACK_HP = 0.55        # ... and this share of HP
ATTACK_ISOLATED_M = 75.0  # no other hunter this close to the target (cluster mates excepted) ...
ATTACK_MAX_SEEN = 2     # ... and at most this many mechs with eyes on us
CLUSTER_M = 14.0        # lightning chain range: mechs this close to the target are chained
COUNTER_MAX_M = 140.0   # a hunter this close makes us hold a doorway ...
AMBUSH_COST_M = 40.0    # ... within this walk
EXIT_M = 18.0           # a way out: open ground this far ...
EXIT_SEP_RAD = 1.6      # ... in two directions at least this far apart
MORTAR_LASER_MULT = 2.5  # brawl_mortar with a laser: target weight of a mortar holder in laser range
MORTAR_LANE_M = 45.0    # without a laser: no attack on a target this close to a mortar holder


def light_range(me):
    lt = me.by_kind.get("lightning")
    return lt.stat("range", 45.0) if lt is not None else 0.0


def has_lightning(me):
    return me.by_kind.get("lightning") is not None


def _d(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def chain_count(target, mechs, t, rng=CLUSTER_M):
    """How many other mechs a lightning arc on `target` chains onto (at most 2 by the config)."""
    p = target.est_pos(t)
    n = 0
    for o in mechs:
        if o is target or o.dead:
            continue
        if _d(o.est_pos(t), p) <= rng:
            n += 1
    return min(n, 2)


def has_exits(world, n, open_m=EXIT_M, sep=EXIT_SEP_RAD):
    """A doorway with a second way out: open ground in two directions at least `sep` apart."""
    open_yaws = []
    for k in range(world.nfan):
        yaw = 2.0 * math.pi * k / world.nfan
        if world.fan_dist(n, yaw) >= open_m:
            open_yaws.append(yaw)
    for i, a in enumerate(open_yaws):
        for b in open_yaws[i + 1:]:
            if angdiff(a, b) >= sep:
                return True
    return False


class AmbushSearch(nav.NearestSearch):
    """Nearest walkable cell hidden from every watcher, a wall beside it, two ways out, not burned."""

    def __init__(self, world, start, watchers, burned, max_cost=AMBUSH_COST_M, height=9.0):
        watchers = list(watchers)
        burned = list(burned)

        def pred(n):
            if not world.main[n]:
                return False
            if world.clear_dm[world.vidx(*world.node_xy(n))] < 33:
                return False
            x, y = world.node_xy(n)
            for bx, by in burned:
                if math.hypot(x - bx, y - by) < SEEN_RADIUS_M:
                    return False
            if world.wall_arc(n, within=8.0) < 0.12:
                return False
            if not has_exits(world, n):
                return False
            return hidden_at(world, n, watchers, height)

        super().__init__(world, start, pred, max_cost)


class Brawler:
    """Per-match state of the brawler pieces."""

    def __init__(self, world):
        self.w = world
        self.seen = []              # (t, x, y): where a mech had eyes on us
        self.search = None
        self.spot = None            # (x, y) ambush cell
        self.spot_t = -1e9
        self.fail_t = -1e9
        self.mode = None            # 'commit' | 'attack' | 'counter' | None (this decision)

    # ------------------------------------------------------------ the 5 s rule
    def note_seen(self, me, enemies):
        t = me.t
        self.seen = [s for s in self.seen if t - s[0] <= SEEN_MEMORY_S]
        if any(e.sees_us and not e.dead for e in enemies):
            if not self.seen or t - self.seen[-1][0] >= SEEN_EVERY_S \
                    or _d(self.seen[-1][1:], me.pos) > 3.0:
                self.seen.append((t, me.pos[0], me.pos[1]))

    def burned(self, t):
        return [(x, y) for (ts, x, y) in self.seen if t - ts <= SEEN_MEMORY_S]

    def spot_burned(self, xy, t):
        return any(math.hypot(xy[0] - x, xy[1] - y) < SEEN_RADIUS_M for x, y in self.burned(t))

    # ------------------------------------------------------------ choices
    @staticmethod
    def hunting(tr, me, hunting_fn):
        if tr.dead or tr.kind != "mech":
            return False
        if hunting_fn(me, tr):
            return True
        return tr.in_obs and tr.sees_us and angdiff(tr.yaw, bearing(tr.pos, me.pos)) <= 0.45

    def commit_target(self, me, enemies, hunters, squad):
        """brawl_commit: (target, chain count) when a hunter is inside lightning range, else None.
        The target is the mech the arc chains through most, then the nearest; mortar holders last
        (brawl_mortar) unless nothing else is in reach."""
        if not (squad and PF.on("brawl_commit")) or not has_lightning(me) or me.energy < COMMIT_ENERGY:
            return None
        t = me.t
        rng = light_range(me) - COMMIT_MARGIN_M
        if not any(_d(h.est_pos(t), me.pos) <= rng and h.los for h in hunters):
            return None
        best = None
        for e in enemies:
            if e.dead or not e.los:
                continue
            d = _d(e.est_pos(t), me.pos)
            if d > rng:
                continue
            mortar_last = PF.on("brawl_mortar") and e.has("mortar") and not me.has("laser") and d > 20.0
            key = (0 if mortar_last else 1, chain_count(e, enemies, t), -d)
            if best is None or key > best[0]:
                best = (key, e)
        return (best[1], best[0][1]) if best else None

    def attack_target(self, me, enemies, hunters, squad):
        """brawl_attack: an isolated or clustered hunter worth closing on, else None."""
        if not (squad and PF.on("brawl_attack")) or not has_lightning(me):
            return None
        if me.energy < ATTACK_ENERGY or me.hp_frac < ATTACK_HP:
            return None
        t = me.t
        if sum(1 for e in enemies if e.sees_us and not e.dead) > ATTACK_MAX_SEEN:
            return None
        mortars = [e for e in enemies if not e.dead and e.has("mortar")]
        best = None
        for h in hunters:
            if h.phantom or t - h.t_seen > 3.0:
                continue
            p = h.est_pos(t)
            d = _d(p, me.pos)
            if d > ATTACK_MAX_M:
                continue
            if PF.on("brawl_mortar") and not me.has("laser"):
                if h.has("mortar") or any(m is not h and _d(m.est_pos(t), p) <= MORTAR_LANE_M for m in mortars):
                    continue
            mates = [o for o in enemies if o is not h and not o.dead and _d(o.est_pos(t), p) <= CLUSTER_M]
            if len(mates) > 2:
                continue
            others = [o for o in hunters if o is not h and o not in mates
                      and (_d(o.est_pos(t), p) <= ATTACK_ISOLATED_M or _d(o.est_pos(t), me.pos) <= ATTACK_ISOLATED_M)]
            if others:
                continue
            key = (len(mates), -d)
            if best is None or key > best[0]:
                best = (key, h)
        return best[1] if best else None

    def counter_wanted(self, me, hunters, squad):
        """brawl_counter: a hunter coming (within 140 m, outside lightning range): the nearest one."""
        if not (squad and PF.on("brawl_counter")) or not has_lightning(me):
            return None
        t = me.t
        rng = light_range(me)
        best = None
        for h in hunters:
            d = _d(h.est_pos(t), me.pos)
            if rng - COMMIT_MARGIN_M < d <= COUNTER_MAX_M and (best is None or d < best[0]):
                best = (d, h)
        return best[1] if best else None

    # ------------------------------------------------------------ the ambush cell
    def want_spot(self, me, watchers):
        t = me.t
        if self.spot is not None and t - self.spot_t < 3.0 and not self.spot_burned(self.spot, t):
            return
        if self.search is not None or t - self.fail_t < 1.0 or not watchers:
            return
        start = self.w.nearest_free(me.pos[0], me.pos[1], 5)
        if start is None:
            return
        self.spot = None
        self.spot_t = t
        self.search = AmbushSearch(self.w, start, watchers, self.burned(t), height=me.height)

    def run(self, deadline, t=None):
        if self.search is None:
            return
        if self.search.run(deadline):
            res = self.search.result
            if res is not None:
                self.spot = self.w.node_xy(res[0])
            else:
                self.fail_t = self.spot_t
            self.search = None


def brawl_upgrade_score(me, uid, squad):
    """brawl_loadout: lightning upgrades first while we carry lightning (squad mode only)."""
    if not (squad and PF.on("brawl_loadout")) or not me.has("lightning"):
        return None
    return {"lightning_efficiency": 108, "lightning_range": 106, "lightning_chain": 97,
            "reactor": 90, "capacitor": 88}.get(uid)
