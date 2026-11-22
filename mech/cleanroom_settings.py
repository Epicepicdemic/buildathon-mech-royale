"""Switches and numbers of the clean-room pieces (mech/cleanroom.py) in free-for-all mode.

Free-for-all mode is also every match's opening: the squad detector (mech/profile.py) decides
nothing before 35 s. Squad-mode play reads the same names with a `cr_` prefix from the
switchboard (mech/squad_settings.py), so the two modes are tuned apart.

All pieces are off by default, so the default build plays exactly like f90bf0a (B2).
A variant commit carries `mech/cleanroom_settings.json` (overrides; an unknown key stops the bot).
For sandbox sweeps the environment variable MECH_CR overrides both modes at once:
    MECH_CR="gear=1,energy=1,engage_m=70"     (a key=value list; ffa: or sq: limits one to a mode)
"""
import json
import os

DEFAULTS = {
    # --- 1. gear first, engage only a real threat or an easy kill
    "gear": 0,
    "gear_until_s": 1e9,    # the gear phase ends at this match time (default: never)
    "engage_m": 60.0,       # a mech in sight this close is a threat
    "hit_s": 4.0,           # ... so is one that hit us this recently
    "aim_m": 110.0,         # ... or one holding its torso on us for a second within this
    "weak_hp": 0.35,        # ... an easy kill: under this HP share ...
    "weak_m": 130.0,        # ... in sight within this
    "endgame_n": 4,         # ... and every mech once this few are alive
    "loot_mult": 1.0,       # loot walk radii x this (on top of the wide `far` radii)
    "chase_s": 22.0,        # an item chased this long without reaching it ...
    "chase_ban_s": 45.0,    # ... is dropped for this long
    # --- 6. hunt: once geared and healthy, turn the gear and HP into kills
    "hunt": 0,
    "hunt_cubes": 3,        # geared: a laser and this many cubes ...
    "hunt_cubes_alone": 8,  # ... or this many without a laser
    "hunt_hp": 0.80,        # start hunting at or over this HP share ...
    "hunt_stop_hp": 0.55,   # ... stop under this
    "hunt_energy": 40.0,    # ... and start only with this much energy
    "hunt_memory_s": 30.0,  # seek mechs seen this recently (and gunfire heard in the last 10 s)
    "hunt_fight_bonus_m": 80.0,  # a mech busy fighting someone else counts as this much closer
    # --- 2. energy and noise: boost only for a reason
    "energy": 0,
    "silent": 0,            # idle travel under the step-sound speed (5 m/s), slack permitting
    "silent_slack_s": 30.0,
    "boost_hit_s": 2.0,     # a retreat boosts only when hit this recently
    "boost_short_m": 75.0,  # lightning held: boost to close on a target beyond its range within this
    # --- 3. hide to heal, hold fire on drones and crates while hurt
    "heal": 0,
    "retreat_hp": 0.50,     # retreat (hide and stand still) under this HP share ...
    "back_hp": 0.92,        # ... until this ...
    "quiet_s": 12.0,        # ... or nothing seen or hit us for this long ...
    "ok_hp": 0.70,          # ... and HP at least this
    "hold_fire_hp": 0.50,   # no drone/crate fire under this HP share (it resets the regen clock)
    "threat_window_s": 8.0, # hide from every mech seen this recently
    "unseen_m": 70.0,       # a hit from out of sight: hide from a point this far down its bearing
    # --- 4. fight cell choice by weapon readiness
    "cells": 0,
    "ring_m": 7.0,          # candidate cells this far out
    "seen_w": 25.0,         # penalty per other enemy that would see the cell
    "range_w": 0.35,        # penalty per metre off the preferred range
    "flip_min_s": 1.2,      # strafe side flips at random after this ...
    "flip_max_s": 3.0,      # ... and before this
    "want_gun_m": 70.0,     # preferred range: gun only
    "want_laser_m": 95.0,   # ... with a laser
    "want_light_m": 32.0,   # ... with lightning
    "avoid_light_m": 60.0,  # ... at least this from an enemy lightning holder when we lack lightning
    "avoid_mortar_m": 80.0, # ... and from a mortar holder
    # --- 5. free-for-all home spot between fights (free-for-all only)
    "home": 0,
    "home_memory_s": 45.0,  # cost the home cell against every mech seen this recently ...
    "home_dwell_s": 30.0,   # ... and keep it this long (or until the circle moves)
}

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cleanroom_settings.json")


def _check(over, where):
    bad = sorted(k for k in over if k not in DEFAULTS)
    if bad:
        raise KeyError("unknown clean-room settings in %s: %s" % (where, ", ".join(bad)))


def load(path):
    with open(path) as f:
        over = json.load(f)
    _check(over, path)
    s = dict(DEFAULTS)
    s.update(over)
    return s


S = load(PATH) if os.path.exists(PATH) and not os.environ.get("MECH_SQUAD_DEFAULTS") else dict(DEFAULTS)


def _env():
    """MECH_CR: 'k=v,...' (both modes), 'ffa:k=v' or 'sq:k=v' (one mode)."""
    ffa, sq = {}, {}
    raw = os.environ.get("MECH_CR", "")
    for part in [p.strip() for p in raw.split(",") if p.strip()]:
        scope = None
        if part.startswith("ffa:") or part.startswith("sq:"):
            scope, part = part.split(":", 1)
        k, _, v = part.partition("=")
        v = float(v) if v else 1.0
        if scope in (None, "ffa"):
            ffa[k] = v
        if scope in (None, "sq"):
            sq[k] = v
    _check(ffa, "MECH_CR")
    _check(sq, "MECH_CR")
    return ffa, sq


ENV_FFA, ENV_SQ = _env()
