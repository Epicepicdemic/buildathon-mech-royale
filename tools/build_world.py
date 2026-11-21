"""Build-time world model for the bot. Requires NumPy; the bot itself does not.

Reads the arena (v2/world.json, v2/terrain.i16) and writes one binary file of
lookup tables that `mech/world.py` loads with the standard library:

1 m layers (one value per terrain vertex, x = minX + i, y = minY + j):
  floor   int16 cm   the surface a walking mech stands on (terrain, or a low deck)
  sight   int16 dm   top of whatever blocks sight and shots (terrain or prism top)
  clear   uint8 dm   horizontal distance from a mech centre to the nearest thing it
                     would collide with at this floor (prism, map edge, cliff), cap 25.5 m
2 m layers (navigation grid, node (u, v) is vertex (2u, 2v)):
  edges   uint8      bit k set = move to neighbour k is walkable (see DIRS)
  expo    uint8      share of the surrounding 200 m from which we are seen (x255)
  fan     uint8 x 32 distance (m, cap 255) to the first mech-hiding wall per direction
  canopy  int16 x 2  lowest crown base / highest crown top over the cell (dm), or 0/0
  roof    int16 dm   roof height if a mech can stand here on a roof, else -32768
A JSON block carries strongpoints, roof patches and the parameters used.

Usage: python3 tools/build_world.py ARENA_DIR OUT_FILE
"""
import json
import math
import struct
import sys
import time
import zlib

import numpy as np

STEP_UP = 1.2
HEIGHT = 9.0
EYE = 8.0
RADIUS = 2.5
PLAN_CLEAR = 3.1          # nav nodes need this much clearance (radius + margin)
EDGE_CLEAR = 2.9          # points between nodes need this much
SLOPE_LIMIT = math.tan(math.radians(46.0))   # rulebook limit is 50 degrees; keep a margin
CLEAR_CAP = 25.5
HIDE_H = 9.0              # a wall this tall above our floor hides a mech
# neighbour k: (du, dv). Bit k of `edges` is the move to that neighbour.
DIRS = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]
NFAN = 32


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def poly_inside(ring, X, Y):
    xs = np.asarray(ring[0::2], float)
    ys = np.asarray(ring[1::2], float)
    inside = np.zeros(X.shape, bool)
    n = len(xs)
    j = n - 1
    for i in range(n):
        if ys[i] != ys[j]:
            cond = (ys[i] > Y) != (ys[j] > Y)
            xint = (xs[j] - xs[i]) * (Y - ys[i]) / (ys[j] - ys[i]) + xs[i]
            inside ^= cond & (X < xint)
        j = i
    return inside


def poly_dist(ring, X, Y):
    """Distance from each point to the polygon boundary."""
    xs = np.asarray(ring[0::2], float)
    ys = np.asarray(ring[1::2], float)
    ax, ay = xs, ys
    bx, by = np.roll(xs, -1), np.roll(ys, -1)
    px = X.reshape(-1, 1)
    py = Y.reshape(-1, 1)
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    L2[L2 == 0] = 1e-12
    t = np.clip(((px - ax) * dx + (py - ay) * dy) / L2, 0, 1)
    qx = ax + t * dx - px
    qy = ay + t * dy - py
    return np.sqrt((qx * qx + qy * qy).min(1)).reshape(X.shape)


def shift(a, dv, du, fill):
    """b[v, u] = a[v + dv, u + du], `fill` where that is off the grid."""
    b = np.full_like(a, fill)
    n0, n1 = a.shape[0], a.shape[1]
    vs = slice(max(0, -dv), n0 - max(0, dv))
    us = slice(max(0, -du), n1 - max(0, du))
    vt = slice(max(0, dv), n0 + min(0, dv))
    ut = slice(max(0, du), n1 + min(0, du))
    b[vs, us] = a[vt, ut]
    return b


