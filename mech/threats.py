"""Threat memory: every mech and drone we have seen, heard or been hit by.

* Seen mechs report their true position while in view and for `vision.memory_s` (5 s) after.
  After they drop out of the observation we extrapolate briefly and grow an uncertainty radius.
* Sounds give a bearing with Gaussian error (sigma = bearing_noise_deg) and a distance band.
  A sound is attached to a known track when it fits; otherwise it becomes a sector contact.
* Damage events name the attacker and give a bearing even when we cannot see it.
"""
import math

from .geom import wrap, angdiff

MECH_WEAPON_SOUNDS = ("gun", "mortar", "laser", "laser_charge", "lightning", "reload", "step", "boost", "jet")


class Track:
    __slots__ = ("id", "kind", "pos", "vel", "yaw", "pitch", "hp", "max_hp", "cubes", "weapons", "charging",
                 "t_seen", "t_first", "in_obs", "los", "sees_us", "dead", "phantom", "hist", "dmg_to_us",
                 "t_hit_us", "t_we_hit", "dmg_from_us", "t_hp_drop_other", "t_fired_other", "t_heard",
                 "heard_kind", "home", "aggro", "t_reload_heard", "hp_prev", "t_fired")

    def __init__(self, tid, kind, t):
        self.id = tid
        self.kind = kind
        self.pos = [0.0, 0.0, 0.0]
        self.vel = [0.0, 0.0, 0.0]
        self.yaw = 0.0
        self.pitch = 0.0
        self.hp = None
        self.max_hp = None
        self.cubes = 0
        self.weapons = ["gun"] if kind == "mech" else []
        self.charging = False
        self.t_seen = -1e9
        self.t_first = t
        self.in_obs = False
        self.los = False
        self.sees_us = False
        self.dead = False
        self.phantom = False          # only known from a damage bearing or sound
        self.hist = []                # (t, x, y, vx, vy)
        self.dmg_to_us = []           # (t, amount)
        self.t_hit_us = -1e9
        self.t_we_hit = -1e9
        self.dmg_from_us = 0.0
        self.t_hp_drop_other = -1e9   # it lost HP to someone else
        self.t_fired_other = -1e9     # we saw it shoot at someone else
        self.t_heard = -1e9
        self.heard_kind = None
        self.home = None
        self.aggro = False
        self.t_reload_heard = -1e9
        self.hp_prev = None
        self.t_fired = -1e9

    # ------------------------------------------------------------------ estimates
    def age(self, t):
        return t - self.t_seen

    def est_pos(self, t):
        """Best guess of the position now."""
        if self.in_obs:
            return self.pos
        dt = min(max(0.0, t - self.t_seen), 1.5)
        return [self.pos[0] + self.vel[0] * dt, self.pos[1] + self.vel[1] * dt, self.pos[2]]

    def uncertainty(self, t, speed=11.7):
        if self.in_obs:
            return 0.0
        base = 25.0 if self.phantom else 0.0
        return base + speed * max(0.0, t - self.t_seen)

    def hp_frac(self):
        if self.hp is None or not self.max_hp:
            return 1.0
        return self.hp / self.max_hp

    def has(self, kind):
        return kind in self.weapons

    def dodging(self):
        """Has it changed direction sharply or woven smoothly in the last ~2 s?"""
        h = self.hist
        if len(h) < 4:
            return False
        t_cut = h[-1][0] - 2.0
        hh = [e for e in h if e[0] >= t_cut]
        for a, b in zip(hh, hh[1:]):
            sa = math.hypot(a[3], a[4])
            sb = math.hypot(b[3], b[4])
            if sa > 3.0 and sb > 3.0:
                if (a[3] * b[3] + a[4] * b[4]) / (sa * sb) < 0.3:
                    return True
        # Smooth sinusoidal weave (e.g. period 1.2-2.0 s): adjacent 0.1 s ticks barely turn,
        # but velocity 0.35-0.75 s apart rotates by > 47 degrees (cos < 0.68).
        n = len(hh)
        for i in range(n):
            ti, vxi, vyi = hh[i][0], hh[i][3], hh[i][4]
            si = math.hypot(vxi, vyi)
            if si <= 3.0:
                continue
            for j in range(i + 3, n):
                dt = hh[j][0] - ti
                if dt < 0.32:
                    continue
                if dt > 0.80:
                    break
                vxj, vyj = hh[j][3], hh[j][4]
                sj = math.hypot(vxj, vyj)
                if sj > 3.0 and (vxi * vxj + vyi * vyj) / (si * sj) < 0.68:
                    return True
        return False

    def mean_vel(self, window=0.75):
        """Mean horizontal velocity over `window` s; damps lead overshoot against weavers."""
        h = self.hist
        if len(h) < 3:
            return tuple(self.vel)
        t_cut = h[-1][0] - window
        sub = [e for e in h if e[0] >= t_cut]
        if len(sub) < 2:
            return tuple(self.vel)
        inv = 1.0 / len(sub)
        return (sum(e[3] for e in sub) * inv, sum(e[4] for e in sub) * inv, self.vel[2])

    def still(self):
        return math.hypot(self.vel[0], self.vel[1]) < 1.5

    def recently_hit_us(self, t, window=3.0):
        return t - self.t_hit_us <= window

    def dmg_rate_to_us(self, t, window=3.0):
        return sum(a for (tt, a) in self.dmg_to_us if t - tt <= window) / window

    def engaged_elsewhere(self, t, window=4.0):
        return (t - self.t_hp_drop_other <= window) or (t - self.t_fired_other <= window)


