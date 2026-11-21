"""Route tests on the real map.

For N random cases: draw a zone chain the way the settings allow (each next centre uniform in
the disc that keeps the next circle inside the current one), pick a phase, a start time between
the announcement and the shrink, and a walkable start inside the current circle. Then:

  1. plan with the bot's RoutePlanner (fastest first, cover only with slack);
  2. validate every step of the planned path against the 1 m tables independently of the
     planner (clearance >= radius + 0.3, floor steps within the 50 degree slope limit);
  3. drive tools/kinesim.KMech along it with the bot's Follower, speed policy and stuck
     monitor, and record when it is inside the next circle;
  4. compare with the fastest possible arrival (walking distance from the zone field, boosting).

A failure is a case where the fastest route could arrive by the deadline and ours did not, or
an invalid path step, or the simulated mech never arrives.
"""
import json
import math
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mech.world import World            # noqa: E402
from mech import nav, routing           # noqa: E402
from tools.kinesim import KMech          # noqa: E402

GZIP_STREAM = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "check", "stream.jsonl.gz")
SAFE_MARGIN = 6.0


def load_config():
    import gzip
    with gzip.open(GZIP_STREAM, "rt") as f:
        return json.loads(f.readline())["config"]


def zone_chain(cfg, rng):
    z = cfg["zone"]
    c = tuple(z["start_center"])
    r = z["start_radius"]
    t = 0.0
    out = []
    for ph in z["phases"]:
        nr = ph["radius"]
        d = (r - nr) * math.sqrt(rng.random())
        a = rng.random() * 2 * math.pi
        nc = (c[0] + d * math.cos(a), c[1] + d * math.sin(a))
        out.append(dict(center=c, radius=r, next_center=nc, next_radius=nr, t_announce=t,
                        shrink_start=t + ph["hold_s"], shrink_end=t + ph["hold_s"] + ph["shrink_s"]))
        t += ph["hold_s"] + ph["shrink_s"]
        c, r = nc, nr
    return out


def validate_nodes(w, nodes, radius=2.5):
    """Independent check of each grid step: sample every 0.25 m on the 1 m tables."""
    lim = math.tan(math.radians(50.0))
    bad = []
    for a, b in zip(nodes, nodes[1:]):
        ax, ay = w.node_xy(a)
        bx, by = w.node_xy(b)
        L = math.hypot(bx - ax, by - ay)
        if L > 3.0:
            bad.append((a, b, "jump"))
            continue
        n = max(1, int(L / 0.25))
        prev = None
        for k in range(n + 1):
            x = ax + (bx - ax) * k / n
            y = ay + (by - ay) * k / n
            if w.clearance(x, y) < radius + 0.3:
                bad.append((a, b, "clearance"))
                break
            f = w.floor(x, y)
            if prev is not None:
                hd = math.hypot(x - prev[0], y - prev[1])
                # neighbouring vertices: allow the slope limit over the 1 m vertex spacing
                if abs(f - prev[2]) > lim * max(hd, 1.0) + 1e-6:
                    bad.append((a, b, "slope"))
                    break
            prev = (x, y, f)
    return bad


def pick_goal(w, field, start_xy, ph):
    """The tactical goal: the best strongpoint inside the safe circle, else the nearest safe node."""
    cx, cy = ph["next_center"]
    rs = max(2.0, ph["next_radius"] - SAFE_MARGIN)
    best = None
    for sp in w.strongpoints:
        x, y = sp[0], sp[1]
        if math.hypot(x - cx, y - cy) <= rs - 4:
            n = w.node(x, y)
            if not w.main[n]:
                continue
            score = math.hypot(x - start_xy[0], y - start_xy[1]) + 300 * sp[2]
            if best is None or score < best[0]:
                best = (score, n)
    if best:
        return best[1]
    n = w.nearest_free(start_xy[0], start_xy[1])
    path = field.path(n)
    return path[-1]


