"""Approximate mech movement on our own world tables, for testing path following.

Not the game engine. It follows the rulebook's movement description: acceleration toward the
commanded velocity (ground 58.5, air 12 m/s^2), boost and jets with energy, gravity 22, jumping,
wall sliding against the clearance raster, the 1.2 m step limit. Two 0.05 s substeps per
decision, like the server. Jets climb at max_rise_speed minus one tick of gravity (8.9 m/s),
which is what the recorded stream shows.
"""
import math


class KMech:
    def __init__(self, world, x, y, stats=None):
        s = stats or {}
        self.w = world
        self.r = float(s.get("radius", 2.5))
        self.walk = float(s.get("walk_speed", 11.7))
        self.boost_speed = float(s.get("boost_speed", 19.5))
        self.ga = float(s.get("ground_accel", 58.5))
        self.aa = float(s.get("air_accel", 12.0))
        self.g = float(s.get("gravity", 22.0))
        self.jump_v = float(s.get("jump_speed", 11.0))
        self.jet_a = float(s.get("jet_accel", 34.0))
        self.rise = float(s.get("max_rise_speed", 10.0))
        self.step = float(s.get("step_up", 1.2))
        self.emax = float(s.get("energy", 100.0))
        self.regen = float(s.get("energy_regen", 18.0))
        self.delay = float(s.get("energy_regen_delay", 0.6))
        self.bdrain = float(s.get("boost_drain", 17.0))
        self.jdrain = float(s.get("jet_drain", 15.0))
        self.x, self.y = x, y
        self.z = world.floor(x, y)
        self.vx = self.vy = self.vz = 0.0
        self.energy = self.emax
        self.since_spend = 10.0
        self.grounded = True
        self.t = 0.0
        self.blocked_ticks = 0

    def _top_near(self, x, y):
        best = -1e9
        for dx in (-2.5, 0.0, 2.5):
            for dy in (-2.5, 0.0, 2.5):
                best = max(best, self.w.sight(x + dx, y + dy))
        return best

    def _free(self, x, y, z):
        w = self.w
        if not w.in_bounds(x, y, self.r):
            return False
        f = w.floor(x, y)
        if f > z + self.step + 1e-6:
            return False                     # ledge too high for our feet
        if w.clearance(x, y) >= self.r:
            return True
        # inside wall clearance: only free if we are above whatever is there
        return z >= self._top_near(x, y) - 0.05

    def substep(self, move, boost, jump, dt=0.05):
        mx, my = move
        ml = math.hypot(mx, my)
        if ml > 1:
            mx, my = mx / ml, my / ml
        spending = False
        if self.grounded:
            speed = self.walk
            if boost and ml > 0.05 and self.energy > 0:
                speed = self.boost_speed
                self.energy = max(0.0, self.energy - self.bdrain * dt)
                spending = True
            acc = self.ga
            if jump:
                self.vz = self.jump_v
                self.grounded = False
        else:
            speed = self.walk
            acc = self.aa
            if boost and self.energy > 0:
                self.vz = min(self.rise, self.vz + self.jet_a * dt)
                self.energy = max(0.0, self.energy - self.jdrain * dt)
                spending = True
        tvx, tvy = mx * speed, my * speed
        dvx, dvy = tvx - self.vx, tvy - self.vy
        dl = math.hypot(dvx, dvy)
        if dl > acc * dt:
            dvx, dvy = dvx / dl * acc * dt, dvy / dl * acc * dt
        self.vx += dvx
        self.vy += dvy
        if not self.grounded:
            self.vz -= self.g * dt
        # horizontal move with sliding
        nx, ny = self.x + self.vx * dt, self.y + self.vy * dt
        if self._free(nx, ny, self.z):
            self.x, self.y = nx, ny
        elif self._free(nx, self.y, self.z):
            self.x = nx
            self.vy = 0.0
            self.blocked_ticks += 1
        elif self._free(self.x, ny, self.z):
            self.y = ny
            self.vx = 0.0
            self.blocked_ticks += 1
        else:
            self.vx = self.vy = 0.0
            self.blocked_ticks += 1
        # vertical
        f = self.w.floor(self.x, self.y)
        if self.grounded:
            if f < self.z - 0.6:
                self.grounded = False
                self.vz = 0.0
            else:
                self.z = f
        if not self.grounded:
            self.z += self.vz * dt
            if self.z <= f:
                self.z = f
                self.vz = 0.0
                self.grounded = True
        if spending:
            self.since_spend = 0.0
        else:
            self.since_spend += dt
            if self.since_spend >= self.delay:
                self.energy = min(self.emax, self.energy + self.regen * dt)
        self.t += dt

    def decide(self, move, boost=False, jump=False):
        """Apply one decision (two substeps)."""
        self.substep(move, boost, jump)
        self.substep(move, boost, False)

    @property
    def pos(self):
        return [self.x, self.y, self.z]
