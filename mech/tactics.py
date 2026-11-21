"""What to do this decision: Line B aggressive tactics from t = 0 through the final circle.

Fights from the first second when an enemy is in range and the fight is not clearly lost, routes
aggressively through weapons, cubes, crates, NPCs and kill drops using spatial loot memory,
never retreats silently while line of sight holds, third-parties mid-game fights from circle-edge
cover, and maintains 360-degree open-sector awareness plus laser/mortar/lightning counters.

The same rules run in every match. Nothing here tries to tell practice from the final.
"""
import math
import random

from . import air as A
from . import doctrine as D
from . import positions as P
from .geom import angdiff, bearing, unit, wrap
from .gunner import Target
from .loot import LootMemory

WANTED_WEAPONS = ("laser", "lightning")     # laser first (kills happen at range), lightning second, never mortar


def upgrade_score(me, uid):
    """Context-aware upgrade valuation tailored to the weapons we actually carry."""
    base = {
        "plating": 100,
        "armor": 95,
        "nanites": 92,
        "gun_rate": 80,
        "gun_damage": 79,
        "servos": 75,
        "gun_velocity": 72,
        "gun_mag": 66,
        "reactor": 62,
        "capacitor": 56,
        "gyros": 50,
        "jets": 46,
        "sensors": 44,
        "laser_pierce": 35,
        "mortar_reload": 12,
        "mortar_velocity": 10,
        "mortar_splash": 8,
    }
    if uid in ("laser_damage", "laser_charge"):
        if me.has("laser"):
            return 89 if uid == "laser_damage" else 85
        return 58 if uid == "laser_damage" else 54
    if uid in ("lightning_range", "lightning_efficiency", "lightning_chain"):
        if me.has("lightning"):
            return {"lightning_range": 83, "lightning_efficiency": 78, "lightning_chain": 64}[uid]
        return 30
    return base.get(uid, 20)


class Decision:
    def __init__(self):
        self.intent = None
        self.target = None          # gunner.Target
        self.take = None
        self.drop = None
        self.pick = None
        self.face = None
        self.speed = "walk"
        self.evasive = False
        self.threat_yaw = None
        self.hold = False
        self.label = ""
        self.direct = None          # walk straight to this point (roofs have no grid)


