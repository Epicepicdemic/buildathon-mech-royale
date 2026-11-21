"""Where to hold: cells scored against the live threats, picked at random among the good ones.

Nothing here is a fixed list. Each call scans walkable cells inside the safe circle near us, scores
them from the shipped tables (exposure, walls around) and from the current threats (distance, and
whether a hiding wall stands between), and draws one of the best few with the match's seeded RNG,
so a rival reading this code cannot know in advance where we will stand.
"""
import math


def score_cell(world, n, me_xy, circle, threats, small, drones=(), crowded=False):
    x, y = world.node_xy(n)
    cx, cy, r = circle
    expo = world.expo[n] / 255.0
    arc = world.arc[n] / 255.0
    d_us = math.hypot(x - me_xy[0], y - me_xy[1])
    dc = math.hypot(x - cx, y - cy)
    if small:
        ring_pen = 1.5 * dc
    elif crowded and r > 55.0:
        # Mid-game (4+ bots, medium/large circle): hold the quiet 0.42-0.75*r ring behind walls
        # rather than the centre bowl where rotating bots crossfire from all sides.
        frac = dc / max(r, 1.0)
        if frac < 0.42:
            ring_pen = (0.42 - frac) * r * 0.95
        elif frac > 0.76:
            ring_pen = (frac - 0.76) * r * 1.15
        else:
            ring_pen = 0.0
    else:
        ring_pen = 0.35 * dc
    s = d_us * 0.85 + 360.0 * expo + ring_pen - 42.0 * arc
    open_dirs = []
    near_dirs = []
    for (tx, ty, tz) in threats:
        dx, dy = tx - x, ty - y
        dt = math.hypot(dx, dy)
        if dt < 120.0:
            s += (120.0 - dt) * 1.5
        if dt > 1.0 and dt < 230.0:
            ux, uy = dx / dt, dy / dt
            if dt < 175.0:
                near_dirs.append((ux, uy))
            if world.fan_dist(n, math.atan2(dy, dx)) >= dt:
                s += 65.0                      # no hiding wall between it and this cell
                open_dirs.append((ux, uy))
    # Anti-sandwich: heavy penalty if two threats have wide-angle (>100 deg) open sightlines onto n,
    # or if n sits directly between two nearby threats (>115 deg).
    for i in range(len(open_dirs)):
        ux1, uy1 = open_dirs[i]
        for j in range(i + 1, len(open_dirs)):
            if ux1 * open_dirs[j][0] + uy1 * open_dirs[j][1] < -0.17:
                s += 150.0
                break
    for i in range(len(near_dirs)):
        ux1, uy1 = near_dirs[i]
        for j in range(i + 1, len(near_dirs)):
            if ux1 * near_dirs[j][0] + uy1 * near_dirs[j][1] < -0.42:
                s += 65.0
                break
    for (hx, hy) in drones:
        dd = math.hypot(hx - x, hy - y)
        if dd < 55.0:
            s += (55.0 - dd) * 1.6         # NPC drones wake inside 45 m of their patch
    return s


MAX_CELLS = 700
MAX_THREATS = 6


def candidates(world, me_xy, circle, threats, max_from_me=260.0, max_expo=0.35, drones=(), alive=None):
    cx, cy, r = circle
    small = r < 45.0 or (alive is not None and alive <= 2 and r < 80.0)
    crowded = alive is None or alive >= 4
    # bounded work: the nearest few threats, and a stride that keeps the scan to about MAX_CELLS cells
    threats = sorted(threats, key=lambda p: (p[0] - me_xy[0]) ** 2 + (p[1] - me_xy[1]) ** 2)[:MAX_THREATS]
    drones = list(drones)[:MAX_THREATS]
    side = 2.0 * min(r, max_from_me)
    stride = max(1, int(math.ceil(side / world.res / math.sqrt(MAX_CELLS))))
    res = world.res
    reach = min(r, max_from_me + r)
    u0 = max(0, int((cx - reach - world.x0) / res))
    u1 = min(world.nu - 1, int((cx + reach - world.x0) / res) + 1)
    v0 = max(0, int((cy - reach - world.y0) / res))
    v1 = min(world.nv - 1, int((cy + reach - world.y0) / res) + 1)
    out = []
    for v in range(v0, v1 + 1, stride):
        y = world.y0 + v * res
        if abs(y - cy) > r or abs(y - me_xy[1]) > max_from_me:
            continue
        row = v * world.nu
        for u in range(u0, u1 + 1, stride):
            n = row + u
            if not world.main[n] or world.expo[n] > max_expo * 255:
                continue
            x = world.x0 + u * res
            if (x - cx) ** 2 + (y - cy) ** 2 > r * r or abs(x - me_xy[0]) > max_from_me:
                continue
            out.append((score_cell(world, n, me_xy, circle, threats, small, drones, crowded=crowded), n))
    return out


def choose(scored, rng, top=6, temperature=30.0):
    """Draw one of the best `top` (score, node) pairs, better scores more likely."""
    if not scored:
        return None
    scored = sorted(scored)[:top]
    s0 = scored[0][0]
    weights = [math.exp(-(s - s0) / temperature) for s, _ in scored]
    tot = sum(weights)
    pick = rng.random() * tot
    for w_, (s, n) in zip(weights, scored):
        pick -= w_
        if pick <= 0:
            return n
    return scored[-1][1]


def choose_hold(world, me_xy, circle, threats, rng, top=6, drones=(), alive=None):
    """A node to hold inside `circle` (cx, cy, r), or None. `drones`: NPC home points to keep clear of."""
    cands = candidates(world, me_xy, circle, threats, drones=drones, alive=alive)
    if not cands:
        cands = candidates(world, me_xy, circle, threats, max_from_me=2000.0, max_expo=1.0, drones=drones, alive=alive)
    return choose(cands, rng, top)
