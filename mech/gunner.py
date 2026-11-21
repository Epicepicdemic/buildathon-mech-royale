"""Weapon control: where the torso points and which triggers are held.

* Gun: led shots from the muzzle, fired only when the torso's actual aim is on the lead point.
  Long bursts (whole magazine) inside the reversal range or at a still/unaware/rear target,
  short bursts (3-4 rounds) beyond it; single shots when harassing.
* Laser: hold to charge, track the target, release when fully charged and the aim is on.
* Lightning: fires when the target is the nearest thing in the cone and in range.
* Mortar: ballistic arc (low if the line is clear, high to lob over a building).
"""
import math
import random

from . import ballistics as bal
from .geom import wrap, angdiff, clamp


class Target:
    """Anything we shoot at: pos (feet), vel, radius, height, and whether we can see it."""

    __slots__ = ("id", "kind", "pos", "vel", "radius", "height", "visible", "track")

    def __init__(self, tid, kind, pos, vel=(0.0, 0.0, 0.0), radius=2.5, height=9.0, visible=True, track=None):
        self.id, self.kind, self.pos, self.vel = tid, kind, pos, vel
        self.radius, self.height, self.visible, self.track = radius, height, visible, track

    @staticmethod
    def from_track(tr, t, size_per_cube=0.05):
        s = 1.0 + size_per_cube * (tr.cubes or 0)
        if tr.kind == "npc":
            return Target(tr.id, "npc", tr.est_pos(t), tr.vel, 1.8, 3.5, tr.los, tr)
        return Target(tr.id, "mech", tr.est_pos(t), tr.vel, 2.5 * s, 9.0 * s, tr.los, tr)

    def center(self):
        return (self.pos[0], self.pos[1], self.pos[2] + self.height * 0.5)


