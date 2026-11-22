"""Route planning on the 2 m navigation grid.

* `ZoneField`: reverse Dijkstra from every walkable node inside a circle. After it finishes,
  `cost(n)` is the walking distance from n to the circle; it is computed in slices so that no
  decision spends more than its budget.
* `AStar`: resumable forward search to one goal, with an optional per-node cost multiplier
  (used to prefer covered ground).
* `nearest_where`: resumable Dijkstra from a start that stops at the first node satisfying a
  predicate (cover search, peek search).
* `smooth_target`: picks the farthest point ahead on a path that can be walked straight to.
"""
import heapq
import math
import time


# ---------------------------------------------------------------- deterministic search budget
# Cost of one unit of search work, in budget units (about microseconds of one worker CPU,
# measured with analysis/mech/sim/reports/cr3/clockwork.py and loscost.py: zone-field pop 0.98 us,
# A* pop 2.7 us, a nearest-search check 8 us plus its line of sight). The same work is done on a fast
# or a slow machine.
ZONE_POP = 1.0
ASTAR_POP = 2.8
NEAREST_CHECK = 8.0         # a nearest-search check without its line-of-sight work ...
LOS_STEP = 0.08             # ... plus this per line-of-sight sample step it ran (World.los_steps; a hide
                            # check costs 2.0 + 0.078 us per step on one worker CPU: cr3/loscost.py)


class WorkBudget:
    """One decision's search work: `units`, shared by the searches in order. A search given
    `at(floor)` stops once the units left fall to its floor. `hard` (perf_counter seconds) is a
    safety stop only, for a machine far slower than any we measured."""

    def __init__(self, units, hard):
        self.left = float(units)
        self.hard = hard

    def at(self, floor):
        return _Slice(self, float(floor))


class _Slice:
    __slots__ = ("b", "floor")

    def __init__(self, b, floor):
        self.b, self.floor = b, floor

    def spend(self, n):
        self.b.left -= n

    def out(self):
        return self.b.left <= self.floor or time.perf_counter() > self.b.hard

    def has(self):
        return not self.out()

    def raised(self, extra):
        return _Slice(self.b, self.floor + extra)


class _Clock:
    """A plain perf_counter deadline (the old interface: tools and tests pass 1e18)."""
    __slots__ = ("d",)

    def __init__(self, d):
        self.d = d

    def spend(self, n):
        pass

    def out(self):
        return time.perf_counter() > self.d

    def has(self):
        return not self.out()

    def raised(self, extra):
        return _Clock(self.d - extra * 1e-6)


def as_slice(deadline):
    return deadline if hasattr(deadline, "spend") else _Clock(deadline)


INF = float("inf")