class Tactics:
    def __init__(self, world, cfg, mover, cover, threats, zone, seed=0):
        self.w = world
        self.cfg = cfg
        self.mv = mover
        self.cov = cover
        self.th = threats
        self.zone = zone
        self.loot = LootMemory(cfg)
        self.opening_done = True     # Line B fights and collects from t = 0
        self.opening_search = None
        self.second_search = None
        self.hold_goal = None
        self.hold_key = None
        self.hold_t = -1e9
        self.relocate_until = -1e9
        self.relocate_from = None
        self.kills_seen = 0
        self.expose_t = None
        self.expose_hp = None
        self.last_play = None
        self.play_since = 0.0
        self.healing = False
        self.look_phase = 0.0
        self.rear_flip = 1
        self.crate_ban = {}
        self.npc_ban = {}
        self.loot_note = ""
        self.last_harass_hit = -1e9
        self.air = None              # the air play in progress (mech/air.py)
        self.roof_mode = False       # we mean to be on a roof
        self.roof_spots = None       # (peek, hide, t) on the roof
        self.air_ban_until = -1e9
        self.rng = random.Random((seed or 0) ^ 0x5A17)
        self.prev_corner_spot = None
        self.displace = None         # ((x, y), until): moving off a spot a mortar may be falling on
        self.mortar_jink_until = -1e9
        self.spawn = None

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _dist(me, tr):
        p = tr.est_pos(me.t)
        return math.hypot(p[0] - me.pos[0], p[1] - me.pos[1])

    def _max_weapon_range(self, me):
        rng = 230.0
        if me.has("laser") and me.t >= self.th.short_until:
            rng = max(rng, me.by_kind["laser"].stat("range", 280.0))
        if me.has("gun"):
            rng = max(rng, me.by_kind["gun"].stat("range", 230.0))
        return rng

    def _shootable(self, me, enemies):
        """The best enemy we can see and reach with a weapon now: attackers & dying mechs first."""
        max_r = self._max_weapon_range(me)
        best = None
        for e in enemies:
            if not e.los or not e.in_obs:
                continue
            d = self._dist(me, e)
            if d > max_r:
                continue
            score = d - (180.0 if me.t - e.t_hit_us <= 2.0 else 0.0) - (120.0 if e.hp_frac() < 0.35 else 0.0)
            if best is None or score < best[0]:
                best = (score, e)
        return best[1] if best else None

    def _me_weapons(self, me):
        return {w.kind: w.stats for w in me.weapons}

    def situation(self, me, enemy, others, corner):
        unseen = self.th.unseen_attacker(me.t, 2.0)
        g = me.by_kind.get("gun")
        shorted = me.t < self.th.short_until
        return D.Situation(
            me.t, me.xy, me.yaw, me.hp, me.max_hp, self._me_weapons(me), enemy, others,
            me.cubes, me.energy, me.armor, corner,
            unseen is not None and (enemy is None or not enemy.los),
            enemy.los if enemy is not None else False,
            self.cfg, not me.grounded, self.healing,
            g.ammo if g is not None else None,
            shorted=shorted,
        )

    def watchers(self, me, window=6.0):
        return self.th.threat_points(me, window)

    def danger_yaw(self, me):
        """Bearing to keep our front 180-degree armor arc and 120-degree sight cone toward."""
        t = me.t
        rh = self.th.last_rear_hit
        if rh is not None and t - rh[0] <= 1.8 and rh[1] is not None:
            tr = self.th.tracks.get(rh[2])
            if tr is not None and tr.in_obs and not tr.dead:
                return bearing(me.pos, tr.est_pos(t))
            return rh[1]
        ld = self.th.last_damage
        if ld is not None and t - ld[0] <= 2.5 and ld[1] is not None:
            tr = self.th.tracks.get(ld[2])
            if tr is not None and tr.in_obs and not tr.dead:
                return bearing(me.pos, tr.est_pos(t))
            return ld[1]
        # Consider nearby active mechs; if two are within 165 m at <= 145 deg separation, face their
        # weighted bisector so neither gets a 1.5x rear shot on us.
        scored = []
        for tr in self.th.active_mechs(t, 5.0):
            p = tr.est_pos(t)
            d = math.hypot(p[0] - me.pos[0], p[1] - me.pos[1])
            sc = d - (85.0 if tr.sees_us else 0.0) - (65.0 if tr.charging else 0.0)
            scored.append((sc, d, bearing(me.pos, p)))
        if scored:
            scored.sort()
            if len(scored) >= 2 and scored[0][1] < 165.0 and scored[1][1] < 165.0:
                b1, b2 = scored[0][2], scored[1][2]
                if angdiff(b1, b2) < 2.53:
                    vx = math.cos(b1) * 1.25 + math.cos(b2)
                    vy = math.sin(b1) * 1.25 + math.sin(b2)
                    if math.hypot(vx, vy) > 0.2:
                        return math.atan2(vy, vx)
            return scored[0][2]
        for c in self.th.contacts:
            if t - c.t <= 3.0:
                return c.bearing
        return None

    def laser_threat(self, me):
        """An enemy charging a laser within ~49 deg of us (catches off-axis pre-snap charges too)."""
        for tr in self.th.active_mechs(me.t, 1.0):
            if tr.charging and tr.in_obs and self._dist(me, tr) <= 285.0:
                b = bearing(tr.pos, me.pos)
                if angdiff(tr.yaw, b) < 0.85:
                    return tr
        return None

    # ------------------------------------------------------------------ positions & 360 awareness
    def hold_spot(self, me):
        """A covered cell inside the next circle, preferring the quiet 0.42-0.75*r ring in crowded
        mid-game circles and rotating periodically when idle so we sweep new lanes for loot/fights."""
        z = self.zone
        cx, cy, rs = self.mv.safe_circle(z)
        key = (z.version, round(cx), round(cy))
        dwell = 16.0 if (self.th.alive is None or self.th.alive >= 4) else 22.0
        if self.hold_goal is not None and key == self.hold_key and me.t - self.hold_t < dwell \
                and math.hypot(self.hold_goal[0] - cx, self.hold_goal[1] - cy) <= rs:
            return self.hold_goal
        threats = self.th.threat_points(me, 20.0)
        drones = [tr.home for tr in self.th.npcs() if tr.home is not None]
        n = P.choose_hold(self.w, me.xy, (cx, cy, max(2.0, rs - 4.0)), threats, self.rng,
                          drones=drones, alive=self.th.alive)
        if n is None or not self.w.main[n]:
            n = self.w.nearest_main(cx, cy, 32)
        self.hold_goal = self.w.node_xy(n) if n is not None else (cx, cy)
        self.hold_key = key
        self.hold_t = me.t
        return self.hold_goal

    def patrol_yaw(self, me, move_yaw=None):
        """Sweep forward/inward while periodically checking open rear/flank lanes (fan_dist > 50 m)
        so no hunter can close inside our 120-degree rear blind spot unseen."""
        z = self.zone
        if move_yaw is not None:
            base = move_yaw
        elif math.hypot(z.next_center[0] - me.pos[0], z.next_center[1] - me.pos[1]) > 15.0:
            base = bearing(me.pos, z.next_center)
        else:
            base = me.yaw
        self.look_phase += 0.1
        slot = self.look_phase % 2.2
        n = self.w.nearest_free(me.pos[0], me.pos[1], 3)
        if slot >= 1.8 and n is not None:
            # 0.4 s rear/flank check along the widest open corridor behind us (> 95 deg from base)
            if slot < 1.9:
                self.rear_flip = -self.rear_flip
            best_open = None
            for k in range(self.w.nfan):
                yaw = 2.0 * math.pi * k / self.w.nfan
                diff = wrap(yaw - base)
                if abs(diff) < 1.65:
                    continue
                fd = self.w.fan_dist(n, yaw)
                if fd <= 48:
                    continue
                side_bonus = 25.0 if diff * self.rear_flip > 0 else 0.0
                sc = fd + side_bonus
                if best_open is None or sc > best_open[0]:
                    best_open = (sc, yaw)
            if best_open is not None:
                return best_open[1]
        return wrap(base + 0.85 * math.sin(self.look_phase * 0.9))

    def lookout_yaw(self, me):
        return self.patrol_yaw(me, None)

    # ------------------------------------------------------------------ aggressive loot & farming
    def loot_choice(self, me, obs):
        """Best weapon pickup, kill drop, or cube from live sight + spatial LootMemory, inside safe zone."""
        z = self.zone
        cx, cy, rs = self.mv.safe_circle(z)
        slack = self.mv.slack(me, z)
        kinds = [w.kind for w in me.weapons]
        reach = float((self.cfg.get("loot") or {}).get("pickup_radius", 4.0))
        best = None

        def zone_ok(px, py):
            if z.margin_at(px, py, me.t + 2.0) < 0.0 and math.hypot(px - cx, py - cy) > rs:
                return False
            if slack < 22.0 and math.hypot(px - cx, py - cy) > rs + 8.0:
                return False
            return True

        # 1. Weapon pickups (live + remembered): laser first, lightning second, never mortar
        for pid, (k, pp, _) in self.loot.pickups.items():
            if k not in WANTED_WEAPONS or k in kinds:
                continue
            drop = None
            if not me.slots_free:
                mort = me.by_kind.get("mortar")
                light = me.by_kind.get("lightning")
                if mort is not None:
                    drop = mort.slot
                elif light is not None and k == "laser":
                    if light.cooldown > 0.2:
                        continue
                    drop = light.slot
                else:
                    continue
            d = math.hypot(pp[0] - me.pos[0], pp[1] - me.pos[1])
            max_d = 145.0 if k == "laser" else 95.0
            if d > max_d or abs(pp[2] - me.pos[2]) > 6.5 or not zone_ok(pp[0], pp[1]):
                continue
            d_eff = d * (0.38 if k == "laser" else 0.72)
            in_reach = abs(pp[2] - me.pos[2]) <= 4.0 and d <= reach - 0.25 \
                and any(p.get("id") == pid for p in (obs.get("pickups") or []))
            if best is None or d_eff < best[0]:
                best = (d_eff, ("pickup", pid, (pp[0], pp[1]), drop, in_reach))

        # 2. Inferred kill drops (cubes + 2, plus any wanted non-gun weapon)
        for idx, (kp, kcubes, kwpns, _) in enumerate(self.loot.kill_drops):
            d = math.hypot(kp[0] - me.pos[0], kp[1] - me.pos[1])
            has_wanted = any(w in WANTED_WEAPONS and w not in kinds for w in kwpns)
            max_d = 135.0 if has_wanted else 105.0
            if d > max_d or abs(kp[2] - me.pos[2]) > 6.5 or not zone_ok(kp[0], kp[1]):
                continue
            val = min(kcubes, 6) * 8.0 + (35.0 if has_wanted else 0.0)
            d_eff = max(1.0, d - val) * 0.65
            if best is None or d_eff < best[0]:
                best = (d_eff, ("killdrop", "kd%d" % idx, (kp[0], kp[1]), None, False))

        # 3. Individual & clustered cubes (live + remembered)
        cube_items = list(self.loot.cubes.items())
        for cid, (cp, _) in cube_items:
            d = math.hypot(cp[0] - me.pos[0], cp[1] - me.pos[1])
            max_d = 90.0 if me.cubes < 10 else 55.0
            if d > max_d or abs(cp[2] - me.pos[2]) > 4.0 or not zone_ok(cp[0], cp[1]):
                continue
            # Cluster bonus when multiple cubes lie together (e.g. visible kill pile or drone drop)
            nearby = sum(1 for _, (op, _) in cube_items if math.hypot(op[0] - cp[0], op[1] - cp[1]) < 9.0)
            mult = 0.55 if me.cubes < 10 else 0.85
            d_eff = max(1.0, d - (nearby - 1) * 10.0) * mult
            if best is None or d_eff < best[0]:
                best = (d_eff, ("cube", cid, (cp[0], cp[1]), None, False))

        return best[1] if best is not None else None

    def shoot_choice(self, me, obs, allow_npc=True):
        """A crate or NPC drone worth farming now, as a Target (or None)."""
        t = me.t
        z = self.zone
        gun = me.by_kind.get("gun")
        light = me.by_kind.get("lightning")
        best = None
        for c in obs.get("crates") or []:
            cp = c.get("pos")
            if self.crate_ban.get(c["id"], -1e9) > t:
                continue
            d = math.hypot(cp[0] - me.pos[0], cp[1] - me.pos[1])
            if d > 88.0 or z.margin_at(cp[0], cp[1], t + 3.0) < 0.0:
                continue
            if gun is None and (light is None or d > 42.0):
                continue
            tg = Target(c["id"], "crate", (cp[0], cp[1], cp[2]), (0.0, 0.0, 0.0), 1.4, 2.4, True)
            if not self.w.shot_clear(me.muzzle, tg.center()):
                self.crate_ban[c["id"]] = t + 2.5
                continue
            hp_factor = float(c.get("hp", 300.0)) / 300.0
            score = d * (0.65 + 0.35 * hp_factor)
            if best is None or score < best[0]:
                best = (score, tg)
        if best is None and allow_npc and gun is not None:
            for tr in self.th.npcs():
                if not tr.in_obs or not tr.los:
                    continue
                if self.npc_ban.get(tr.id, -1e9) > t:
                    continue
                p = tr.pos
                d = math.hypot(p[0] - me.pos[0], p[1] - me.pos[1])
                max_d = 115.0 if not tr.aggro else 135.0
                if d > max_d or z.margin_at(p[0], p[1], t + 3.0) < 0.0:
                    continue
                if me.hp_frac < 0.62 and not (tr.aggro and d < 65.0):
                    continue
                tg = Target.from_track(tr, t)
                score = d + (0.0 if tr.aggro else 22.0)
                if best is None or score < best[0]:
                    best = (score, tg)
        return best[1] if best else None

    # ------------------------------------------------------------------ main
    def decide(self, me, obs):
        t = me.t
        dec = Decision()
        if self.spawn is None:
            self.spawn = tuple(me.pos)
        th = self.th
        z = self.zone
        self.loot.update(obs, me, self.w, th.recent_kill_drops)

        # Context-aware upgrade selection
        offer = obs.get("offer")
        if offer and offer.get("choices"):
            ch = offer["choices"]
            dec.pick = max(range(len(ch)), key=lambda i: upgrade_score(me, ch[i].get("id")))

        if th.kills > self.kills_seen:
            self.kills_seen = th.kills
            self.relocate_until = t + 7.0
            self.relocate_from = (me.pos[0], me.pos[1])
            self.cov.reset()

        # Active enemies now (from t = 0!)
        active = th.active_mechs(t, 6.0)
        enemies = [e for e in active if not e.phantom or t - e.t_hit_us <= 4.0]
        n_me = self.w.nearest_free(me.pos[0], me.pos[1], 4)
        wall_beside = n_me is not None and self.w.wall_arc(n_me, within=8.0) >= 0.18
        corner_ready = (self.cov.hide is not None and self.cov.peek is not None) or \
            (wall_beside and self.cov.fail_t < t - 2.0)

        target, calc = D.pick_target(
            lambda e: self.situation(me, e, [o for o in enemies if o is not e], corner_ready),
            enemies, t, me.xy,
        )
        others = [o for o in enemies if o is not target and
                  (o.sees_us or t - o.t_hit_us <= 3.0 or (o.los and self._dist(me, o) <= 110.0))]
        sit = self.situation(me, target, others, corner_ready)
        play, calc = D.choose_play(sit, calc if target is not None else None, prev=self.last_play)
        slack = self.mv.slack(me, z)
        dec.threat_yaw = self.danger_yaw(me)
        lt = self.laser_threat(me)

        # Zone pressure overrides lingering fights
        if slack < 4.0 and play in (D.CORNER, D.HARASS):
            play = D.ASSAULT if (calc is not None and calc.ratio >= 0.95 and calc.dist < 65) else D.BREAK
        # Broke contact and nobody is on us: return to looting / healing / repositioning
        if play == D.BREAK and target is not None and not any(e.sees_us or e.los for e in enemies) \
                and t - max([e.t_hit_us for e in enemies] + [-1e9]) > 5.0 and me.hp_frac >= 0.85 \
                and not sit.unseen_hit:
            play = D.REPOSITION
        if play != self.last_play:
            self.last_play = play
            self.play_since = t

        alt_shoot = self._shootable(me, enemies)
        ctx = {
            "calc": calc,
            "hide": self.cov.hide_xy(),
            "peek": self.cov.peek_xy(),
            "danger_yaw": dec.threat_yaw,
            "last_harass_hit": self.last_harass_hit,
            "shootable": alt_shoot is not None,
        }
        gun = me.by_kind.get("gun")
        laser = me.by_kind.get("laser")
        light = me.by_kind.get("lightning")
        ctx["reloading"] = gun is not None and gun.reload > 0 and len(me.weapons) == 1
        ready = True
        if gun is not None and (gun.reload > 0 or (gun.ammo is not None and gun.ammo < 2)):
            ready = (laser is not None and laser.cooldown <= 0.1 and t >= th.short_until) or \
                (light is not None and light.cooldown <= 0.1 and target is not None and self._dist(me, target) <= 45.0)
        ctx["weapon_ready"] = ready

        # Exposure accounting for corner peeking
        exposed = any(e.sees_us for e in enemies)
        if exposed and self.expose_t is None:
            self.expose_t, self.expose_hp = t, me.hp
        elif not exposed:
            self.expose_t, self.expose_hp = None, None
        ctx["hurt_in_exposure"] = self.expose_hp is not None and self.expose_hp - me.hp > max(320.0, 0.24 * me.max_hp)

        intent = D.PLAYS[play].act(sit, ctx)
        hx = self.cov.hide_xy()
        intent.cover_near = wall_beside or (hx is not None and math.hypot(hx[0] - me.pos[0], hx[1] - me.pos[1]) <= 12.0)
        dec.intent = intent
        self.healing = False

        cube_sz = float((self.cfg.get("cubes") or {}).get("size_per_cube", 0.05))
        if target is not None:
            dec.target = Target.from_track(target, t, cube_sz)
            # If the chosen target is behind a wall, shoot whoever IS visible right now (including during BREAK!)
            if not target.los and alt_shoot is not None:
                dec.target = Target.from_track(alt_shoot, t, cube_sz)
        elif alt_shoot is not None and play != D.REPOSITION:
            dec.target = Target.from_track(alt_shoot, t, cube_sz)

        # ---------------- the air: brief pop-ups, roof edges, last-circle roofs (never exposed WallHop!)
        watchers = self.watchers(me)
        if self._air(me, obs, dec, play, target, calc, enemies, slack):
            dec.label = "air: " + self.air.name if self.air is not None else (intent.label or "")
            return dec

        # ---------------- per-play movement
        if target is not None and play != D.REPOSITION:
            self.cov.want_hide(me, watchers, t, max_cost=65.0)
            self.cov.want_peek(Target.from_track(target, t, cube_sz).center(), t)

        if play == D.ASSAULT:
            if intent.goal is not None:
                far = math.hypot(intent.goal[0] - me.pos[0], intent.goal[1] - me.pos[1]) > 110.0
                self.mv.go(intent.goal, me, z, prefer_cover=far, tag="assault", replan_dist=8.0)
            else:
                self.mv.clear()
            dec.speed = intent.speed
            dec.evasive = intent.evasive
        elif play == D.CORNER:
            self.cov.want_hide(me, watchers, t, max_cost=65.0)
            if target is not None:
                self.cov.want_peek(Target.from_track(target, t, cube_sz).center(), t)
            if intent.spot == "hide" and self.prev_corner_spot == "peek":
                self.cov.peek_used()        # never step out of the same edge twice running
            self.prev_corner_spot = intent.spot
            if intent.goal is not None:
                self.mv.go(intent.goal, me, z, prefer_cover=False, tag="corner-" + str(intent.spot), replan_dist=2.5)
            elif hx is not None and math.hypot(hx[0] - me.pos[0], hx[1] - me.pos[1]) <= 12.0:
                # One-step cover available while peek finishes computing: stay right beside cover while firing
                self.mv.go(hx, me, z, prefer_cover=False, tag="corner-step", replan_dist=3.0)
            else:
                self.mv.clear()
            dec.evasive = intent.goal is None or intent.spot == "peek"
            dec.speed = "walk"
        elif play == D.HARASS:
            self.cov.want_hide(me, watchers, t, max_cost=45.0)
            if intent.goal is not None:
                self.mv.go(intent.goal, me, z, prefer_cover=False, tag="harass", replan_dist=3.0)
            else:
                self.mv.clear()
            dec.evasive = True
        elif play == D.BREAK:
            hide = self.cov.hide_xy()
            hit_now = t - th.t_taken <= 1.2
            if hit_now and hide is not None and math.hypot(hide[0] - me.pos[0], hide[1] - me.pos[1]) < 3.0:
                # Hit at our "hidden" node: invalidate and find fresh cover
                self.cov.reset()
                hide = None
            if watchers:
                self.cov.want_hide(me, watchers, t, max_cost=105.0)
            hide = self.cov.hide_xy() if hide is not None or self.cov.hide is not None else None

            # Fighting retreat: keep shooting visible attackers at full 1.0x movement speed until LOS breaks!
            vis_shooter = target if (target is not None and target.los) else alt_shoot
            if vis_shooter is not None:
                dec.target = Target.from_track(vis_shooter, t, cube_sz)
                intent.fire = "long"
                intent.no_laser_start = True
                intent.label = "break: fighting"
            else:
                dec.target = None
                intent.fire = "none"

            # Kite away from close enemy lightning (< 50 m) if cover isn't right next to us or leads into them
            light_threat = next(
                (e for e in enemies if e.has("lightning") and self._dist(me, e) < 50.0),
                None,
            )
            kite_goal = None
            if light_threat is not None and not me.has("lightning"):
                lp = light_threat.est_pos(t)
                dl = math.hypot(lp[0] - me.pos[0], lp[1] - me.pos[1])
                if hide is None or math.hypot(hide[0] - me.pos[0], hide[1] - me.pos[1]) > 14.0 \
                        or math.hypot(hide[0] - lp[0], hide[1] - lp[1]) < dl:
                    kite_goal = self.displacement(me, bearing(me.pos, lp), dist=24.0, away_only=True)

            if kite_goal is not None:
                self._go_instant_or_plan(kite_goal, me, z, "kite-light")
            elif hide is not None:
                self.mv.go(hide, me, z, prefer_cover=False, tag="break", replan_dist=4.0)
            elif (hit_now or sit.unseen_hit) and th.last_damage is not None and th.last_damage[1] is not None:
                goal = self.displacement(me, th.last_damage[1], dist=15.0)
                if goal is not None:
                    self._go_instant_or_plan(goal, me, z, "break-disp")
                else:
                    self.mv.go(self.hold_spot(me), me, z, prefer_cover=True, tag="hold")
            else:
                self.mv.go(self.hold_spot(me), me, z, prefer_cover=True, tag="hold")

            dec.speed = "boost" if me.energy > 18 and not self.mv.arrived(me) else "walk"
            dec.evasive = not self.mv.arrived(me) and (
                any(e.sees_us or e.los for e in enemies) or sit.unseen_hit or t - th.t_taken <= 2.5
            )
            dec.face = dec.threat_yaw
        else:
            self._reposition(me, obs, dec, watchers, slack)

        # ---------------- counters to lasers & mortars
        self._counter_laser(me, dec, lt, enemies)
        self._counter_mortar(me, dec)
        if t < self.mortar_jink_until:
            dec.evasive = True

        # Zone safety override when outside current circle or when slack runs tight
        cx, cy, rs = self.mv.safe_circle(z)
        out_now = (not me.in_zone) or z.margin_at(me.pos[0], me.pos[1], t) < 2.0
        if out_now or (slack < 15.0 and math.hypot(me.pos[0] - cx, me.pos[1] - cy) > rs):
            self.mv.go(self.hold_spot(me), me, z, prefer_cover=(not out_now and slack > 8.0), tag="zone", replan_dist=8.0)
            dec.hold = False
            if out_now and me.energy > 15.0:
                dec.speed = "boost"
            if intent.fire == "long" and (dec.target is None or self._dist_pt(me, dec.target.pos) > 90.0):
                intent.fire = "short"
            intent.label = (intent.label or "") + " >zone"

        if lt is not None and play != D.ASSAULT:
            dec.evasive = True
        if intent.fire == "single" and dec.target is not None and gun is not None and gun.ammo is not None:
            for e in obs.get("events") or []:
                if e.get("type") == "hit" and e.get("target") == dec.target.id:
                    self.last_harass_hit = t
        if dec.face is None and dec.threat_yaw is not None:
            dec.face = dec.threat_yaw
        dec.label = intent.label if intent is not None else ""
        if self.loot_note and play == D.REPOSITION:
            dec.label += " " + self.loot_note
        return dec

    @staticmethod
    def _dist_pt(me, p):
        return math.hypot(p[0] - me.pos[0], p[1] - me.pos[1])

    def _go_instant_or_plan(self, goal, me, z, tag):
        """For short walkable displacements, set a 2-node path immediately (0 ms delay) if possible."""
        if self.w.walkable(me.pos[0], me.pos[1], goal[0], goal[1], need=2.6, skip_start=1.0):
            n0 = self.w.nearest_free(me.pos[0], me.pos[1], 3)
            n1 = self.w.nearest_free(goal[0], goal[1], 3)
            if n0 is not None and n1 is not None and n0 != n1:
                self.mv.go_path([n0, n1], tag)
                return
        self.mv.go(goal, me, z, prefer_cover=False, tag=tag, replan_dist=3.0)

    def _reposition(self, me, obs, dec, watchers, slack):
        """No active enemy duel: collect weapons/kill-drops/cubes, farm crates & NPCs on the move,
        heal behind cover when wounded under pressure, contest/take drops, and patrol circle edge."""
        t = me.t
        z = self.zone
        intent = dec.intent
        self.loot_note = ""

        near_active = [tr for tr in self.th.active_mechs(t, 6.0) if self._dist(me, tr) < 75.0]
        under_pressure = bool(near_active) or any(tr.sees_us for tr in self.th.mechs()) or (t - self.th.t_taken <= 3.5)
        # Immediately after a bruising fight with another shooter nearby (near sound or active contact),
        # duck out of sight to start regen before chasing distant ordinary cubes.
        close_sound = any(t - c.t <= 6.0 and c.band in ("near", "mid") for c in self.th.contacts)
        hurt_after_fight = (t - self.th.t_fight < 12.0) and (under_pressure or close_sound) and me.hp_frac < 0.65

        # 1. Weapons, kill drops, and cubes (walking & picking up items never interrupts HP regeneration!)
        if not under_pressure and slack > 13.0:
            lc = self.loot_choice(me, obs)
            if lc is not None:
                kind, lid, pos, drop, in_reach = lc
                d_loot = math.hypot(pos[0] - me.pos[0], pos[1] - me.pos[1])
                high_value = kind in ("pickup", "killdrop") or d_loot <= 14.0
                if not hurt_after_fight or high_value:
                    self.loot_note = kind
                    if kind == "pickup" and in_reach:
                        dec.take = lid
                        dec.drop = drop
                    self.mv.go(pos, me, z, prefer_cover=False, tag="loot-%s" % lid, replan_dist=2.0)
                    dec.speed = "boost" if (kind in ("pickup", "killdrop") and d_loot > 12.0 and me.energy > 42.0) \
                        or (t < 25.0 and d_loot > 16.0 and me.energy > 55.0) else "walk"
                    intent.label = "loot"
                    return

        # 2. Heal behind cover when genuinely hurt (or right after a fight with shooters around)
        want_heal = (me.hp_frac < 0.88 and (under_pressure or hurt_after_fight)) or (me.hp_frac < 0.68)
        if want_heal and me.in_zone and slack > 12.0:
            self.healing = True
            intent.fire = "none"
            intent.label = "heal"
            w25 = self.th.threat_points(me, 25.0)
            if w25:
                self.cov.want_hide(me, w25, t, max_cost=80.0)
                hide = self.cov.hide_xy()
                if hide is not None:
                    self.mv.go(hide, me, z, prefer_cover=False, tag="heal-hide", replan_dist=4.0)
                    dec.speed = "boost" if under_pressure and me.energy > 25 else "walk"
                    return
            goal = self.hold_spot(me)
            self.mv.go(goal, me, z, prefer_cover=True, tag="hold")
            dec.speed = "walk"
            return

        # 3. Farm crates & NPC drones aggressively while moving toward their drops!
        enemy_within_135 = any(self._dist(me, tr) < 135.0 for tr in self.th.active_mechs(t, 8.0))
        if not under_pressure and not enemy_within_135 and slack > 14.0:
            allow_npc = not any(self._dist(me, tr) < 155.0 for tr in self.th.active_mechs(t, 8.0))
            sc = self.shoot_choice(me, obs, allow_npc=allow_npc)
            if sc is not None:
                self.loot_note = "shoot %s" % sc.kind
                dec.target = sc
                intent.fire = "long"
                d = math.hypot(sc.pos[0] - me.pos[0], sc.pos[1] - me.pos[1])
                if sc.kind == "crate":
                    # Walk right onto the crate while shooting it so we scoop its cube + weapon drop immediately!
                    if d > 3.5:
                        self.mv.go((sc.pos[0], sc.pos[1]), me, z, prefer_cover=False,
                                   tag="crate-%s" % sc.id, replan_dist=2.5)
                        dec.speed = "boost" if d > 28.0 and me.energy > 58.0 else "walk"
                    else:
                        self.mv.clear()
                else:
                    # NPC drone (2 cubes): strafe evasively, close once weakened to scoop both cubes
                    if sc.track is not None and sc.track.hp is not None and sc.track.hp < 150.0 and d > 6.0:
                        self.mv.go((sc.pos[0], sc.pos[1]), me, z, prefer_cover=False,
                                   tag="npc-%s" % sc.id, replan_dist=3.0)
                    elif d > 52.0:
                        self.mv.go((sc.pos[0], sc.pos[1]), me, z, prefer_cover=False,
                                   tag="npc-%s" % sc.id, replan_dist=4.0)
                    else:
                        self.mv.clear()
                    dec.evasive = True
                intent.label = "farm %s" % sc.kind
                return

        # 4. Supply drops (take uncontested drops; hold covered ambush angle near contested ones)
        if not under_pressure and slack > 18.0:
            drop_act = self.drop_choice(me, obs)
            if drop_act is not None:
                mode_d, dpos = drop_act
                self.loot_note = mode_d
                self.mv.go(dpos, me, z, prefer_cover=(mode_d == "ambush-drop"), tag=mode_d, replan_dist=3.5)
                dec.speed = "boost" if mode_d == "drop" and me.energy > 55.0 else "walk"
                intent.label = mode_d
                return

        # 5. After a kill when enemies watched: shift to fresh cover once nearby drops are cleared
        if t < self.relocate_until and watchers:
            self.cov.want_hide(me, watchers, t, max_cost=100.0, away_from=self.relocate_from, min_dist=28.0)
            hide = self.cov.hide_xy()
            if hide is not None:
                self.mv.go(hide, me, z, prefer_cover=False, tag="relocate", replan_dist=4.0)
                intent.label = "relocate"
                dec.speed = "walk"
                return

        # 6. Patrol / hold strongpoint on the quiet edge of the next circle
        goal = self.hold_spot(me)
        self.mv.go(goal, me, z, prefer_cover=True, tag="hold")
        arrived = self.mv.arrived(me)
        intent.label = "hold" if arrived else "to hold"
        # Sprint out of exposed spawns in the opening seconds while keeping reserves for a fight
        dec.speed = "boost" if (not arrived and t < 18.0 and me.energy > 55.0) else "walk"
        if arrived:
            dec.face = self.lookout_yaw(me)
            dec.hold = True

    # ------------------------------------------------------------------ counters
    def _counter_laser(self, me, dec, lt, enemies):
        """Enemy charging a laser toward us: inside lightning range short it; if 1-step cover is
        reachable within the remaining charge, boost behind it while firing gun; else boost-jink."""
        if lt is None:
            return False
        t = me.t
        p = lt.est_pos(t)
        d = math.hypot(p[0] - me.pos[0], p[1] - me.pos[1])
        light = me.by_kind.get("lightning")
        if light is not None and d <= light.stat("range", 45.0) * 0.95 and me.energy >= 5.0:
            dec.target = Target.from_track(lt, t)
            dec.intent.fire = "long"
            dec.intent.label = "short its laser"
            return True
        hide = self.cov.hide_xy()
        spd = me.boost_speed if me.energy > 12.0 else me.walk
        reach = spd * 0.65 + 1.2
        if hide is not None and math.hypot(hide[0] - me.pos[0], hide[1] - me.pos[1]) <= reach:
            self.mv.go(hide, me, self.zone, prefer_cover=False, tag="dodge-laser", replan_dist=2.0)
            dec.speed = "boost" if me.energy > 10.0 else "walk"
            dec.hold = False
            dec.intent.no_laser_start = True
            dec.intent.label = "duck the laser"
            return True
        dec.evasive = True
        dec.hold = False
        if me.energy > 25.0:
            dec.speed = "boost"
            dec.intent.no_laser_start = True
        dec.threat_yaw = bearing(me.pos, p)
        dec.intent.label = (dec.intent.label or "") + " jink"
        return True

    def _counter_mortar(self, me, dec):
        """Mortar launch heard: if slow/standing, immediately follow a walkable 13 m path across the
        bearing (via mover so shell-dodge & boost stay active); if already moving fast, jink."""
        t = me.t
        mh = self.th.mortar_heard
        if self.displace is not None:
            goal, until = self.displace
            if t < until and math.hypot(goal[0] - me.pos[0], goal[1] - me.pos[1]) > 1.8:
                self._go_instant_or_plan(goal, me, self.zone, "mortar-move")
                dec.speed = "boost" if me.energy > 20.0 else "walk"
                dec.hold = False
                dec.intent.label = "mortar: move"
                return True
            self.displace = None
        if mh is None or t - mh[0] > 0.35 or not me.grounded:
            return False
        if me.speed() > 3.0:
            self.mortar_jink_until = t + 1.6
            return False
        goal = self.displacement(me, mh[1], dist=13.5)
        if goal is None:
            return False
        self.displace = (goal, t + 2.2)
        self._go_instant_or_plan(goal, me, self.zone, "mortar-move")
        dec.speed = "boost" if me.energy > 20.0 else "walk"
        dec.hold = False
        dec.intent.label = "mortar: move"
        return True

    def displacement(self, me, bearing_yaw, dist=13.5, away_only=False):
        """A walkable point `dist` m away, preferring across `bearing_yaw` (or away if `away_only`)."""
        x, y = me.pos[0], me.pos[1]
        side = 1 if self.rng.random() < 0.5 else -1
        if away_only:
            options = [
                bearing_yaw + math.pi,
                bearing_yaw + math.pi + side * 0.45,
                bearing_yaw + math.pi - side * 0.45,
                bearing_yaw + side * math.pi / 2,
                bearing_yaw - side * math.pi / 2,
            ]
        else:
            options = [
                bearing_yaw + side * math.pi / 2,
                bearing_yaw - side * math.pi / 2,
                bearing_yaw + math.pi + side * 0.6,
                bearing_yaw + math.pi - side * 0.6,
                bearing_yaw + math.pi,
            ]
        for d_try in (dist, dist * 0.65):
            for a in options:
                gx, gy = x + math.cos(a) * d_try, y + math.sin(a) * d_try
                if self.w.walkable(x, y, gx, gy, need=2.7, skip_start=1.0):
                    return (gx, gy)
        return None

    # ------------------------------------------------------------------ the air
    def _air(self, me, obs, dec, play, target, calc, enemies, slack):
        """Air plays: brief laser pop-ups behind cover, roof fighting/drops, and uncontested final-circle
        roofs. Never climbs a wall while chased or in sight of an enemy."""
        t = me.t
        w = self.w
        z = self.zone
        intent = dec.intent
        mech_cfg = self.cfg.get("mech", {})
        on_roof = me.grounded and w.roof_at(me.pos[0], me.pos[1]) is not None and \
            me.pos[2] > w.floor(me.pos[0], me.pos[1]) + 4.0
        final_roof = None
        if z.next_radius <= 25.0:
            final_roof = A.LastCircleRoof.find(w, me, z.next_center, max(z.next_radius, 6.0), mech_cfg)

        if self.air is not None:
            if self.air.done:
                if isinstance(self.air, A.DropAttack):
                    self.roof_mode = False
                self.air = None
            else:
                self.mv.clear()
                self.mv.allow_roof = True
                if isinstance(self.air, A.PopUp):
                    intent.fire = "long"
                    intent.spot = "peek"
                return True

        # 1. Last circle closing on a building: climb onto that roof unless under direct LOS fire with room to spare
        direct_los_fire = any(e.los for e in enemies) and z.radius > 35.0
        if final_roof is not None and t >= self.air_ban_until:
            fx, fy, fh = final_roof
            if on_roof and abs(me.pos[2] - fh) < 1.5:
                self.roof_mode = True
                self.mv.allow_roof = True
                if math.hypot(fx - me.pos[0], fy - me.pos[1]) > 0.4:
                    dec.direct = (fx, fy)
                intent.label = "last circle: roof"
                return True
            if not direct_los_fire:
                hop = A.WallHop.find(w, me, max_climb=56.0, cfg_mech=mech_cfg, reach=14.0)
                if hop is not None and abs(hop[2] - fh) < 1.5:
                    self.air = A.WallHop()
                    self.air.start(t, hop)
                    self.roof_mode = True
                    return True
                if z.radius <= 40.0:
                    self.mv.go((fx, fy), me, z, prefer_cover=False, tag="last-roof-wall", replan_dist=4.0)
                    dec.hold = False
                    if me.energy > 20.0:
                        dec.speed = "boost"
                    intent.label = "last circle: to roof"
                    return False

        # 2. On a roof: fight from its edge (respecting -0.7 rad pitch), drop-attack with lightning, or step down
        if self.roof_mode and on_roof:
            self.mv.allow_roof = True
            leave = slack < 20.0 and final_roof is None
            if any(e.has("mortar") and self._dist(me, e) <= 235.0 for e in enemies):
                leave = True
            if target is not None and not leave:
                tp = target.est_pos(t)
                light = me.by_kind.get("lightning")
                if light is not None and calc is not None and (calc.ratio >= 1.0 or calc.finishable) and \
                        play in (D.ASSAULT, D.CORNER):
                    land = A.DropAttack.find(w, me, tp, target.yaw, light.stat("range", 45.0))
                    if land is not None:
                        self.air = A.DropAttack()
                        self.air.start(t, land, me.pos[2])
                        return True
                if self.roof_spots is None or t - self.roof_spots[2] > 1.5:
                    res = A.RoofEdge.find(w, me, (tp[0], tp[1], tp[2] + 4.5))
                    self.roof_spots = (res[0], res[1], t) if res else None
                if self.roof_spots is not None and play in (D.CORNER, D.ASSAULT, D.HARASS, D.BREAK):
                    peek, hide = self.roof_spots[0], self.roof_spots[1]
                    gun = me.by_kind.get("gun")
                    ready = gun is None or (gun.reload <= 0 and (gun.ammo or 0) > 2)
                    hurt = self.expose_hp is not None and self.expose_hp - me.hp > 0.2 * me.max_hp
                    if play == D.BREAK or not ready or hurt:
                        dec.direct = hide
                        if target.los:
                            intent.fire = "long"
                            intent.no_laser_start = True
                        else:
                            intent.fire = "none"
                            intent.reload_ok = True
                        intent.label = "roof: back"
                    else:
                        dec.direct = peek
                        intent.fire = "long"
                        intent.label = "roof: edge"
                    self.mv.clear()
                    return True
            if any(e.sees_us for e in enemies):
                leave = True
            if not leave and (me.hp_frac < 0.93 or target is not None):
                dec.hold = True
                intent.fire = "none" if target is None else intent.fire
                intent.label = "roof: heal"
                self.mv.clear()
                return True
            self.roof_mode = False
            self.mv.allow_roof = False
            return False

        if not on_roof:
            self.roof_mode = self.roof_mode and self.air is not None
            self.mv.allow_roof = False
        if t < self.air_ban_until or not me.grounded:
            return False

        # 3. Brief laser pop-up from behind a wall when nobody can see us
        laser = me.by_kind.get("laser")
        if laser is not None and t >= self.th.short_until and target is not None \
                and play in (D.ASSAULT, D.CORNER, D.HARASS) and not target.los \
                and not any(e.sees_us for e in enemies) and laser.cooldown <= 0.1 and t - target.t_seen < 3.0:
            tp = target.est_pos(t)
            d = math.hypot(tp[0] - me.pos[0], tp[1] - me.pos[1])
            if 40.0 <= d <= laser.stat("range", 280.0) * 0.9:
                rise = A.PopUp.find(w, me, (tp[0], tp[1], tp[2] + 4.5), A.PopUp.MAX_RISE, mech_cfg)
                if rise is not None:
                    self.air = A.PopUp()
                    self.air.start(t, rise)
                    self.air_ban_until = t + 6.0
                    intent.fire = "long"
                    intent.spot = "peek"
                    return True
        return False

    def drop_choice(self, me, obs):
        """Return ('drop', xy) for a quiet supply drop, or ('ambush-drop', xy) to contest one from cover."""
        z = self.zone
        cx, cy, rs = self.mv.safe_circle(z)
        best = None
        for d in obs.get("drops") or []:
            p = d.get("pos")
            if p is None:
                continue
            dist = math.hypot(p[0] - me.pos[0], p[1] - me.pos[1])
            if dist > 180.0 or math.hypot(p[0] - cx, p[1] - cy) > rs:
                continue
            eta = dist / 11.5
            if d.get("lands_in", 0) > eta + 10.0:
                continue
            holder = d.get("holder")
            crowded = holder not in (None, me.id) or any(
                me.t - tr.t_seen < 15.0 and math.hypot(tr.est_pos(me.t)[0] - p[0], tr.est_pos(me.t)[1] - p[1]) < 85.0
                for tr in self.th.mechs()
            )
            if not crowded:
                if best is None or dist < best[0]:
                    best = (dist, ("drop", (p[0], p[1])))
            elif me.hp_frac >= 0.80 and dist > 55.0:
                # Approach a covered cell ~65 m from the contested drop to third-party the claimant
                ux, uy = unit(me.pos[0] - p[0], me.pos[1] - p[1])
                ax, ay = p[0] + ux * 65.0, p[1] + uy * 65.0
                n = self.w.nearest_free(ax, ay, 10)
                if n is not None and self.w.main[n]:
                    sc = dist + 35.0
                    if best is None or sc < best[0]:
                        best = (sc, ("ambush-drop", self.w.node_xy(n)))
        return best[1] if best else None
