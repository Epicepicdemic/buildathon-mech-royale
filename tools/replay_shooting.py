"""Our shooting against real movement, offline, from recorded matches.

Lead test: at sample times along every recorded mech trajectory, a virtual shooter stands at range
D (30-200 m) in a random direction, observes the target's position and velocity as the server
reports them, and aims with a lead predictor (mech/ballistics.py for ours). The bullet leaves one
tick later from 3 m out along the aim at the gun's speed and is swept through the target's real
recorded path (a 2.5 m + 0.3 m bullet radius cylinder, 9 m tall). We report how often the predicted
point is within a mech's radius of where the target really is when the bullet arrives, and how
often the swept bullet hits with and without the gun's 0.6 degree spread.

The recordings give positions every 0.1 s. The server's reported velocity is approximated by the
central difference of positions (0.2 s span).

Usage: python3 tools/replay_shooting.py RECORDINGS_DIR [--kind team|lobby|all] [--json OUT]
"""
import argparse
import json
import math
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import replays as R                      # noqa: E402
from mech import ballistics as bal       # noqa: E402
from mech import dodge                   # noqa: E402

SPEED = 252.0
RADIUS = 2.5
BULLET_R = 0.3
HEIGHT = 9.0
MUZZLE = 7.0
SPREAD = math.radians(0.6)
RANGES = (30, 50, 78, 100, 125, 150, 200)
FIRE_DELAY = 0.05        # the shot leaves on the first simulated tick after our answer


class Track:
    """A mech's recorded path as (t, x, y, z) with linear interpolation."""

    def __init__(self, m, i):
        rows = m.rows[i]
        self.ts = sorted(rows)
        self.t = [k / m.hz for k in self.ts]
        self.p = [(rows[k][R.X], rows[k][R.Y], rows[k][R.Z]) for k in self.ts]
        self.cubes = [rows[k][R.CUBES] for k in self.ts]
        self.death = m.death_t(i)

    def at(self, t):
        ts = self.t
        if t <= ts[0]:
            return self.p[0]
        if t >= ts[-1]:
            return None
        lo, hi = 0, len(ts) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if ts[mid] <= t:
                lo = mid
            else:
                hi = mid
        if ts[hi] - ts[lo] > 0.25:          # a gap in the recording
            return None
        u = (t - ts[lo]) / (ts[hi] - ts[lo])
        a, b = self.p[lo], self.p[hi]
        return (a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u, a[2] + (b[2] - a[2]) * u)

    def vel(self, t, span=0.1):
        a, b = self.at(t - span), self.at(t + span)
        if a is None or b is None:
            return None
        return ((b[0] - a[0]) / (2 * span), (b[1] - a[1]) / (2 * span), (b[2] - a[2]) / (2 * span))


def sweep_hit(origin, direction, track, t_fire, t_arr=None, radius=RADIUS, window=0.15, dt=0.004):
    """Does a bullet leaving `origin` (already 3 m out) at t_fire along unit `direction` enter the
    target's cylinder on its real path? Swept over `window` s either side of the expected arrival
    (t_arr, relative to t_fire). Returns (hit, closest horizontal miss)."""
    best = 1e9
    rr = radius + BULLET_R
    t_max = 1.2 if t_arr is None else t_arr + window
    t = 0.0 if t_arr is None else max(0.0, t_arr - window)
    while t <= t_max:
        q = track.at(t_fire + t)
        if q is None:
            break
        bx = origin[0] + direction[0] * SPEED * t
        by = origin[1] + direction[1] * SPEED * t
        bz = origin[2] + direction[2] * SPEED * t
        dh = math.hypot(bx - q[0], by - q[1])
        if dh < best:
            best = dh
        if dh <= rr and q[2] - BULLET_R <= bz <= q[2] + HEIGHT + BULLET_R:
            return True, dh
        # stop once the bullet is well past the target
        if t > 0.05 and (bx - origin[0]) * direction[0] + (by - origin[1]) * direction[1] > \
                math.hypot(q[0] - origin[0], q[1] - origin[1]) + 8.0:
            break
        t += dt
    return False, best


