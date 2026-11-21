"""Shared fixtures: the recorded match settings and quick constructors."""
import gzip
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mech.threats import Track          # noqa: E402

_cfg = None
_world = None


def stream_rows():
    with gzip.open(os.path.join(ROOT, "check", "stream.jsonl.gz"), "rt") as f:
        return [json.loads(line) for line in f]


def config():
    global _cfg
    if _cfg is None:
        _cfg = stream_rows()[0]["config"]
    return _cfg


def world():
    global _world
    if _world is None:
        from mech.world import World
        _world = World()
    return _world


def track(tid=5, pos=(100.0, 0.0, 0.0), vel=(0.0, 0.0, 0.0), yaw=3.14159, hp=1400.0, max_hp=1400.0,
          weapons=("gun",), los=True, sees_us=True, t=10.0, cubes=0):
    tr = Track(tid, "mech", t)
    tr.pos = list(pos)
    tr.vel = list(vel)
    tr.yaw = yaw
    tr.hp = hp
    tr.max_hp = max_hp
    tr.weapons = list(weapons)
    tr.los = los
    tr.sees_us = sees_us
    tr.in_obs = True
    tr.t_seen = t
    tr.cubes = cubes
    return tr


def self_obs(pos=(0.0, 0.0, 0.0), aim=(0.0, 0.0), hp=1400, weapons=None, energy=100, t=10.0, vel=(0, 0, 0)):
    cfg = config()
    ws = cfg["weapons"]
    if weapons is None:
        weapons = [{"kind": "gun", "cooldown": 0, "ammo": 12, "reload": 0, "stats": dict(ws["gun"])}, None]
    return {"type": "obs", "tick": int(t * 20), "t": t, "alive": 16,
            "self": {"id": 3, "pos": list(pos), "vel": list(vel), "aim": list(aim), "hp": hp, "max_hp": 1400,
                     "energy": energy, "max_energy": 100, "grounded": True, "cubes": 0, "upgrades": [],
                     "weapons": weapons, "in_zone": True, "stats": dict(cfg["mech"])},
            "mechs": [], "projectiles": [], "crates": [], "cubes": [], "pickups": [], "sounds": [], "beams": [],
            "events": [], "drops": [], "offer": None,
            "zone": {"center": [41.3, -72.8], "radius": 560, "next_center": [-42.12, 7.11], "next_radius": 380,
                     "shrink_start": 60, "shrink_end": 110, "dps": 20}}


def weapon(kind, slot_stats=None, **kw):
    cfg = config()
    w = {"kind": kind, "cooldown": 0.0, "stats": dict(cfg["weapons"][kind])}
    if kind == "gun":
        w.update(ammo=12, reload=0)
    if kind == "laser":
        w.update(charge=0.0)
    w.update(kw)
    return w