class ZoneField:
    """Walking distance (metres) from every node to the nearest node inside a circle."""

    def __init__(self, world, cx, cy, r, stay=None, outside_mult=5.0):
        """`stay` = (cx, cy, r): the current zone. Walking outside it costs `outside_mult` times
        more, so paths go around buildings on the inside of the circle when they can."""
        self.w = world
        self.cx, self.cy, self.r = cx, cy, r
        nu, nv = world.nu, world.nv
        self.dist = [INF] * (nu * nv)
        self.om = outside_mult
        self.inside = None
        if stay is not None:
            scx, scy, sr = stay
            ins = bytearray(nu * nv)
            for v in range(nv):
                dy = world.y0 + v * world.res - scy
                if abs(dy) > sr:
                    continue
                half = math.sqrt(sr * sr - dy * dy)
                u0 = max(0, int(math.ceil((scx - half - world.x0) / world.res)))
                u1 = min(nu - 1, int(math.floor((scx + half - world.x0) / world.res)))
                if u0 <= u1:
                    ins[v * nu + u0:v * nu + u1 + 1] = b"\x01" * (u1 - u0 + 1)
            self.inside = ins
        self.heap = []
        self.done = False
        self.pops = 0
        res = world.res
        x0, y0 = world.x0, world.y0
        dist = self.dist
        edges = world.edges
        band = 2.5 * res
        for v in range(nv):
            y = y0 + v * res
            dy = y - cy
            if abs(dy) > r:
                continue
            half = math.sqrt(max(0.0, r * r - dy * dy))
            u0 = max(0, int(math.ceil((cx - half - x0) / res)))
            u1 = min(nu - 1, int(math.floor((cx + half - x0) / res)))
            if u0 > u1:
                continue
            row = v * nu
            dist[row + u0:row + u1 + 1] = [0.0] * (u1 - u0 + 1)
            # seed the nodes near the edge of the disc
            inner = r - band
            if abs(dy) >= inner:
                seeds = range(u0, u1 + 1)
            else:
                ih = math.sqrt(inner * inner - dy * dy)
                a1 = min(u1, int(math.ceil((cx - ih - x0) / res)))
                b0 = max(u0, int(math.floor((cx + ih - x0) / res)))
                seeds = list(range(u0, a1 + 1)) + list(range(b0, u1 + 1))
            for u in seeds:
                n = row + u
                if edges[n]:
                    self.heap.append((0.0, n))
        heapq.heapify(self.heap)

    def run(self, deadline):
        """Expand until done or past `deadline` (perf_counter seconds). Returns done."""
        if self.done:
            return True
        heap = self.heap
        dist = self.dist
        edges = self.w.edges
        table = self.w.nbr_table
        pop, push = heapq.heappop, heapq.heappush
        sl = as_slice(deadline)
        count = 0
        ins = self.inside
        om = self.om
        while heap:
            d, n = pop(heap)
            if d > dist[n]:
                continue
            if ins is None:
                for o, c in table[edges[n]]:
                    m = n + o
                    nd = d + c
                    if nd < dist[m]:
                        dist[m] = nd
                        push(heap, (nd, m))
            else:
                for o, c in table[edges[n]]:
                    m = n + o
                    nd = d + (c if ins[m] else c * om)
                    if nd < dist[m]:
                        dist[m] = nd
                        push(heap, (nd, m))
            count += 1
            if count & 255 == 0:
                sl.spend(256 * ZONE_POP)
                if sl.out():
                    self.pops += count
                    return False
        self.pops += count
        self.done = True
        return True

    def cost(self, n):
        return self.dist[n] if n is not None else INF

    def inside(self, x, y, margin=0.0):
        return math.hypot(x - self.cx, y - self.cy) <= self.r - margin

    def descend(self, n):
        """Best next node from n toward the circle (None at the circle or if unreachable)."""
        dist = self.dist
        d0 = dist[n]
        if d0 == 0.0 or d0 == INF:
            return None
        best, bn = INF, None
        ins = self.inside
        for o, c in self.w.nbr_table[self.w.edges[n]]:
            m = n + o
            dm = dist[m]
            if dm < d0:
                v = dm + (c if ins is None or ins[n] else c * self.om)
                if v < best:
                    best, bn = v, m
        return bn

    def path(self, n, max_nodes=2000):
        out = [n]
        while len(out) < max_nodes:
            m = self.descend(out[-1])
            if m is None:
                break
            out.append(m)
        return out


class AStar:
    """Resumable A* from start to goal over walkable nodes. `mult[n]` (optional) scales the cost
    of entering n; it must be >= 1 so the straight-line heuristic stays admissible."""

    def __init__(self, world, start, goal, mult=None, max_pops=200000, stay=None, outside_mult=5.0):
        """`stay` = (cx, cy, r): entering a node outside this circle (the current zone) costs
        `outside_mult` times more, so routes go round buildings on the inside of the circle."""
        self.w = world
        self.start, self.goal = start, goal
        self.mult = mult
        self.stay = stay
        self.om = outside_mult
        self.max_pops = max_pops
        gv, gu = divmod(goal, world.nu)
        self.gu, self.gv = gu, gv
        self.g = {start: 0.0}
        self.parent = {start: None}
        self.heap = [(self.h(start), 0.0, start)]
        self.result = None       # list of nodes, or [] when unreachable
        self.pops = 0

    def h(self, n):
        v, u = divmod(n, self.w.nu)
        du = abs(u - self.gu)
        dv = abs(v - self.gv)
        return self.w.res * (max(du, dv) + 0.41421356 * min(du, dv))

    def run(self, deadline):
        if self.result is not None:
            return True
        heap = self.heap
        g = self.g
        parent = self.parent
        edges = self.w.edges
        table = self.w.nbr_table
        mult = self.mult
        goal = self.goal
        nu = self.w.nu
        gu, gv = self.gu, self.gv
        res = self.w.res
        pop, push = heapq.heappop, heapq.heappush
        sl = as_slice(deadline)
        count = 0
        stay = self.stay
        if stay is not None:
            # in grid units relative to the grid origin
            scu = (stay[0] - self.w.x0) / res
            scv = (stay[1] - self.w.y0) / res
            sr2 = (stay[2] / res) ** 2
            om = self.om
        while heap:
            f, d, n = pop(heap)
            if d > g.get(n, INF):
                continue
            if n == goal:
                path = []
                while n is not None:
                    path.append(n)
                    n = parent[n]
                path.reverse()
                self.result = path
                self.pops += count
                return True
            for o, c in table[edges[n]]:
                m = n + o
                nd = d + (c * mult[m] if mult is not None else c)
                if stay is not None:
                    v, u = divmod(m, nu)
                    if (u - scu) ** 2 + (v - scv) ** 2 > sr2:
                        nd += (nd - d) * (om - 1.0)
                if nd < g.get(m, INF):
                    g[m] = nd
                    parent[m] = n
                    v, u = divmod(m, nu)
                    du = u - gu if u > gu else gu - u
                    dv = v - gv if v > gv else gv - v
                    hh = res * (du + dv - 0.58578644 * (du if du < dv else dv))
                    push(heap, (nd + hh, nd, m))
            count += 1
            if count & 127 == 0:
                if self.pops + count > self.max_pops:
                    self.result = []
                    self.pops += count
                    return True
                sl.spend(128 * ASTAR_POP)
                if sl.out():
                    self.pops += count
                    return False
        self.result = []
        self.pops += count
        return True


