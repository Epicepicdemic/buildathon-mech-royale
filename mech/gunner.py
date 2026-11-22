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
from . import profile as PF
from . import squad_settings as SS
from .geom import wrap, angdiff, clamp

GUN_LEAD_RAD = 0.12         # gun_during_charge: the gun fires while the laser charges only when its
                            # lead is this close to the laser's line (the torso never leaves it)
AIR_LONG_M = 165.0          # airborne_aim: long bursts at an airborne mech to this range ...
AIR_FALL_VZ = 6.0           # ... and gravity in the lead while it climbs slower than this
LASER_LAT = 0.05            # laser_aim: one tick from the decision to the beam
LASER_HOLD_S = 2.0          # laser_hold: keep a charge on a dropped target this long
ACC_MAX = 80.0              # accel_lead: estimated target acceleration is clipped to this (m/s^2)


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
        self.squad = False      # set by the brain each decision (squad_settings applies in squad mode)
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
        self.laser_tgt, self.laser_tgt_t = None, -1e9
        self.debug = ""
        lat = (cfg.get("match") or {})
        self.dt = float(lat.get("decide_every", 2)) / float(lat.get("tick_hz", 20))

    # ------------------------------------------------------------------ helpers
    def _slewed(self, me, yaw, pitch, sub_dt=0.05):
        dy = wrap(yaw - me.yaw)
        my = me.turn_rate * sub_dt
        sy = yaw if abs(dy) <= my else wrap(me.yaw + math.copysign(my, dy))
        dp = pitch - me.pitch
        mp = me.pitch_rate * sub_dt
        sp = pitch if abs(dp) <= mp else me.pitch + math.copysign(mp, dp)
        return sy, clamp(sp, me.min_pitch, me.max_pitch)

    @staticmethod
    def _airborne(tgt):
        tr = tgt.track
        return PF.on("airborne_aim") and tgt.kind == "mech" and tr is not None and bool(getattr(tr, "airborne", False))

    def _fall_lead(self, me, tgt, tvel, speed, latency, ground):
        """Like bal.lead, with gravity on z (brainstorm/r12/mech-carbon.md): an airborne mech off its
        jets falls at 22 m/s^2 until it lands on the ground under it."""
        g = float((self.cfg.get("mech") or {}).get("gravity", 22.0))
        ox, oy, oz = me.muzzle
        ox += me.vel[0] * latency
        oy += me.vel[1] * latency
        c = tgt.center()
        zmin = ground + tgt.height * 0.5 if ground is not None else -1e9
        t = 0.0
        q = c
        for _ in range(5):
            T = latency + t
            q = (c[0] + tvel[0] * T, c[1] + tvel[1] * T, max(zmin, c[2] + tvel[2] * T - 0.5 * g * T * T))
            d = math.sqrt((q[0] - ox) ** 2 + (q[1] - oy) ** 2 + (q[2] - oz) ** 2)
            t = max(0.0, d - bal.SHOT_OFFSET) / speed
        T = latency + t
        q = (c[0] + tvel[0] * T, c[1] + tvel[1] * T, max(zmin, c[2] + tvel[2] * T - 0.5 * g * T * T))
        yaw, pitch = bal.aim_angles(q[0] - ox, q[1] - oy, q[2] - oz)
        return yaw, pitch, t, q

    def _gun_aim(self, me, tgt, gun):
        speed = gun.stat("speed", 252.0)
        tvel = tgt.vel
        tr = tgt.track
        air = self._airborne(tgt)
        if tr is not None and tgt.kind == "mech" and not air:
            # (in the air it cannot reverse: no weave damping)
            dist = math.hypot(tgt.pos[0] - me.pos[0], tgt.pos[1] - me.pos[1])
            if dist > bal.reversal_range(speed) and tr.dodging() and hasattr(tr, "mean_vel"):
                mv = tr.mean_vel(0.75)
                tvel = (0.48 * tvel[0] + 0.52 * mv[0], 0.48 * tvel[1] + 0.52 * mv[1], tvel[2])
        acc = self._target_acc(tgt, tvel is tgt.vel)
        if acc is not None:
            vmax = max(float((self.cfg.get("mech") or {}).get("walk_speed", 11.7)), math.hypot(tvel[0], tvel[1]))
            g, zmin = 0.0, None
            if air and tvel[2] < AIR_FALL_VZ:
                g = float((self.cfg.get("mech") or {}).get("gravity", 22.0))
                ground = getattr(tr, "ground", None)
                zmin = ground + tgt.height * 0.5 if ground is not None else None
            sol = bal.lead_accel(me.muzzle, tgt.center(), tvel, acc, speed, latency=0.05, ovel=me.vel,
                                 vmax=vmax, g=g, zmin=zmin)
            ta = bal.time_to_align(me.yaw, me.pitch, sol[0], sol[1], me.turn_rate, me.pitch_rate)
            yaw, pitch, tf, aimp = bal.lead_accel(me.muzzle, tgt.center(), tvel, acc, speed, latency=0.05 + min(ta, 0.5),
                                                  ovel=me.vel, vmax=vmax, g=g, zmin=zmin)
            return yaw, clamp(pitch, me.min_pitch, me.max_pitch), tf, aimp
        if air and tvel[2] < AIR_FALL_VZ:
            ground = getattr(tr, "ground", None)
            yaw0, pitch0, _, _ = self._fall_lead(me, tgt, tvel, speed, 0.05, ground)
            ta = bal.time_to_align(me.yaw, me.pitch, yaw0, pitch0, me.turn_rate, me.pitch_rate)
            yaw, pitch, tf, aimp = self._fall_lead(me, tgt, tvel, speed, 0.05 + min(ta, 0.5), ground)
            return yaw, clamp(pitch, me.min_pitch, me.max_pitch), tf, aimp
        yaw0, pitch0, _, _ = bal.lead(me.muzzle, tgt.center(), tvel, speed, latency=0.05, ovel=me.vel)
        # allow for the time the torso needs to get there
        ta = bal.time_to_align(me.yaw, me.pitch, yaw0, pitch0, me.turn_rate, me.pitch_rate)
        yaw, pitch, tf, aimp = bal.lead(me.muzzle, tgt.center(), tvel, speed, latency=0.05 + min(ta, 0.5), ovel=me.vel)
        return yaw, clamp(pitch, me.min_pitch, me.max_pitch), tf, aimp

    def _target_acc(self, tgt, raw_vel=True):
        """accel_lead: the track's measured horizontal acceleration, or None (off, no track, or a
        weave-damped velocity is in use)."""
        tr = tgt.track
        if not PF.on("accel_lead") or tr is None or not raw_vel or not hasattr(tr, "accel"):
            return None
        ax, ay = tr.accel()
        a = math.hypot(ax, ay)
        if a < 2.0:
            return None
        if a > ACC_MAX:
            ax, ay = ax * ACC_MAX / a, ay * ACC_MAX / a
        return (ax, ay)

    def _laser_aim(self, me, tgt):
        """laser_aim: the beam leaves on the first tick after this decision (0.05 s), from where our
        muzzle will be then, at where the target will be then (its acceleration included)."""
        lat = LASER_LAT
        acc = self._target_acc(tgt) or (0.0, 0.0)
        vmax = max(float((self.cfg.get("mech") or {}).get("walk_speed", 11.7)), math.hypot(tgt.vel[0], tgt.vel[1]))
        g = float((self.cfg.get("mech") or {}).get("gravity", 22.0)) if self._airborne(tgt) and tgt.vel[2] < AIR_FALL_VZ else 0.0
        p = bal.predict(tgt.center(), tgt.vel, acc, lat, vmax, g=g)
        m = me.muzzle
        ox, oy = m[0] + me.vel[0] * lat, m[1] + me.vel[1] * lat
        yaw, pitch = bal.aim_angles(p[0] - ox, p[1] - oy, p[2] - m[2])
        return yaw, clamp(pitch, me.min_pitch, me.max_pitch), p

    def _clear_part(self, me, tgt, aim, aimp):
        """clear_aim: (aim, line_ok). Keeps the aim when the muzzle-to-lead-point line is clear;
        else re-aims at the head (0.8 of the height) or the legs (0.2), whichever line is clear;
        else line_ok is False (the shot would end in the wall)."""
        if self._clear_line(me, aimp):
            return aim, True
        m = me.muzzle
        for frac in (0.3, -0.3):
            q = (aimp[0], aimp[1], aimp[2] + tgt.height * frac)
            if self._clear_line(me, q):
                yaw, pitch = bal.aim_angles(q[0] - m[0], q[1] - m[1], q[2] - m[2])
                return [yaw, clamp(pitch, me.min_pitch, me.max_pitch)], True
        return aim, False

    def _direct_aim(self, me, tgt, latency=0.05):
        c = tgt.center()
        p = (c[0] + tgt.vel[0] * latency, c[1] + tgt.vel[1] * latency, c[2] + tgt.vel[2] * latency)
        m = me.muzzle
        yaw, pitch = bal.aim_angles(p[0] - m[0], p[1] - m[1], p[2] - m[2])
        return yaw, clamp(pitch, me.min_pitch, me.max_pitch), p

    def _clear_line(self, me, p):
        return self.w is None or self.w.shot_clear(me.muzzle, p)

    def _lightning_clear(self, me, tgt, others, rng):
        """Lightning hits the nearest thing in its cone with LOS, then chains up to chain_range."""
        d_t = math.hypot(tgt.pos[0] - me.pos[0], tgt.pos[1] - me.pos[1])
        lw = ((self.cfg.get("weapons") or {}).get("lightning") or {})
        cone = math.radians(float(lw.get("cone_deg", 30.0))) / 2
        chain_rng = float(lw.get("chain_range", 14.0))
        for o in others:
            if o is tgt.track or (tgt.track is not None and getattr(o, "id", None) == tgt.id):
                continue
            kind = getattr(o, "kind", None)
            # Hitting a closer enemy mech in our cone is still pure PvP damage + short!
            if kind == "mech":
                continue
            op = o.pos if not hasattr(o, "est_pos") else o.est_pos(me.t)
            d = math.hypot(op[0] - me.pos[0], op[1] - me.pos[1])
            if d >= d_t or d > rng:
                continue
            if angdiff(math.atan2(op[1] - me.pos[1], op[0] - me.pos[0]), me.yaw) > cone:
                continue
            oh = 2.4 if kind == "crate" else 3.5
            if not self._clear_line(me, (op[0], op[1], op[2] + oh * 0.5)):
                continue
            # If the obstacle is right next to the target, lightning chains onto the target anyway.
            if math.hypot(op[0] - tgt.pos[0], op[1] - tgt.pos[1]) <= chain_rng * 0.9:
                continue
            return False
        return True

    # ------------------------------------------------------------------ main
    def laser_damage(self, me, laser):
        """Damage of a release now: 35% at the 0.15 s minimum, rising linearly to full at the
        0.75 s charge, times our cube multiplier."""
        from .doctrine import cube_mult
        full = laser.stat("charge", 0.75)
        lo = laser.stat("min_charge", 0.15)
        if laser.charge < lo:
            return 0.0
        mf = laser.stat("min_frac", 0.35)
        frac = mf + (1.0 - mf) * min(1.0, (laser.charge - lo) / max(full - lo, 1e-6))
        return laser.stat("damage", 315.0) * frac * cube_mult(me.cubes, self.cfg)

    def release_damage(self, me, laser, tgt):
        from .doctrine import cookoff_mult
        mult = cookoff_mult(tgt.track, self.cfg) if tgt.track is not None else 1.0
        return self.laser_damage(me, laser) * mult

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
        no_laser_start = bool(getattr(intent, "no_laser_start", False)) if intent is not None else False
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
            gun_sol = None
            if laser_active:
                if PF.on("laser_aim"):
                    yaw, pitch, aimp = self._laser_aim(me, tgt)
                else:
                    yaw, pitch, aimp = self._direct_aim(me, tgt, latency=self.dt)
                full = laser.stat("charge", 0.75)
                aim = [yaw, pitch]
                if gun is not None and PF.on("gun_during_charge"):
                    # the torso stays on the laser's line (following the gun's lead cost 928be36 36%
                    # of its laser damage, brainstorm/r12/mech-neon.md); the gun fires on its own test
                    # only when its lead is within 0.12 rad of that line
                    gs = self._gun_aim(me, tgt, gun)
                    if angdiff(gs[0], yaw) <= GUN_LEAD_RAD and abs(gs[1] - pitch) <= GUN_LEAD_RAD:
                        gun_sol = gs
                sy, sp = self._slewed(me, yaw, pitch, 0.05)
                on = bal.on_target(sy, sp, yaw, pitch, cdist, tgt.radius, tgt.height / 2, 0.7)
                clear = tgt.visible and self._clear_line(me, aimp)
                if self.laser_hold_since is None and laser.charge >= full - 1e-3:
                    self.laser_hold_since = t
                hold_s = float(((self.cfg.get("weapons") or {}).get("laser") or {}).get("hold_s", 2.0))
                expiring = self.laser_hold_since is not None and t - self.laser_hold_since > hold_s - 0.35
                release = (laser.charge >= full - 1e-3 and on and clear) or (expiring and on)
                # a partial charge is still worth releasing when the target is about to break sight or we are retreating
                if not release and laser.charge >= 0.40 * full and on and clear \
                        and (no_laser_start or (dist > 140 and tgt.track is not None and not tgt.track.los)):
                    release = True
                if not release and on and clear and PF.on("partial_release") and tgt.track is not None \
                        and tgt.track.hp is not None and (SS.get("partial_frac") if self.squad else 0.7) * self.release_damage(me, laser, tgt) >= tgt.track.hp:
                    release = True                  # a partial charge already kills it
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
            gun_line = True
            if PF.on("clear_aim") and gun is not None and not laser_active and self.w is not None:
                # the bullet goes to the lead point, not to where the target is now: aim at a part of
                # it the line reaches (centre, else head, else legs), or hold the gun
                aim, gun_line = self._clear_part(me, tgt, aim, aimp)
            yaw, pitch = aim
            sy, sp = self._slewed(me, yaw, pitch, 0.05)
            on = bal.on_target(sy, sp, yaw, pitch, cdist, tgt.radius, tgt.height / 2, 0.78)
            tc = tgt.center()
            clear = tgt.visible and (self._clear_line(me, tc) or self._clear_line(me, (tc[0], tc[1], tgt.pos[2] + tgt.height * 0.8)))
            info["on"] = on
            info["clear"] = clear
            # --- gun
            if gun is not None and (not laser_active or gun_sol is not None):
                g_ok = gun.reload <= 0 and (gun.ammo is None or gun.ammo > 0) and cdist <= gun.stat("range", 230.0) * 0.99
                if gun_sol is not None:
                    # the gun's own test while the laser charges: the torso within tolerance of the lead
                    gsy, gsp = self._slewed(me, aim[0], aim[1], 0.05)
                    on = bal.on_target(gsy, gsp, gun_sol[0], gun_sol[1], cdist, tgt.radius, tgt.height / 2, 0.78)
                if g_ok and on and clear and gun_line:
                    rr = bal.reversal_range(gun.stat("speed", 252.0))
                    tr = tgt.track
                    # Full bursts inside max(rr, 95 m) or whenever target is not actively dodging;
                    # beyond 160 m without nearby cover, use brisk 5-shot bursts if dodging.
                    long_ok = dist <= max(rr, 95.0) or tgt.kind != "mech" or tr is None \
                        or not tr.dodging() or tr.still() or not tr.sees_us \
                        or (self._airborne(tgt) and dist <= AIR_LONG_M)
                    if dist > 160.0 and not getattr(intent, "cover_near", True) and tr is not None and tr.dodging():
                        long_ok = False
                    eff = mode
                    if mode == "long" and not long_ok:
                        eff = "short"
                    if eff == "long":
                        fire[gun.slot] = True
                    elif eff == "short":
                        if t >= self.burst_rest_until:
                            if self.burst_count >= 5:
                                self.burst_count = 0
                                self.burst_rest_until = t + self.rng.uniform(0.28, 0.48)
                            else:
                                fire[gun.slot] = True
                    elif eff == "single":
                        if t - self.last_harass >= 3.4:
                            fire[gun.slot] = True
                            self.last_harass = t
                    info["burst"] = eff
            # --- laser: start a charge when we will have a shot (unless retreating at full speed or shooting a crate)
            if laser is not None and not laser_active and not no_laser_start and tgt.kind != "crate" \
                    and laser.cooldown <= 0.05 and t >= self.short_until \
                    and mode in ("long", "short", "single") and cdist <= laser.stat("range", 280.0) \
                    and (clear or intent.spot in ("peek",)):
                fire[laser.slot] = True
                info["laser"] = "start"
            # --- lightning
            if light is not None:
                rng = light.stat("range", 45.0)
                cone = math.radians(light.stat("cone_deg", 30.0)) / 2
                sy_l, _ = self._slewed(me, yaw, pitch, 0.05)
                yaw_err = angdiff(sy_l, math.atan2(tgt.pos[1] - me.pos[1], tgt.pos[0] - me.pos[0]))
                if dist <= rng * 0.97 and yaw_err <= cone * 0.85 and light.cooldown <= 0.05 and clear \
                        and me.energy >= light.stat("energy", 3.0) + 1.5 and mode != "single" \
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
        if tgt is not None and laser is not None and laser.charge > 0:
            self.laser_tgt, self.laser_tgt_t = tgt, t
        if aim is None and laser is not None and laser.charge > 0 and not no_laser_start and PF.on("laser_hold"):
            aim = self._laser_hold(me, laser, fire, info)
        if aim is None:
            yaw = face_yaw if face_yaw is not None else (move_yaw if move_yaw is not None else me.yaw)
            aim = [yaw, 0.0]
            # keep a laser charge alive only while it can still be used and we aren't sprinting away
            if laser is not None and laser.charge > 0 and not no_laser_start:
                fire[laser.slot] = laser.charge < laser.stat("charge", 0.75) and intent is not None and intent.fire != "none"
        # --- reload: behind cover, when out of sight with a low mag, or when empty
        if gun is not None and gun.reload <= 0 and gun.ammo is not None:
            mag = gun.stat("mag", 12.0)
            if gun.ammo == 0:
                reload = True
            elif intent is not None and intent.reload_ok and gun.ammo < mag * 0.65 and not fire[gun.slot]:
                reload = True
        if gun is not None and fire[gun.slot]:
            reload = False
        aim[1] = clamp(aim[1], me.min_pitch, me.max_pitch)
        self.debug = info
        return aim, fire, reload, info

    def _laser_hold(self, me, laser, fire, info):
        """laser_hold: the decision dropped the target mid-charge (no target, or no fire intent).
        Letting go would fire the charge along whatever the torso faces; instead keep the beam on
        the last target's estimated position for up to LASER_HOLD_S and release when it is on and
        clear (it came back), else keep holding (the 2 s hold auto-fires on that line at worst)."""
        lt = getattr(self, "laser_tgt", None)
        t = me.t
        if lt is None or lt.track is None or t - self.laser_tgt_t > LASER_HOLD_S or getattr(lt.track, "dead", False):
            return None
        tr = lt.track
        cube_sz = float((self.cfg.get("cubes") or {}).get("size_per_cube", 0.05))
        tg = Target.from_track(tr, t, cube_sz) if hasattr(tr, "est_pos") else lt
        yaw, pitch, aimp = self._laser_aim(me, tg)
        cdist = math.dist(me.muzzle, tg.center())
        sy, sp = self._slewed(me, yaw, pitch, 0.05)
        on = bal.on_target(sy, sp, yaw, pitch, cdist, tg.radius, tg.height / 2, 0.7)
        clear = bool(getattr(tr, "los", False)) and self._clear_line(me, aimp)
        full = laser.stat("charge", 0.75)
        release = on and clear and laser.charge >= 0.4 * full
        fire[laser.slot] = not release
        if release:
            self.laser_hold_since = None
        info["laser"] = "hold-release" if release else "hold"
        return [yaw, pitch]

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
