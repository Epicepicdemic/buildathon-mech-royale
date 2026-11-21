"""Static world model: the tables built by tools/build_world.py, and queries on them.

Standard library only. Two grids:
  * the 1 m vertex grid of the terrain file (floor, sight-blocking height, clearance)
  * the 2 m navigation grid (node n = v * nu + u at x = x0 + 2u, y = y0 + 2v)
"""
import array
import json
import math
import os
import struct
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(os.path.dirname(HERE), "worlddata", "world.bin")

SQRT2 = math.sqrt(2.0)
CANOPY_BLOCK_M = 4.0       # metres of crown along a sight line that hide a mech (0.35 density, 0.25 threshold)


class World:
    def __init__(self, path=DEFAULT_PATH):
        with open(path, "rb") as f:
            if f.read(4) != b"MRW2":
                raise ValueError("not a world file")
            n = struct.unpack("<I", f.read(4))[0]
            meta = json.loads(f.read(n))
            blob = f.read()
        self.meta = meta
        self.nx, self.ny = meta["nx"], meta["ny"]
        self.x0, self.y0 = meta["x0"], meta["y0"]
        self.nu, self.nv = meta["nu"], meta["nv"]
        self.res = meta["res"]
        self.nfan = meta["nfan"]
        self.bounds = meta["bounds"]
        self.params = meta["params"]
        self.roofs = meta["roofs"]                   # [x, y, area, height, height above ground]

        def layer(name, code):
            m = meta["layers"][name]
            raw = zlib.decompress(blob[m["offset"]:m["offset"] + m["size"]])
            if code is None:
                return raw
            a = array.array(code)
            a.frombytes(raw)
            return a

        self.floor_cm = layer("floor", "h")
        self.sight_dm = layer("sight", "h")
        self.clear_dm = layer("clear", None)
        self.edges = layer("edges", None)
        self.expo = layer("expo", None)
        self.fan = layer("fan", None)
        self.can_lo = layer("can_lo", "h")
        self.can_hi = layer("can_hi", "h")
        self.roof_dm = layer("roof", "h")
        self.roof_id = layer("roof_id", "H")
        self.main = layer("main", None)       # 1 = node in the main walkable area
        self.arc = layer("arc", None)         # share of directions with a hiding wall within 12 m (x255)
        nu = self.nu
        self.dirs = [tuple(d) for d in meta["dirs"]]
        self.offs = [dv * nu + du for du, dv in self.dirs]
        self.dcost = [self.res * (SQRT2 if du and dv else 1.0) for du, dv in self.dirs]
        # neighbour lists per edge mask value (256 patterns) for fast expansion
        self.nbr_table = []
        for mask in range(256):
            self.nbr_table.append(tuple((self.offs[k], self.dcost[k]) for k in range(8) if mask >> k & 1))

    # ------------------------------------------------------------- 1 m queries
    def vidx(self, x, y):
        i = int(round(x - self.x0))
        j = int(round(y - self.y0))
        if i < 0:
            i = 0
        elif i >= self.nx:
            i = self.nx - 1
        if j < 0:
            j = 0
        elif j >= self.ny:
            j = self.ny - 1
        return j * self.nx + i

    def floor(self, x, y):
        return self.floor_cm[self.vidx(x, y)] * 0.01

    def sight(self, x, y):
        return self.sight_dm[self.vidx(x, y)] * 0.1

    def clearance(self, x, y):
        return self.clear_dm[self.vidx(x, y)] * 0.1

    def in_bounds(self, x, y, margin=0.0):
        b = self.bounds
        return b["minX"] + margin <= x <= b["maxX"] - margin and b["minY"] + margin <= y <= b["maxY"] - margin

    # ------------------------------------------------------------- nav grid
    def node(self, x, y):
        u = int(round((x - self.x0) / self.res))
        v = int(round((y - self.y0) / self.res))
        u = 0 if u < 0 else self.nu - 1 if u >= self.nu else u
        v = 0 if v < 0 else self.nv - 1 if v >= self.nv else v
        return v * self.nu + u

    def node_xy(self, n):
        v, u = divmod(n, self.nu)
        return (self.x0 + u * self.res, self.y0 + v * self.res)

    def free(self, n):
        return self.edges[n] != 0

    def neighbors(self, n):
        return self.nbr_table[self.edges[n]]

    def nearest_free(self, x, y, max_r=12):
        """The walkable node nearest to (x, y), preferring nodes reachable in a straight walk."""
        n = self.node(x, y)
        if self.edges[n]:
            return n
        v0, u0 = divmod(n, self.nu)
        best = None
        for r in range(1, max_r + 1):
            for dv in range(-r, r + 1):
                for du in (range(-r, r + 1) if abs(dv) == r else (-r, r)):
                    u, v = u0 + du, v0 + dv
                    if 0 <= u < self.nu and 0 <= v < self.nv:
                        m = v * self.nu + u
                        if self.edges[m]:
                            px, py = self.node_xy(m)
                            d = math.hypot(px - x, py - y)
                            if best is None or d < best[0]:
                                best = (d, m)
            if best is not None and best[0] <= r * self.res:
                return best[1]
        return best[1] if best else None

    def nearest_main(self, x, y, max_r=24):
        """The nearest walkable node on the main ground network (excludes isolated roofs/islands)."""
        n = self.node(x, y)
        if self.edges[n] and self.main[n]:
            return n
        v0, u0 = divmod(n, self.nu)
        best = None
        for r in range(1, max_r + 1):
            for dv in range(-r, r + 1):
                for du in (range(-r, r + 1) if abs(dv) == r else (-r, r)):
                    u, v = u0 + du, v0 + dv
                    if 0 <= u < self.nu and 0 <= v < self.nv:
                        m = v * self.nu + u
                        if self.edges[m] and self.main[m]:
                            px, py = self.node_xy(m)
                            d = math.hypot(px - x, py - y)
                            if best is None or d < best[0]:
                                best = (d, m)
            if best is not None and best[0] <= r * self.res:
                return best[1]
        return best[1] if best else None

    def exposure(self, n):
        return self.expo[n] / 255.0

    def fan_dist(self, n, yaw):
        """Distance (m) from node n to the first mech-hiding wall in direction yaw (255 = none)."""
        k = int(round(yaw / (2 * math.pi) * self.nfan)) % self.nfan
        return self.fan[n * self.nfan + k]

    def wall_arc(self, n, within=12.0):
        """Fraction of directions with a hiding wall within `within` metres."""
        base = n * self.nfan
        return sum(1 for k in range(self.nfan) if self.fan[base + k] <= within) / self.nfan

    def roof_at(self, x, y):
        """Roof height if (x, y) is standable roof, else None."""
        h = self.roof_dm[self.node(x, y)]
        return None if h == -32768 else h * 0.1

    # ------------------------------------------------------------- sight and shots
    def line_clear(self, ax, ay, az, bx, by, bz, step=1.0, canopy=False, skip_end=1.0):
        """True if nothing solid (buildings, terrain) cuts the segment a->b.
        With canopy=True, also fails when the line runs through CANOPY_BLOCK_M of tree crown."""
        dx, dy, dz = bx - ax, by - ay, bz - az
        L = math.hypot(dx, dy)
        if L < 2 * skip_end + 0.01:
            return True
        n = int(L / step)
        inv = 1.0 / n
        nx = self.nx
        x0, y0 = self.x0, self.y0
        sd = self.sight_dm
        k0 = max(1, int(math.ceil(skip_end / L * n)))
        k1 = n - k0
        can = 0.0
        seg = L / n
        nu = self.nu
        nv = self.nv
        res = self.res
        for k in range(k0, k1 + 1):
            t = k * inv
            x = ax + dx * t
            y = ay + dy * t
            z = az + dz * t
            i = int(x - x0 + 0.5)
            j = int(y - y0 + 0.5)
            if i < 0 or j < 0 or i >= nx or j >= self.ny:
                return False
            if sd[j * nx + i] > z * 10.0:
                return False
            if canopy:
                cu = int((x - x0) / res + 0.5)
                cv = int((y - y0) / res + 0.5)
                if cu >= nu:
                    cu = nu - 1
                if cv >= nv:
                    cv = nv - 1
                m = cv * nu + cu
                hi = self.can_hi[m]
                if hi and self.can_lo[m] <= z * 10.0 <= hi:
                    can += seg
                    if can >= CANOPY_BLOCK_M:
                        return False
        return True

    def sees(self, eye, feet, height=9.0, near_range=45.0, step=1.0):
        """Can an eye at `eye` see a mech standing at `feet` (centre or top)? Buildings, terrain,
        and canopy beyond near_range block. Field of view is not considered here."""
        d = math.hypot(feet[0] - eye[0], feet[1] - eye[1])
        canopy = d > near_range
        if self.line_clear(eye[0], eye[1], eye[2], feet[0], feet[1], feet[2] + height, step=step, canopy=canopy):
            return True
        return self.line_clear(eye[0], eye[1], eye[2], feet[0], feet[1], feet[2] + height * 0.5, step=step,
                               canopy=canopy)

    def shot_clear(self, a, b):
        """A straight shot from a to b is not stopped by buildings or terrain (trees stop no shots)."""
        return self.line_clear(a[0], a[1], a[2], b[0], b[1], b[2], step=1.0, canopy=False, skip_end=0.5)

    # ------------------------------------------------------------- walking
    def walkable(self, ax, ay, bx, by, need=2.8, step=0.5, skip_start=0.0):
        """A mech centre can walk straight from a to b: clearance and slope along the segment.
        Clearance is not checked within `skip_start` metres of a (we are already standing there)."""
        dx, dy = bx - ax, by - ay
        L = math.hypot(dx, dy)
        n = max(1, int(L / step))
        need_dm = need * 10.0
        lim = math.tan(math.radians(self.params.get("slope_limit_deg", 46.0)))
        cd = self.clear_dm
        fc = self.floor_cm
        nx = self.nx
        x0, y0 = self.x0, self.y0
        prev_f = None
        hstep = L / n
        maxrise = lim * 1.5 * max(hstep, 1.0) * 100.0
        k_skip = int(skip_start / hstep) if hstep > 0 else 0
        for k in range(n + 1):
            t = k / n
            i = int(ax + dx * t - x0 + 0.5)
            j = int(ay + dy * t - y0 + 0.5)
            if i < 0 or j < 0 or i >= nx or j >= self.ny:
                return False
            idx = j * nx + i
            if cd[idx] < need_dm and k > k_skip:
                return False
            f = fc[idx]
            if prev_f is not None and abs(f - prev_f) > maxrise:
                return False
            prev_f = f
        return True
