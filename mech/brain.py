"""The bot: one decision per observation, inside a fixed time budget.

Per decision: parse, update threat memory and the zone, decide (tactics + doctrine), spend the
remaining budget on searches (zone field, routes, cover), then steer the legs and the torso.
Long searches are resumable, so no decision waits for one to finish.
"""
import gc
import math
import random
import sys
import time

from .world import World
from .zone import Zone
from .threats import Threats
from .state import Me
from .mover import Mover
from .cover import CoverState
from .gunner import Gunner
from .tactics import Tactics

WORK_BUDGET_S = 0.009       # searches stop after this much of the decision has passed
NAME = "Wilce-B"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


class Bot:
    name = NAME

    def __init__(self, hello, world=None):
        self.hello = hello
        self.cfg = hello.get("config", {})
        self.seed = int(hello.get("seed", 0) or 0)
        self.slot = hello.get("slot")
        self.world = world or World()
        self.zone = Zone(self.cfg)
        self.threats = Threats(self.cfg, self.world, self.slot)
        self.mover = Mover(self.world, self.cfg, self.seed)
        self.cover = CoverState(self.world, random.Random(self.seed ^ 0xC0E5))
        self.gunner = Gunner(self.cfg, self.world, self.seed)
        self.tactics = Tactics(self.world, self.cfg, self.mover, self.cover, self.threats, self.zone, self.seed)
        self.decisions = 0
        self.prev_charge = 0.0
        self.worst_ms = 0.0
        self.last_label = ""
        # Move everything loaded so far out of the collector's way (measured pauses stay under 1 ms).
        gc.collect()
        gc.freeze()

    def act(self, obs):
        t0 = time.perf_counter()
        cfg = self.cfg
        me = Me(obs, cfg)
        self.zone.update(obs["zone"], me.t)
        self.threats.my_id = me.id
        self.threats.update(obs, me)
        self.mover.mob.update(me.stats)
        dec = self.tactics.decide(me, obs)
        # ---- background searches inside the budget
        end = t0 + WORK_BUDGET_S
        self.mover.work(me, self.zone, end - 0.0012)
        self.cover.run(end - 0.0004)
        for srch in (self.tactics.opening_search, self.tactics.second_search):
            if srch is not None and not srch.done and time.perf_counter() < end:
                srch.run(end)
        # ---- legs: an air manoeuvre, a straight walk on a roof, or the mover
        laser = me.by_kind.get("laser")
        released = self.prev_charge > 0 and (laser is None or laser.charge == 0)
        self.prev_charge = laser.charge if laser is not None else 0.0
        airp = self.tactics.air
        if airp is not None and not airp.done:
            if hasattr(airp, "at_height"):
                move, boost, jump, done = airp.step(me, laser_released=released)
            else:
                move, boost, jump, done = airp.step(me)
        elif dec.direct is not None:
            sdodge, surq = self.mover._dodge_shells(me, self.threats.projectiles)
            if sdodge is not None:
                move = [sdodge[0], sdodge[1]]
                boost, jump = bool(surq and me.energy > 8.0), False
            else:
                dx, dy = dec.direct[0] - me.pos[0], dec.direct[1] - me.pos[1]
                d = math.hypot(dx, dy)
                move = [dx / d, dy / d] if d > 0.8 else [0.0, 0.0]
                boost, jump = False, False
        else:
            move, boost, jump = self.mover.steer(me, self.zone, dec.speed, dec.evasive, dec.threat_yaw, dec.hold,
                                                 shells=self.threats.projectiles)
        move_yaw = math.atan2(move[1], move[0]) if math.hypot(move[0], move[1]) > 0.3 else None
        if dec.face is None and dec.target is None:
            dec.face = self.tactics.patrol_yaw(me, move_yaw)
        # ---- torso and triggers
        obstacles = [tr for tr in self.threats.tracks.values() if tr.in_obs and not tr.dead]
        for c in obs.get("crates") or []:
            obstacles.append(_Crate(c))
        aim, fire, reload, info = self.gunner.control(me, dec.intent, dec.target, obstacles, dec.face, move_yaw,
                                                      obs.get("events") or [])
        action = {"tick": obs["tick"], "move": [round(move[0], 4), round(move[1], 4)], "boost": bool(boost),
                  "jump": bool(jump), "aim": [round(aim[0], 5), round(aim[1], 5)], "fire": [bool(fire[0]), bool(fire[1])],
                  "reload": bool(reload)}
        take, drop = dec.take, dec.drop
        if take is None:
            take, drop = self._free_take(me, obs)
        if take is not None:
            action["take"] = take
            if drop is not None:
                action["drop"] = drop
        if dec.pick is not None:
            action["pick"] = dec.pick
        label = (dec.label or "")[:40]
        if label != self.last_label:
            action["debug"] = {"label": label}
            self.last_label = label
        self.decisions += 1
        ms = (time.perf_counter() - t0) * 1000
        if ms > self.worst_ms:
            self.worst_ms = ms
        return action

    def _free_take(self, me, obs):
        """Pick up a wanted weapon we are standing on, whatever else we are doing."""
        reach = float((self.cfg.get("loot") or {}).get("pickup_radius", 4.0))
        kinds = [w.kind for w in me.weapons]
        # a laser before lightning: kills happen at range
        order = {"laser": 0, "lightning": 1}
        for p in sorted(obs.get("pickups") or [], key=lambda q: order.get(q.get("kind"), 9)):
            k = p.get("kind")
            if k not in ("laser", "lightning") or k in kinds:
                continue
            pp = p.get("pos")
            if math.hypot(pp[0] - me.pos[0], pp[1] - me.pos[1]) <= reach - 0.2 and abs(pp[2] - me.pos[2]) <= 3.8:
                if me.slots_free:
                    return p["id"], None
                mort = me.by_kind.get("mortar")
                if mort is not None:
                    return p["id"], mort.slot
                light = me.by_kind.get("lightning")
                if k == "laser" and light is not None and light.cooldown <= 0.2:
                    return p["id"], light.slot
        return None, None


class _Crate:
    """Crates count as lightning targets (it hits the nearest thing in its cone)."""
    __slots__ = ("id", "pos", "kind")

    def __init__(self, c):
        self.id = c.get("id")
        self.pos = c.get("pos")
        self.kind = "crate"
