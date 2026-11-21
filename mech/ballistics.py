"""Aiming maths: led gun shots, the mortar arc, and whether the torso is on target."""
import math

from .geom import wrap

SHOT_OFFSET = 3.0     # shots appear radius + 0.5 m out along the aim


def aim_angles(dx, dy, dz):
    return math.atan2(dy, dx), math.atan2(dz, math.hypot(dx, dy))


def lead(origin, target, tvel, speed, latency=0.05, ovel=None, offset=SHOT_OFFSET, iters=4):
    """Aim (yaw, pitch) and flight time for a straight shot at `speed` from `origin` (the muzzle)
    at a target now at `target` moving with `tvel`. The shot leaves `latency` seconds from now
    and appears `offset` metres out along the aim. Constant-velocity prediction."""
    ox, oy, oz = origin
    if ovel is not None:
        ox += ovel[0] * latency
        oy += ovel[1] * latency
    px = target[0] + tvel[0] * latency
    py = target[1] + tvel[1] * latency
    pz = target[2] + tvel[2] * latency
    t = 0.0
    for _ in range(iters):
        qx, qy, qz = px + tvel[0] * t, py + tvel[1] * t, pz + tvel[2] * t
        d = math.sqrt((qx - ox) ** 2 + (qy - oy) ** 2 + (qz - oz) ** 2)
        t = max(0.0, d - offset) / speed
    qx, qy, qz = px + tvel[0] * t, py + tvel[1] * t, pz + tvel[2] * t
    yaw, pitch = aim_angles(qx - ox, qy - oy, qz - oz)
    return yaw, pitch, t, (qx, qy, qz)


def aim_tolerance(dist, radius=2.5, half_height=4.5, frac=0.75):
    """Angular half-widths (yaw, pitch) within which a shot at the centre still hits."""
    d = max(dist, 1.0)
    return max(0.004, math.atan2(radius * frac, d)), max(0.004, math.atan2(half_height * frac, d))


def on_target(cur_yaw, cur_pitch, want_yaw, want_pitch, dist, radius=2.5, half_height=4.5, frac=0.75):
    ty, tp = aim_tolerance(dist, radius, half_height, frac)
    return abs(wrap(cur_yaw - want_yaw)) <= ty and abs(cur_pitch - want_pitch) <= tp


def time_to_align(cur_yaw, cur_pitch, want_yaw, want_pitch, turn_rate, pitch_rate):
    return max(abs(wrap(want_yaw - cur_yaw)) / max(turn_rate, 1e-3), abs(want_pitch - cur_pitch) / max(pitch_rate, 1e-3))


def mortar_pitch(dist, dh, v, g, high=False):
    """Launch pitch to land a shell dist metres away horizontally and dh metres higher (from the
    launch point). None if out of range."""
    if dist < 0.5:
        return None
    v2 = v * v
    disc = v2 * v2 - g * (g * dist * dist + 2 * dh * v2)
    if disc < 0:
        return None
    r = math.sqrt(disc)
    num = v2 + r if high else v2 - r
    return math.atan2(num, g * dist)


def mortar_solution(tube, target, tvel, v, g, high=False, min_pitch=-0.7, max_pitch=1.3, offset=SHOT_OFFSET, iters=5):
    """(yaw, pitch, flight_time, aim_point) for a mortar shell from `tube` to hit `target` (moving
    with tvel), or None. Accounts for the shell appearing `offset` m out along the aim."""
    tx, ty, tz = target
    t = 0.0
    pitch = None
    yaw = 0.0
    for _ in range(iters):
        qx, qy, qz = tx + tvel[0] * t, ty + tvel[1] * t, tz + tvel[2] * t
        yaw = math.atan2(qy - tube[1], qx - tube[0])
        p0 = pitch if pitch is not None else 0.6
        sx = tube[0] + offset * math.cos(p0) * math.cos(yaw)
        sy = tube[1] + offset * math.cos(p0) * math.sin(yaw)
        sz = tube[2] + offset * math.sin(p0)
        D = math.hypot(qx - sx, qy - sy)
        pitch = mortar_pitch(D, qz - sz, v, g, high)
        if pitch is None:
            return None
        t = D / max(v * math.cos(pitch), 1e-6)
    if pitch < min_pitch or pitch > max_pitch:
        return None
    return yaw, pitch, t, (tx + tvel[0] * t, ty + tvel[1] * t, tz + tvel[2] * t)


def shell_path(tube, yaw, pitch, v, g, t_end, step=0.1, offset=SHOT_OFFSET):
    """Points along a shell's flight (for checking that the arc clears buildings)."""
    cx, sx = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    x0 = tube[0] + offset * cp * cx
    y0 = tube[1] + offset * cp * sx
    z0 = tube[2] + offset * sp
    pts = []
    t = step
    while t < t_end:
        pts.append((x0 + v * cp * cx * t, y0 + v * cp * sx * t, z0 + v * sp * t - 0.5 * g * t * t))
        t += step
    return pts


def lateral_speed(shooter, target_vel, target_pos):
    """Target speed across the line of fire (m/s)."""
    dx, dy = target_pos[0] - shooter[0], target_pos[1] - shooter[1]
    d = math.hypot(dx, dy)
    if d < 1e-6:
        return 0.0
    return abs(dx * target_vel[1] - dy * target_vel[0]) / d


def reversal_range(speed, accel=58.5, walk=11.7, radius=2.5):
    """Range beyond which a target that reverses direction right after a shot escapes a led
    bullet: the bullet's flight time exceeds the time to build `radius` of displacement error."""
    # displacement error after time t of a reversal from +walk: e(t) = walk*t - (walk*t - a t^2/2)... the
    # predicted point keeps going at +walk while the target decelerates at `accel`: e = accel t^2 / 2
    t_rev = 2 * walk / accel
    t = math.sqrt(2 * radius / accel)
    if t > t_rev:
        t = t_rev + (radius - accel * t_rev ** 2 / 2) / (2 * walk)
    return speed * t + SHOT_OFFSET
