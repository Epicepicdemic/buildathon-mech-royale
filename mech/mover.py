"""Legs: turns goals into move/boost/jump each decision.

Owns the zone field, the current route and its follower, local paths (hide/peek spots), the
irregular jinking used under fire, stepping off incoming shells, stuck recovery (jump, then
jets, then replan) and escaping islands (low roofs, the stadium bowl) with jets.
"""
import math
import random
import time

from . import dodge, nav, routing
from .geom import unit, wrap

INF = float("inf")


class Mover:
    def __init__(self, world, cfg, seed=0):
        self.w = world
        self.cfg = cfg
        self.rng = random.Random(seed ^ 0x5EED)
        self.mob = routing.Mobility(cfg.get("mech", {}))
        self.cover_mult = routing.cover_multiplier(world)
        self.zf = None
        self.zf_key = None
        self.zf_prev = None
        self.route = None
        self.fol = None
        self.goal = None
        self.goal_tag = None
        self.planner = None
        self.plan_goal = None
        self.plan_tag = None
        self.last_plan_t = -1e9
        self.stuck = routing.StuckMonitor()
        self.booster = routing.Booster()
        self.jinker = dodge.Jinker(random.Random(seed ^ 0x51DE))
        self.anchor = None
        self.escape = None
        self.note = ""
        self.urgent = False
        self.replans = 0
        self.mode_speed = "walk"
        self.allow_roof = False       # set while we mean to be on a roof (the roof plays)
        self.exposed = False
        self.last_move = None
        self.hop = None               # routing.Hop in flight
        self.hop_fol = None           # the follower whose hop it is
        self.zone_fol = None          # persistent follower for urgent zone runs (paths may hop)
        self.zone_hops = {}
        self.zone_fol_t = -1e9
        self.hops_banned = 0

    # ------------------------------------------------------------------ background work
    def work(self, me, zone, deadline):
        """Advance the zone field and any route search until `deadline` (perf_counter)."""
        key = (round(zone.next_center[0], 1), round(zone.next_center[1], 1), round(zone.next_radius, 1))
        if key != self.zf_key:
            self.zf_prev = self.zf if (self.zf is not None and self.zf.done) else self.zf_prev
            r = zone.safe_radius(self.safe_margin(zone))
            stay = (zone.center[0], zone.center[1], max(1.0, zone.radius - 4.0))
            self.zf = nav.ZoneField(self.w, zone.next_center[0], zone.next_center[1], r, stay=stay)
            self.zf_key = key
        if not self.zf.done:
            self.zf.run(deadline)
        if self.planner is not None and time.perf_counter() < deadline:
            if self.planner.run(deadline):
                res = self.planner.result
                if res is not None:
                    self._set_route(res, self.plan_goal, self.plan_tag)
                    self.note = self.planner.note
                else:
                    self.note = "plan failed: " + self.planner.note
                self.planner = None

    def safe_margin(self, zone):
        return min(10.0, max(3.0, zone.next_radius * 0.12))

    def safe_circle(self, zone):
        return (zone.next_center[0], zone.next_center[1], zone.safe_radius(self.safe_margin(zone)))

    def field_ready(self):
        return self.zf is not None and self.zf.done

    def dist_to_safety(self, x, y, zone):
        """Walking metres to the safe part of the next circle (estimate if the field is not ready)."""
        cx, cy, r = self.safe_circle(zone)
        d = math.hypot(x - cx, y - cy) - r
        if d <= 0:
            return 0.0
        if self.field_ready():
            n = self.w.nearest_free(x, y, 4)
            if n is not None:
                c = self.zf.cost(n)
                if c < INF:
                    return c
        return d * 1.35

    def slack(self, me, zone):
        """Seconds to spare if we left for safety now at walking speed."""
        d = self.dist_to_safety(me.pos[0], me.pos[1], zone)
        if d <= 0:
            return INF
        tw = self.mob.time_walk(d * routing.PATH_FACTOR)
        return zone.deadline() - me.t - tw - routing.deadline_margin(tw)

    # ------------------------------------------------------------------ goals
    def _set_route(self, route, goal, tag):
        self.route = route
        self.fol = routing.Follower(self.w, route.pts, route.cum, hops=route.hops)
        self.goal = goal
        self.goal_tag = tag
        self.stuck.reset()

    def clear(self):
        self.route = None
        self.fol = None
        self.goal = None
        self.goal_tag = None
        self.planner = None

    def go(self, goal, me, zone, prefer_cover=True, tag="", replan_dist=6.0):
        """Ask for a route to `goal` (x, y). Keeps following the old route until the new one is ready."""
        if goal is None:
            return
        same = self.goal is not None and math.hypot(goal[0] - self.goal[0], goal[1] - self.goal[1]) <= replan_dist \
            and tag == self.goal_tag and self.route is not None
        pending = self.planner is not None and self.plan_goal is not None and \
            math.hypot(goal[0] - self.plan_goal[0], goal[1] - self.plan_goal[1]) <= replan_dist and tag == self.plan_tag
        if same or pending:
            return
        start = self.w.nearest_free(me.pos[0], me.pos[1], 6)
        gn = self.w.nearest_free(goal[0], goal[1], 8)
        if start is None or gn is None:
            return
        if not self.w.main[gn]:
            return
        self.plan_goal = goal
        self.plan_tag = tag
        cx, cy, r = zone.circle_at(me.t + 8.0)
        self.planner = routing.RoutePlanner(self.w, start, gn, self.mob, me.energy, me.t, zone.deadline(),
                                            self.safe_circle(zone), self.cover_mult, prefer_cover,
                                            zone_field=self.zf if self.field_ready() else None,
                                            stay=(cx, cy, max(1.0, r - 3.0)))
        self.last_plan_t = me.t

    def go_path(self, nodes, tag):
        """Follow a path found by a local search (hide/peek)."""
        if not nodes:
            return
        if self.goal_tag == tag and self.route is not None and self.route.nodes and self.route.nodes[-1] == nodes[-1]:
            return
        L = nav.path_length(self.w, nodes)
        r = routing.Route(self.w, nodes, "local", False, L, L)
        self.planner = None
        self._set_route(r, self.w.node_xy(nodes[-1]), tag)

    def arrived(self, me, tol=2.5, goal=None):
        """At the end of the current route (or within tol of `goal` when there is no route)."""
        if self.fol is None:
            g = goal if goal is not None else self.goal
            return g is not None and math.hypot(g[0] - me.pos[0], g[1] - me.pos[1]) <= tol
        return self.fol.finished(me.pos[0], me.pos[1], tol)

    # ------------------------------------------------------------------ steering
    def steer(self, me, zone, speed="walk", evasive=False, threat_yaw=None, hold=False, shells=(), exposed=False):
        """(move [x, y], boost, jump) for this decision. `exposed`: an enemy can see us now, so
        stuck recovery may hop but must not jet (a mech hanging in the air is the easiest target)."""
        self.exposed = exposed
        w = self.w
        x, y = me.pos[0], me.pos[1]
        t = me.t
        n = w.node(x, y)
        boost = False
        jump = False
        move = (0.0, 0.0)
        self.urgent = False
        # 0. a hop in flight keeps the legs until it lands
        if self.hop is not None:
            res = self._fly_hop(me)
            if res is not None:
                return res
        # 1. islands: low roofs, the stadium bowl, anywhere the grid says we cannot walk out
        #    (unless a route with hops is taking us across one)
        nf = w.nearest_free(x, y, 3)
        on_island = nf is None or not w.main[nf]
        crossing = (self.route is not None and self.route.hops) or (self.zone_fol is not None and self.zone_hops
                                                                  and t - self.zone_fol_t < 2.0)
        if on_island and not self.allow_roof and not crossing:
            esc = self._escape_target(x, y)
            if esc is not None:
                dx, dy = esc[0] - x, esc[1] - y
                move = unit(dx, dy)
                st = self.stuck.update(t, x, y, True)
                jump = me.grounded and (st >= 1 or self._blocked_ahead(x, y, move))
                boost = (not me.grounded) and me.energy > 3 and self._jet_needed(me, move)
                self.note = "escape island"
                return [move[0], move[1]], boost, jump
        # 2. urgency: outside the zone or about to be caught: fastest path, boost
        margin_now = zone.margin_at(x, y, t)
        bspeed = zone.boundary_speed()
        urgent = (not me.in_zone) or (margin_now < bspeed * 3.0 + 6.0 and zone.margin_next(x, y) < 0 and t >= zone.shrink_start - 3)
        if not urgent and zone.margin_next(x, y) < 0:
            urgent = self.slack(me, zone) < 3.0
        if urgent and not self.field_ready():
            # the zone field is still being computed (about 100 ms of work, spread over decisions;
            # longer on a slow machine): head straight for the circle meanwhile, round what is in
            # the way, rather than stand still
            self.urgent = True
            cx, cy, _r = self.safe_circle(zone)
            mv = self._fallback_dir(x, y, cx, cy)
            if mv is not None:
                boost = self.booster.want(True, me.energy) and me.grounded
                self.last_move = mv
                st = self.stuck.update(t, x, y, True)
                jump, boost = self._unstick(st, me, False, boost)
                self.note = "zone run (no field yet)"
                return [mv[0], mv[1]], boost, jump
        if urgent and self.field_ready():
            self.urgent = True
            fol = self.zone_fol
            if fol is None or t - self.zone_fol_t > 1.5 or fol.progress(x, y) > 6.0:
                path = self.zf.path(w.nearest_free(x, y, 4) or n, 60)
                self.zone_hops = routing.route_hops(w, path)
                fol = self.zone_fol = routing.Follower(w, nav.path_points(w, path), hops=self.zone_hops)
                self.zone_fol_t = t
            if len(fol.pts) > 1:
                k = fol.hop_due(x, y)
                if k is not None:
                    res = self._start_hop(me, fol, k, self.zone_hops[k])
                    if res is not None:
                        return res
                tx, ty, rem, off = fol.target(x, y)
                move = unit(tx - x, ty - y)
                if evasive and exposed:
                    # under fire on the way back: jink, keeping most of the heading
                    move = self._jink(me, move, threat_yaw, base_w=1.6)
                boost = self.booster.want(True, me.energy)
                self.last_move = move
                st = self.stuck.update(t, x, y, True)
                jump, boost = self._unstick(st, me, jump, boost)
                if not me.grounded and boost and not self._jet_needed(me, move):
                    boost = False       # in the air, boosting is jetting: only to clear a ledge
                self.note = "zone run"
                return [move[0], move[1]], boost, jump
        # 3. follow the route
        commanded = False
        if self.fol is not None and not hold and self.route is not None:
            k = self.fol.hop_due(x, y)
            if k is not None and k in self.route.hops:
                res = self._start_hop(me, self.fol, k, self.route.hops[k])
                if res is not None:
                    return res
        if self.fol is not None and not hold:
            tx, ty, rem, off = self.fol.target(x, y)
            if me.grounded and self.route is not None and self.goal is not None and \
                    abs(w.floor(tx, ty) - me.pos[2]) > 2.5 and self.fol.hop_due(x, y) is None:
                # off a roof the route crosses (or below one it climbs): that point is out of reach
                # on foot from here, so plan again from where we are
                off = 99.0
            if off > 10.0 and self.route is not None and self.goal is not None:
                # knocked off the route: plan again from here
                self.replans += 1
                g, tag = self.goal, self.goal_tag
                self.route = None
                self.fol = None
                self.goal = None
                self.go(g, me, zone, tag=tag)
            elif self.fol.finished(x, y, 1.5):
                move = (0.0, 0.0)
            else:
                d = math.hypot(tx - x, ty - y)
                move = unit(tx - x, ty - y)
                # slow down onto the last point
                if self.fol.last_target >= len(self.fol.pts) - 1 and d < 3.0:
                    move = (move[0] * d / 3.0, move[1] * d / 3.0)
                commanded = d > 0.6
                # speed for the route: boost if walking would be late
                if self.route is not None and self.route.kind != "local":
                    rem_enter = max(0.0, self.route.enter_len - (self.route.length - rem))
                    mode = routing.speed_mode(rem_enter, t, zone.deadline(), self.mob, me.energy, reserve=0.0)
                    if mode == "boost":
                        speed = "boost"
        # 4. jinking under fire: irregular sidesteps across the enemy's line
        if evasive:
            move = self._jink(me, move, threat_yaw)
            commanded = commanded or math.hypot(*move) > 0.3
        # 5. step off incoming mortar shells
        dodge = self._dodge_shells(me, shells)
        if dodge is not None:
            move = dodge
            commanded = True
        if speed == "silent":
            f = min(1.0, self.mob.silent / max(self.mob.walk, 0.1))
            move = (move[0] * f, move[1] * f)
        boost = self.booster.want(speed == "boost" and math.hypot(*move) > 0.5, me.energy)
        self.last_move = move
        st = self.stuck.update(t, x, y, commanded)
        if st >= 3 and self.goal is not None:
            # replan from where we are, after a sidestep
            g, tag = self.goal, self.goal_tag
            self.route = None
            self.fol = None
            self.goal = None
            self.go(g, me, zone, tag=tag)
            self.stuck.reset()
            self.replans += 1
        jump, boost = self._unstick(st, me, jump, boost)
        if boost and not me.grounded and not self._jet_needed(me, move):
            # in the air the boost button is the jets: running speed is not wanted up there
            boost = False
        return [move[0], move[1]], boost, jump

    def _fallback_dir(self, x, y, gx, gy):
        """Unit direction toward (gx, gy) that can be walked for 8 m, deflected up to 90 degrees."""
        base = math.atan2(gy - y, gx - x)
        for off in (0.0, 0.4, -0.4, 0.8, -0.8, 1.2, -1.2, 1.57, -1.57):
            a = base + off
            ux, uy = math.cos(a), math.sin(a)
            if self.w.walkable(x, y, x + ux * 8.0, y + uy * 8.0, need=2.7, skip_start=1.0):
                return (ux, uy)
        return None

    # ------------------------------------------------------------------ hops
    def _start_hop(self, me, fol, k, info):
        """At the launch point of hop k: start it (or wait for the energy it needs)."""
        climb = info.top > me.pos[2] + 0.6
        if climb and me.energy < info.energy:
            self.note = "hop: waiting for energy"
            return [0.0, 0.0], False, False
        self.hop = routing.Hop(me.t, fol.pts[k], fol.pts[k + 1], info, self.w)
        self.hop_fol = fol
        return self._fly_hop(me)

    def _fly_hop(self, me):
        h = self.hop
        mv, boost, jump = h.step(me.t, me.pos[0], me.pos[1], me.pos[2], me.grounded, me.energy)
        if h.done:
            if self.hop_fol is not None:
                self.hop_fol.passed_hop(h.info.idx)
            self.hop = None
            self.hop_fol = None
            self.stuck.reset()
            self.stuck.grace(me.t + 1.2)
            return None
        if h.failed:
            # never try that one again this match: ban the edge and rebuild the zone field without it
            fol = self.hop_fol
            if fol is not None:
                a = self.w.nearest_free(fol.pts[h.info.idx][0], fol.pts[h.info.idx][1], 2)
                b = self.w.nearest_free(fol.pts[h.info.idx + 1][0], fol.pts[h.info.idx + 1][1], 2)
                if a is not None and b is not None:
                    self.w.ban_hop(a, b)
                    self.hops_banned += 1
            self.zf_key = None
            self.hop = None
            self.hop_fol = None
            self.zone_fol = None
            # drop the route: the tactics layer asks for its goal again next decision and the
            # planner starts from where we are now
            self.route = None
            self.fol = None
            self.goal = None
            self.replans += 1
            return None
        self.note = "hop"
        return [mv[0], mv[1]], boost, jump

    def _unstick(self, st, me, jump, boost):
        if st >= 1 and me.grounded:
            jump = True
        if st >= 2 and not me.grounded and me.energy > 5 and not getattr(self, "exposed", False):
            mv = self.last_move if getattr(self, "last_move", None) is not None else (math.cos(me.yaw), math.sin(me.yaw))
            if self._jet_needed(me, mv):
                boost = True
        return jump, boost

    def _jet_needed(self, me, move, look=4.0, max_climb=14.0):
        """Jet only while the feet are below the top of what is in front of us, and only when that
        top is low enough to clear. Version 2 jetted for as long as it stayed stuck and rose 20 to
        36 m into the view of the mechs shooting it."""
        mx, my = move[0], move[1]
        n = math.hypot(mx, my)
        if n < 0.2:
            return False
        mx, my = mx / n, my / n
        x, y = me.pos[0], me.pos[1]
        top = -1e9
        for k in range(1, int(look) + 1):
            top = max(top, self.w.sight(x + mx * (me.radius + k * 0.75), y + my * (me.radius + k * 0.75)))
        if top - me.pos[2] > max_climb:
            return False
        return me.pos[2] < top + 0.8

    def _blocked_ahead(self, x, y, d):
        return not self.w.walkable(x, y, x + d[0] * 4.0, y + d[1] * 4.0, need=2.4, skip_start=1.0)

    def _escape_target(self, x, y):
        if self.escape is not None and math.hypot(self.escape[0] - x, self.escape[1] - y) < 60 and \
                self.escape[2] > 0:
            return self.escape
        w = self.w
        n0 = w.node(x, y)
        v0, u0 = divmod(n0, w.nu)
        best = None
        for r in range(1, 45):
            for dv in range(-r, r + 1):
                for du in ((-r, r) if abs(dv) != r else range(-r, r + 1)):
                    u, v = u0 + du, v0 + dv
                    if 0 <= u < w.nu and 0 <= v < w.nv:
                        m = v * w.nu + u
                        if w.main[m] and w.clearance(*w.node_xy(m)) >= 4.0:
                            px, py = w.node_xy(m)
                            d = math.hypot(px - x, py - y)
                            if best is None or d < best[0]:
                                best = (d, px, py)
            if best is not None:
                break
        if best is None:
            return None
        self.escape = (best[1], best[2], 1)
        return self.escape

    def _jink(self, me, move, threat_yaw, base_w=None):
        u = self.jinker.direction(me.t, move, threat_yaw, base_w)
        if u is None:
            return move
        x, y = me.pos[0], me.pos[1]
        if not self.w.walkable(x, y, x + u[0] * 5.0, y + u[1] * 5.0, need=2.7, skip_start=1.0):
            self.jinker.flip()
            u2 = self.jinker.direction(me.t, move, threat_yaw, base_w)
            if u2 is None or not self.w.walkable(x, y, x + u2[0] * 5.0, y + u2[1] * 5.0, need=2.7, skip_start=1.0):
                return move
            return u2
        return u

    def _dodge_shells(self, me, shells):
        g = float(((self.cfg.get("weapons") or {}).get("mortar") or {}).get("gravity", 22.0))
        best = None
        for p in shells:
            if p.get("kind") != "mortar":
                continue
            px, py, pz = p["pos"]
            vx, vy, vz = p["vel"]
            # time to fall to our floor level
            dz = pz - me.pos[2]
            disc = vz * vz + 2 * g * dz
            if disc < 0:
                continue
            tl = (vz + math.sqrt(disc)) / g
            lx, ly = px + vx * tl, py + vy * tl
            d = math.hypot(lx - me.pos[0], ly - me.pos[1])
            if d < 10.0 and tl < 2.5 and (best is None or tl < best[0]):
                best = (tl, lx, ly)
        if best is None:
            return None
        return unit(me.pos[0] - best[1], me.pos[1] - best[2]) if math.hypot(me.pos[0] - best[1], me.pos[1] - best[2]) > 0.5 \
            else (1.0, 0.0)
