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
        clock = time.perf_counter
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
            if count & 255 == 0 and clock() > deadline:
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

    def __init__(self, world, start, goal, mult=None, max_pops=200000):
        self.w = world
        self.start, self.goal = start, goal
        self.mult = mult
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
        clock = time.perf_counter
        count = 0
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
                if clock() > deadline:
                    self.pops += count
                    return False
        self.result = []
        self.pops += count
        return True


class NearestSearch:
    """Resumable Dijkstra (walking distance) from `start` that stops at the first node for which
    `pred(n)` is true, within `max_cost` metres. `result` is (node, path) or None when not found."""

    def __init__(self, world, start, pred, max_cost=80.0, mult=None):
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
        clock = time.perf_counter
        pop, push = heapq.heappop, heapq.heappush
        while heap:
            d, n = pop(heap)
            if d > g.get(n, INF):
                continue
            self.checked += 1
            if pred(n):
                path = []
                m = n
                while m is not None:
                    path.append(m)
                    m = parent[m]
                path.reverse()
                self.result = (n, path)
                self.done = True
                return True
            if d <= self.max_cost:
                for o, c in table[edges[n]]:
                    m = n + o
                    nd = d + (c * mult[m] if mult is not None else c)
                    if nd < g.get(m, INF):
                        g[m] = nd
                        parent[m] = n
                        push(heap, (nd, m))
            if (self.checked & 7) == 0 and clock() > deadline:
                return False
        self.done = True
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
