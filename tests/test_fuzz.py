"""Every observation field the sandbox never produces (lasers, lightning, mortars, loot, cubes,
crates, drops, offers, effects, NPCs), thrown at the bot at random for a few thousand decisions.
Each action must be schema-valid and act() must never raise (the wrapper would hide it in a match)."""
import json
import math
import os
import random
import sys
import time
import unittest

from tests.helpers import ROOT, config, weapon, world
from tests.speed import speed_factor
from mech.brain import Bot

sys.path.insert(0, os.path.join(ROOT, "check"))
import check as kitcheck          # noqa: E402

KINDS = ["gun", "laser", "lightning", "mortar"]


def random_obs(rng, w, t, tick, me_pos, cfg):
    x, y = me_pos
    z = w.floor(x, y)
    ws = [weapon("gun", ammo=rng.randint(0, 12), reload=rng.choice([0.0, 0.0, 1.2, 6.9e-17]))]
    second = rng.choice([None, "laser", "lightning", "mortar"])
    if second == "laser":
        ws.append(weapon("laser", charge=rng.choice([0.0, 0.0, 0.3, 0.75]), cooldown=rng.choice([0.0, 0.8])))
    elif second == "lightning":
        ws.append(weapon("lightning", cooldown=rng.choice([0.0, 0.1])))
    elif second == "mortar":
        ws.append(weapon("mortar", cooldown=rng.choice([0.0, 1.5])))
    else:
        ws.append(None)
    if rng.random() < 0.1:
        ws = [ws[1], ws[0]] if ws[1] is not None else ws
    mechs = []
    for k in range(rng.randint(0, 4)):
        a = rng.uniform(-math.pi, math.pi)
        d = rng.uniform(5, 260)
        mx, my = x + math.cos(a) * d, y + math.sin(a) * d
        mechs.append({"id": rng.randint(0, 15), "kind": "mech", "pos": [mx, my, w.floor(mx, my)],
                      "vel": [rng.uniform(-12, 12), rng.uniform(-12, 12), 0.0],
                      "aim": [rng.uniform(-math.pi, math.pi), rng.uniform(-0.3, 0.3)],
                      "hp": rng.uniform(10, 1400), "max_hp": 1400, "cubes": rng.randint(0, 12),
                      "weapons": rng.sample(KINDS, rng.randint(1, 2)), "charging": rng.random() < 0.3})
    for k in range(rng.randint(0, 2)):
        a = rng.uniform(-math.pi, math.pi)
        d = rng.uniform(10, 120)
        nx, ny = x + math.cos(a) * d, y + math.sin(a) * d
        mechs.append({"id": 100 + rng.randint(0, 11), "kind": "npc", "pos": [nx, ny, w.floor(nx, ny)],
                      "vel": [1.0, 0.0, 0.0], "aim": [0.0, 0.0], "hp": 350, "max_hp": 350})
    def near(r):
        a = rng.uniform(-math.pi, math.pi)
        d = rng.uniform(1, r)
        px, py = x + math.cos(a) * d, y + math.sin(a) * d
        return [px, py, w.floor(px, py)]
    sounds = [{"kind": rng.choice(["gun", "mortar", "explosion", "laser", "laser_charge", "lightning", "jet",
                                   "death", "crate", "npc_gun", "step", "boost", "reload", "drop"]),
               "bearing": rng.uniform(-math.pi, math.pi), "dist": rng.choice(["near", "mid", "far"]),
               "age": rng.uniform(0, 0.1)} for _ in range(rng.randint(0, 4))]
    events = []
    for _ in range(rng.randint(0, 3)):
        et = rng.choice(["damaged", "hit", "effect", "drop", "kill", "pickup", "upgrade", "took", "dropped",
                         "take_failed"])
        if et == "damaged":
            zone = rng.random() < 0.2
            events.append({"type": et, "amount": 45, "weapon": "zone" if zone else rng.choice(KINDS + ["npc"]),
                           "bearing": None if zone else rng.uniform(-3, 3), "by": None if zone else rng.randint(0, 15),
                           "rear": rng.random() < 0.3})
        elif et == "hit":
            events.append({"type": et, "target": rng.choice([rng.randint(0, 15), 104, 1003]), "amount": 45,
                           "weapon": "gun"})
        elif et == "effect":
            events.append({"type": et, "effect": rng.choice(["cookoff", "short", "concuss"]), "by": 4})
        elif et == "kill":
            events.append({"type": et, "target": rng.randint(0, 15)})
        elif et == "pickup":
            events.append({"type": et, "cubes": rng.randint(1, 12)})
        elif et == "drop":
            events.append({"type": et, "id": 20000, "cubes": 5})
        elif et == "upgrade":
            events.append({"type": et, "id": "plating"})
        elif et in ("took", "dropped"):
            events.append({"type": et, "id": 8003, "kind": "laser", "slot": 1})
        else:
            events.append({"type": et, "id": 8003, "reason": "gone"})
    offer = None
    if rng.random() < 0.05:
        ups = [u["id"] for u in cfg["upgrades"]]
        offer = {"id": 1, "expires": t + 5, "choices": [{"id": u, "name": u, "desc": u} for u in rng.sample(ups, 3)]}
    proj = []
    for _ in range(rng.randint(0, 3)):
        p = near(80)
        p[2] += rng.uniform(2, 40)
        proj.append({"id": rng.randint(100, 900), "kind": rng.choice(["gun", "mortar", "npc"]), "pos": p,
                     "vel": [rng.uniform(-70, 70), rng.uniform(-70, 70), rng.uniform(-30, 30)]})
    beams = [{"by": rng.randint(0, 15), "kind": rng.choice(["laser", "lightning"]), "path": [near(100), near(100)],
              "age": 0.05} for _ in range(rng.randint(0, 2))]
    nr = rng.choice([380, 240, 140, 70, 25, 0])
    return {"type": "obs", "tick": tick, "t": t, "alive": rng.randint(2, 16),
            "self": {"id": 3, "pos": [x, y, z + rng.choice([0.0, 0.0, 6.0])], "vel": [rng.uniform(-12, 12), rng.uniform(-12, 12), 0],
                     "aim": [rng.uniform(-3, 3), rng.uniform(-0.5, 0.5)], "hp": rng.uniform(1, 1400), "max_hp": 1400,
                     "energy": rng.uniform(0, 100), "max_energy": 100, "grounded": rng.random() < 0.8,
                     "cubes": rng.randint(0, 12), "upgrades": [], "weapons": ws, "in_zone": rng.random() < 0.9,
                     "stats": dict(cfg["mech"])},
            "mechs": mechs, "projectiles": proj,
            "crates": [{"id": 1000 + k, "pos": near(70), "hp": 300} for k in range(rng.randint(0, 2))],
            "cubes": [{"id": 5000 + k, "pos": near(60)} for k in range(rng.randint(0, 3))],
            "pickups": [{"id": 8000 + k, "kind": rng.choice(KINDS[1:]), "pos": near(30)} for k in range(rng.randint(0, 2))],
            "sounds": sounds, "beams": beams, "events": events,
            "drops": [{"id": 20000, "pos": near(150), "radius": 8, "lands_in": rng.choice([0.0, 10.0]), "capture_s": 4,
                       "progress": 0.0, "holder": rng.choice([None, 3, 7])}] if rng.random() < 0.3 else [],
            "zone": {"center": [41.3, -72.8], "radius": 560, "next_center": [x + rng.uniform(-200, 200), y + rng.uniform(-200, 200)],
                     "next_radius": nr, "shrink_start": t + rng.uniform(-20, 30), "shrink_end": t + rng.uniform(31, 60),
                     "dps": 20},
            "offer": offer}


