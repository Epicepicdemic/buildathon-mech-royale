"""The shrinking zone: where the circle is at any time, and how much time we have.

`obs.zone` gives the current circle, the next circle and the shrink window. Between
shrink_start and shrink_end the centre and radius move linearly. A point inside the next
circle stays inside the moving circle for the whole shrink (both circles are convex and
nested), so "inside the next circle by shrink_end" is the arrival rule we plan against.
"""
import math


class Zone:
    def __init__(self, cfg):
        z = cfg.get("zone", {})
        self.phases = z.get("phases", [])
        self.center = tuple(z.get("start_center", (0.0, 0.0)))
        self.radius = float(z.get("start_radius", 560.0))
        self.next_center = self.center
        self.next_radius = self.radius
        self.shrink_start = 0.0
        self.shrink_end = 0.0
        self.dps = 0.0
        self.t = 0.0
        self.version = 0      # bumps whenever the next circle changes

    def update(self, zone, t):
        nc = (float(zone["next_center"][0]), float(zone["next_center"][1]))
        nr = float(zone["next_radius"])
        if nc != self.next_center or nr != self.next_radius:
            self.version += 1
        self.center = (float(zone["center"][0]), float(zone["center"][1]))
        self.radius = float(zone["radius"])
        self.next_center, self.next_radius = nc, nr
        self.shrink_start = float(zone["shrink_start"])
        self.shrink_end = float(zone["shrink_end"])
        self.dps = float(zone.get("dps", 0.0))
        self.t = t

    # ------------------------------------------------------------------ geometry
    def circle_at(self, t):
        """(cx, cy, r) of the zone at match time t (valid until shrink_end)."""
        if t <= self.shrink_start or self.shrink_end <= self.shrink_start:
            u = 0.0 if t <= self.shrink_start else 1.0
        elif t >= self.shrink_end:
            u = 1.0
        else:
            u = (t - self.shrink_start) / (self.shrink_end - self.shrink_start)
        cx = self.center[0] + (self.next_center[0] - self.center[0]) * u
        cy = self.center[1] + (self.next_center[1] - self.center[1]) * u
        r = self.radius + (self.next_radius - self.radius) * u
        return cx, cy, r

    def margin_at(self, x, y, t):
        """Distance inside the zone at time t (negative = outside)."""
        cx, cy, r = self.circle_at(t)
        return r - math.hypot(x - cx, y - cy)

    def margin_next(self, x, y):
        return self.next_radius - math.hypot(x - self.next_center[0], y - self.next_center[1])

    def inside_next(self, x, y, margin=0.0):
        return self.margin_next(x, y) >= margin

    def boundary_speed(self):
        """Fastest the edge can move inward during the current shrink (m/s)."""
        dur = self.shrink_end - self.shrink_start
        if dur <= 0:
            return 0.0
        dc = math.hypot(self.next_center[0] - self.center[0], self.next_center[1] - self.center[1])
        return (self.radius - self.next_radius + dc) / dur

    def time_until_outside(self, x, y, t):
        """Seconds from t until the moving edge passes (x, y); inf if it never does this phase."""
        if self.margin_at(x, y, t) < 0:
            return 0.0
        if self.margin_next(x, y) >= 0:
            return math.inf
        lo, hi = max(t, self.shrink_start), self.shrink_end
        if hi <= lo:
            return math.inf if self.margin_at(x, y, hi) >= 0 else 0.0
        for _ in range(30):
            mid = 0.5 * (lo + hi)
            if self.margin_at(x, y, mid) >= 0:
                lo = mid
            else:
                hi = mid
        return lo - t

    def safe_radius(self, margin):
        """Radius of the region we aim for inside the next circle."""
        r = self.next_radius
        return max(min(r * 0.5, 3.0), r - margin)

    def final_phase(self):
        return self.next_radius <= 0.5

    def phase_index(self):
        for k, p in enumerate(self.phases):
            if abs(p.get("radius", -1) - self.next_radius) < 0.5:
                return k
        return len(self.phases)

    def deadline(self):
        """Match time by which we should be inside the next circle."""
        return self.shrink_end
