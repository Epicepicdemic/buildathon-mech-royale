"""Deadline-aware route choice, path following and stuck recovery.

The rule: first find the fastest route and its travel time. Spend slack on a covered route only
when the covered route still arrives inside the next circle with margin before the deadline;
otherwise take the fastest route, and boost when walking would be late.
"""
import math
import time

from . import nav
from .geom import unit

INF = float("inf")


class Mobility:
    """Travel-time model from the live mech stats."""

    def __init__(self, stats=None):
        self.update(stats or {})

    def update(self, s):
        self.walk = float(s.get("walk_speed", 11.7))
        self.boost = float(s.get("boost_speed", 19.5))
        self.max_energy = float(s.get("energy", 100.0))
        self.drain = float(s.get("boost_drain", 17.0))
        self.regen = float(s.get("energy_regen", 18.0))
        self.delay = float(s.get("energy_regen_delay", 0.6))
        # average speed when cycling: walk W s (regen after the delay), then boost what was gained
        W = 3.0
        B = self.regen * max(0.0, W - self.delay) / max(self.drain, 1e-6)
        self.cycle = (self.walk * W + self.boost * B) / (W + B)
        self.silent = 4.8

    def time_walk(self, d):
        return d / max(self.walk, 0.1)

    def time_fast(self, d, energy):
        """Fastest time to cover d metres boosting whenever energy allows."""
        burst_t = max(0.0, energy) / max(self.drain, 1e-6)
        burst_d = burst_t * self.boost
        if d <= burst_d:
            return d / self.boost
        return burst_t + (d - burst_d) / max(self.cycle, 0.1)


PATH_FACTOR = 1.08         # path following overhead (turns, acceleration)


def deadline_margin(travel_s):
    return 2.0 + 0.1 * travel_s


def enter_length(world, nodes, cx, cy, r):
    """Path length until the first node inside the circle (cx, cy, r); inf if never."""
    tot = 0.0
    prev = None
    for n in nodes:
        p = world.node_xy(n)
        if prev is not None:
            tot += math.hypot(p[0] - prev[0], p[1] - prev[1])
        if math.hypot(p[0] - cx, p[1] - cy) <= r:
            return tot
        prev = p
    return INF


class HopInfo:
    __slots__ = ("idx", "top", "energy", "land_floor", "launch_floor")

    def __init__(self, idx, top, energy, land_floor, launch_floor):
        self.idx, self.top, self.energy = idx, top, energy
        self.land_floor, self.launch_floor = land_floor, launch_floor


def route_hops(world, nodes):
    """{index k: HopInfo} for steps nodes[k] -> nodes[k + 1] that are hop edges (not grid neighbours)."""
    out = {}
    nu = world.nu
    for k in range(len(nodes) - 1):
        a, b = nodes[k], nodes[k + 1]
        va, ua = divmod(a, nu)
        vb, ub = divmod(b, nu)
        if abs(ua - ub) <= 1 and abs(va - vb) <= 1:
            continue
        h = world.hop(a, b)
        if h is None:
            continue
        ax, ay = world.node_xy(a)
        bx, by = world.node_xy(b)
        out[k] = HopInfo(k, h[2], h[1], world.floor(bx, by), world.floor(ax, ay))
    return out