def unit3(yaw, pitch):
    return (math.cos(pitch) * math.cos(yaw), math.cos(pitch) * math.sin(yaw), math.sin(pitch))


# ---------------------------------------------------------------------------- predictors
def pred_none(muzzle, c, v, hist):
    return c


def pred_ours(muzzle, c, v, hist):
    """mech/ballistics.lead as the gunner calls it (aim already on: no torso latency)."""
    _, _, _, q = bal.lead(muzzle, c, v, SPEED, latency=FIRE_DELAY)
    return q


def make_damped(k):
    def f(muzzle, c, v, hist):
        _, _, _, q = bal.lead(muzzle, c, (v[0] * k, v[1] * k, v[2] * k), SPEED, latency=FIRE_DELAY)
        return q
    return f


def pred_ground(muzzle, c, v, hist):
    """Ours with vertical velocity ignored while the target is on the ground (bobbing on steps)."""
    vz = v[2] if abs(v[2]) > 3.0 else 0.0
    _, _, _, q = bal.lead(muzzle, c, (v[0], v[1], vz), SPEED, latency=FIRE_DELAY)
    return q


def pred_mean(muzzle, c, v, hist):
    """Velocity averaged over the last 0.6 s (a target that zigzags has a small mean)."""
    if hist is None:
        return pred_ours(muzzle, c, v, hist)
    _, _, _, q = bal.lead(muzzle, c, hist, SPEED, latency=FIRE_DELAY)
    return q


def make_accel(k=0.5, vmax=19.5):
    """Ours plus `k` times the acceleration seen over the last 0.3 s (hist2 = velocity then), with
    the predicted speed capped at boost speed."""
    def f(muzzle, c, v, hist, hist2=None):
        if hist2 is None:
            return pred_ours(muzzle, c, v, hist)
        ax, ay = (v[0] - hist2[0]) / 0.3 * k, (v[1] - hist2[1]) / 0.3 * k
        # flight time from the plain lead, then add the acceleration term
        _, _, tf, q = bal.lead(muzzle, c, v, SPEED, latency=FIRE_DELAY)
        T = tf + FIRE_DELAY
        vx, vy = v[0] + ax * T, v[1] + ay * T
        sp = math.hypot(vx, vy)
        if sp > vmax:
            s_ = vmax / sp
            ax, ay = (vx * s_ - v[0]) / T, (vy * s_ - v[1]) / T
        return (q[0] + 0.5 * ax * T * T, q[1] + 0.5 * ay * T * T, q[2])
    f.wants_hist2 = True
    return f


PREDICTORS = {"no lead": pred_none, "ours + accel x0.5": make_accel(0.5), "ours + accel x1": make_accel(1.0), "ours (v2)": pred_ours, "lead x0.75": make_damped(0.75),
              "lead x0.5": make_damped(0.5), "ours, no vz on ground": pred_ground, "mean vel 0.6 s": pred_mean}