class Gunner:
    def __init__(self, cfg, world, seed=0):
        self.cfg = cfg
        self.w = world
        self.rng = random.Random(seed)
        self.burst_count = 0
        self.burst_rest_until = -1e9
        self.last_gun_fire = -1e9
        self.last_harass = -1e9
        self.ammo_prev = None
        self.shots = 0
        self.short_until = -1e9          # lightning short effect: no laser charging
        self.laser_hold_since = None
        self.debug = ""
        lat = (cfg.get("match") or {})
        self.dt = float(lat.get("decide_every", 2)) / float(lat.get("tick_hz", 20))

    # ------------------------------------------------------------------ helpers
    def _gun_aim(self, me, tgt, gun):
        speed = gun.stat("speed", 252.0)
        yaw0, pitch0, _, _ = bal.lead(me.muzzle, tgt.center(), tgt.vel, speed, latency=0.05, ovel=me.vel)
        # allow for the time the torso needs to get there
        ta = bal.time_to_align(me.yaw, me.pitch, yaw0, pitch0, me.turn_rate, me.pitch_rate)
        yaw, pitch, tf, aimp = bal.lead(me.muzzle, tgt.center(), tgt.vel, speed, latency=0.05 + min(ta, 0.5), ovel=me.vel)
        return yaw, clamp(pitch, me.min_pitch, me.max_pitch), tf, aimp

    def _direct_aim(self, me, tgt, latency=0.05):
        c = tgt.center()
        p = (c[0] + tgt.vel[0] * latency, c[1] + tgt.vel[1] * latency, c[2] + tgt.vel[2] * latency)
        m = me.muzzle
        yaw, pitch = bal.aim_angles(p[0] - m[0], p[1] - m[1], p[2] - m[2])
        return yaw, clamp(pitch, me.min_pitch, me.max_pitch), p

    def _clear_line(self, me, p):
        return self.w is None or self.w.shot_clear(me.muzzle, p)

    def _lightning_clear(self, me, tgt, others, rng):
        """Lightning hits the nearest thing in its cone: make sure that is our target."""
        d_t = math.hypot(tgt.pos[0] - me.pos[0], tgt.pos[1] - me.pos[1])
        cone = math.radians(float(((self.cfg.get("weapons") or {}).get("lightning") or {}).get("cone_deg", 30.0))) / 2
        for o in others:
            if o is tgt.track or (tgt.track is not None and getattr(o, "id", None) == tgt.id):
                continue
            op = o.pos if not hasattr(o, "est_pos") else o.est_pos(me.t)
            d = math.hypot(op[0] - me.pos[0], op[1] - me.pos[1])
            if d < d_t and d <= rng and angdiff(math.atan2(op[1] - me.pos[1], op[0] - me.pos[0]), me.yaw) <= cone:
                return False
        return True

    # ------------------------------------------------------------------ main
    def control(self, me, intent, tgt, obstacles=(), face_yaw=None, move_yaw=None, events=()):
        """Returns (aim [yaw, pitch], fire [b0, b1], reload bool, info dict)."""
        t = me.t
        for e in events:
            if e.get("type") == "effect" and e.get("effect") == "short":
                lw = (self.cfg.get("weapons") or {}).get("lightning") or {}
                self.short_until = t + float(lw.get("short_s", 2.0))
        fire = [False, False]
        reload = False
        info = {}
        gun = me.by_kind.get("gun")
        laser = me.by_kind.get("laser")
        light = me.by_kind.get("lightning")
        mortar = me.by_kind.get("mortar")
        mode = intent.fire if intent is not None else "none"
        aim = None
        # gun magazine tracking for bursts
        if gun is not None:
            if self.ammo_prev is not None and gun.ammo is not None and gun.ammo < self.ammo_prev:
                self.shots += self.ammo_prev - gun.ammo
                self.burst_count += self.ammo_prev - gun.ammo
                self.last_gun_fire = t
            self.ammo_prev = gun.ammo
        if tgt is not None and mode != "none":
            dist = math.hypot(tgt.pos[0] - me.pos[0], tgt.pos[1] - me.pos[1])
            cdist = math.dist(me.muzzle, tgt.center())
            # --- laser takes the aim while charging (hitscan: aim straight at it)
            laser_active = laser is not None and laser.charge > 0
            if laser_active:
                yaw, pitch, aimp = self._direct_aim(me, tgt, latency=self.dt)
                aim = [yaw, pitch]
                full = laser.stat("charge", 0.75)
                on = bal.on_target(me.yaw, me.pitch, yaw, pitch, cdist, tgt.radius, tgt.height / 2, 0.7)
                clear = tgt.visible and self._clear_line(me, aimp)
                if self.laser_hold_since is None and laser.charge >= full - 1e-3:
                    self.laser_hold_since = t
                hold_s = float(((self.cfg.get("weapons") or {}).get("laser") or {}).get("hold_s", 2.0))
                expiring = self.laser_hold_since is not None and t - self.laser_hold_since > hold_s - 0.35
                release = (laser.charge >= full - 1e-3 and on and clear) or (expiring and on)
                # a partial charge is still worth releasing when the target is about to break sight
                if not release and laser.charge >= 0.45 * full and on and clear and dist > 150 and tgt.track is not None \
                        and not tgt.track.los:
                    release = True
                fire[laser.slot] = not release
                if release:
                    self.laser_hold_since = None
                info["laser"] = "release" if release else "charge %.2f" % laser.charge
            elif gun is not None:
                yaw, pitch, tf, aimp = self._gun_aim(me, tgt, gun)
                aim = [yaw, pitch]
            else:
                yaw, pitch, aimp = self._direct_aim(me, tgt)
                aim = [yaw, pitch]
            yaw, pitch = aim
            on = bal.on_target(me.yaw, me.pitch, yaw, pitch, cdist, tgt.radius, tgt.height / 2, 0.75)
            clear = tgt.visible and self._clear_line(me, (tgt.center()[0], tgt.center()[1], tgt.center()[2]))
            info["on"] = on
            info["clear"] = clear
            # --- gun
            if gun is not None and not laser_active:
                g_ok = gun.reload <= 0 and (gun.ammo is None or gun.ammo > 0) and cdist <= gun.stat("range", 230.0) * 0.98
                if g_ok and on and clear:
                    rr = bal.reversal_range(gun.stat("speed", 252.0))
                    tr = tgt.track
                    # real bots mostly do not dodge: full bursts at range unless this one is changing direction
                    long_ok = dist <= rr or tgt.kind != "mech" or tr is None or not tr.dodging() or tr.still() \
                        or not tr.sees_us
                    eff = mode
                    if mode == "long" and not long_ok:
                        eff = "short"
                    if eff == "long":
                        fire[gun.slot] = True
                    elif eff == "short":
                        if t >= self.burst_rest_until:
                            if self.burst_count >= 4:
                                self.burst_count = 0
                                self.burst_rest_until = t + self.rng.uniform(0.5, 0.9)
                            else:
                                fire[gun.slot] = True
                    elif eff == "single":
                        if t - self.last_harass >= 3.4:
                            fire[gun.slot] = True
                            self.last_harass = t
                    info["burst"] = eff
                if mode != "short" or t < self.burst_rest_until - 0.05:
                    pass
            # --- laser: start a charge when we will have a shot
            if laser is not None and not laser_active and laser.cooldown <= 0.05 and t >= self.short_until \
                    and mode in ("long", "short", "single") and cdist <= laser.stat("range", 280.0) \
                    and (clear or intent.spot in ("peek",)):
                fire[laser.slot] = True
                info["laser"] = "start"
            # --- lightning
            if light is not None:
                rng = light.stat("range", 45.0)
                cone = math.radians(light.stat("cone_deg", 30.0)) / 2
                yaw_err = angdiff(me.yaw, math.atan2(tgt.pos[1] - me.pos[1], tgt.pos[0] - me.pos[0]))
                if dist <= rng * 0.96 and yaw_err <= cone * 0.8 and light.cooldown <= 0.05 and clear \
                        and me.energy >= light.stat("energy", 3.0) + 2 and mode != "single" \
                        and self._lightning_clear(me, tgt, obstacles, rng):
                    fire[light.slot] = True
            # --- mortar
            if mortar is not None and mortar.cooldown <= 0.05 and 14.0 <= dist <= 235.0 and mode in ("long", "short"):
                sol = self.mortar_aim(me, mortar, tgt)
                if sol is not None and not laser_active:
                    myaw, mpitch, _ = sol
                    if gun is None or not fire[gun.slot]:
                        aim = [myaw, mpitch]
                        if abs(wrap(me.yaw - myaw)) < 0.02 and abs(me.pitch - mpitch) < 0.02:
                            fire[mortar.slot] = True
        if aim is None:
            yaw = face_yaw if face_yaw is not None else (move_yaw if move_yaw is not None else me.yaw)
            aim = [yaw, 0.0]
            # keep a laser charge alive only while it can still be used
            if laser is not None and laser.charge > 0:
                fire[laser.slot] = laser.charge < laser.stat("charge", 0.75) and intent is not None and intent.fire != "none"
        # --- reload: behind cover, or when empty
        if gun is not None and gun.reload <= 0 and gun.ammo is not None:
            mag = gun.stat("mag", 12.0)
            if gun.ammo == 0:
                reload = True
            elif intent is not None and intent.reload_ok and gun.ammo < mag * 0.6 and not fire[gun.slot]:
                reload = True
        if gun is not None and fire[gun.slot]:
            reload = False
        aim[1] = clamp(aim[1], me.min_pitch, me.max_pitch)
        self.debug = info
        return aim, fire, reload, info

    def mortar_aim(self, me, mortar, tgt):
        v = mortar.stat("speed", 72.0)
        g = mortar.stat("gravity", 22.0)
        tube = (me.pos[0], me.pos[1], me.pos[2] + me.height + 0.5)
        aimpt = (tgt.pos[0], tgt.pos[1], tgt.pos[2] + 1.0)
        for high in (False, True):
            sol = bal.mortar_solution(tube, aimpt, tgt.vel, v, g, high, me.min_pitch, me.max_pitch)
            if sol is None:
                continue
            yaw, pitch, tf, landing = sol
            if self.w is None or self.arc_clear(tube, yaw, pitch, v, g, tf):
                return yaw, pitch, tf
        return None

    def arc_clear(self, tube, yaw, pitch, v, g, tf):
        pts = bal.shell_path(tube, yaw, pitch, v, g, tf - 0.15, step=0.1)
        for x, y, z in pts:
            if self.w.sight(x, y) > z:
                return False
        return True
