"""The air, for moving rather than fighting (DOCTRINE.md, "The air").

Five named plays, each with `find(...)` (is it available here, and where to) and a small state
machine `step(me) -> (move, boost, jump, done)` that drives the legs for a few seconds:

    WallHop        chased along a low building: jump, jet onto the roof
    RoofEdge       on a roof: step to the edge to fire, step back out of sight
    DropAttack     on a roof above an enemy: step off into lightning range, onto its rear
    PopUp          laser charged behind a wall: rise above the roofline, release, drop
    LastCircleRoof the final circle closes on a building: get onto its roof near the centre

Hovering in view is what to avoid: every play spends as little time in the air as it can.
Roofs come from the world tables (`roof` heights per 2 m node, `roof_id` patches).
"""
import math

from .geom import unit, bearing

JET_CLIMB = 8.9          # m/s sustained jet climb seen in the recorded stream (not the documented 10)
JUMP_RISE = 2.75         # a jump alone peaks about this high


def climb_energy(height, cfg_mech=None, margin=12.0):
    """Energy needed to jet `height` metres up (jets drain 15/s, climb 8.9 m/s)."""
    m = cfg_mech or {}
    drain = float(m.get("jet_drain", 15.0))
    h = max(0.0, height - JUMP_RISE * 0.5)
    return h / JET_CLIMB * drain + margin


def roof_cells_near(world, x, y, radius, min_h=None, max_h=None, ground=None):
    """Standable roof nodes within `radius` m: list of (dist, nx, ny, roof_h, roof_id)."""
    out = []
    r = int(radius / world.res) + 1
    n0 = world.node(x, y)
    v0, u0 = divmod(n0, world.nu)
    for dv in range(-r, r + 1):
        v = v0 + dv
        if v < 0 or v >= world.nv:
            continue
        row = v * world.nu
        for du in range(-r, r + 1):
            u = u0 + du
            if u < 0 or u >= world.nu:
                continue
            n = row + u
            h = world.roof_dm[n]
            if h == -32768:
                continue
            h *= 0.1
            if min_h is not None and h < min_h:
                continue
            if max_h is not None and h > max_h:
                continue
            px, py = world.node_xy(n)
            d = math.hypot(px - x, py - y)
            if d <= radius:
                out.append((d, px, py, h, world.roof_id[n]))
    out.sort()
    return out


def roof_interior(world, x, y, h, back=3.0):
    """Is (x, y) at least `back` metres in from the edge of a roof at height h (all neighbours roof)?"""
    for dx, dy in ((back, 0), (-back, 0), (0, back), (0, -back)):
        rh = world.roof_at(x + dx, y + dy)
        if rh is None or abs(rh - h) > 1.2:
            return False
    return True


class AirPlay:
    name = "air"

    def __init__(self):
        self.phase = "idle"
        self.t0 = None
        self.target = None
        self.done = False

    def start(self, t, target):
        self.phase = "go"
        self.t0 = t
        self.target = target
        self.done = False


class WallHop(AirPlay):
    """Escape a pursuer by hopping onto a low roof (<= max_climb above our floor)."""
    name = "wallhop"
    MAX_CLIMB = 22.0
    TIMEOUT = 6.0

    @staticmethod
    def find(world, me, away_from=None, max_climb=MAX_CLIMB, cfg_mech=None, reach=14.0):
        """A roof spot 4-14 m away we can jet onto with the energy we have, preferring away from
        the pursuer. Returns (x, y, roof_h) or None."""
        f = me.pos[2]
        best = None
        for d, px, py, h, rid in roof_cells_near(world, me.pos[0], me.pos[1], reach, f + 4.0, f + max_climb):
            if d < 3.0:
                continue
            if me.energy < climb_energy(h - f, cfg_mech):
                continue
            if not roof_interior(world, px, py, h, 2.5):
                continue
            score = d + 0.3 * (h - f)
            if away_from is not None:
                ax, ay = away_from
                # moving toward the pursuer is worse
                to_p = math.hypot(ax - me.pos[0], ay - me.pos[1])
                to_p2 = math.hypot(ax - px, ay - py)
                score += max(0.0, to_p - to_p2) * 1.5
            if best is None or score < best[0]:
                best = (score, (px, py, h))
        return best[1] if best else None

    def step(self, me):
        tx, ty, h = self.target
        dx, dy = tx - me.pos[0], ty - me.pos[1]
        d = math.hypot(dx, dy)
        mv = unit(dx, dy)
        if self.t0 is not None and me.t - self.t0 > self.TIMEOUT:
            self.done = True
            return [0.0, 0.0], False, False, True
        feet_clear = me.pos[2] >= h + 0.6
        if self.phase == "go":
            # walk to the wall foot, then jump
            if me.grounded and me.pos[2] < h - 1.0:
                self.phase = "climb"
                return [mv[0], mv[1]], False, True, False
            self.phase = "climb"
        if self.phase == "climb":
            if me.grounded and me.pos[2] >= h - 1.3:
                self.done = True
                self.phase = "landed"
                return [0.0, 0.0], False, False, True
            # jets until the feet clear the roof edge; keep pressing toward the roof (walls slide us)
            boost = not feet_clear and not me.grounded and me.energy > 1.0
            jump = me.grounded and me.pos[2] < h - 1.0
            push = mv if (feet_clear or d > 1.5) else (0.0, 0.0)
            return [push[0], push[1]], boost, jump, False
        return [0.0, 0.0], False, False, True