class FuzzTests(unittest.TestCase):
    def test_random_observations_never_break_the_bot(self):
        cfg = config()
        w = world()
        schema = kitcheck.Schema(os.path.join(ROOT, "mechfight.schema.json"))
        rng = random.Random(11)
        mains = [n for n in range(0, w.nu * w.nv, 97) if w.main[n]]
        decisions = 0
        cpu = []
        for episode in range(6):
            bot = Bot({"type": "hello", "config": cfg, "seed": episode, "slot": 3}, world=w)
            pos = w.node_xy(rng.choice(mains))
            t = 0.0
            for k in range(400):
                if rng.random() < 0.05:
                    pos = w.node_xy(rng.choice(mains))
                obs = random_obs(rng, w, t, 2 * k, pos, cfg)
                c0 = time.thread_time()
                a = bot.act(obs)
                cpu.append((time.thread_time() - c0) * 1000)
                a["type"] = "action"
                errs = schema.errors(a, "Action")
                self.assertEqual(errs, [], (episode, k, a))
                self.assertLessEqual(len(json.dumps(a)), 65536)
                decisions += 1
                t += 0.1
        self.assertEqual(decisions, 2400)
        cpu.sort()
        # the search budget is fixed work, so CPU time follows this machine's speed: the 20 ms target holds on
        # a machine as fast as one c7i worker CPU and scales up on slower ones (tests/speed.py) ...
        factor = speed_factor(w)
        limit = 20.0 * factor
        print("\nfuzz: %d decisions, cpu p50 %.1f p99 %.1f worst %.1f ms (speed factor %.2f, limit %.1f ms, backstop 35 ms)"
              % (len(cpu), cpu[len(cpu) // 2], cpu[int(len(cpu) * 0.99)], cpu[-1], factor, limit))
        # every zone change and dozens of entities each tick: still inside the scaled 20 ms target ...
        self.assertLess(cpu[-1], limit)
        # ... and never a decision over 35 ms of CPU on any machine (headroom under the match's 50 ms)
        self.assertLess(cpu[-1], 35.0)


if __name__ == "__main__":
    unittest.main()
