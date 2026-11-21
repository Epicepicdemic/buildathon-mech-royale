"""Legs: turns goals into move/boost/jump each decision.

Owns the zone field, the current route and its follower, local paths (hide/peek spots), the
irregular jinking used under fire, stepping off incoming shells, stuck recovery (jump, then
jets, then replan) and escaping islands (low roofs, the stadium bowl) with jets.
"""
import math
import random
import time

from . import nav, routing
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
        self.jink_sign = 1
        self.jink_until = 0.0
        self.anchor = None
        self.escape = None
        self.note = ""
        self.urgent = False
        self.replans = 0
        self.mode_speed = "walk"
        self.allow_roof = False       # set while we mean to be on a roof (the roof plays)

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
            zf_dl = deadline - 0.003 if self.planner is not None else deadline
            if time.perf_counter() < zf_dl:
                self.zf.run(zf_dl)
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
        self.fol = routing.Follower(self.w, route.pts, route.cum)
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
        start = self.w.nearest_main(me.pos[0], me.pos[1], 6)
        gn = self.w.nearest_main(goal[0], goal[1], 24)
        if start is None or gn is None:
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
    def steer(self, me, zone, speed="walk", evasive=False, threat_yaw=None, hold=False, shells=()):
        """(move [x, y], boost, jump) for this decision."""
        w = self.w
        x, y = me.pos[0], me.pos[1]
        t = me.t
        n = w.node(x, y)
        boost = False
        jump = False
        move = (0.0, 0.0)
        self.urgent = False
        # 1. islands: low roofs, the stadium bowl, anywhere the grid says we cannot walk out
        nf = w.nearest_free(x, y, 3)
        on_island = nf is None or not w.main[nf]
        if on_island and not self.allow_roof:
            esc = self._escape_target(x, y)
            if esc is not None:
                dx, dy = esc[0] - x, esc[1] - y
                move = unit(dx, dy)
                st = self.stuck.update(t, x, y, True)
                jump = me.grounded and (st >= 1 or self._blocked_ahead(x, y, move))
                boost = (not me.grounded) and me.energy > 3
                self.note = "escape island"
                return [move[0], move[1]], boost, jump
        # 2. urgency: outside the zone or about to be caught: fastest path, boost
        margin_now = zone.margin_at(x, y, t)
        bspeed = zone.boundary_speed()
        urgent = (not me.in_zone) or (margin_now < bspeed * 3.0 + 6.0 and zone.margin_next(x, y) < 0 and t >= zone.shrink_start - 3)
        if not urgent and zone.margin_next(x, y) < 0:
            urgent = self.slack(me, zone) < 3.0
        if urgent:
            self.urgent = True
            active_zf = None
            if margin_now < 0 and self.zf_prev is not None and self.zf_prev.done:
                active_zf = self.zf_prev
            elif self.field_ready():
                active_zf = self.zf
            elif self.zf_prev is not None and self.zf_prev.done:
                active_zf = self.zf_prev
            path = active_zf.path(w.nearest_free(x, y, 4) or n, 40) if active_zf is not None else []
            if len(path) > 1:
                pts = nav.path_points(w, path)
                fol = routing.Follower(w, pts)
                tx, ty, rem, off = fol.target(x, y)
                move = unit(tx - x, ty - y)
            else:
                tc = zone.center if margin_now < 0 else zone.next_center
                byaw = math.atan2(tc[1] - y, tc[0] - x)
                for deg in (0, 35, -35, 70, -70, 110, -110):
                    ux, uy = math.cos(byaw + math.radians(deg)), math.sin(byaw + math.radians(deg))
                    if w.walkable(x, y, x + ux * 4.5, y + uy * 4.5, need=2.5, skip_start=1.5):
                        move = (ux, uy)
                        break
                else:
                    move = unit(tc[0] - x, tc[1] - y)
            bdodge = self._dodge_bullets(me, shells, move, forward_weight=0.75)
            if bdodge is not None:
                move = bdodge
            sdodge, _ = self._dodge_shells(me, shells)
            if sdodge is not None:
                move = unit(move[0] * 0.45 + sdodge[0] * 0.85, move[1] * 0.45 + sdodge[1] * 0.85)
            boost = self.booster.want(True, me.energy)
            st = self.stuck.update(t, x, y, True)
            jump, boost = self._unstick(st, me, jump, boost)
            self.note = "zone run"
            return [move[0], move[1]], boost, jump
        # 3. follow the route (or step toward a pending goal while the planner finishes)
        commanded = False
        if self.fol is not None and not hold:
            tx, ty, rem, off = self.fol.target(x, y)
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
        elif self.planner is not None and self.plan_goal is not None and not hold:
            gx, gy = self.plan_goal
            dg = math.hypot(gx - x, gy - y)
            if dg > 1.5:
                byaw = math.atan2(gy - y, gx - x)
                for deg in (0, 35, -35, 70, -70):
                    ux, uy = math.cos(byaw + math.radians(deg)), math.sin(byaw + math.radians(deg))
                    if w.walkable(x, y, x + ux * 4.5, y + uy * 4.5, need=2.5, skip_start=1.5):
                        move = (ux, uy)
                        commanded = True
                        break
        # 4. jinking under fire: irregular sidesteps across the enemy's line
        if evasive:
            move = self._jink(me, move, threat_yaw)
            commanded = commanded or math.hypot(*move) > 0.3
        # 5. sidestep incoming gun/drone bullets & falling mortar shells
        bdodge = self._dodge_bullets(me, shells, move)
        if bdodge is not None:
            move = bdodge
            commanded = True
        dodge, shell_urgent = self._dodge_shells(me, shells)
        if dodge is not None:
            move = dodge
            commanded = True
            if shell_urgent and me.energy > 8.0:
                speed = "boost"
        if speed == "silent":
            f = min(1.0, self.mob.silent / max(self.mob.walk, 0.1))
            move = (move[0] * f, move[1] * f)
        boost = self.booster.want(speed == "boost" and math.hypot(*move) > 0.5, me.energy)
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
        return [move[0], move[1]], boost, jump

    def _unstick(self, st, me, jump, boost):
        if st >= 1 and me.grounded:
            jump = True
        if st >= 2 and not me.grounded and me.energy > 5:
            boost = True
        return jump, boost

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

    def _jink(self, me, move, threat_yaw):
        t = me.t
        if t >= self.jink_until:
            self.jink_sign = -self.jink_sign if self.rng.random() < 0.8 else self.jink_sign
            self.jink_until = t + self.rng.uniform(0.3, 0.55)
        if threat_yaw is not None:
            px, py = -math.sin(threat_yaw), math.cos(threat_yaw)     # across the line of fire
        elif math.hypot(*move) > 0.1:
            px, py = -move[1], move[0]
        else:
            px, py = -math.sin(me.yaw), math.cos(me.yaw)
        s = self.jink_sign
        mx = move[0] * 0.6 + px * s * 0.8
        my = move[1] * 0.6 + py * s * 0.8
        u = unit(mx, my)
        x, y = me.pos[0], me.pos[1]
        if not self.w.walkable(x, y, x + u[0] * 5.0, y + u[1] * 5.0, need=2.7, skip_start=1.0):
            self.jink_sign = -s
            mx = move[0] * 0.6 - px * s * 0.8
            my = move[1] * 0.6 - py * s * 0.8
            u = unit(mx, my)
            if not self.w.walkable(x, y, x + u[0] * 5.0, y + u[1] * 5.0, need=2.7, skip_start=1.0):
                return move
        return u

    def _dodge_bullets(self, me, shells, cur_move, forward_weight=0.45):
        """Step perpendicular to the earliest incoming gun/npc bullet whose ray passes through us."""
        if not shells:
            return None
        mx, my, mz = me.pos
        hit_r = me.radius + 1.1
        best = None
        for p in shells:
            if p.get("kind") not in ("gun", "npc"):
                continue
            pos = p.get("pos")
            vel = p.get("vel")
            if not pos or not vel:
                continue
            vx, vy, vz = vel[0], vel[1], vel[2]
            sp2 = vx * vx + vy * vy
            if sp2 < 100.0:
                continue
            rx, ry = mx - pos[0], my - pos[1]
            tc = (rx * vx + ry * vy) / sp2
            if tc < 0.06 or tc > 0.90:
                continue
            zc = pos[2] + vz * tc
            if zc < mz - 1.0 or zc > mz + me.height + 1.0:
                continue
            sp = math.sqrt(sp2)
            nx, ny = -vy / sp, vx / sp
            lat = (rx - vx * tc) * nx + (ry - vy * tc) * ny
            if abs(lat) < hit_r and (best is None or tc < best[0]):
                best = (tc, nx, ny, lat)
        if best is None:
            return None
        _, nx, ny, lat = best
        if abs(lat) > 0.35:
            sign = 1.0 if lat >= 0.0 else -1.0
        else:
            proj = cur_move[0] * nx + cur_move[1] * ny
            sign = (1.0 if proj >= 0.0 else -1.0) if abs(proj) > 0.15 else float(self.jink_sign)
        for s in (sign, -sign):
            if s != sign and abs(lat) > 1.4:
                break
            u = unit(cur_move[0] * forward_weight + nx * s * 0.90, cur_move[1] * forward_weight + ny * s * 0.90)
            if self.w.walkable(mx, my, mx + u[0] * 4.5, my + u[1] * 4.5, need=2.6, skip_start=1.0):
                return u
        return None

    def _dodge_shells(self, me, shells):
        """Return (walkable_escape_unit_vec, is_urgent) for the soonest dangerous mortar shell."""
        if not shells:
            return None, False
        g = float(((self.cfg.get("weapons") or {}).get("mortar") or {}).get("gravity", 22.0))
        mx, my, mz = me.pos
        best = None
        for p in shells:
            if p.get("kind") != "mortar":
                continue
            px, py, pz = p["pos"]
            vx, vy, vz = p["vel"]
            dz = pz - mz
            disc = vz * vz + 2 * g * dz
            if disc < 0:
                continue
            tl = (vz + math.sqrt(disc)) / g
            lx, ly = px + vx * tl, py + vy * tl
            d = math.hypot(lx - mx, ly - my)
            if d < 10.5 and 0.0 < tl < 2.6 and (best is None or tl < best[0]):
                best = (tl, lx, ly, d)
        if best is None:
            return None, False
        tl, lx, ly, d0 = best
        urgent = d0 < 8.5 and tl < 1.7
        base_yaw = math.atan2(my - ly, mx - lx) if d0 > 0.3 else me.yaw
        step_d = min(8.0, max(4.5, me.walk * min(tl, 0.7)))
        c0 = self.w.clearance(mx, my)
        skip = 1.0 if c0 >= 2.7 else 2.3
        best_dir = None
        for k in range(16):
            deg = (k // 2 + (k & 1)) * 22.5 * (1 if k & 1 else -1) if k else 0.0
            a = base_yaw + math.radians(deg)
            ux, uy = math.cos(a), math.sin(a)
            ok = self.w.walkable(mx, my, mx + ux * 5.5, my + uy * 5.5, need=2.5, skip_start=skip)
            if not ok and c0 < 2.6:
                # Pressed tight against a wall (e.g. (20,-85) with west wall at x=19): allow directions
                # that stay out of buildings and strictly improve or keep wall clearance >= 2.2 m.
                ok = all(self.w.sight(mx + ux * s, my + uy * s) <= mz + 1.6 for s in (1.5, 3.0, 5.0)) \
                    and self.w.clearance(mx + ux * 4.0, my + uy * 4.0) >= max(2.2, c0)
            if ok:
                sep = math.hypot(mx + ux * step_d - lx, my + uy * step_d - ly)
                if best_dir is None or sep > best_dir[0]:
                    best_dir = (sep, (ux, uy))
        if best_dir is not None:
            return best_dir[1], urgent
        return (math.cos(base_yaw), math.sin(base_yaw)), urgent