def lead_test(matches, rng, step=0.5, spread_samples=4):
    out = {p: {D: [0, 0, 0, 0] for D in RANGES} for p in PREDICTORS}   # n, within radius, hit, hit w/ spread
    moving = {p: {D: [0, 0, 0, 0] for D in RANGES} for p in PREDICTORS}
    for m in matches:
        for i in m.roster:
            if not m.rows.get(i):
                continue
            tr = Track(m, i)
            t = tr.t[0] + 1.0
            end = min(tr.death, tr.t[-1]) - 1.5
            while t < end:
                p0 = tr.at(t)
                v = tr.vel(t)
                a = tr.at(t - 0.6)
                if p0 is None or v is None or a is None:
                    t += step
                    continue
                hist = ((p0[0] - a[0]) / 0.6, (p0[1] - a[1]) / 0.6, 0.0)
                v_prev = tr.vel(t - 0.3)
                spd = math.hypot(v[0], v[1])
                c = (p0[0], p0[1], p0[2] + HEIGHT / 2)
                for D in RANGES:
                    ang = rng.uniform(-math.pi, math.pi)
                    muzzle = (p0[0] + math.cos(ang) * D, p0[1] + math.sin(ang) * D, p0[2] + MUZZLE)
                    for name, f in PREDICTORS.items():
                        q = f(muzzle, c, v, hist, v_prev) if getattr(f, "wants_hist2", False) else f(muzzle, c, v, hist)
                        yaw, pitch = bal.aim_angles(q[0] - muzzle[0], q[1] - muzzle[1], q[2] - muzzle[2])
                        d = unit3(yaw, pitch)
                        origin = (muzzle[0] + d[0] * 3.0, muzzle[1] + d[1] * 3.0, muzzle[2] + d[2] * 3.0)
                        dist = math.dist(origin, q)
                        t_arr = t + FIRE_DELAY + dist / SPEED
                        actual = tr.at(t_arr)
                        if actual is None:
                            continue
                        within = math.hypot(q[0] - actual[0], q[1] - actual[1]) <= RADIUS and \
                            actual[2] <= q[2] <= actual[2] + HEIGHT
                        ta = dist / SPEED
                        hit, _ = sweep_hit(origin, d, tr, t + FIRE_DELAY, ta)
                        hs = 0
                        for _ in range(spread_samples):
                            ds = unit3(yaw + rng.gauss(0, SPREAD), pitch + rng.gauss(0, SPREAD))
                            o2 = (muzzle[0] + ds[0] * 3.0, muzzle[1] + ds[1] * 3.0, muzzle[2] + ds[2] * 3.0)
                            hs += sweep_hit(o2, ds, tr, t + FIRE_DELAY, ta)[0]
                        for tab in ((out,) + ((moving,) if spd > 4.0 else ())):
                            row = tab[name][D]
                            row[0] += 1
                            row[1] += within
                            row[2] += hit
                            row[3] += hs / spread_samples
                t += step
    return out, moving


def print_lead(tab, title):
    print(f"\n### {title}")
    print("| predictor | " + " | ".join(f"{D} m" for D in RANGES) + " |")
    print("|---|" + "---|" * len(RANGES))
    for name, rows in tab.items():
        cells = []
        for D in RANGES:
            n, w, h, hs = rows[D]
            cells.append(f"{w / n:.0%} / {h / n:.0%} / {hs / n:.0%}" if n else "—")
        print(f"| {name} | " + " | ".join(cells) + " |")
    n = tab[next(iter(tab))][RANGES[0]][0]
    print(f"\n(cells: predicted point within {RADIUS} m when the bullet arrives / swept bullet hits / "
          f"hits with spread; n = {n} samples per range)")


# ---------------------------------------------------------------------------- dodge test
class PathTrack:
    """A simulated target path with the same interface as Track (at)."""

    def __init__(self, t0, pts, dt):
        self.t0, self.pts, self.dt = t0, pts, dt

    def at(self, t):
        k = (t - self.t0) / self.dt
        if k < 0 or k >= len(self.pts) - 1:
            return None
        i = int(k)
        u = k - i
        a, b = self.pts[i], self.pts[i + 1]
        return (a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u, a[2])


def simulate(p0, v0, policy, rng, duration=1.3, dt=0.05, accel=58.5, walk=11.7):
    """Kinematic mech on flat ground from (p0, v0). `policy(t, v) -> commanded velocity`, decided
    every 0.1 s as the bot is."""
    pts = [p0]
    p = list(p0)
    v = [v0[0], v0[1]]
    cmd = policy(0.0, v)
    t = 0.0
    k = 0
    while t < duration:
        if k % 2 == 0 and k > 0:
            cmd = policy(t, v)
        dvx, dvy = cmd[0] - v[0], cmd[1] - v[1]
        dn = math.hypot(dvx, dvy)
        lim = accel * dt
        if dn > lim:
            dvx, dvy = dvx / dn * lim, dvy / dn * lim
        v[0] += dvx
        v[1] += dvy
        p[0] += v[0] * dt
        p[1] += v[1] * dt
        pts.append((p[0], p[1], p0[2]))
        t += dt
        k += 1
    return pts