class NearestSearch:
    """Resumable Dijkstra (walking distance) from `start` that stops at the first node for which
    `pred(n)` is true, within `max_cost` metres. `result` is (node, path) or None when not found."""

    def __init__(self, world, start, pred, max_cost=80.0, mult=None, alts=1, alt_slack=0.0, rng=None):
        """With alts > 1, keep searching until `alts` matches or `alt_slack` metres beyond the first,
        then draw one of them with `rng` (nearer ones more likely)."""
        self.w = world
        self.pred = pred
        self.max_cost = max_cost
        self.mult = mult
        self.g = {start: 0.0}
        self.parent = {start: None}
        self.heap = [(0.0, start)]
        self.done = False
        self.result = None
        self.checked = 0
        self.alts = alts
        self.alt_slack = alt_slack
        self.rng = rng
        self.found = []

    def run(self, deadline):
        if self.done:
            return True
        heap = self.heap
        g = self.g
        parent = self.parent
        edges = self.w.edges
        table = self.w.nbr_table
        mult = self.mult
        pred = self.pred
        sl = as_slice(deadline)
        w = self.w
        los0 = w.los_steps
        pop, push = heapq.heappop, heapq.heappush
        while heap:
            d, n = pop(heap)
            if d > g.get(n, INF):
                continue
            self.checked += 1
            if self.found and d > self.found[0][0] + self.alt_slack:
                return self._finish()
            if pred(n):
                self.found.append((d, n))
                if len(self.found) >= self.alts:
                    return self._finish()
            if d <= self.max_cost:
                for o, c in table[edges[n]]:
                    m = n + o
                    nd = d + (c * mult[m] if mult is not None else c)
                    if nd < g.get(m, INF):
                        g[m] = nd
                        parent[m] = n
                        push(heap, (nd, m))
            if (self.checked & 7) == 0:
                los1 = w.los_steps
                sl.spend(8 * NEAREST_CHECK + (los1 - los0) * LOS_STEP)
                los0 = los1
                if sl.out():
                    return False
        return self._finish()

    def _finish(self):
        self.done = True
        self.pred = None            # drop the closure (it refers back to its owner)
        if not self.found:
            return True
        if len(self.found) == 1 or self.rng is None:
            n = self.found[0][1]
        else:
            d0 = self.found[0][0]
            ws = [1.0 / (1.0 + (d - d0) / 6.0) for d, _ in self.found]
            pick = self.rng.random() * sum(ws)
            n = self.found[-1][1]
            for w_, (d, m) in zip(ws, self.found):
                pick -= w_
                if pick <= 0:
                    n = m
                    break
        path = []
        m = n
        while m is not None:
            path.append(m)
            m = self.parent[m]
        path.reverse()
        self.result = (n, path)
        return True


def path_length(world, nodes):
    tot = 0.0
    prev = None
    for n in nodes:
        p = world.node_xy(n)
        if prev is not None:
            tot += math.hypot(p[0] - prev[0], p[1] - prev[1])
        prev = p
    return tot


def path_points(world, nodes):
    return [world.node_xy(n) for n in nodes]


def smooth_points(world, pts, need=2.9, max_skip=30):
    """Greedy string pulling: drop intermediate points while a straight walk stays clear."""
    if len(pts) <= 2:
        return list(pts)
    out = [pts[0]]
    i = 0
    n = len(pts)
    while i < n - 1:
        j = min(n - 1, i + max_skip)
        while j > i + 1 and not world.walkable(pts[i][0], pts[i][1], pts[j][0], pts[j][1], need):
            j -= 1
        out.append(pts[j])
        i = j
    return out
