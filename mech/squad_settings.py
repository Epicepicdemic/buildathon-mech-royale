"""The switchboard: every number that shapes squad-mode (practice board) play, in one place.

The defaults are B2's (8d7d9cb) values, so the default build plays exactly like B2. A variant is
a commit that changes only `mech/squad_settings.json` (written by tools/variant.py): any key there
overrides the default of the same name; an unknown key stops the bot at start so a typo cannot pass.

Only squad-mode code reads these (mech/profile.py decides the mode). Free-for-all play keeps its
own constants and reads none of them (tests/test_switchboard.py checks that with the read log).
The mode detector's own thresholds are not here: they keep free-for-all safe (0 false squad
switches over the 1,072 seats of 67 recorded top-team free-for-alls) and must not be tuned
against the practice board.
"""
import json
import math
import os

DEFAULTS = {
    # --- no-break rule (squad_no_break): when a retreat is allowed at all
    "lose_s": 1.5,              # clearly losing this long before a break stands
    "lose_near_m": 160.0,       # mechs this close count for "two or three on us"
    "lose_hp_two": 0.55,        # two or more near and our HP under this: losing
    "lose_hp_three": 0.80,      # three or more near and our HP under this: losing
    "lose_hp_alone": 0.35,      # our HP under this against a healthier target: losing
    "finish_hp": 0.30,          # never break while the target is at or under this HP share
    "isolate_m": 24.0,          # isolation cell within this, hidden from all shooters but the target
    "isolate_on": 1,            # 0: no isolation step
    # --- play choice (doctrine.choose_play, squad mode)
    "assault_ratio": 1.5,       # fight-calculation ratio (their time to kill us / ours) for assault
    "even_ratio": 0.72,         # ... for the corner fight / standing fight instead of a break
    "corner_take": 0.72,        # ... from a ready corner
    "their_dps_mult": 1.0,      # scales what the fight calculation expects the house bots to land
    # --- gear before killing (gear_first)
    "gear_on": 1,               # 0: no gear pacing
    "gear_until_s": 65.0,       # pacing ends at this match time
    "gear_kills": 1,            # ... or after more kills than this
    "gear_cubes": 6,            # geared: a laser and this many cubes ...
    "gear_cubes_alone": 10,     # ... or this many cubes without a laser
    "gear_need_laser": 1,       # 0: cubes alone (gear_cubes) count as geared
    "gear_defend_m": 55.0,      # a hunter this close is fought
    "gear_defend_hp": 0.5,      # under this HP share the pacing ends for good
    "gear_near_m": 180.0,       # a second hunter this close: fight
    "gear_min_m": 120.0,        # keep the hunter at least this far ...
    "gear_max_m": 145.0,        # ... and never walk toward it from inside this
    "gear_roam_m": 160.0,       # remembered crates and drones worth the walk
    "gear_laser_m": 240.0,      # laser pickups chased this far while pacing
    "gear_lightning_m": 160.0,  # lightning pickups chased this far while pacing
    "gear_cube_m": 120.0,       # cubes chased this far while pacing
    "hunt_aim_rad": 0.2,        # a hunter holds its torso this close on us ...
    "hunt_aim_s": 1.0,          # ... this long ...
    "hunt_memory_s": 10.0,      # ... and counts as hunting this long after (or after its last hit)
    # --- engagement ranges (doctrine.Assault in squad mode)
    "want_lightning_m": 38.0,   # assault range with lightning
    "want_gun_m": 62.0,         # ... with the gun alone
    "want_laser_m": 85.0,       # ... with a laser (no lightning)
    "floor_lightning_m": 70.0,  # never inside this of a lightning holder (kite back)
    "floor_mortar_m": 95.0,     # never inside this of a mortar holder
    "finish_on": 0,             # 1: close in to finish a weak target (B2: off)
    "finish_m": 20.0,           # ... to this
    "finish_target_hp": 0.38,   # ... when it is under this HP share
    "finish_own_hp": 0.42,      # ... and we are over this
    # --- target priority (doctrine.pick_target in squad mode)
    "score_dps_add": 80.0,      # score = (its DPS on us + this) / max(its HP, score_hp_floor)
    "score_hp_floor": 120.0,
    "sticky": 2.2,              # the current target's multiplier ...
    "sticky_weak": 3.0,         # ... when it is under sticky_weak_hp
    "sticky_weak_hp": 0.35,
    "unseen_mult": 0.35,        # a remembered or heard target
    "engaged_mult": 1.55,       # a target busy with someone else
    "hit_us_mult": 1.3,         # whoever hit us in the last 2 s
    "finishable_mult": 2.1,     # one burst kills it
    "reload_mult": 1.4,         # heard reloading in the last reload_s
    "mortar_mult": None,        # holds a mortar, when we hold a laser: None = 1 + the config's laser
                                # cookoff (B2's doctrine.cookoff_mult, +60% today); a number overrides
    "facing_away_mult": 1.0,    # its torso turned away from us (more than 90 degrees)
    "dist_scale_m": 250.0,      # score / (1 + distance / this)
    "reload_s": 2.2,            # a reload heard this recently is still going on
    "reload_discount": 0.25,    # its gun counts this much in the fight calculation meanwhile
    # --- laser
    "partial_frac": 0.7,        # release early when this share of a release now kills
    # --- loot, crates, drones, healing (tactics._reposition in squad mode)
    "loot_laser_m": 145.0,
    "loot_lightning_m": 95.0,
    "loot_cube_m": 90.0,
    "loot_killdrop_m": 105.0,
    "loot_killdrop_wanted_m": 135.0,
    "crate_m": 88.0,
    "npc_m": 115.0,
    "npc_aggro_m": 135.0,
    "npc_hp": 0.62,             # farm drones only over this HP share (unless one is on us)
    "heal_hp": 0.68,            # heal behind cover under this HP share ...
    "heal_pressure_hp": 0.88,   # ... or under this right after a fight / under pressure
    "loot_slack_s": 13.0,       # zone slack needed to loot
    # --- energy
    "boost_low": 8.0,           # boost until the tank is down to this ...
    "boost_resume": 40.0,       # ... then walk until it is back to this
    "assault_boost_energy": 35.0,  # assault approach boosts over this energy
    "break_boost_energy": 18.0,    # fighting retreat boosts over this energy
    # --- upgrades: ids listed first, in this order (empty: B2's context scores)
    "upgrade_order": [],
}

