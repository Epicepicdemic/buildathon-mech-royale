"""Spatial memory of cubes, weapon pickups, crates, and kill drops.

The server lists pickups, cubes, and crates only while our mech sees them, and a killed mech
drops all of its cubes plus two (and any non-gun weapons) at the spot where it died. Keeping
a short-lived map of seen loot and inferred kill drops lets Line B route through cubes and
weapons without forgetting them the moment the torso turns to fight or scan.
"""
import math

from .geom import angdiff


class LootMemory:
    def __init__(self, cfg):
        self.cfg = cfg
        v = cfg.get("vision") or {}
        self.near_range = float(v.get("near_range", 45.0))
        self.vis_range = float(v.get("range", 200.0))
        self.fov_half = math.radians(float(v.get("fov_deg", 120.0))) * 0.5
        self.cube_reach = float((cfg.get("cubes") or {}).get("pickup_radius", 3.5))
        self.loot_reach = float((cfg.get("loot") or {}).get("pickup_radius", 4.0))
        self.pickups = {}       # id -> [kind, [x, y, z], t_seen]
        self.cubes = {}         # id -> [[x, y, z], t_seen]
        self.crates = {}        # id -> [[x, y, z], hp, t_seen]
        self.kill_drops = []    # [pos, est_cubes, weapons, t_died]

    def update(self, obs, me, world=None, new_kill_drops=()):
        t = me.t
        mx, my, mz = me.pos
        # 1. events: explicit takes, failed takes, and cube pickups
        for e in obs.get("events") or []:
            et = e.get("type")
            if et == "took":
                self.pickups.pop(e.get("id"), None)
            elif et == "take_failed" and e.get("reason") in ("gone", "have"):
                self.pickups.pop(e.get("id"), None)
            elif et == "pickup":
                # remove the closest remembered cube (or decrement a kill drop) near our feet
                best_cid = None
                for cid, (cp, _) in self.cubes.items():
                    d = math.hypot(cp[0] - mx, cp[1] - my)
                    if d <= self.cube_reach + 2.0 and (best_cid is None or d < best_cid[0]):
                        best_cid = (d, cid)
                if best_cid is not None:
                    self.cubes.pop(best_cid[1], None)

        # 2. record currently visible items
        seen_pickups = set()
        for p in obs.get("pickups") or []:
            pid = p.get("id")
            pp = p.get("pos")
            if pid is not None and pp is not None:
                seen_pickups.add(pid)
                self.pickups[pid] = [p.get("kind"), [float(pp[0]), float(pp[1]), float(pp[2])], t]

        seen_cubes = set()
        for c in obs.get("cubes") or []:
            cid = c.get("id")
            cp = c.get("pos")
            if cid is not None and cp is not None:
                seen_cubes.add(cid)
                self.cubes[cid] = [[float(cp[0]), float(cp[1]), float(cp[2])], t]

        seen_crates = set()
        for c in obs.get("crates") or []:
            cid = c.get("id")
            cp = c.get("pos")
            if cid is not None and cp is not None:
                seen_crates.add(cid)
                self.crates[cid] = [[float(cp[0]), float(cp[1]), float(cp[2])], float(c.get("hp", 300.0)), t]

        # 3. add inferred kill drops (from kills out of view or just died)
        for kp, kcubes, kwpns, kt in new_kill_drops:
            # if we already see real cubes right at this spot, those track it directly
            if any(math.hypot(cp[0] - kp[0], cp[1] - kp[1]) < 6.0 for cp, _ in self.cubes.values()):
                continue
            if any(math.hypot(d[0][0] - kp[0], d[0][1] - kp[1]) < 6.0 for d in self.kill_drops):
                continue
            self.kill_drops.append([[float(kp[0]), float(kp[1]), float(kp[2])], max(2, int(kcubes)), list(kwpns), kt])

        # 4. prune stale or clearly-collected items
        eye = me.eye
        for pid in list(self.pickups):
            if pid in seen_pickups:
                continue
            _, pp, ts = self.pickups[pid]
            if t - ts > 45.0 or self._should_see_now(me, eye, pp, world):
                del self.pickups[pid]

        for cid in list(self.cubes):
            if cid in seen_cubes:
                continue
            cp, ts = self.cubes[cid]
            d = math.hypot(cp[0] - mx, cp[1] - my)
            if t - ts > 35.0 or (d <= self.cube_reach - 0.3 and abs(cp[2] - mz) <= 3.5) \
                    or self._should_see_now(me, eye, cp, world):
                del self.cubes[cid]

        for cid in list(self.crates):
            if cid in seen_crates:
                continue
            cp, _, ts = self.crates[cid]
            if t - ts > 50.0 or self._should_see_now(me, eye, cp, world, height=2.5):
                del self.crates[cid]

        kept = []
        for kd in self.kill_drops:
            kp, kcubes, kwpns, kt = kd
            d = math.hypot(kp[0] - mx, kp[1] - my)
            if t - kt > 35.0:
                continue
            if d <= self.cube_reach and abs(kp[2] - mz) <= 4.0:
                continue
            # once real cubes at this kill spot are visible in obs, let self.cubes own them
            if any(math.hypot(cp[0] - kp[0], cp[1] - kp[1]) < 7.0 for cp, ts in self.cubes.values() if ts == t):
                continue
            if self._should_see_now(me, eye, kp, world):
                continue
            kept.append(kd)
        self.kill_drops = kept

    def _should_see_now(self, me, eye, pos, world, height=1.2):
        """Conservative check: if we are close or looking straight at `pos` with a clear line and
        no canopy, it would be in `obs` if it still existed."""
        dx, dy = pos[0] - me.pos[0], pos[1] - me.pos[1]
        d = math.hypot(dx, dy)
        if d > self.vis_range * 0.92:
            return False
        in_near = d <= self.near_range * 0.88
        if not in_near:
            if angdiff(me.yaw, math.atan2(dy, dx)) > self.fov_half - 0.12:
                return False
        if world is None:
            return in_near and d < 12.0
        # require canopy-free clear line so we don't falsely prune items hidden behind trees
        return world.line_clear(eye[0], eye[1], eye[2], pos[0], pos[1], pos[2] + height, step=1.5, canopy=True)