class PopUp(AirPlay):
    """Laser pop-up: charge behind a wall, rise until the target is in sight, release, drop."""
    name = "popup"
    MAX_RISE = 20.0

    @staticmethod
    def find(world, me, target_center, max_rise=MAX_RISE, cfg_mech=None):
        """Height above our feet at which our eye first sees the target, if it is within reach
        and we have the energy; None otherwise (or if we already see it)."""
        x, y, z = me.pos
        eye = me.eye_h
        for rise in range(0, int(max_rise) + 1):
            ez = z + eye + rise
            if world.line_clear(x, y, ez, target_center[0], target_center[1], target_center[2]):
                if rise == 0:
                    return None
                if me.energy < climb_energy(rise + 1.0, cfg_mech, margin=8.0):
                    return None
                return rise + 1.0
        return None

    def start(self, t, target):
        super().start(t, target)
        self.base_z = None
        self.released = False

    def step(self, me, laser_released=False):
        rise = self.target
        if self.base_z is None:
            self.base_z = me.pos[2]
        if laser_released:
            self.released = True
        if self.released or (self.t0 is not None and me.t - self.t0 > 3.5):
            # stop the jets and drop
            self.done = me.grounded
            return [0.0, 0.0], False, False, self.done
        up = me.pos[2] - self.base_z
        if me.grounded and up < 0.5:
            return [0.0, 0.0], False, True, False
        boost = up < rise and me.energy > 1.0
        return [0.0, 0.0], boost, False, False

    def at_height(self, me):
        return self.base_z is not None and me.pos[2] - self.base_z >= self.target - 0.6


class GunPop(PopUp):
    """gun_popup: jet-peek over low cover with the gun. Jet until the eye clears the cover by a
    metre, cut the jets and fall: the gun fires on the way up and down, no hovering. Only for low
    cover (MAX_RISE) and with an energy reserve left after the climb (RESERVE)."""
    name = "gunpop"
    MAX_RISE = 8.0
    RESERVE = 35.0

    @staticmethod
    def find(world, me, target_center, max_rise=MAX_RISE, cfg_mech=None):
        x, y, z = me.pos
        eye = me.eye_h
        for rise in range(0, int(max_rise) + 1):
            ez = z + eye + rise
            if world.line_clear(x, y, ez, target_center[0], target_center[1], target_center[2]):
                if rise == 0:
                    return None
                if me.energy < climb_energy(rise + 1.0, cfg_mech, margin=GunPop.RESERVE):
                    return None
                return rise + 1.0
        return None

    def start(self, t, target):
        super().start(t, target)
        self.cut = False

    def step(self, me, laser_released=False):
        if self.base_z is None:
            self.base_z = me.pos[2]
        up = me.pos[2] - self.base_z
        if self.cut or up >= self.target or (self.t0 is not None and me.t - self.t0 > 2.0):
            self.cut = True
            self.done = me.grounded and (self.t0 is None or me.t - self.t0 > 0.3)
            return [0.0, 0.0], False, False, self.done
        if me.grounded and up < 0.5:
            return [0.0, 0.0], False, True, False
        return [0.0, 0.0], me.energy > 1.0, False, False