def run(n_cases=500, seed=7828, sim=True, verbose=False):
    w = World()
    cfg = load_config()
    rng = random.Random(seed)
    mob = routing.Mobility(cfg["mech"])
    cover = routing.cover_multiplier(w)
    mains = [n for n in range(w.nu * w.nv) if w.main[n]]
    stats = dict(cases=0, invalid_paths=0, infeasible=0, feasible=0, met=0, missed=0, unreached=0,
                 cover_routes=0, boost_routes=0, stuck_events=0, outside_s=0.0, plan_ms_max=0.0,
                 plan_ms_total=0.0, max_decision_ms=0.0)
    failures = []
    t_start = time.time()
    fields = {}
    while stats["cases"] < n_cases:
        chain = zone_chain(cfg, rng)
        k = rng.randrange(len(chain))
        ph = chain[k]
        cx, cy = ph["center"]
        r = ph["radius"]
        # start somewhere walkable inside the current circle
        for _ in range(200):
            n0 = rng.choice(mains)
            sx, sy = w.node_xy(n0)
            if math.hypot(sx - cx, sy - cy) <= r:
                break
        else:
            continue
        # half the cases start any time in the hold, half late in the shrink, to stress deadlines
        if rng.random() < 0.5:
            now = rng.uniform(ph["t_announce"], ph["shrink_start"])
        else:
            now = rng.uniform(ph["shrink_start"], ph["shrink_end"] - 1.0)
        energy = rng.uniform(15.0, 100.0)
        deadline = ph["shrink_end"]
        ncx, ncy = ph["next_center"]
        rs = max(2.0, ph["next_radius"] - SAFE_MARGIN)
        field = nav.ZoneField(w, ncx, ncy, rs)
        field.run(1e18)
        fast_d = field.cost(n0)
        if fast_d == math.inf:
            continue
        stats["cases"] += 1
        feasible = now + mob.time_fast(fast_d * routing.PATH_FACTOR, energy) <= deadline
        goal = pick_goal(w, field, (sx, sy), ph)
        t0 = time.perf_counter()
        planner = routing.RoutePlanner(w, n0, goal, mob, energy, now, deadline, (ncx, ncy, rs), cover_mult=cover,
                                         zone_field=field)
        planner.run(1e18)
        ms = (time.perf_counter() - t0) * 1000
        stats["plan_ms_max"] = max(stats["plan_ms_max"], ms)
        stats["plan_ms_total"] += ms
        route = planner.result
        if route is None:
            stats["unreached"] += 1
            failures.append(dict(case=stats["cases"], why="no route", start=(sx, sy), goal=w.node_xy(goal)))
            continue
        if route.kind == "cover":
            stats["cover_routes"] += 1
        if route.boost:
            stats["boost_routes"] += 1
        if route.kind == "zone":
            stats["zone_routes"] = stats.get("zone_routes", 0) + 1
        bad = validate_nodes(w, route.nodes)
        if bad:
            stats["invalid_paths"] += 1
            failures.append(dict(case=stats["cases"], why="invalid path", detail=[(w.node_xy(a), w.node_xy(b), m) for a, b, m in bad[:3]]))
        arrive = None
        if sim:
            arrive, outside, stuck_ev, dms = simulate(w, route, mob, now, deadline, (ncx, ncy, rs), ph, energy)
            stats["outside_s"] += outside
            stats["stuck_events"] += stuck_ev
            stats["max_decision_ms"] = max(stats["max_decision_ms"], dms)
        else:
            arrive = now + mob.time_walk(route.enter_len * routing.PATH_FACTOR)
        if feasible:
            stats["feasible"] += 1
            if arrive is not None and arrive <= deadline:
                stats["met"] += 1
            elif sim and not fastest_can(w, field, n0, mob, now, deadline, (ncx, ncy, rs), ph, energy):
                stats["feasible"] -= 1
                stats["infeasible"] += 1
                stats["infeasible_by_sim"] = stats.get("infeasible_by_sim", 0) + 1
            else:
                stats["missed"] += 1
                failures.append(dict(case=stats["cases"], why="missed deadline", start=(round(sx), round(sy)),
                                     goal=w.node_xy(goal), now=round(now, 1), deadline=deadline,
                                     arrive=None if arrive is None else round(arrive, 1),
                                     fast_d=round(fast_d), route_len=round(route.length), kind=route.kind,
                                     enter=route.enter_len if route.enter_len == math.inf else round(route.enter_len), boost=route.boost))
        else:
            stats["infeasible"] += 1
        if verbose and stats["cases"] % 50 == 0:
            print(stats["cases"], json.dumps(stats), f"{time.time() - t_start:.0f}s", file=sys.stderr)
    stats["elapsed_s"] = round(time.time() - t_start, 1)
    return stats, failures


def fastest_can(w, field, n0, mob, now, deadline, safe, ph, energy):
    """Does the fastest route (zone field path, boosting throughout) arrive in time in the sim?"""
    nodes = field.path(n0)
    e = routing.enter_length(w, nodes, *safe)
    r = routing.Route(w, nodes, "zone", True, e, e)
    arrive, _, _, _ = simulate(w, r, mob, now, deadline, safe, ph, energy, always_boost=True)
    return arrive is not None and arrive <= deadline


def simulate(w, route, mob, now, deadline, safe, ph, energy=100.0, always_boost=False):
    """Drive the kinematic mech along the route. Returns (arrival time or None, seconds outside the
    moving zone, stuck events, worst follower decision ms)."""
    cx, cy, rs = safe
    x, y = route.pts[0]
    m = KMech(w, x, y)
    m.energy = energy
    fol = routing.Follower(w, route.pts, route.cum)
    stuck = routing.StuckMonitor()
    booster = routing.Booster()
    t = now
    outside = 0.0
    worst = 0.0
    limit = deadline + 60.0
    while t < limit:
        if math.hypot(m.x - cx, m.y - cy) <= rs:
            return t, outside, stuck.events, worst
        t0 = time.perf_counter()
        tx, ty, rem, off = fol.target(m.x, m.y)
        # remaining distance until inside the safe circle
        rem_enter = max(0.0, route.enter_len - (route.length - rem))
        mode = "boost" if always_boost else routing.speed_mode(rem_enter, t, deadline, mob, m.energy, reserve=0.0)
        dx, dy = tx - m.x, ty - m.y
        d = math.hypot(dx, dy)
        move = (dx / d, dy / d) if d > 0.3 else (0.0, 0.0)
        st = stuck.update(t, m.x, m.y, d > 0.3)
        jump = st >= 1 and m.grounded
        boost = booster.want(mode == "boost", m.energy) or (st >= 2 and not m.grounded)
        worst = max(worst, (time.perf_counter() - t0) * 1000)
        m.decide(move, boost, jump)
        t += 0.1
        # zone at time t
        u = 0.0 if t <= ph["shrink_start"] else min(1.0, (t - ph["shrink_start"]) / (ph["shrink_end"] - ph["shrink_start"]))
        zcx = ph["center"][0] + (ph["next_center"][0] - ph["center"][0]) * u
        zcy = ph["center"][1] + (ph["next_center"][1] - ph["center"][1]) * u
        zr = ph["radius"] + (ph["next_radius"] - ph["radius"]) * u
        if math.hypot(m.x - zcx, m.y - zcy) > zr:
            outside += 0.1
    return None, outside, stuck.events, worst


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 500
    st, fails = run(n, verbose=True)
    print(json.dumps(st, indent=1))
    for f in fails[:20]:
        print(json.dumps(f))