class SoundContact:
    __slots__ = ("kind", "bearing", "band", "t", "pos", "count")

    def __init__(self, kind, bearing, band, t, pos):
        self.kind, self.bearing, self.band, self.t, self.pos = kind, bearing, band, t, pos
        self.count = 1


class Threats:
    def __init__(self, cfg, world=None, my_id=None):
        self.cfg = cfg
        self.w = world
        self.my_id = my_id
        v = cfg.get("vision", {})
        s = cfg.get("sound", {})
        self.memory_s = float(v.get("memory_s", 5.0))
        self.near_range = float(v.get("near_range", 45.0))
        self.vis_range = float(v.get("range", 200.0))
        self.fov = math.radians(float(v.get("fov_deg", 120.0)))
        self.sigma = math.radians(float(s.get("bearing_noise_deg", 7.0)))
        self.near = float(s.get("near", 60.0))
        self.mid = float(s.get("mid", 160.0))
        self.ranges = dict(s.get("range", {}))
        self.tracks = {}
        self.contacts = []            # SoundContact (unattached mech sounds)
        self.deaths = []              # (t, source) 'kill' | 'sound' | 'alive'
        self.alive = None
        self.t = 0.0
        self.kills = 0
        self.last_kill_t = -1e9
        self.projectiles = []
        self.last_damage = None       # (t, bearing, by, weapon)
        self.last_rear_hit = None     # (t, bearing, by, weapon)
        self.dmg_taken = []           # (t, amount, weapon)
        self.t_dealt = -1e9           # we last dealt damage
        self.t_taken = -1e9           # we last took (non-zone) damage
        self.t_zone_dmg = -1e9
        self.mortar_heard = None      # (t, bearing, band) of the latest mortar launch heard
        self.prev_in_obs = set()
        self.t_fight = -1e9           # we last dealt or took weapon damage
        self.short_until = -1e9       # lightning short effect: laser disabled until this t
        self.recent_kill_drops = []   # (pos, est_cubes, weapons, t) emitted this tick

    # ------------------------------------------------------------------ helpers
    def band_dist(self, kind, band):
        if band == "near":
            return self.near * 0.55
        if band == "mid":
            return 0.5 * (self.near + self.mid)
        rng = float(self.ranges.get(kind, 300.0))
        return 0.5 * (self.mid + max(self.mid + 20.0, rng))

    def band_limits(self, kind, band):
        if band == "near":
            return 0.0, self.near
        if band == "mid":
            return self.near, self.mid
        return self.mid, float(self.ranges.get(kind, 600.0))

    def mechs(self, alive_only=True):
        return [tr for tr in self.tracks.values() if tr.kind == "mech" and not (alive_only and tr.dead)]

    def npcs(self):
        return [tr for tr in self.tracks.values() if tr.kind == "npc" and not tr.dead]

    # ------------------------------------------------------------------ update
    def update(self, obs, me):
        t = me.t
        self.t = t
        self.recent_kill_drops = []
        mx, my = me.pos[0], me.pos[1]
        eye = me.eye
        muzzle = me.muzzle
        seen_ids = set()
        for m in obs.get("mechs") or []:
            tid = m.get("id")
            if tid is None or tid == me.id:
                continue
            kind = m.get("kind", "mech")
            tr = self.tracks.get(tid)
            if tr is None:
                tr = Track(tid, kind, t)
                self.tracks[tid] = tr
            seen_ids.add(tid)
            tr.kind = kind
            tr.phantom = False
            tr.dead = False
            p = m.get("pos") or tr.pos
            tr.pos = [float(p[0]), float(p[1]), float(p[2])]
            v = m.get("vel") or (0, 0, 0)
            tr.vel = [float(v[0]), float(v[1]), float(v[2])]
            a = m.get("aim") or (tr.yaw, tr.pitch)
            tr.yaw, tr.pitch = float(a[0]), float(a[1])
            if m.get("hp") is not None:
                newhp = float(m["hp"])
                if tr.hp is not None and newhp < tr.hp - 0.5 and t - tr.t_we_hit > 0.25:
                    tr.t_hp_drop_other = t
                tr.hp = newhp
            if m.get("max_hp") is not None:
                tr.max_hp = float(m["max_hp"])
            tr.cubes = int(m.get("cubes", tr.cubes) or 0)
            if kind == "mech":
                tr.weapons = list(m.get("weapons") or tr.weapons)
                tr.charging = bool(m.get("charging", False))
            if kind == "npc" and tr.home is None:
                tr.home = (tr.pos[0], tr.pos[1])
            tr.t_seen = t
            tr.hist.append((t, tr.pos[0], tr.pos[1], tr.vel[0], tr.vel[1]))
            if len(tr.hist) > 30:
                del tr.hist[:-30]
        for tid, tr in self.tracks.items():
            tr.in_obs = tid in seen_ids
            if not tr.in_obs:
                tr.los = False
                tr.sees_us = False
                tr.charging = False if t - tr.t_seen > 1.0 else tr.charging
        # Shot line vs optical sight for entries in obs (trees block sight >45 m, NEVER shots!)
        if self.w is not None:
            for tid in seen_ids:
                tr = self.tracks[tid]
                d = math.hypot(tr.pos[0] - mx, tr.pos[1] - my)
                if d > 285:
                    tr.los = False
                    tr.sees_us = False
                    continue
                s = 1.0 + 0.05 * (tr.cubes or 0) if tr.kind == "mech" else 1.0
                h = 9.0 * s if tr.kind == "mech" else 3.5
                c_pt = (tr.pos[0], tr.pos[1], tr.pos[2] + h * 0.5)
                top_pt = (tr.pos[0], tr.pos[1], tr.pos[2] + h * 0.82)
                tr.los = self.w.shot_clear(muzzle, c_pt) or self.w.shot_clear(muzzle, top_pt)
                their_eye = (tr.pos[0], tr.pos[1], tr.pos[2] + (8.0 * s if tr.kind == "mech" else 2.5))
                opt_sees = self.w.sees(their_eye, me.pos, me.height, self.near_range)
                in_wedge = angdiff(tr.yaw, math.atan2(my - tr.pos[1], mx - tr.pos[0])) <= self.fov / 2 + 0.08
                tr.sees_us = opt_sees and (d <= self.near_range or (in_wedge and d <= self.vis_range * 1.15))
        self._events(obs, me)
        # Anyone who just hit us has a line on us even if beyond nominal 200 m (e.g. sensors upgrade)
        for tr in self.tracks.values():
            if not tr.dead and t - tr.t_hit_us <= 1.2:
                tr.sees_us = True
        self._beams(obs, me)
        self._sounds(obs, me)
        self.projectiles = [p for p in (obs.get("projectiles") or [])]
        alive = obs.get("alive")
        if alive is not None:
            if self.alive is not None and alive < self.alive:
                n_died = self.alive - alive
                for _ in range(n_died):
                    self.deaths.append((t, "alive"))
                # a wounded mech that vanished from view on the tick someone died is that someone
                gone = [tr for tr in self.tracks.values() if tr.kind == "mech" and not tr.dead and not tr.in_obs
                        and tr.id in self.prev_in_obs and (tr.hp_frac() < 0.45 or t - tr.t_we_hit <= 2.5)]
                gone.sort(key=lambda tr: tr.hp_frac())
                for tr in gone[:n_died]:
                    tr.dead = True
                    if not tr.phantom:
                        self.recent_kill_drops.append(
                            (list(tr.est_pos(t)), (tr.cubes or 0) + 2, [w for w in tr.weapons if w != "gun"], t)
                        )
            self.alive = alive
        self.prev_in_obs = set(seen_ids)
        # forget stale phantoms and old contacts
        for tid in list(self.tracks):
            tr = self.tracks[tid]
            if tr.phantom and t - max(tr.t_seen, tr.t_hit_us, tr.t_heard) > 12.0:
                del self.tracks[tid]
            elif tr.kind == "npc" and t - tr.t_seen > 60.0:
                del self.tracks[tid]
        self.contacts = [c for c in self.contacts if t - c.t <= 8.0]

    def _events(self, obs, me):
        t = me.t
        for e in obs.get("events") or []:
            et = e.get("type")
            if et == "damaged":
                by = e.get("by")
                amt = float(e.get("amount", 0.0) or 0.0)
                wpn = e.get("weapon")
                if by is None or wpn == "zone":
                    self.t_zone_dmg = t
                    continue
                self.t_taken = t
                if wpn != "npc":
                    self.t_fight = t
                self.dmg_taken.append((t, amt, wpn))
                b = e.get("bearing")
                self.last_damage = (t, b, by, wpn)
                if b is not None and angdiff(me.yaw, b) > math.pi * 0.5:
                    self.last_rear_hit = (t, b, by, wpn)
                tr = self.tracks.get(by)
                if tr is None:
                    kind = "npc" if (wpn == "npc" or (isinstance(by, (int, float)) and by >= 100)) else "mech"
                    tr = Track(by, kind, t)
                    tr.phantom = True
                    self.tracks[by] = tr
                    if b is not None:
                        guess = {"lightning": 35.0, "npc": 40.0, "gun": 110.0, "laser": 165.0, "mortar": 140.0}.get(wpn, 100.0)
                        tr.pos = [me.pos[0] + math.cos(b) * guess, me.pos[1] + math.sin(b) * guess, me.pos[2]]
                        tr.t_seen = t - 0.5
                elif tr.phantom and b is not None:
                    guess = {"lightning": 35.0, "npc": 40.0, "gun": 110.0, "laser": 165.0, "mortar": 140.0}.get(wpn, 100.0)
                    tr.pos = [me.pos[0] + math.cos(b) * guess, me.pos[1] + math.sin(b) * guess, me.pos[2]]
                    tr.t_seen = t - 0.3
                if wpn in ("gun", "laser", "lightning", "mortar") and wpn not in tr.weapons:
                    tr.weapons.append(wpn)
                tr.t_hit_us = t
                tr.dmg_to_us.append((t, amt))
                if len(tr.dmg_to_us) > 40:
                    del tr.dmg_to_us[:-40]
                if tr.kind == "npc":
                    tr.aggro = True
            elif et == "hit":
                tgt = e.get("target")
                self.t_dealt = t
                if isinstance(tgt, (int, float)) and tgt < 100:
                    self.t_fight = t
                tr = self.tracks.get(tgt)
                if tr is not None:
                    tr.t_we_hit = t
                    tr.dmg_from_us += float(e.get("amount", 0.0) or 0.0)
                    if tr.kind == "npc":
                        tr.aggro = True
            elif et == "kill":
                tr = self.tracks.get(e.get("target"))
                if tr is not None:
                    tr.dead = True
                    if tr.kind == "mech" and not tr.phantom:
                        self.recent_kill_drops.append(
                            (list(tr.est_pos(t)), (tr.cubes or 0) + 2, [w for w in tr.weapons if w != "gun"], t)
                        )
                self.kills += 1
                self.last_kill_t = t
                self.deaths.append((t, "kill"))
            elif et == "effect" and e.get("effect") == "short":
                lw = ((self.cfg.get("weapons") or {}).get("lightning") or {})
                self.short_until = t + float(lw.get("short_s", 2.0))
                tr = self.tracks.get(e.get("by"))
                if tr is not None and "lightning" not in tr.weapons:
                    tr.weapons.append("lightning")
        if len(self.dmg_taken) > 80:
            del self.dmg_taken[:-80]

    def _beams(self, obs, me):
        """A beam that ends near another mech tells us its shooter is busy with someone else."""
        t = me.t
        for b in obs.get("beams") or []:
            by = b.get("by")
            path = b.get("path") or []
            tr = self.tracks.get(by)
            if tr is None or len(path) < 2:
                continue
            tr.t_fired = t
            if b.get("kind") not in tr.weapons:
                tr.weapons.append(b.get("kind"))
            end = path[-1]
            if math.hypot(end[0] - me.pos[0], end[1] - me.pos[1]) > 8.0:
                tr.t_fired_other = t
        for p in obs.get("projectiles") or []:
            # a projectile whose velocity points well away from us was fired at someone else
            v = p.get("vel") or (0, 0, 0)
            sp = math.hypot(v[0], v[1])
            if sp < 1:
                continue
            pos = p.get("pos")
            # find the shooter: nearest mech behind the projectile along its path
            best = None
            for tr in self.tracks.values():
                if tr.kind != "mech" or not tr.in_obs:
                    continue
                dx, dy = pos[0] - tr.pos[0], pos[1] - tr.pos[1]
                along = (dx * v[0] + dy * v[1]) / sp
                if along <= 0:
                    continue
                perp = abs(dx * v[1] - dy * v[0]) / sp
                if perp < 3.0 and (best is None or along < best[0]):
                    best = (along, tr)
            if best:
                tr = best[1]
                tr.t_fired = t
                tox, toy = me.pos[0] - pos[0], me.pos[1] - pos[1]
                if (tox * v[0] + toy * v[1]) / (sp * max(1e-6, math.hypot(tox, toy))) < 0.9:
                    tr.t_fired_other = t

    def _sounds(self, obs, me):
        t = me.t
        for s in obs.get("sounds") or []:
            kind = s.get("kind")
            if kind == "mortar":
                self.mortar_heard = (t - float(s.get("age", 0.0) or 0.0), float(s.get("bearing", 0.0)), s.get("dist"))
            if kind not in MECH_WEAPON_SOUNDS:
                if kind == "death":
                    self.deaths.append((t, "sound"))
                continue
            b = float(s.get("bearing", 0.0))
            band = s.get("dist", "mid")
            lo, hi = self.band_limits(kind, band)
            d = self.band_dist(kind, band)
            ts = t - float(s.get("age", 0.0) or 0.0)
            # attach to a known mech when bearing and distance band fit
            best = None
            for tr in self.tracks.values():
                if tr.kind != "mech" or tr.dead:
                    continue
                p = tr.est_pos(t)
                dd = math.hypot(p[0] - me.pos[0], p[1] - me.pos[1])
                err = angdiff(b, math.atan2(p[1] - me.pos[1], p[0] - me.pos[0]))
                slack = tr.uncertainty(t) / max(dd, 1.0)
                if err <= 3 * self.sigma + slack and lo - 15 - tr.uncertainty(t) <= dd <= hi + 15 + tr.uncertainty(t):
                    if best is None or err < best[0]:
                        best = (err, tr)
            if best:
                tr = best[1]
                tr.t_heard = t
                tr.heard_kind = kind
                if kind in ("gun", "laser", "lightning", "mortar") and kind not in tr.weapons:
                    tr.weapons.append(kind)
                if kind == "laser_charge" and "laser" not in tr.weapons:
                    tr.weapons.append("laser")
                if kind == "reload":
                    tr.t_reload_heard = ts
                continue
            pos = (me.pos[0] + math.cos(b) * d, me.pos[1] + math.sin(b) * d)
            merged = False
            for c in self.contacts:
                if c.band == band and angdiff(c.bearing, b) < 2.5 * self.sigma:
                    c.bearing = wrap(c.bearing + wrap(b - c.bearing) / (c.count + 1))
                    c.count += 1
                    c.t = t
                    c.kind = kind
                    c.pos = (me.pos[0] + math.cos(c.bearing) * d, me.pos[1] + math.sin(c.bearing) * d)
                    merged = True
                    break
            if not merged:
                self.contacts.append(SoundContact(kind, b, band, t, pos))

    # ------------------------------------------------------------------ queries
    def active_mechs(self, t=None, window=6.0):
        """Mechs that are in view, in memory, or were heard/hit us recently."""
        t = self.t if t is None else t
        out = []
        for tr in self.tracks.values():
            if tr.kind != "mech" or tr.dead:
                continue
            if tr.in_obs or t - tr.t_seen <= window or t - tr.t_hit_us <= window or t - tr.t_heard <= window:
                out.append(tr)
        return out

    def threat_points(self, me, window=6.0, include_contacts=True):
        """Positions (x, y, z_eye) from which something hostile might be watching us."""
        pts = []
        for tr in self.active_mechs(me.t, window):
            if tr.phantom and self.last_damage is not None and self.last_damage[2] == tr.id \
                    and self.last_damage[1] is not None:
                # only a bearing is known: something anywhere along it may be watching
                b = self.last_damage[1]
                for d in (45.0, 95.0, 160.0, 220.0):
                    x, y = me.pos[0] + math.cos(b) * d, me.pos[1] + math.sin(b) * d
                    f = self.w.floor(x, y) if self.w is not None else me.pos[2]
                    pts.append((x, y, f + 8.0))
                continue
            p = tr.est_pos(me.t)
            pts.append((p[0], p[1], p[2] + 8.0))
        if include_contacts:
            for c in self.contacts:
                if me.t - c.t <= 5.0 and c.band != "far":
                    f = self.w.floor(c.pos[0], c.pos[1]) if self.w is not None else me.pos[2]
                    pts.append((c.pos[0], c.pos[1], f + 8.0))
        return pts

    def incoming_heat(self, t, window=2.0):
        return sum(a for (tt, a, w) in self.dmg_taken if t - tt <= window)

    def unseen_attacker(self, t, window=2.0):
        """We were hit recently by something we cannot see."""
        for tr in self.tracks.values():
            if t - tr.t_hit_us <= window and not tr.los:
                return tr
        return None