class Hop:
    """Flying one hop edge: walk at the obstacle, jump, jet until the feet clear its top, cross with
    the feet kept above whatever is under the body, carry on to the landing node and come down.
    Pressing toward the landing while rising is safe: walls slide us. A drop (nothing above our
    feet) is a walk off the edge."""
    TIMEOUT = 6.0
    CLEAR = 0.9           # feet this far above the top before we stop climbing
    BODY = 2.7            # radius of the footprint that must be clear before we come down

    def __init__(self, t, launch, land, info, world=None):
        self.t0 = t
        self.launch, self.land, self.info = launch, land, info
        self.w = world
        self.left_ground = False
        self.cleared = False       # the feet have been above the top
        self.tries = 0
        self.done = False
        self.failed = False

    def _under(self, x, y, mv):
        """Highest solid under us and just ahead along our way (sight raster), or -inf without a
        world. Walls beside the path do not count: we slide along those, we do not cross them."""
        w = self.w
        if w is None:
            return -1e9
        b = self.BODY
        best = w.sight(x, y)
        for k in (0.5, 1.0):
            v = w.sight(x + mv[0] * b * k, y + mv[1] * b * k)
            if v > best:
                best = v
        return best

    def step(self, t, x, y, z, grounded, energy):
        """(move, boost, jump) for this decision; sets done / failed."""
        info = self.info
        dx, dy = self.land[0] - x, self.land[1] - y
        d = math.hypot(dx, dy)
        mv = unit(dx, dy) if d > 0.3 else (0.0, 0.0)
        if t - self.t0 > self.TIMEOUT:
            self.failed = True
            return (0.0, 0.0), False, False
        climb = info.top > z + 0.6 and not self.cleared
        if grounded:
            # on the ground below the top (a climb that fell back, even after clearing it in the
            # air): jump again; walking on would press into the wall until the timeout
            climb = info.top > z + 0.6
            # landed: near the landing node, or past most of the hop at the landing floor (on the
            # launch side of a thin wall the floor is the same height, so the side matters)
            lx, ly = self.land[0] - self.launch[0], self.land[1] - self.launch[1]
            L2 = lx * lx + ly * ly
            along = ((x - self.launch[0]) * lx + (y - self.launch[1]) * ly) / L2 if L2 > 1e-6 else 1.0
            near_land = d < 3.0 or (along >= 0.6 and abs(z - info.land_floor) < 1.3 and d < 7.0 and t - self.t0 > 0.4)
            if info.land_floor > info.launch_floor + 1.2 and z < info.land_floor - 1.0:
                near_land = False      # a climb is done only once we are up there
            if self.left_ground and near_land:
                self.done = True
                return mv, False, False
            if not climb:
                # a drop, or we are up already: walk on toward the landing
                if near_land and t - self.t0 > 0.2:
                    self.done = True
                return mv, False, False
            if self.left_ground:
                self.tries += 1
                if self.tries > 2:
                    self.failed = True
                    return (0.0, 0.0), False, False
            self.left_ground = True
            self.cleared = False
            return mv, False, True
        self.left_ground = True
        if z >= info.top + self.CLEAR - 0.05:
            self.cleared = True
        if not self.cleared:
            boost = z < info.top + self.CLEAR and energy > 0.5
        else:
            # crossing: hold the feet above whatever is still under the body, then come down
            under = self._under(x, y, mv)
            boost = under > info.land_floor + 1.2 and z < under + 0.8 and energy > 0.5
            if not boost and self.w is not None and info.land_floor > info.launch_floor + 1.2 \
                    and self.w.floor(x, y) < info.land_floor - 1.0 and z < info.land_floor + 0.8 and energy > 0.5:
                boost = True           # up onto a roof: stay above it until our centre is over it
            elif not boost and d < 3.5:
                mv = (0.0, 0.0)        # over the landing: stop pushing, or momentum carries us past
        return mv, boost, False