def jink_policy(rng, leg_s, threat_yaw, base, speed=11.7, phase=True):
    j = dodge.Jinker(rng, leg_s=leg_s)
    j.sign = 1 if rng.random() < 0.5 else -1
    if phase:
        # the shot lands somewhere in the middle of the pattern, not at the start of a leg
        j.until = rng.uniform(0.0, leg_s[1])
    else:
        j.until = 0.0

    def f(t, v):
        u = j.direction(t, base, threat_yaw)
        if u is None:
            return (0.0, 0.0)
        return (u[0] * speed, u[1] * speed)
    return f


def real_shots(m):
    """Gun bullets fired by one mech at another: (t_fire, origin, unit dir, speed, shooter, target,
    range at fire). The target is the mech the bullet passes closest to."""
    hz = m.hz
    tracks = {i: Track(m, i) for i in m.roster if m.rows.get(i)}
    out = []
    for pid, pts in m.bullets.items():
        pts = [p for p in pts if p[4] == 0]
        if len(pts) < 2:
            continue
        (ta, xa, ya, za, _), (tb, xb, yb, zb, _) = pts[0], pts[1]
        dtb = (tb - ta) / hz
        if dtb <= 0:
            continue
        vx, vy, vz = (xb - xa) / dtb, (yb - ya) / dtb, (zb - za) / dtb
        sp = math.sqrt(vx * vx + vy * vy + vz * vz)
        if sp < 200 or sp > 420:
            continue
        d = (vx / sp, vy / sp, vz / sp)
        t0 = ta / hz
        # shooter: a mech whose muzzle lies on the line behind the first position
        shooter = None
        for i, tr in tracks.items():
            q = tr.at(t0)
            if q is None:
                continue
            # the bullet is `along` metres ahead of this mech on its line of flight
            ox, oy = xa - q[0], ya - q[1]
            along = (ox * d[0] + oy * d[1]) / max(1e-6, math.hypot(d[0], d[1]))
            perp = abs(ox * d[1] - oy * d[0]) / max(1e-6, math.hypot(d[0], d[1]))
            if 0 <= along <= 35 and perp < 3.0 and (shooter is None or along < shooter[0]):
                shooter = (along, i, q)
        if shooter is None:
            continue
        back, sid, sq = shooter
        t_fire = t0 - max(0.0, back - 3.0) / sp
        origin = (xa - d[0] * max(0.0, back - 3.0), ya - d[1] * max(0.0, back - 3.0), za - d[2] * max(0.0, back - 3.0))
        best = None
        for i, tr in tracks.items():
            if i == sid:
                continue
            q0 = tr.at(t_fire)
            if q0 is None:
                continue
            rng_f = math.hypot(q0[0] - origin[0], q0[1] - origin[1])
            if rng_f < 12 or rng_f > 235:
                continue
            ta_ = rng_f / sp
            q = tr.at(t_fire + ta_)
            if q is None:
                continue
            ox, oy = q[0] - origin[0], q[1] - origin[1]
            hd = math.hypot(d[0], d[1])
            along = (ox * d[0] + oy * d[1]) / hd
            perp = abs(ox * d[1] - oy * d[0]) / hd
            if along > 0 and perp < 8.0 and (best is None or perp < best[0]):
                best = (perp, i, rng_f)
        if best is None:
            continue
        out.append((t_fire, origin, d, sp, sid, best[1], best[2], tracks[best[1]]))
    return out


DODGE_BANDS = ((12, 45), (45, 78), (78, 150), (150, 235))


