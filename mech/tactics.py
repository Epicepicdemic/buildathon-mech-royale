"""What to do this decision: the opening, fights (via the doctrine), healing, loot, positioning
and the last circles. Produces an Intent plus the movement and item choices that go with it.

The same rules run in every match. Nothing here tries to tell practice from the final.
"""
import math

import random
import time

from . import air as A
from . import doctrine as D
from . import positions as P
from .cover import HideSearch, LowExposureSearch
from .geom import angdiff, bearing, wrap
from .gunner import Target

UPGRADE_PRIORITY = {
    "plating": 100, "nanites": 95, "armor": 90,
    "laser_damage": 70, "laser_charge": 68, "lightning_efficiency": 66, "gun_rate": 64, "gun_damage": 63,
    "gun_mag": 55, "lightning_range": 54, "gun_velocity": 52, "lightning_chain": 45,
    "servos": 50, "capacitor": 48, "jets": 44, "reactor": 42, "gyros": 40, "sensors": 38,
    "mortar_reload": 20, "mortar_velocity": 18, "mortar_splash": 16, "laser_pierce": 30,
}
WANTED_WEAPONS = ("laser", "lightning")     # in order of preference: kills happen at range


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
        self.opening_done = False
        self.opening_search = None
        self.opening_goal = None
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
        self.drop_target = None
        self.look_phase = 0.0
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
        self.second_goal = None      # the opening's second, unseen position
        self.second_search = None
        self.spawn = None
        self.first_reached = False

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _dist(me, tr):
        p = tr.est_pos(me.t)
        return math.hypot(p[0] - me.pos[0], p[1] - me.pos[1])

    def _shootable(self, me, enemies):
        """The best enemy we can see and reach with a weapon now: whoever hit us last, else the nearest."""
        best = None
        for e in enemies:
            if not e.los or not e.in_obs:
                continue
            d = self._dist(me, e)
            if d > 230.0:
                continue
            score = d - (200.0 if me.t - e.t_hit_us <= 2.0 else 0.0)
            if best is None or score < best[0]:
                best = (score, e)
        return best[1] if best else None

    def _me_weapons(self, me):
        return {w.kind: w.stats for w in me.weapons}

    def situation(self, me, enemy, others, corner):
        unseen = self.th.unseen_attacker(me.t, 2.0)
        g = me.by_kind.get("gun")
        return D.Situation(me.t, me.xy, me.yaw, me.hp, me.max_hp, self._me_weapons(me), enemy, others,
                           me.cubes, me.energy, me.armor, corner, unseen is not None and (enemy is None or not enemy.los),
                           enemy.los if enemy is not None else False, self.cfg, not me.grounded, self.healing,
                           g.ammo if g is not None else None)

    def watchers(self, me, window=6.0):
        return self.th.threat_points(me, window)

    def danger_yaw(self, me):
        """Bearing of the most dangerous attacker (recent damage first)."""
        ld = self.th.last_damage
        if ld is not None and me.t - ld[0] <= 2.5 and ld[1] is not None:
            tr = self.th.tracks.get(ld[2])
            if tr is not None and tr.in_obs:
                p = tr.est_pos(me.t)
                return bearing(me.pos, p)
            return ld[1]
        best = None
        for tr in self.th.active_mechs(me.t, 5.0):
            p = tr.est_pos(me.t)
            d = math.hypot(p[0] - me.pos[0], p[1] - me.pos[1])
            score = d - (80 if tr.sees_us else 0) - (60 if tr.charging else 0)
            if best is None or score < best[0]:
                best = (score, bearing(me.pos, p))
        if best:
            return best[1]
        for c in self.th.contacts:
            if me.t - c.t <= 3.0:
                return c.bearing
        return None

    def laser_threat(self, me):
        """An enemy charging a laser that points at us and can see us."""
        for tr in self.th.active_mechs(me.t, 1.0):
            if tr.charging and tr.in_obs:
                b = bearing(tr.pos, me.pos)
                if angdiff(tr.yaw, b) < 0.12:
                    return tr
        return None

    # ------------------------------------------------------------------ positions
    def hold_spot(self, me):
        """A covered cell inside the next circle, scored against the current threats and drawn at
        random among the best few (mech/positions.py). Kept for a while once chosen."""
        z = self.zone
        cx, cy, rs = self.mv.safe_circle(z)
        key = (z.version, round(cx), round(cy))
        if self.hold_goal is not None and key == self.hold_key and me.t - self.hold_t < 25.0 \
                and math.hypot(self.hold_goal[0] - cx, self.hold_goal[1] - cy) <= rs:
            return self.hold_goal
        threats = self.th.threat_points(me, 20.0)
        drones = [tr.home for tr in self.th.npcs() if tr.home is not None]
        n = P.choose_hold(self.w, me.xy, (cx, cy, max(2.0, rs - 4.0)), threats, self.rng, drones=drones)
        if n is None:
            n = self.w.nearest_free(cx, cy, 30)
        self.hold_goal = self.w.node_xy(n) if n is not None else (cx, cy)
        self.hold_key = key
        self.hold_t = me.t
        return self.hold_goal

    def lookout_yaw(self, me):
        """Where to look while holding: toward the zone centre, sweeping 50 degrees either way."""
        z = self.zone
        d = math.hypot(z.next_center[0] - me.pos[0], z.next_center[1] - me.pos[1])
        if d > 15:
            base = bearing(me.pos, z.next_center)
        else:
            n = self.w.nearest_free(me.pos[0], me.pos[1], 3)
            base = me.yaw
            if n is not None:
                best = None
                for k in range(self.w.nfan):
                    yaw = 2 * math.pi * k / self.w.nfan
                    f = self.w.fan_dist(n, yaw)
                    if best is None or f > best[0]:
                        best = (f, yaw)
                base = best[1]
        self.look_phase += 0.1
        return wrap(base + 0.85 * math.sin(self.look_phase * 0.6))

    # ------------------------------------------------------------------ loot
    def loot_choice(self, me, obs):
        """(goal xy, take id, drop slot, crate/npc Target) for safe opportunities, or None."""
        t = me.t
        best = None
        kinds = [w.kind for w in me.weapons]
        reach = float((self.cfg.get("loot") or {}).get("pickup_radius", 4.0))
        for p in obs.get("pickups") or []:
            k = p.get("kind")
            pp = p.get("pos")
            if k not in WANTED_WEAPONS or k in kinds:
                if not (k == "mortar" and not me.slots_free == [] and len(kinds) < 2 and t > 90 and "mortar" not in kinds
                        and not any(x in kinds for x in WANTED_WEAPONS)):
                    continue
            drop = None
            if not me.slots_free:
                # swap a mortar for any wanted weapon, and lightning for a laser; never drop the gun
                mort = me.by_kind.get("mortar")
                light = me.by_kind.get("lightning")
                if mort is not None and k in WANTED_WEAPONS:
                    drop = mort.slot
                elif light is not None and k == "laser":
                    if light.cooldown > 0.2:
                        continue
                    drop = light.slot
                else:
                    continue
            d = math.hypot(pp[0] - me.pos[0], pp[1] - me.pos[1])
            if d > 80 or abs(pp[2] - me.pos[2]) > 6:
                continue
            d_eff = d * (0.7 if k == "laser" else 1.0)
            if best is None or d_eff < best[0]:
                best = (d_eff, ("pickup", p["id"], (pp[0], pp[1]), drop, abs(pp[2] - me.pos[2]) <= 4 and d <= reach - 0.3))
        for c in obs.get("cubes") or []:
            cp = c.get("pos")
            d = math.hypot(cp[0] - me.pos[0], cp[1] - me.pos[1])
            if d > 55 or abs(cp[2] - me.pos[2]) > 3.5:
                continue
            if best is None or d * 0.8 < best[0]:
                best = (d * 0.8, ("cube", c["id"], (cp[0], cp[1]), None, False))
        if best is not None:
            return best[1]
        return None

    def shoot_choice(self, me, obs, quiet):
        """A crate or NPC drone worth shooting now, as a Target (or None)."""
        t = me.t
        gun = me.by_kind.get("gun")
        light = me.by_kind.get("lightning")
        best = None
        if not quiet:
            return None
        for c in obs.get("crates") or []:
            cp = c.get("pos")
            if self.crate_ban.get(c["id"], -1e9) > t:
                continue
            d = math.hypot(cp[0] - me.pos[0], cp[1] - me.pos[1])
            if d > 70 or (gun is None and (light is None or d > 40)):
                continue
            tg = Target(c["id"], "crate", (cp[0], cp[1], cp[2]), (0.0, 0.0, 0.0), 1.4, 2.4, True)
            if not self.w.shot_clear(me.muzzle, tg.center()):
                self.crate_ban[c["id"]] = t + 3.0
                continue
            if best is None or d < best[0]:
                best = (d, tg)
        if best is None and me.hp_frac >= 0.7 and gun is not None:
            for tr in self.th.npcs():
                if not tr.in_obs or not tr.los:
                    continue
                if self.npc_ban.get(tr.id, -1e9) > t:
                    continue
                p = tr.pos
                d = math.hypot(p[0] - me.pos[0], p[1] - me.pos[1])
                if d > 140:
                    continue
                tg = Target.from_track(tr, t)
                if best is None or d + 40 < best[0]:
                    best = (d + 40, tg)
        return best[1] if best else None

    # ------------------------------------------------------------------ main
    def decide(self, me, obs):
        t = me.t
        dec = Decision()
        if self.spawn is None:
            self.spawn = tuple(me.pos)
        th = self.th
        z = self.zone
        # offers
        offer = obs.get("offer")
        if offer and offer.get("choices"):
            ch = offer["choices"]
            dec.pick = max(range(len(ch)), key=lambda i: UPGRADE_PRIORITY.get(ch[i].get("id"), 10))
        if th.kills > self.kills_seen:
            self.kills_seen = th.kills
            self.relocate_until = t + 9.0
            self.relocate_from = (me.pos[0], me.pos[1])
            self.cov.reset()
        # enemies we know about now
        active = th.active_mechs(t, 6.0)
        enemies = [e for e in active if not e.phantom or t - e.t_hit_us <= 4.0]
        corner_ready = self.cov.hide is not None and self.cov.peek is not None
        target, calc = D.pick_target(lambda e: self.situation(me, e, [o for o in enemies if o is not e], corner_ready),
                                     enemies, t, me.xy)
        others = [o for o in enemies if o is not target and
                  (o.sees_us or t - o.t_hit_us <= 3.0 or (o.los and self._dist(me, o) <= 100.0))]
        sit = self.situation(me, target, others, corner_ready)
        play, calc = D.choose_play(sit, calc if target is not None else None, prev=self.last_play)
        slack = self.mv.slack(me, z)
        hp_frac = me.hp_frac
        dec.threat_yaw = self.danger_yaw(me)
        lt = self.laser_threat(me)
        # zone pressure overrides lingering fights
        if slack < 4.0 and play in (D.CORNER, D.HARASS):
            play = D.ASSAULT if (calc is not None and calc.ratio >= 1.0 and calc.dist < 60) else D.BREAK
        # broke contact and nobody is on us: back to normal business (healing first)
        if play == D.BREAK and target is not None and not any(e.sees_us for e in enemies) \
                and t - max([e.t_hit_us for e in enemies] + [-1e9]) > 6.0 and me.hp_frac >= 0.85 \
                and not sit.unseen_hit:
            play = D.REPOSITION
        if play != self.last_play:
            self.last_play = play
            self.play_since = t
        ctx = {"calc": calc, "hide": self.cov.hide_xy(), "peek": self.cov.peek_xy(), "danger_yaw": dec.threat_yaw,
               "last_harass_hit": self.last_harass_hit}
        gun = me.by_kind.get("gun")
        laser = me.by_kind.get("laser")
        ctx["reloading"] = gun is not None and gun.reload > 0 and len(me.weapons) == 1
        ready = True
        if gun is not None and (gun.reload > 0 or (gun.ammo is not None and gun.ammo < 3)):
            ready = laser is not None and laser.cooldown <= 0.1
        ctx["weapon_ready"] = ready
        # exposure accounting for the corner fight
        exposed = any(e.sees_us for e in enemies)
        if exposed and self.expose_t is None:
            self.expose_t, self.expose_hp = t, me.hp
        elif not exposed:
            self.expose_t, self.expose_hp = None, None
        ctx["hurt_in_exposure"] = self.expose_hp is not None and self.expose_hp - me.hp > max(300.0, 0.22 * me.max_hp)
        intent = D.PLAYS[play].act(sit, ctx)
        hx = self.cov.hide_xy()
        intent.cover_near = hx is not None and math.hypot(hx[0] - me.pos[0], hx[1] - me.pos[1]) <= 12.0
        dec.intent = intent
        self.healing = False
        if target is not None:
            dec.target = Target.from_track(target, t, float((self.cfg.get("cubes") or {}).get("size_per_cube", 0.05)))
            # return fire: if the chosen target cannot be shot now, shoot whoever can be (attackers first)
            if play in (D.ASSAULT, D.CORNER, D.HARASS) and not target.los:
                alt = self._shootable(me, enemies)
                if alt is not None:
                    dec.target = Target.from_track(alt, t)
        # ---------------- the air: a manoeuvre in progress, or one worth starting
        watchers = self.watchers(me)
        if self._air(me, obs, dec, play, target, calc, enemies, slack):
            dec.label = "air: " + self.air.name if self.air is not None else (intent.label or "")
            return dec
        # ---------------- per play movement
        # keep a corner against the primary enemy ready, whatever the play, so the doctrine can use it
        if target is not None and play != D.REPOSITION:
            self.cov.want_hide(me, watchers, t, max_cost=60.0)
            self.cov.want_peek(Target.from_track(target, t).center(), t)
        if play == D.ASSAULT:
            if intent.goal is not None:
                far = math.hypot(intent.goal[0] - me.pos[0], intent.goal[1] - me.pos[1]) > 120
                self.mv.go(intent.goal, me, z, prefer_cover=far, tag="assault", replan_dist=10.0)
            else:
                self.mv.clear()
            dec.speed = intent.speed
            dec.evasive = intent.evasive
        elif play == D.CORNER:
            self.cov.want_hide(me, watchers, t, max_cost=60.0)
            if target is not None:
                self.cov.want_peek(Target.from_track(target, t).center(), t)
            if intent.spot == "hide" and self.prev_corner_spot == "peek":
                self.cov.peek_used()        # never step out of the same edge twice running
            self.prev_corner_spot = intent.spot
            if intent.goal is not None:
                self.mv.go(intent.goal, me, z, prefer_cover=False, tag="corner-" + str(intent.spot), replan_dist=2.5)
            else:
                self.mv.clear()
            dec.evasive = intent.goal is None or intent.spot == "peek"
            dec.speed = "walk"
        elif play == D.HARASS:
            self.cov.want_hide(me, watchers, t, max_cost=40.0)
            if intent.goal is not None:
                self.mv.go(intent.goal, me, z, prefer_cover=False, tag="harass", replan_dist=3.0)
            else:
                self.mv.clear()
            dec.evasive = True
        elif play == D.BREAK:
            hide = self.cov.hide_xy()
            hit_now = t - th.t_taken <= 1.0
            if hit_now and hide is not None and math.hypot(hide[0] - me.pos[0], hide[1] - me.pos[1]) < 3.0:
                # we are "hidden" and still being hit: our sight estimate is wrong here, find another spot
                self.cov.reset()
                hide = None
            if watchers:
                self.cov.want_hide(me, watchers, t, max_cost=100.0)
            hide = self.cov.hide_xy() if hide is not None or self.cov.hide is not None else None
            if hit_now:
                alt = self._shootable(me, enemies)
                if alt is not None:
                    # return fire while we go: it can see us anyway
                    dec.target = Target.from_track(alt, t)
                    intent.fire = "short"
            if hide is not None:
                self.mv.go(hide, me, z, prefer_cover=False, tag="break", replan_dist=4.0)
            else:
                self.mv.go(self.hold_spot(me), me, z, prefer_cover=True, tag="hold")
            dec.speed = "boost" if me.energy > 25 and not self.mv.arrived(me) else "walk"
            dec.evasive = not self.mv.arrived(me) and any(e.sees_us for e in enemies)
            dec.face = dec.threat_yaw
            # a laser that is already full can still go off on the way
            if laser is not None and laser.charge > 0 and target is not None and target.los:
                intent.fire = "long"
        else:
            self._reposition(me, obs, dec, watchers, slack)
        # ---------------- counters to what beats cover and dodging
        self._counter_laser(me, dec, lt, enemies)
        self._counter_mortar(me, dec)
        # the zone comes first: with little slack left, every play walks toward safety
        cx, cy, rs = self.mv.safe_circle(z)
        if slack < 15.0 and math.hypot(me.pos[0] - cx, me.pos[1] - cy) > rs:
            self.mv.go(self.hold_spot(me), me, z, prefer_cover=slack > 8.0, tag="zone", replan_dist=8.0)
            dec.hold = False
            if intent.fire == "long":
                intent.fire = "short"
            intent.label = (intent.label or "") + " >zone"
        if lt is not None and play != D.ASSAULT:
            dec.evasive = True
        if intent.fire == "single" and dec.target is not None and gun is not None and gun.ammo is not None:
            # count the harassing hits we land
            for e in obs.get("events") or []:
                if e.get("type") == "hit" and e.get("target") == dec.target.id:
                    self.last_harass_hit = t
        # torso: the danger bearing when nothing to shoot
        if dec.face is None:
            if dec.threat_yaw is not None:
                dec.face = dec.threat_yaw
        dec.label = intent.label if intent is not None else ""
        if self.loot_note and play == D.REPOSITION:
            dec.label += " " + self.loot_note
        return dec

    def _reposition(self, me, obs, dec, watchers, slack):
        """No fight: opening, relocating after a kill, healing, loot, supply drops, holding."""
        t = me.t
        z = self.zone
        intent = dec.intent
        self.loot_note = ""
        recent = [tr for tr in self.th.mechs() if t - max(tr.t_seen, tr.t_heard, tr.t_hit_us) <= 25.0]
        quiet = not recent and not [c for c in self.th.contacts if t - c.t <= 8.0]
        # 1. the opening: get out of sight first
        if not self.opening_done:
            if t > 35.0:
                self.opening_done = True
            elif watchers:
                self.cov.want_hide(me, watchers, t, max_cost=120.0)
                hide = self.cov.hide_xy()
                if hide is not None:
                    if math.hypot(hide[0] - me.pos[0], hide[1] - me.pos[1]) > 3.0 and self.second_goal is None:
                        self.mv.go(hide, me, z, prefer_cover=False, tag="open-hide", replan_dist=4.0)
                        dec.speed = "boost" if me.energy > 30 else "walk"
                        intent.label = "opening: hide"
                        return
                    # hidden: the hunter saw where we went; slip away quietly to a spot it did not see
                    if self._opening_second(me, dec, watchers):
                        return
                    self.opening_done = True
            else:
                if self.opening_search is None and self.opening_goal is None:
                    start = self.w.nearest_free(me.pos[0], me.pos[1], 6)
                    if start is not None:
                        self.opening_search = LowExposureSearch(self.w, start)
                if self.opening_search is not None and self.opening_search.done:
                    res = self.opening_search.result
                    self.opening_goal = self.w.node_xy(res[0]) if res else self.hold_spot(me)
                    self.opening_search = None
                if self.opening_goal is not None:
                    there = math.hypot(self.opening_goal[0] - me.pos[0], self.opening_goal[1] - me.pos[1]) < 3.0
                    self.first_reached = self.first_reached or there
                    if not self.first_reached:
                        self.mv.go(self.opening_goal, me, z, prefer_cover=False, tag="open-cover")
                        dec.speed = "boost"
                        intent.label = "opening: cover"
                        return
                    # in cover: keep going, silently, to a second spot nobody saw us reach
                    if self._opening_second(me, dec, watchers):
                        return
                    self.opening_done = True
                intent.label = "opening: searching"
                return
        # 2. cubes and weapons lying nearby when nobody is on us (walking does not stop healing),
        #    including the cubes a kill drops, before we move on
        near_active = [tr for tr in self.th.active_mechs(t, 6.0) if self._dist(me, tr) < 90.0]
        safe = not near_active and t - self.th.t_taken > 5.0 and not any(tr.sees_us for tr in self.th.mechs())
        # after a fight, a third mech waits for the winner: unless nothing has been heard nearby,
        # get out of sight and heal before collecting
        noisy = any(not tr.dead and t - max(tr.t_seen, tr.t_heard) <= 10.0 for tr in self.th.mechs()) or \
            any(t - c.t <= 10.0 and c.band != "far" for c in self.th.contacts)
        after_fight = t - self.th.t_fight < 30.0
        if after_fight and noisy and me.hp_frac < 0.93:
            safe = False
        if safe and slack > 15.0:
            lc = self.loot_choice(me, obs)
            if lc is not None:
                kind, lid, pos, drop, in_reach = lc
                self.loot_note = kind
                if kind == "pickup" and in_reach:
                    dec.take = lid
                    dec.drop = drop
                self.mv.go(pos, me, z, prefer_cover=False, tag="loot-%s" % lid, replan_dist=2.0)
                dec.speed = "walk"
                intent.label = "loot"
                return
        # 3. after a kill: move, the squad shares what it sees
        if t < self.relocate_until and watchers:
            self.cov.want_hide(me, watchers, t, max_cost=110.0, away_from=self.relocate_from, min_dist=30.0)
            hide = self.cov.hide_xy()
            if hide is not None:
                self.mv.go(hide, me, z, prefer_cover=False, tag="relocate", replan_dist=4.0)
                intent.label = "relocate"
                dec.speed = "walk"
                return
        # 4. heal: stay out of sight, hold fire
        want_heal = me.hp_frac < 0.93 and me.in_zone and slack > 12.0
        if want_heal:
            self.healing = True
            intent.fire = "none"
            intent.label = "heal"
            w30 = self.th.threat_points(me, 30.0)
            if w30:
                self.cov.want_hide(me, w30, t, max_cost=80.0)
                hide = self.cov.hide_xy()
                if hide is not None:
                    self.mv.go(hide, me, z, prefer_cover=False, tag="heal-hide", replan_dist=4.0)
                    dec.speed = "walk"
                    return
            # nobody known: heal on the way to (or at) the hold spot, silently
            goal = self.hold_spot(me)
            self.mv.go(goal, me, z, prefer_cover=True, tag="hold")
            dec.speed = "silent" if slack > 40 else "walk"
            return
        # 5. crates, drones and supply drops when it is quiet (shooting is heard at 320 m)
        if quiet and slack > 20.0:
            sc = self.shoot_choice(me, obs, quiet and t < 300)
            if sc is not None:
                self.loot_note = "shoot %s" % sc.kind
                dec.target = sc
                intent.fire = "long"
                self.mv.clear()
                if sc.kind == "npc":
                    d = math.hypot(sc.pos[0] - me.pos[0], sc.pos[1] - me.pos[1])
                    if d < 50 and sc.track is not None and not sc.track.aggro:
                        self.mv.go(self.hold_spot(me), me, z, tag="hold")
                    dec.evasive = sc.track is not None and sc.track.aggro
                return
            drop = self.drop_choice(me, obs)
            if drop is not None:
                self.loot_note = "drop"
                self.mv.go(drop, me, z, prefer_cover=False, tag="supply", replan_dist=3.0)
                dec.speed = "walk"
                return
        # 6. hold a strongpoint in the next circle
        goal = self.hold_spot(me)
        self.mv.go(goal, me, z, prefer_cover=True, tag="hold")
        intent.label = "hold" if self.mv.arrived(me) else "to hold"
        dec.speed = "walk"
        if self.mv.arrived(me):
            dec.face = self.lookout_yaw(me)
            dec.hold = True

    def _opening_second(self, me, dec, watchers):
        """After the first cover: walk at under 5 m/s (silent) to a cell hidden from the known threats
        and from where we spawned, at least 25 m on. True while doing so."""
        t = me.t
        if t > 32.0:
            return False
        if self.second_goal is None:
            if self.second_search is None:
                eyes = list(watchers)
                if self.spawn is not None:
                    eyes.append((self.spawn[0], self.spawn[1], self.spawn[2] + 8.0))
                start = self.w.nearest_free(me.pos[0], me.pos[1], 5)
                if start is None or not eyes:
                    return False
                self.second_search = HideSearch(self.w, start, eyes, max_cost=90.0, max_expo=0.3,
                                                away_from=(me.pos[0], me.pos[1]), min_dist=25.0,
                                                height=me.height, rng=self.rng)
            if not self.second_search.done:          # advanced by the brain inside the budget
                dec.hold = True
                dec.intent.label = "opening: watch"
                return True
            res = self.second_search.result
            self.second_search = None
            if res is None:
                return False
            self.second_goal = self.w.node_xy(res[0])
        if math.hypot(self.second_goal[0] - me.pos[0], self.second_goal[1] - me.pos[1]) < 3.0:
            return False
        self.mv.go(self.second_goal, me, self.zone, prefer_cover=True, tag="open-second", replan_dist=4.0)
        dec.speed = "silent"
        dec.intent.label = "opening: second"
        return True

    # ------------------------------------------------------------------ counters
    def _counter_laser(self, me, dec, lt, enemies):
        """A visible enemy charging a laser at us: inside lightning range, short it; otherwise get
        behind cover within the 0.75 s charge if a hidden cell is that close; otherwise jink."""
        if lt is None:
            return False
        t = me.t
        p = lt.est_pos(t)
        d = math.hypot(p[0] - me.pos[0], p[1] - me.pos[1])
        light = me.by_kind.get("lightning")
        if light is not None and d <= light.stat("range", 45.0) * 0.95 and me.energy >= 6:
            dec.target = Target.from_track(lt, t)
            dec.intent.fire = "long"
            dec.intent.label = "short its laser"
            return True
        hide = self.cov.hide_xy()
        reach = me.walk * 0.75 + 1.0
        if hide is not None and math.hypot(hide[0] - me.pos[0], hide[1] - me.pos[1]) <= reach:
            self.mv.go(hide, me, self.zone, prefer_cover=False, tag="dodge-laser", replan_dist=2.0)
            dec.speed = "boost" if me.energy > 10 else "walk"
            dec.hold = False
            dec.intent.label = "duck the laser"
            return True
        dec.evasive = True
        dec.hold = False
        dec.intent.label = (dec.intent.label or "") + " jink"
        return True

    def _counter_mortar(self, me, dec):
        """A mortar launch heard while we stand still: the shell lands in 1-3 s with a 7 m blast.
        Move 12+ m now, across the bearing it came from, to a cell we can walk to straight."""
        t = me.t
        mh = self.th.mortar_heard
        if self.displace is not None:
            goal, until = self.displace
            if t < until and math.hypot(goal[0] - me.pos[0], goal[1] - me.pos[1]) > 1.5:
                dec.direct = goal
                dec.hold = False
                dec.intent.label = "mortar: move"
                return True
            self.displace = None
        if mh is None or t - mh[0] > 0.35 or me.speed() > 3.0 or not me.grounded:
            return False
        goal = self.displacement(me, mh[1])
        if goal is None:
            return False
        self.displace = (goal, t + 2.5)
        self.mv.clear()
        dec.direct = goal
        dec.hold = False
        dec.intent.label = "mortar: move"
        return True

    def displacement(self, me, bearing_yaw, dist=13.0):
        """A walkable point `dist` m away, preferring across the shell's bearing, then away from it."""
        x, y = me.pos[0], me.pos[1]
        side = 1 if self.rng.random() < 0.5 else -1
        options = [bearing_yaw + side * math.pi / 2, bearing_yaw - side * math.pi / 2,
                   bearing_yaw + math.pi + side * 0.6, bearing_yaw + math.pi - side * 0.6, bearing_yaw + math.pi]
        for a in options:
            gx, gy = x + math.cos(a) * dist, y + math.sin(a) * dist
            if self.w.walkable(x, y, gx, gy, need=2.7, skip_start=1.0):
                return (gx, gy)
        return None

    # ------------------------------------------------------------------ the air
    def _air(self, me, obs, dec, play, target, calc, enemies, slack):
        """Start or continue an air play, and fight from a roof. True when it decides the legs."""
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
        # a manoeuvre in progress keeps the legs until it is done
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
        # 1. last circle closing on a building: get onto that roof
        if final_roof is not None and t >= self.air_ban_until:
            fx, fy, fh = final_roof
            if on_roof and abs(me.pos[2] - fh) < 1.5:
                self.roof_mode = True
                self.mv.allow_roof = True
                if math.hypot(fx - me.pos[0], fy - me.pos[1]) > 2.0:
                    dec.direct = (fx, fy)
                intent.label = "last circle: roof"
                return True
            hop = A.WallHop.find(w, me, max_climb=56.0, cfg_mech=mech_cfg, reach=14.0)
            if hop is not None and abs(hop[2] - fh) < 1.5:
                self.air = A.WallHop()
                self.air.start(t, hop)
                self.roof_mode = True
                return True
        # 2. on a roof on purpose: fight from its edge, drop onto someone, or come down
        if self.roof_mode and on_roof:
            self.mv.allow_roof = True
            leave = slack < 20.0 and final_roof is None
            # a mortar reaches a roof: do not stay up there with one in range
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
                        intent.fire = "none"
                        intent.reload_ok = True
                        intent.label = "roof: back"
                    else:
                        dec.direct = peek
                        intent.fire = "long"
                        intent.label = "roof: edge"
                    self.mv.clear()
                    return True
            if not leave and (me.hp_frac < 0.93 or target is not None):
                # heal up here, out of reach of ground bots
                dec.hold = True
                intent.fire = "none" if target is None else intent.fire
                intent.label = "roof: heal"
                self.mv.clear()
                return True
            # done with the roof: walk off its edge (the mover's island escape)
            self.roof_mode = False
            self.mv.allow_roof = False
            return False
        if not on_roof:
            self.roof_mode = self.roof_mode and self.air is not None
            self.mv.allow_roof = False
        if t < self.air_ban_until or not me.grounded:
            return False
        # 3. laser pop-up from behind a wall
        laser = me.by_kind.get("laser")
        if laser is not None and target is not None and play in (D.ASSAULT, D.CORNER, D.HARASS) and not target.los \
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
        # 4. wall hop away from a pursuer when there is no cover on the ground nearby
        if play == D.BREAK and target is not None and target.sees_us:
            tp = target.est_pos(t)
            d = math.hypot(tp[0] - me.pos[0], tp[1] - me.pos[1])
            hide = self.cov.hide_xy()
            far_hide = hide is None or math.hypot(hide[0] - me.pos[0], hide[1] - me.pos[1]) > 25.0
            if d < 80.0 and far_hide and slack > 30.0:
                hop = A.WallHop.find(w, me, away_from=(tp[0], tp[1]), cfg_mech=mech_cfg)
                if hop is not None:
                    self.air = A.WallHop()
                    self.air.start(t, hop)
                    self.roof_mode = True
                    self.air_ban_until = t + 10.0
                    return True
        return False

    def drop_choice(self, me, obs):
        z = self.zone
        cx, cy, rs = self.mv.safe_circle(z)
        best = None
        for d in obs.get("drops") or []:
            p = d.get("pos")
            if d.get("holder") not in (None, me.id):
                continue
            dist = math.hypot(p[0] - me.pos[0], p[1] - me.pos[1])
            if dist > 160 or math.hypot(p[0] - cx, p[1] - cy) > rs:
                continue
            eta = dist / 11.0
            if d.get("lands_in", 0) > eta + 8:
                continue
            crowded = False
            for tr in self.th.mechs():
                q = tr.est_pos(me.t)
                if me.t - tr.t_seen < 15 and math.hypot(q[0] - p[0], q[1] - p[1]) < 90:
                    crowded = True
            if crowded:
                continue
            if best is None or dist < best[0]:
                best = (dist, (p[0], p[1]))
        return best[1] if best else None