FLAGS = {"isolate_on", "gear_on", "gear_need_laser", "finish_on"}                 # 0 or 1
FRACS = {"lose_hp_two", "lose_hp_three", "lose_hp_alone", "finish_hp", "gear_defend_hp",
         "sticky_weak_hp", "npc_hp", "heal_hp", "heal_pressure_hp", "finish_target_hp",
         "finish_own_hp", "reload_discount"}                                     # shares in [0, 1]
COUNTS = {"gear_kills", "gear_cubes", "gear_cubes_alone"}                       # whole numbers >= 0
DIVISORS = {"dist_scale_m", "score_hp_floor"}                                   # divided by: > 0
OPTIONAL = {"mortar_mult"}                                                      # None or a number >= 0
LISTS = {"upgrade_order"}                                                       # lists of upgrade ids


def _reject_constant(c):
    raise ValueError("squad settings: %s is not a finite number" % c)


def check(name, v):
    """Refuse a value the bot must not run with (raises at import, so a bad variant never plays)."""
    def num(x):
        return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
    if name in LISTS:
        if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v) or len(set(v)) != len(v):
            raise TypeError("squad setting %s: %r must be a list of distinct upgrade ids" % (name, v))
        return
    if name in OPTIONAL and v is None:
        return
    if not num(v):
        raise TypeError("squad setting %s: %r is not a finite number" % (name, v))
    if name in FLAGS and v not in (0, 1):
        raise ValueError("squad setting %s: %r must be 0 or 1" % (name, v))
    if name in FRACS and not 0.0 <= v <= 1.0:
        raise ValueError("squad setting %s: %r must be within [0, 1]" % (name, v))
    if name in COUNTS and (v < 0 or v != int(v)):
        raise ValueError("squad setting %s: %r must be a whole number >= 0" % (name, v))
    if name in DIVISORS and v <= 0:
        raise ValueError("squad setting %s: %r must be > 0" % (name, v))
    if v < 0:
        raise ValueError("squad setting %s: %r must be >= 0" % (name, v))


def load(path):
    """DEFAULTS with the overrides in `path`; anything the bot must not run with stops it at import:
    a top level that is not an object, an unknown key, a wrong type, a value out of range (finite
    numbers, flags 0/1, shares in [0, 1], whole counts, positive divisors, distinct upgrade ids),
    or a boost reserve above its resume level."""
    with open(path) as f:
        over = json.load(f, parse_constant=_reject_constant)
    if not isinstance(over, dict):
        raise TypeError("squad settings: the top level must be an object, not %s" % type(over).__name__)
    bad = sorted(k for k in over if k not in DEFAULTS)
    if bad:
        raise KeyError("unknown squad settings: %s" % ", ".join(bad))
    for k, v in over.items():
        check(k, v)
    s = dict(DEFAULTS)
    s.update(over)
    if s["boost_low"] > s["boost_resume"]:
        raise ValueError("squad settings: boost_low %r is above boost_resume %r" % (s["boost_low"], s["boost_resume"]))
    return s


PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "squad_settings.json")
# the unit tests pin B2's behaviour, so they run on the defaults (tests/__init__.py sets this);
# the variant file itself is still checked there (test_switchboard)
S = load(PATH) if os.path.exists(PATH) and not os.environ.get("MECH_SQUAD_DEFAULTS") else dict(DEFAULTS)

TRACE = False       # tests: record every read
READS = []


def get(name):
    if TRACE:
        READS.append(name)
    return S[name]