def dodge_test(matches, rng, legs):
    """Hit rate of real bullets against: the real target, and the same target from the moment of
    firing moving straight on, stopping, reversing, or jinking with each leg-length setting."""
    models = ["real", "straight on", "stop", "reverse"] + [f"jink {lo:.2f}-{hi:.2f} s" for lo, hi in legs] + \
        [f"boost-jink {lo:.2f}-{hi:.2f} s" for lo, hi in legs]
    tab = {mname: {b: [0, 0.0] for b in DODGE_BANDS} for mname in models}
    for m in matches:
        for (t_fire, origin, d, sp, sid, tid, rng_f, tr) in real_shots(m):
            band = next((b for b in DODGE_BANDS if b[0] <= rng_f < b[1]), None)
            if band is None:
                continue
            p0 = tr.at(t_fire)
            v0 = tr.vel(t_fire)
            if p0 is None or v0 is None:
                continue
            ta_ = rng_f / sp
            threat_yaw = math.atan2(origin[1] - p0[1], origin[0] - p0[0])
            spd = math.hypot(v0[0], v0[1])
            base = (v0[0] / spd, v0[1] / spd) if spd > 1.0 else (0.0, 0.0)

            def run(track):
                global SPEED
                keep = SPEED
                SPEED = sp
                try:
                    return sweep_hit(origin, d, track, t_fire, ta_)[0]
                finally:
                    SPEED = keep

            tab["real"][band][0] += 1
            tab["real"][band][1] += run(tr)
            simple = {
                "straight on": lambda t, v: (v0[0], v0[1]),
                "stop": lambda t, v: (0.0, 0.0),
                "reverse": lambda t, v: (-v0[0], -v0[1]) if spd > 1 else (0.0, 0.0),
            }
            for name, pol in simple.items():
                pts = simulate(p0, v0, pol, rng)
                tab[name][band][0] += 1
                tab[name][band][1] += run(PathTrack(t_fire, pts, 0.05))
            for (lo, hi) in legs:
                for prefix, speed in (("jink", 11.7), ("boost-jink", 19.5)):
                    name = f"{prefix} {lo:.2f}-{hi:.2f} s"
                    hits = 0.0
                    reps = 4
                    for _ in range(reps):
                        pol = jink_policy(rng, (lo, hi), threat_yaw, base, speed=speed)
                        pts = simulate(p0, v0, pol, rng)
                        hits += run(PathTrack(t_fire, pts, 0.05))
                    tab[name][band][0] += 1
                    tab[name][band][1] += hits / reps
    return tab


def print_dodge(tab):
    print("\n### Real bullets against other ways of moving (hit rate)")
    print("| target motion | " + " | ".join(f"{a}-{b} m" for a, b in DODGE_BANDS) + " |")
    print("|---|" + "---|" * len(DODGE_BANDS))
    for name, rows in tab.items():
        cells = [f"{h / n:.0%}" if n else "—" for (n, h) in (rows[b] for b in DODGE_BANDS)]
        print(f"| {name} | " + " | ".join(cells) + " |")
    print("\n(n per band: " + ", ".join(f"{a}-{b} m: {tab['real'][(a, b)][0]}" for a, b in DODGE_BANDS) + ")")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("recordings")
    ap.add_argument("--kind", default="team", choices=("team", "lobby", "all"))
    ap.add_argument("--json", default=None)
    ap.add_argument("--step", type=float, default=0.5)
    ap.add_argument("--skip-lead", action="store_true")
    ap.add_argument("--skip-dodge", action="store_true")
    a = ap.parse_args()
    ms = R.load(R.default_dirs(a.recordings))
    if a.kind == "team":
        ms = [m for m in ms if m.kind in ("reair", "showdown")]
    elif a.kind == "lobby":
        ms = [m for m in ms if m.kind not in ("reair", "showdown")]
    rng = random.Random(11)
    print(f"{len(ms)} matches ({a.kind})")
    res = {}
    if not a.skip_lead:
        allm, mov = lead_test(ms, rng, step=a.step)
        print_lead(allm, "All sampled moments")
        print_lead(mov, "Target moving faster than 4 m/s")
        res.update(lead_all=allm, lead_moving=mov)
    if not a.skip_dodge:
        legs = [dodge.V2_LEG_S, (0.45, 1.05), (0.6, 1.4), (0.35, 0.8), (0.9, 1.8)]
        tab = dodge_test(ms, rng, legs)
        print_dodge(tab)
        res["dodge"] = {k: {f"{b[0]}-{b[1]}": v for b, v in rows.items()} for k, rows in tab.items()}
    if a.json:
        with open(a.json, "w") as f:
            json.dump({k: ({str(kk): vv for kk, vv in v.items()} if isinstance(v, dict) else v) for k, v in res.items()}, f)


if __name__ == "__main__":
    main()