class Route:
    def __init__(self, world, nodes, kind, boost, enter_len, fast_len):
        self.nodes = nodes
        self.hops = route_hops(world, nodes)
        self.pts = nav.path_points(world, nodes)
        cum = [0.0]
        for a, b in zip(self.pts, self.pts[1:]):
            cum.append(cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
        self.cum = cum
        self.length = cum[-1]
        self.kind = kind            # 'fast' or 'cover'
        self.boost = boost          # walking would be late: boost along it
        self.enter_len = enter_len  # metres along the route until inside the safe circle
        self.fast_len = fast_len    # length of the fastest route to the same goal


class RoutePlanner:
    """Resumable: call run(deadline_perf) each decision until done.

    Order of decisions:
      1. If a zone field is given and even its path (the fastest way into the safe circle) is late
         when walked, take that path and boost.
      2. Fastest route to the goal. If walking it enters the safe circle too late, take the zone
         field path instead (walking if that is in time, else boosting).
      3. Only with slack left: the covered route to the goal, accepted if walking it still enters
         the safe circle with margin; otherwise the fastest route to the goal.
    """

    def __init__(self, world, start, goal, mobility, energy, now, deadline, safe, cover_mult=None,
                 prefer_cover=True, zone_field=None, stay=None):
        self.w = world
        self.start, self.goal = start, goal
        self.mob = mobility
        self.energy = energy
        self.now, self.deadline = now, deadline
        self.safe = safe                     # (cx, cy, r) region that counts as arrived
        self.cover_mult = cover_mult
        self.prefer_cover = prefer_cover and cover_mult is not None
        self.zf = zone_field if (zone_field is not None and zone_field.done) else None
        self.stage = "fast"
        self.search = None
        self.fast = None
        self.result = None
        self.note = ""
        if self.zf is not None:
            d = self.zf.cost(start)
            if d < INF and not self._walk_ok(d):
                self.result = self._zone_route(boost=True)
                self.note = "zone path (late)"
                return
        self.stay = stay
        self.search = nav.AStar(world, start, goal, stay=stay)

    def _walk_ok(self, enter_len):
        if enter_len == INF:
            return False
        t_enter = self.mob.time_walk(enter_len * PATH_FACTOR)
        return self.now + t_enter + deadline_margin(t_enter) <= self.deadline

    def _zone_route(self, boost):
        nodes = self.zf.path(self.start)
        e = enter_length(self.w, nodes, *self.safe)
        if e == INF:
            e = nav.path_length(self.w, nodes)
        return Route(self.w, nodes, "zone", boost, e, e)

    def run(self, deadline):
        while self.result is None:
            if self.search is None:
                return True
            if not self.search.run(deadline):
                return False
            nodes = self.search.result
            if self.stage == "fast":
                if not nodes:
                    self.stage = "failed"
                    self.note = "unreachable"
                    if self.zf is not None and self.zf.cost(self.start) < INF:
                        self.result = self._zone_route(boost=not self._walk_ok(self.zf.cost(self.start)))
                        self.note = "unreachable goal: zone path"
                    self.search = None
                    return True
                L = nav.path_length(self.w, nodes)
                e = enter_length(self.w, nodes, *self.safe)
                self.fast = (nodes, L, e)
                if not self._walk_ok(e):
                    if self.zf is not None and self.zf.cost(self.start) < INF:
                        self.result = self._zone_route(boost=not self._walk_ok(self.zf.cost(self.start)))
                        self.note = "goal too far: zone path"
                    else:
                        self.result = Route(self.w, nodes, "fast", True, e, L)
                        self.note = "fast (late)"
                    return True
                if not self.prefer_cover:
                    self.result = Route(self.w, nodes, "fast", False, e, L)
                    self.note = "fast"
                    return True
                self.stage = "cover"
                self.search = nav.AStar(self.w, self.start, self.goal, mult=self.cover_mult, stay=self.stay)
            else:
                fn, FL, fe = self.fast
                if nodes:
                    e = enter_length(self.w, nodes, *self.safe)
                    if self._walk_ok(e):
                        self.result = Route(self.w, nodes, "cover", False, e, FL)
                        self.note = "cover"
                        return True
                self.result = Route(self.w, fn, "fast", False, fe, FL)
                self.note = "fast (cover too slow)"
                return True
        return True


class Follower:
    """Pure pursuit along a node path: aim for the farthest point ahead that can be walked to."""

    LOOKAHEAD = 14          # nodes (about 28 m)

    def __init__(self, world, pts, cum=None, hops=None):
        self.w = world
        self.pts = pts
        self.hops = sorted(hops) if hops else []     # indices k: pts[k] -> pts[k + 1] is a hop
        if cum is None:
            cum = [0.0]
            for a, b in zip(pts, pts[1:]):
                cum.append(cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
        self.cum = cum
        self.i = 0
        self.last_target = 0

    def progress(self, x, y):
        pts = self.pts
        best = None
        hi = min(len(pts), self.i + 20)
        for k in range(self.i, hi):
            d = (pts[k][0] - x) ** 2 + (pts[k][1] - y) ** 2
            if best is None or d < best[0]:
                best = (d, k)
        if best is not None:
            self.i = best[1]
        return math.sqrt(best[0]) if best else INF

    def next_hop(self):
        for k in self.hops:
            if k >= self.i - 1:
                return k
        return None

    def hop_due(self, x, y, reach=2.6):
        """Index of a hop that starts here (we are at its launch point), or None."""
        k = self.next_hop()
        if k is None:
            return None
        px, py = self.pts[k]
        if math.hypot(px - x, py - y) <= reach:
            return k
        return None

    def passed_hop(self, k):
        """The hop at k has been flown: carry on from its landing point."""
        self.i = k + 1
        self.hops = [h for h in self.hops if h > k]

    def target(self, x, y):
        """(tx, ty, remaining_m, off_path_m)."""
        off = self.progress(x, y)
        pts = self.pts
        last = len(pts) - 1
        j = min(last, self.i + self.LOOKAHEAD)
        k = self.next_hop()
        if k is not None:
            j = min(j, max(k, self.i))          # never aim past the launch point of a hop
        w = self.w
        probes = 0
        while j > self.i + 1:
            # bounded: at most 7 straight-walk probes, sampled every 0.75 m on the 1 m tables
            if w.walkable(x, y, pts[j][0], pts[j][1], need=2.7, step=0.75, skip_start=1.0):
                break
            probes += 1
            j -= 2 if probes < 7 else (j - self.i - 1)
        if j <= self.i:
            j = min(last, self.i + 1)
        self.last_target = j
        tx, ty = pts[j]
        remaining = math.hypot(tx - x, ty - y) + self.cum[last] - self.cum[j]
        return tx, ty, remaining, off

    def finished(self, x, y, tol=2.0):
        tx, ty = self.pts[-1]
        return self.i >= len(self.pts) - 2 and math.hypot(tx - x, ty - y) <= tol


class StuckMonitor:
    """Notices commanded motion that produces none, and escalates: jump, then jets, then replan."""

    def __init__(self):
        self.hist = []          # (t, x, y, commanded)
        self.stage = 0
        self.since = None
        self.events = 0

    def grace(self, until):
        """No stuck calls before `until` (just landed from a hop: air control is weak, so we leave a
        landing slowly, and that is not being stuck)."""
        self.grace_until = until
        self.hist.clear()

    def update(self, t, x, y, commanded):
        if t < getattr(self, "grace_until", -1e9):
            return 0
        self.hist.append((t, x, y, commanded))
        while self.hist and t - self.hist[0][0] > 1.2:
            self.hist.pop(0)
        if len(self.hist) < 6 or not all(h[3] for h in self.hist):
            if self.stage and self._moved(2.5):
                self.stage = 0
            return self.stage
        if not self._moved(1.2):
            if self.stage == 0:
                self.stage = 1
                self.since = t
                self.events += 1
            elif t - self.since > 1.0 and self.stage < 3:
                self.stage += 1
                self.since = t
        elif self._moved(3.0):
            self.stage = 0
        return self.stage

    def _moved(self, d):
        t0, x0, y0, _ = self.hist[0]
        t1, x1, y1, _ = self.hist[-1]
        return math.hypot(x1 - x0, y1 - y0) >= d

    def reset(self):
        self.hist.clear()
        self.stage = 0


def cover_multiplier(world, weight=1.6, wall_bonus=0.25):
    """Per-node cost multiplier (>= 1) that prefers low exposure."""
    expo = world.expo
    N = world.nu * world.nv
    out = [1.0] * N
    for n in range(N):
        if world.edges[n]:
            out[n] = 1.0 + weight * (expo[n] / 255.0) ** 1.5
    return out


def speed_mode(remaining_enter_m, now, deadline, mob, energy, want_silent=False, reserve=12.0):
    """'boost', 'walk' or 'silent' for the rest of a route, from the slack before the deadline."""
    if remaining_enter_m <= 0 or remaining_enter_m == INF:
        return "silent" if want_silent else "walk"
    t_walk = mob.time_walk(remaining_enter_m * PATH_FACTOR)
    if now + t_walk + deadline_margin(t_walk) > deadline:
        return "boost" if energy > reserve else "walk"
    if want_silent:
        t_sil = remaining_enter_m * PATH_FACTOR / mob.silent
        if now + t_sil + deadline_margin(t_sil) <= deadline:
            return "silent"
    return "walk"


class Booster:
    """Boost with hysteresis: once the tank hits `low`, walk until it has refilled to `resume`
    (short bursts keep resetting the regeneration delay and waste energy)."""

    def __init__(self, low=8.0, resume=40.0):
        self.low, self.resume = low, resume
        self.resting = False

    def want(self, need, energy):
        if not need:
            return False
        if self.resting:
            if energy >= self.resume:
                self.resting = False
        elif energy <= self.low:
            self.resting = True
        return not self.resting