def build(arena, out):
    t0 = time.time()
    w = json.load(open(f"{arena}/v2/world.json"))
    T = w["terrain"]
    nx, ny = T["nx"], T["ny"]
    x0, y0 = T["minX"], T["minY"]
    terr = np.fromfile(f"{arena}/v2/{T['file']}", dtype="<i2").reshape(ny, nx).astype(np.float64) * T["scale"]
    B = w["bounds"]
    xs1 = x0 + np.arange(nx) * T["step"]
    ys1 = y0 + np.arange(ny) * T["step"]
    X, Y = np.meshgrid(xs1, ys1)

    prisms = w["prisms"]
    # ---- per prism masks over an expanded bounding box
    MARG = 7
    boxes = []
    for p in prisms:
        rx = p["ring"][0::2]
        ry = p["ring"][1::2]
        i0 = max(0, int(math.floor(min(rx) - x0)) - MARG)
        i1 = min(nx, int(math.ceil(max(rx) - x0)) + MARG + 1)
        j0 = max(0, int(math.floor(min(ry) - y0)) - MARG)
        j1 = min(ny, int(math.ceil(max(ry) - y0)) + MARG + 1)
        if i0 >= i1 or j0 >= j1:
            boxes.append(None)
            continue
        XX = X[j0:j1, i0:i1]
        YY = Y[j0:j1, i0:i1]
        m = poly_inside(p["ring"], XX, YY)
        d = poly_dist(p["ring"], XX, YY)
        for h in p.get("holes") or []:
            if h and len(h) >= 6:
                hm = poly_inside(h, XX, YY)
                hd = poly_dist(h, XX, YY)
                d = np.where(m & hm, hd, np.where(m, np.minimum(d, hd), d))  # inside a hole: distance to hole edge
                m &= ~hm
        d = np.where(m, 0.0, d)
        boxes.append((j0, j1, i0, i1, m, d))
    log(f"prism masks {time.time() - t0:.1f}s")

    # ---- floor: terrain, raised onto low decks and low structures
    floor = terr.copy()
    walk_under = np.zeros((ny, nx), bool)
    sight = terr.copy()
    for p, bx in zip(prisms, boxes):
        if bx is None:
            continue
        j0, j1, i0, i1, m, d = bx
        g = terr[j0:j1, i0:i1]
        under = m & (p["base"] - g >= HEIGHT)
        low = m & ~under & (p["top"] - g < HEIGHT)
        f = floor[j0:j1, i0:i1]
        np.maximum(f, np.where(low, p["top"], -1e9), out=f)
        walk_under[j0:j1, i0:i1] |= under
        s = sight[j0:j1, i0:i1]
        np.maximum(s, np.where(m & ~under, p["top"], -1e9), out=s)

    # ---- steepness of the floor between neighbouring vertices
    steep = np.zeros((ny, nx), bool)
    dx = np.abs(np.diff(floor, axis=1))
    dy = np.abs(np.diff(floor, axis=0))
    sx = dx > SLOPE_LIMIT
    sy = dy > SLOPE_LIMIT
    steep[:, :-1] |= sx
    steep[:, 1:] |= sx
    steep[:-1, :] |= sy
    steep[1:, :] |= sy
    log(f"floor/steep {time.time() - t0:.1f}s steep={steep.mean():.4f}")

    # ---- clearance: distance from a mech centre to what it would hit
    clear = np.full((ny, nx), CLEAR_CAP)
    for p, bx in zip(prisms, boxes):
        if bx is None:
            continue
        j0, j1, i0, i1, m, d = bx
        f = floor[j0:j1, i0:i1]
        blocks = (p["top"] > f + STEP_UP + 1e-6) & (p["base"] < f + HEIGHT - 1e-6)
        c = clear[j0:j1, i0:i1]
        np.minimum(c, np.where(blocks, d, CLEAR_CAP), out=c)
    # map edge is a wall
    edge = np.minimum.reduce([X - B["minX"], B["maxX"] - X, Y - B["minY"], B["maxY"] - Y])
    np.minimum(clear, np.maximum(edge, 0), out=clear)
    # cliffs: distance to a steep vertex (the server uses the floor under the centre, so 1 m of margin)
    R = 4
    sd = np.full((ny, nx), CLEAR_CAP)
    for oy in range(-R, R + 1):
        for ox in range(-R, R + 1):
            r = math.hypot(ox, oy)
            if r > R:
                continue
            sh = shift(steep, oy, ox, False)
            np.minimum(sd, np.where(sh, r, CLEAR_CAP), out=sd)
    # a cliff counts as an obstacle 1.6 m "wider" than it is relative to the body radius:
    # clearance from cliffs = distance + (RADIUS - 1.0), so PLAN_CLEAR keeps the centre >= ~1.6 m away
    np.minimum(clear, np.where(sd < CLEAR_CAP, sd + (RADIUS - 1.0), CLEAR_CAP), out=clear)
    clear = np.maximum(clear, 0)
    log(f"clearance {time.time() - t0:.1f}s")

    # ---- navigation grid (2 m)
    NU = (nx + 1) // 2
    NV = (ny + 1) // 2
    cl2 = clear[0::2, 0::2]
    fl2 = floor[0::2, 0::2]
    node_ok = (cl2 >= PLAN_CLEAR)
    edges = np.zeros((NV, NU), np.uint8)
    for k, (du, dv) in enumerate(DIRS):
        ok = node_ok.copy()
        # neighbour must exist and be free
        tgt = shift(node_ok, dv, du, False)
        ok &= tgt
        # midpoint vertex and the two half steps
        jm = 2 * np.arange(NV)[:, None] + dv
        im = 2 * np.arange(NU)[None, :] + du
        jt = 2 * np.arange(NV)[:, None] + 2 * dv
        it = 2 * np.arange(NU)[None, :] + 2 * du
        valid = (jm >= 0) & (jm < ny) & (im >= 0) & (im < nx) & (jt >= 0) & (jt < ny) & (it >= 0) & (it < nx)
        jmc = np.clip(jm, 0, ny - 1)
        imc = np.clip(im, 0, nx - 1)
        jtc = np.clip(jt, 0, ny - 1)
        itc = np.clip(it, 0, nx - 1)
        jm_b, im_b = np.broadcast_arrays(jmc, imc)
        jt_b, it_b = np.broadcast_arrays(jtc, itc)
        j0_b, i0_b = np.broadcast_arrays(2 * np.arange(NV)[:, None], 2 * np.arange(NU)[None, :])
        mid_clear = clear[jm_b, im_b] >= EDGE_CLEAR
        hstep = math.hypot(du, dv)
        f0 = floor[j0_b, i0_b]
        fm = floor[jm_b, im_b]
        ft = floor[jt_b, it_b]
        rise_ok = (np.abs(fm - f0) <= SLOPE_LIMIT * hstep) & (np.abs(ft - fm) <= SLOPE_LIMIT * hstep)
        midsteep = ~steep[jm_b, im_b]
        ok &= valid & mid_clear & rise_ok & midsteep
        edges |= (ok.astype(np.uint8) << k)
    # keep only symmetric edges
    for k, (du, dv) in enumerate(DIRS):
        kr = (k + 4) % 8
        back = shift((edges >> kr) & 1, dv, du, 0).astype(bool)
        edges &= ~(((~back) & ((edges >> k) & 1).astype(bool)).astype(np.uint8) << k)
    log(f"nav edges {time.time() - t0:.1f}s free={(edges > 0).mean():.3f}")

    # ---- connected components: 1 = the main walkable area, 0 = islands (low roofs, stadium bowl)
    flat = edges.reshape(-1)
    offs = [dv * NU + du for du, dv in DIRS]
    comp = np.full(flat.shape, -1, np.int32)
    sizes = []
    for s0 in np.nonzero(flat)[0]:
        if comp[s0] >= 0:
            continue
        cid = len(sizes)
        comp[s0] = cid
        stack = [int(s0)]
        cnt = 0
        while stack:
            n = stack.pop()
            cnt += 1
            e = int(flat[n])
            for k in range(8):
                if e >> k & 1:
                    m = n + offs[k]
                    if comp[m] < 0:
                        comp[m] = cid
                        stack.append(m)
        sizes.append(cnt)
    main_id = int(np.argmax(sizes))
    MAIN = (comp == main_id).astype(np.uint8).reshape(NV, NU)
    log(f"components {len(sizes)} main={sizes[main_id]} {time.time() - t0:.1f}s")

    # ---- canopy (2 m cells): crown z-range
    tf = w["trees"]["fields"]
    td = np.asarray(w["trees"]["data"], float).reshape(-1, len(tf))
    col = {k: tf.index(k) for k in tf}
    can_lo = np.full((NV, NU), 1e9)
    can_hi = np.full((NV, NU), -1e9)
    gx2 = x0 + np.arange(NU) * 2.0
    gy2 = y0 + np.arange(NV) * 2.0
    for row in td:
        cx = row[col["crownX"]] if "crownX" in col else row[col["x"]]
        cy = row[col["crownY"]] if "crownY" in col else row[col["y"]]
        r = row[col["crownR"]]
        z = row[col["z"]]
        lo = z + row[col["crownBase"]]
        hi = z + row[col["height"]]
        u0 = max(0, int((cx - r - x0) / 2) - 1)
        u1 = min(NU, int((cx + r - x0) / 2) + 2)
        v0 = max(0, int((cy - r - y0) / 2) - 1)
        v1 = min(NV, int((cy + r - y0) / 2) + 2)
        if u0 >= u1 or v0 >= v1:
            continue
        GX, GY = np.meshgrid(gx2[u0:u1], gy2[v0:v1])
        m = (GX - cx) ** 2 + (GY - cy) ** 2 <= r * r
        a = can_lo[v0:v1, u0:u1]
        np.minimum(a, np.where(m, lo, 1e9), out=a)
        b = can_hi[v0:v1, u0:u1]
        np.maximum(b, np.where(m, hi, -1e9), out=b)
    has_can = can_hi > -1e8
    log(f"canopy {time.time() - t0:.1f}s cells={has_can.mean():.3f}")

    # ---- viewshed: exposure and hiding-wall fans on the 2 m grid
    sight2 = sight[0::2, 0::2]
    free2 = edges > 0
    vv, uu = np.nonzero(free2)
    px = x0 + uu * 2.0
    py = y0 + vv * 2.0
    pf = fl2[vv, uu]
    NDIR = NFAN
    MAXD = 200.0
    DS = 2.0
    nsteps = int(MAXD / DS)
    fan = np.full((len(px), NDIR), 255, np.uint8)
    seen_w = np.zeros(len(px))
    tot_w = np.zeros(len(px))
    can_dense = has_can & (can_lo < can_hi)
    for a in range(NDIR):
        th = 2 * math.pi * a / NDIR
        cx_, sy_ = math.cos(th), math.sin(th)
        maxang = np.full(len(px), -1e9)       # max elevation (tan) of blockers seen from our eye
        canopy_run = np.zeros(len(px))
        hidden_found = np.zeros(len(px), bool)
        alive = np.ones(len(px), bool)
        for s in range(1, nsteps + 1):
            d = s * DS
            qx = px + cx_ * d
            qy = py + sy_ * d
            ui = np.rint((qx - x0) / 2.0).astype(int)
            vi = np.rint((qy - y0) / 2.0).astype(int)
            inb = (ui >= 0) & (ui < NU) & (vi >= 0) & (vi < NV)
            alive &= inb
            if not alive.any():
                break
            uc = np.clip(ui, 0, NU - 1)
            vc = np.clip(vi, 0, NV - 1)
            sh = sight2[vc, uc]
            fl = fl2[vc, uc]
            # fan: first wall tall enough to hide us from a mech standing beyond it
            wall = alive & ~hidden_found & (sh - pf >= HIDE_H) & ~free2[vc, uc]
            fan[wall, a] = min(255, int(round(d)))
            hidden_found |= wall
            # exposure: could a mech standing there see our top (pf + 9)?
            notfree = ~free2[vc, uc]
            tgt = alive & ~notfree
            oz = pf + HEIGHT
            vis = tgt & ((fl + EYE - oz) / d >= maxang)
            if d > 45:
                vis &= canopy_run < 2
            seen_w[vis] += d
            tot_w[tgt] += d
            np.maximum(maxang, np.where(alive, (sh - oz) / d, -1e9), out=maxang)
            canopy_run += alive & can_dense[vc, uc] & (can_lo[vc, uc] < pf + HEIGHT) & (can_hi[vc, uc] > pf + 4.5)
    expo = np.where(tot_w > 0, seen_w / np.maximum(tot_w, 1e-9), 0)
    EXPO = np.zeros((NV, NU), np.uint8)
    EXPO[vv, uu] = np.round(expo * 255).astype(np.uint8)
    FAN = np.full((NV, NU, NDIR), 255, np.uint8)
    FAN[vv, uu, :] = fan
    log(f"viewshed {time.time() - t0:.1f}s exposure median={np.median(expo):.3f}")

    # ---- strongpoints: low exposure, hiding wall close on a wide arc
    near = (fan <= 12)
    dbl = np.concatenate([near, near], 1)
    run = np.zeros(len(px), int)
    best = np.zeros(len(px), int)
    for a in range(2 * NDIR):
        run = np.where(dbl[:, a], run + 1, 0)
        best = np.maximum(best, run)
    arc = np.minimum(best, NDIR) * (360.0 / NDIR)
    openn = (fan >= 40)
    exits = (openn & ~np.roll(openn, 1, axis=1)).sum(1)
    score = (1 - expo) + arc / 360.0 + 0.1 * np.minimum(exits, 3)
    cand = (arc >= 120) & (exits >= 1) & (expo <= 0.25)
    order = np.argsort(-score)
    chosen = []
    taken = np.zeros((NV // 8 + 2, NU // 8 + 2), bool)   # 16 m buckets for separation
    for idx in order:
        if not cand[idx]:
            continue
        bu, bv = uu[idx] // 8, vv[idx] // 8
        if taken[max(0, bv - 1):bv + 2, max(0, bu - 1):bu + 2].any():
            continue
        taken[bv, bu] = True
        chosen.append(idx)
    strong = [[round(float(px[i]), 1), round(float(py[i]), 1), round(float(expo[i]), 3), int(arc[i]), int(exits[i])] for i in chosen]
    log(f"strongpoints {len(strong)} {time.time() - t0:.1f}s")

    # ---- roofs: where a mech can stand on top of a building
    roof = np.full((NV, NU), -1e9)
    top2 = sight2
    # a roof cell: covered by a prism (sight > floor + step) whose top is the highest thing here
    on_bld = (top2 - fl2 > STEP_UP) & ~free2
    # clearance on the roof: no higher prism within RADIUS. Approximate with the 2 m top raster.
    ok = on_bld.copy()
    for dv in (-1, 0, 1):
        for du in (-1, 0, 1):
            sh = shift(top2, dv, du, -1e9)
            ok &= sh <= top2 + STEP_UP
    roof[ok] = top2[ok]
    ROOF = np.where(ok, np.round(roof * 10), -32768).astype(np.int16)
    # roof patches: connected flat regions
    lab = -np.ones((NV, NU), np.int32)
    patches = []
    vv_r, uu_r = np.nonzero(ok)
    for v, u in zip(vv_r, uu_r):
        if lab[v, u] >= 0:
            continue
        pid = len(patches)
        stack = [(v, u)]
        lab[v, u] = pid
        cells = []
        while stack:
            a, b = stack.pop()
            cells.append((a, b))
            for dv, du in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                c, d = a + dv, b + du
                if 0 <= c < NV and 0 <= d < NU and ok[c, d] and lab[c, d] < 0 and abs(top2[c, d] - top2[a, b]) <= STEP_UP:
                    lab[c, d] = pid
                    stack.append((c, d))
        cs = np.array(cells)
        h = top2[cs[:, 0], cs[:, 1]]
        g = terr[np.clip(cs[:, 0] * 2, 0, ny - 1), np.clip(cs[:, 1] * 2, 0, nx - 1)]
        patches.append([round(float(x0 + cs[:, 1].mean() * 2), 1), round(float(y0 + cs[:, 0].mean() * 2), 1),
                        len(cells) * 4, round(float(np.median(h)), 1), round(float(np.median(h - g)), 1)])
    log(f"roofs {len(patches)} patches {time.time() - t0:.1f}s")

    # ---- write
    layers = {
        "floor": np.clip(np.round(floor * 100), -32767, 32767).astype("<i2"),
        "sight": np.clip(np.round(sight * 10), -32767, 32767).astype("<i2"),
        "clear": np.clip(np.round(clear * 10), 0, 255).astype(np.uint8),
        "edges": edges.astype(np.uint8),
        "expo": EXPO,
        "fan": FAN.reshape(NV, NU * NDIR),
        "can_lo": np.where(has_can, np.round(can_lo * 10), 0).astype("<i2"),
        "can_hi": np.where(has_can, np.round(can_hi * 10), 0).astype("<i2"),
        "roof": ROOF.astype("<i2"),
        "roof_id": np.where(lab >= 0, lab, 65535).astype("<u2"),
        "main": MAIN,
    }
    meta = {
        "nx": nx, "ny": ny, "x0": x0, "y0": y0, "step": T["step"],
        "nu": NU, "nv": NV, "res": 2.0, "nfan": NDIR,
        "bounds": B, "dirs": DIRS,
        "params": {"plan_clear": PLAN_CLEAR, "edge_clear": EDGE_CLEAR, "slope_limit_deg": 46.0, "hide_h": HIDE_H},
        "strongpoints": strong,
        "roofs": patches,
        "layers": {},
    }
    blobs = []
    off = 0
    for k, a in layers.items():
        raw = zlib.compress(np.ascontiguousarray(a).tobytes(), 6)
        meta["layers"][k] = {"dtype": a.dtype.str, "shape": list(a.shape), "offset": off, "size": len(raw)}
        blobs.append(raw)
        off += len(raw)
    hdr = json.dumps(meta, separators=(",", ":")).encode()
    with open(out, "wb") as f:
        f.write(b"MRW2")
        f.write(struct.pack("<I", len(hdr)))
        f.write(hdr)
        for b in blobs:
            f.write(b)
    log(f"wrote {out} ({(8 + len(hdr) + off) / 1e6:.1f} MB) in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "arena", sys.argv[2] if len(sys.argv) > 2 else "worlddata/world.bin")
