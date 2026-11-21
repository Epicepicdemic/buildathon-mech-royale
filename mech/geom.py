"""Small vector and angle helpers (standard library only)."""
import math

TAU = 2.0 * math.pi


def wrap(a):
    """Angle wrapped to (-pi, pi]."""
    a = (a + math.pi) % TAU - math.pi
    return math.pi if a == -math.pi else a


def angdiff(a, b):
    """Absolute difference between two angles."""
    return abs(wrap(a - b))


def dist2(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def dist3(a, b):
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def bearing(a, b):
    """World yaw from a to b."""
    return math.atan2(b[1] - a[1], b[0] - a[0])


def unit(dx, dy):
    d = math.hypot(dx, dy)
    if d < 1e-9:
        return (0.0, 0.0)
    return (dx / d, dy / d)


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def slew(cur, target, max_step):
    """Move angle `cur` toward `target` by at most `max_step` (radians)."""
    d = wrap(target - cur)
    if abs(d) <= max_step:
        return target
    return wrap(cur + math.copysign(max_step, d))