class RoofEdge(AirPlay):
    """On a roof: a spot at the edge with a shot at the target, and one a step back that hides us."""
    name = "roofedge"

    @staticmethod
    def find(world, me, target_center, radius=18.0):
        """((peek_x, peek_y), (hide_x, hide_y)) on our roof, or None."""
        h = world.roof_at(me.pos[0], me.pos[1])
        if h is None or not me.grounded:
            return None
        min_pitch = getattr(me, "min_pitch", -0.7)
        muzzle_h = getattr(me, "muzzle_h", 7.0)
        rid = world.roof_id[world.node(me.pos[0], me.pos[1])]
        cells = [c for c in roof_cells_near(world, me.pos[0], me.pos[1], radius, h - 1.2, h + 1.2) if c[4] == rid]
        peek = None
        for d, px, py, rh, _ in cells:
            dh = math.hypot(target_center[0] - px, target_center[1] - py)
            pitch = math.atan2(target_center[2] - (rh + muzzle_h), max(dh, 0.1))
            if pitch < min_pitch + 0.03:
                continue
            if world.line_clear(px, py, rh + me.eye_h, target_center[0], target_center[1], target_center[2]):
                peek = (px, py)
                break
        if peek is None:
            return None
        hide = None
        for d, px, py, rh, _ in sorted(cells, key=lambda c: math.hypot(c[1] - peek[0], c[2] - peek[1])):
            if math.hypot(px - peek[0], py - peek[1]) < 3.0:
                continue
            if not world.sees((target_center[0], target_center[1], target_center[2] + 3.5), (px, py, rh), me.height):
                hide = (px, py)
                break
        if hide is None:
            return None
        return peek, hide


class DropAttack(AirPlay):
    """From a roof, step off toward an enemy below so we land inside lightning range on its rear."""
    name = "drop"

    @staticmethod
    def find(world, me, enemy_pos, enemy_yaw, light_range=45.0):
        """Landing point (x, y) or None. Needs us on a roof 6+ m above the enemy, within ~40 m."""
        h = world.roof_at(me.pos[0], me.pos[1])
        if h is None or me.pos[2] - enemy_pos[2] < 6.0:
            return None
        d = math.hypot(enemy_pos[0] - me.pos[0], enemy_pos[1] - me.pos[1])
        if d > light_range - 5.0 or d < 8.0:
            return None
        # behind it: opposite its facing, 15 m out
        lx = enemy_pos[0] - math.cos(enemy_yaw) * 15.0
        ly = enemy_pos[1] - math.sin(enemy_yaw) * 15.0
        n = world.nearest_free(lx, ly, 6)
        if n is None or not world.main[n]:
            n = world.nearest_free(enemy_pos[0], enemy_pos[1], 10)
            if n is None:
                return None
        return world.node_xy(n)

    def step(self, me):
        tx, ty = self.target
        mv = unit(tx - me.pos[0], ty - me.pos[1])
        if me.grounded and self.t0 is not None and me.t - self.t0 > 0.6 and me.pos[2] < self.start_z - 3.0:
            self.done = True
            return [mv[0], mv[1]], False, False, True
        if self.t0 is not None and me.t - self.t0 > 5.0:
            self.done = True
        return [mv[0], mv[1]], False, False, self.done

    def start(self, t, target, z=0.0):
        super().start(t, target)
        self.start_z = z


class LastCircleRoof(AirPlay):
    """The last circle closes on a building: be on its roof, close to the centre."""
    name = "lastroof"

    @staticmethod
    def find(world, me, center, radius, cfg_mech=None):
        """Roof spot inside the circle nearest its centre, if the centre lies on a roof we can reach."""
        if radius > 30.0:
            return None
        h = world.roof_at(center[0], center[1])
        if h is None:
            return None
        f = me.pos[2] if world.roof_at(me.pos[0], me.pos[1]) is None else world.floor(me.pos[0], me.pos[1])
        if h - f > 55.0:
            return None
        cells = roof_cells_near(world, center[0], center[1], max(4.0, radius), h - 1.2, h + 1.2)
        for d, px, py, rh, _ in cells:
            if roof_interior(world, px, py, rh, 2.5):
                return (px, py, rh)
        return None
