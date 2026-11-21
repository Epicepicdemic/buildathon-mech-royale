"""Fighting doctrine (analysis/mech/DOCTRINE.md): the fight calculation and the five plays.

Every decision, for each known enemy, estimate the time for us to kill it and the time for it
(and anyone else shooting at us) to kill us. Their ratio, and how many other threats are near,
picks one of five named plays:

    ASSAULT     we kill at least 1.5x faster and nobody else is close (or it is finishable)
    CORNER      roughly even, or ahead but others are near: step out, fire, step back
    HARASS      the enemy is retreating to heal and we have a sight line: one hit every 4 s
    BREAK       behind, outnumbered, or hit by something we cannot see: run to cover
    REPOSITION  no threat: move, loot, heal

Each play is a class with `act(sit, ctx) -> Intent`; `choose_play(sit)` picks one. Nothing here
touches the network or the clock, so each piece can be tested by feeding it a Situation.
"""
import math

from . import profile as PF

from .ballistics import reversal_range
from .geom import angdiff, bearing

ASSAULT, CORNER, HARASS, BREAK, REPOSITION = "assault", "corner", "harass", "break", "reposition"

ASSAULT_RATIO = 1.5
EVEN_RATIO = 0.72
NEAR_OTHERS_M = 160.0
FINISH_TARGET_HP = 0.38     # finish_close: the target under this share of HP ...
FINISH_OWN_HP = 0.42        # ... us over this ...
FINISH_M = 20.0             # ... closes to this, but never inside
FINISH_LIGHTNING_M = 58.0   # ... its lightning (unless ours is ready: energy over 35) ...
FINISH_MORTAR_M = 75.0      # ... or its mortar's throw-back
STICKY = 2.2                # sticky_target: the current target's score multiplier ...
STICKY_WEAK = 3.0           # ... when it is under 35% HP
RELOAD_S = 2.2              # punish_reload: a reload heard this recently is still going on
HOUSE_LIGHTNING_M = 70.0    # house_aim (squad): never inside this of a lightning holder ...
HOUSE_MORTAR_M = 95.0       # ... or this of a mortar holder
# house_aim: house:expert hit rates by range (analysis/mech/HOUSE-BOTS.md section 1, measured over 42
# matches; the laser's split under 105 m is ours, fitted to its 25.6% overall and 70 m median hit)
HOUSE_GUN = ((45.0, 0.875), (78.0, 0.634), (105.0, 0.431), (135.0, 0.282), (150.0, 0.279), (1e9, 0.169))
HOUSE_LASER = ((45.0, 0.50), (78.0, 0.40), (105.0, 0.25), (135.0, 0.12), (150.0, 0.067), (1e9, 0.04))


def _band(table, dist):
    for top, p in table:
        if dist < top:
            return p
    return table[-1][1]


def cookoff_mult(enemy, cfg):
    """mortar_priority: a laser hit on a mech holding a mortar sets it off (x1.6 by the config)."""
    if not PF.on("mortar_priority") or not getattr(enemy, "has", lambda k: False)("mortar"):
        return 1.0
    return 1.0 + float(_wstats(cfg, "laser").get("cookoff", 0.6))


def reloading(enemy, t):
    return PF.on("punish_reload") and t - getattr(enemy, "t_reload_heard", -1e9) <= RELOAD_S
HYST = 0.2
CORNER_TAKE = 0.72      # from a corner we fight unless the calculation says we clearly lose


# ---------------------------------------------------------------------------- weapon numbers
def _wstats(cfg, kind):
    return (cfg.get("weapons") or {}).get(kind, {}) if cfg else {}


def cube_mult(cubes, cfg=None):
    c = (cfg or {}).get("cubes", {})
    return 1.0 + float(c.get("damage_per_cube", 0.04)) * min(cubes or 0, int(c.get("stat_cap", 10)))


def gun_hit_prob(dist, dodging, airborne=False, speed=252.0, spread_deg=0.6, rng=230.0):
    """Chance a led gun shot lands. Inside the reversal range a led shot lands whatever the
    target does; beyond it a dodging target is mostly missed. Spread costs a little at range."""
    if dist > rng:
        return 0.0
    sigma = math.radians(spread_deg) * dist
    p_spread = 1.0 if sigma < 0.5 else min(1.0, 2.5 / (sigma * 2.2))
    p_spread = max(0.6, min(1.0, p_spread))
    rr = reversal_range(speed, accel=12.0 if airborne else 58.5)
    if dist <= rr:
        return p_spread
    # recorded matches: ~59% of shots hit at 78-150 m and ~42% beyond, against bots that mostly do
    # not dodge; a target that keeps changing direction is much harder
    base = 0.62 if dist <= 150 else 0.45
    if dodging:
        base *= 0.45
    return min(p_spread, base)


def gun_dps(stats, cubes, cfg=None):
    """(burst dps while the magazine lasts, sustained dps over reloads)."""
    dmg = float(stats.get("damage", 45.0)) * cube_mult(cubes, cfg)
    cd = max(0.05, float(stats.get("cooldown", 0.24)))
    mag = float(stats.get("mag", 12.0))
    rl = float(stats.get("reload", 2.4))
    return dmg / cd, dmg * mag / (mag * cd + rl)


def weapon_dps(kind, stats, cubes, dist, target_dodging, target_still, energy=100.0, cfg=None, airborne=False):
    """Damage per second actually landed by one weapon at this range."""
    m = cube_mult(cubes, cfg)
    if kind == "gun":
        _, sus = gun_dps(stats, cubes, cfg)
        p = gun_hit_prob(dist, target_dodging, airborne, float(stats.get("speed", 252.0)),
                         float(stats.get("spread_deg", 0.6)), float(stats.get("range", 230.0)))
        return sus * p
    if kind == "laser":
        if dist > float(stats.get("range", 280.0)):
            return 0.0
        cyc = float(stats.get("charge", 0.75)) + float(stats.get("cooldown", 1.65))
        return float(stats.get("damage", 315.0)) * m / cyc * 0.85
    if kind == "lightning":
        if dist > float(stats.get("range", 45.0)) or energy < float(stats.get("energy", 3.0)) * 4:
            return 0.0
        return float(stats.get("damage", 60.0)) * m / max(0.05, float(stats.get("cooldown", 0.25)))
    if kind == "mortar":
        if dist > 230.0 or dist < 12.0:
            return 0.0
        dmg = float(stats.get("damage", 200.0)) + 0.5 * float(stats.get("splash", 160.0))
        p = 0.6 if target_still else 0.15
        return dmg * m / max(0.5, float(stats.get("cooldown", 2.6))) * p
    return 0.0


# ---------------------------------------------------------------------------- the situation
class Situation:
    """Everything the doctrine needs about one moment, as plain numbers."""

    def __init__(self, t, me_pos, me_yaw, me_hp, me_max_hp, me_weapons, enemy=None, others=(),
                 me_cubes=0, me_energy=100.0, me_armor=0.0, corner=False, unseen_hit=False,
                 long_sight=True, cfg=None, airborne=False, healing=False, gun_ammo=None, shorted=False,
                 house=False):
        self.t = t
        self.me_pos = me_pos
        self.me_yaw = me_yaw
        self.me_hp = me_hp
        self.me_max_hp = me_max_hp
        self.me_weapons = me_weapons          # {kind: stats}
        self.enemy = enemy
        self.others = list(others)
        self.me_cubes = me_cubes
        self.me_energy = me_energy
        self.me_armor = me_armor
        self.corner = corner
        self.unseen_hit = unseen_hit
        self.long_sight = long_sight
        self.cfg = cfg or {}
        self.airborne = airborne
        self.healing = healing
        self.gun_ammo = gun_ammo
        self.shorted = shorted
        self.house = house                    # house_aim: squad mode, the opponents are house bots


class FightCalc:
    """Time-to-kill both ways for one enemy."""

    def __init__(self, sit, enemy):
        self.sit = sit
        self.enemy = enemy
        e = enemy
        p = e.est_pos(sit.t) if hasattr(e, "est_pos") else e.pos
        self.dist = math.hypot(p[0] - sit.me_pos[0], p[1] - sit.me_pos[1])
        # are we behind it? (outside its front 180 degrees)
        to_me = bearing(p, sit.me_pos)
        self.rear = angdiff(getattr(e, "yaw", 0.0), to_me) > math.pi / 2
        # is it facing away from us (unaware) or still?
        self.unaware = not getattr(e, "sees_us", True) and sit.t - getattr(e, "t_hit_us", -1e9) > 3.0
        still = e.still() if hasattr(e, "still") else False
        dodging = e.dodging() if hasattr(e, "dodging") else True
        # airborne_aim: a mech in the air cannot reverse (12 m/s^2 net jet), so our led gun lands
        # to ~170 m (brainstorm/r12/mech-carbon.md: 91.6% measured over 22,234 airborne frames)
        self.e_airborne = PF.on("airborne_aim") and bool(getattr(e, "airborne", False))
        cfg = sit.cfg
        ours = 0.0
        for kind, st in sit.me_weapons.items():
            if kind == "laser" and sit.shorted:
                continue
            d = weapon_dps(kind, st, sit.me_cubes, self.dist, dodging and not self.unaware, still,
                           sit.me_energy, cfg, airborne=self.e_airborne)
            if kind == "laser":
                d *= cookoff_mult(e, cfg)
            ours += d
        if self.rear:
            ours *= float((cfg.get("mech") or {}).get("rear_mult", 1.5))
        self.our_dps = ours
        hp = e.hp if getattr(e, "hp", None) is not None else 1400.0
        self.enemy_hp = hp
        self.ttk_us = hp / ours if ours > 1e-6 else math.inf
        # what it lands on us: we keep moving and dodge beyond the reversal range
        theirs = self.their_dps(e, self.dist)
        if e.engaged_elsewhere(sit.t) if hasattr(e, "engaged_elsewhere") else False:
            theirs *= 0.45
        if self.unaware:
            theirs *= 0.6
        self.their_dps_on_us = theirs
        # its threat over the fight (a reload is a moment to punish, not a reason to rank it lower)
        self.threat_dps = theirs if not reloading(e, sit.t) else theirs * self.their_dps(e, self.dist, False) \
            / max(self.their_dps(e, self.dist), 1e-6)
        extra = 0.0
        for o in sit.others:
            po = o.est_pos(sit.t) if hasattr(o, "est_pos") else o.pos
            d = math.hypot(po[0] - sit.me_pos[0], po[1] - sit.me_pos[1])
            busy = (o.engaged_elsewhere(sit.t) if hasattr(o, "engaged_elsewhere") else False) \
                and sit.t - getattr(o, "t_hit_us", -1e9) > 2.5
            f = 0.2 if busy else (1.0 if getattr(o, "sees_us", True) else 0.5)
            extra += self.their_dps(o, d) * f
        self.others_dps = extra
        incoming = theirs + extra
        self.incoming = incoming
        self.ttk_them = sit.me_hp / incoming if incoming > 1e-6 else math.inf
        # from a corner we are exposed about half of each cycle
        self.ttk_them_corner = self.ttk_them * 2.0
        if self.ttk_us == math.inf:
            self.ratio = 0.0 if incoming > 0 else 1.0
            self.ratio_corner = self.ratio
        else:
            self.ratio = self.ttk_them / self.ttk_us
            self.ratio_corner = self.ttk_them_corner / self.ttk_us
        # one long burst (full magazine) worth of damage
        burst = 0.0
        g = sit.me_weapons.get("gun")
        if g:
            burst += float(g.get("damage", 45.0)) * cube_mult(sit.me_cubes, cfg) * float(g.get("mag", 12.0)) * 0.9
        if "laser" in sit.me_weapons and not sit.shorted:
            burst += float(sit.me_weapons["laser"].get("damage", 315.0)) * cube_mult(sit.me_cubes, cfg) * cookoff_mult(e, cfg)
        if "lightning" in sit.me_weapons and self.dist <= float(sit.me_weapons["lightning"].get("range", 45.0)):
            burst += 60.0 * 10 * cube_mult(sit.me_cubes, cfg)
        if self.rear:
            burst *= 1.5
        self.burst = burst
        self.finishable = hp <= burst

    def their_dps(self, e, dist, discount_reload=True):
        sit = self.sit
        cfg = sit.cfg
        m = cube_mult(getattr(e, "cubes", 0), cfg)
        wpn = getattr(e, "weapons", ["gun"]) or ["gun"]
        tot = 0.0
        we_dodge = not sit.airborne
        house = sit.house and PF.on("house_aim")
        for kind in wpn:
            st = _wstats(cfg, kind)
            if kind == "gun" and house:
                _, sus = gun_dps(st or {}, getattr(e, "cubes", 0), cfg)
                p = 0.0 if dist > float((st or {}).get("range", 230.0)) else _band(HOUSE_GUN, dist)
                tot += sus * p * (0.25 if discount_reload and reloading(e, sit.t) else 1.0)
            elif kind == "laser" and house:
                if dist <= float((st or {}).get("range", 280.0)):
                    tot += 315.0 * m / 2.4 * _band(HOUSE_LASER, dist)
            elif kind == "lightning" and house:
                if dist <= float((st or {}).get("range", 45.0)) + 3:
                    tot += 240.0 * m * 0.83                     # 83.2% of expert zaps land
            elif kind == "mortar" and house:
                if 12.0 < dist <= 235.0:
                    tot += 280.0 * m / 2.6 * 0.38               # 37.7% of expert shells land
            elif kind == "gun":
                _, sus = gun_dps(st or {}, getattr(e, "cubes", 0), cfg)
                rr = reversal_range(float((st or {}).get("speed", 252.0)), accel=12.0 if sit.airborne else 58.5)
                if dist > float((st or {}).get("range", 230.0)):
                    p = 0.0
                elif dist <= rr or not we_dodge:
                    p = 0.8
                else:
                    p = max(0.15, 0.8 * rr / dist * 0.6)
                # (no reload discount here: in a free-for-all a reload heard is no reason to close in;
                # top teams land 85% of gun shots at 45-78 m, recon/replays/tables.md, 67 matches)
                tot += sus * p
            elif kind == "laser":
                if dist <= float((st or {}).get("range", 280.0)):
                    tot += 315.0 * m / 2.4 * 0.8
            elif kind == "lightning":
                if dist <= float((st or {}).get("range", 45.0)) + 3:
                    tot += 240.0 * m * 0.9
            elif kind == "mortar":
                if 12.0 < dist <= 235.0:
                    tot += 280.0 * m / 2.6 * 0.3
        return tot * (1.0 - min(0.9, sit.me_armor))


# ---------------------------------------------------------------------------- intents
class Intent:
    """What a play wants this decision. The mover and the gunner carry it out."""

    def __init__(self, play, target=None):
        self.play = play
        self.target = target
        self.goal = None            # (x, y) to walk to (route)
        self.spot = None            # 'hide' | 'peek' | 'approach' | 'retreat' | None
        self.speed = "walk"         # 'silent' | 'walk' | 'boost'
        self.evasive = False        # irregular direction changes
        self.fire = "none"          # 'none' | 'single' | 'short' | 'long'
        self.face = None            # yaw to hold the torso at when not shooting
        self.reload_ok = False      # a good moment to reload
        self.hold = False           # stand still
        self.cover_near = True      # a hidden cell one step away (long bursts at range need it)
        self.no_laser_start = False # keep full 1.0x walk/boost speed during fighting retreats
        self.label = play


def rear_point(enemy_pos, enemy_yaw, dist):
    """A point `dist` behind the enemy's facing."""
    return (enemy_pos[0] - math.cos(enemy_yaw) * dist, enemy_pos[1] - math.sin(enemy_yaw) * dist)


class Assault:
    """Close under cover to 45 m with lightning (else 60-85 m), arrive on the rear arc when the
    approach allows, keep outside enemy lightning range if we lack lightning, and fire long bursts."""
    name = ASSAULT

    def act(self, sit, ctx):
        e = sit.enemy
        it = Intent(ASSAULT, e)
        p = e.est_pos(sit.t) if hasattr(e, "est_pos") else e.pos
        calc = ctx.get("calc") if ctx else None
        dist = math.hypot(p[0] - sit.me_pos[0], p[1] - sit.me_pos[1])
        want = 38.0 if "lightning" in sit.me_weapons else 62.0
        if "laser" in sit.me_weapons and "lightning" not in sit.me_weapons:
            want = 85.0
        house = getattr(sit, "house", False) and PF.on("house_aim")
        if getattr(e, "has", lambda k: False)("mortar"):
            # its blast throws us back and drains the energy lightning needs: stay at gun/laser range
            want = max(want, HOUSE_MORTAR_M if house else 75.0)
        if getattr(e, "has", lambda k: False)("lightning") and ("lightning" not in sit.me_weapons or house):
            # never walk into a 240-DPS lightning cone when we outrange it with gun/laser (house
            # experts with gun and lightning rush at every range and score 36% of their kills
            # with it: a hard floor of 70 m)
            want = max(want, HOUSE_LIGHTNING_M if house else 58.0)
        if PF.on("finish_close"):
            # finish a weak target from close (recon/rea-mech-royale.md P2): it under 38% HP, us over
            # 42%, at most two others near
            e_hp = e.hp_frac() if hasattr(e, "hp_frac") else 1.0
            if e_hp < FINISH_TARGET_HP and sit.me_hp / max(sit.me_max_hp, 1.0) > FINISH_OWN_HP \
                    and len(sit.others) <= 2:
                # the clamps stand (brainstorm/r12: 20 m walked 928be36 into lightning range)
                fin = FINISH_M
                has = getattr(e, "has", lambda k: False)
                if has("lightning") and not ("lightning" in sit.me_weapons and sit.me_energy >= 35.0):
                    fin = max(fin, FINISH_LIGHTNING_M)
                if has("mortar"):
                    fin = max(fin, FINISH_MORTAR_M)
                want = min(want, fin)
        if dist > want:
            # approach: toward the rear side when it is not looking at us
            if not getattr(e, "sees_us", True):
                it.goal = rear_point(p, getattr(e, "yaw", 0.0), want * 0.7)
            else:
                it.goal = (p[0], p[1])
            it.spot = "approach"
            it.speed = "boost" if dist > want + 25 and sit.me_energy > 35 else "walk"
        elif getattr(e, "has", lambda k: False)("lightning") and ("lightning" not in sit.me_weapons or house) \
                and dist < (HOUSE_LIGHTNING_M if house else 49.0):
            # kite back outside 45 m while firing full bursts
            dx, dy = sit.me_pos[0] - p[0], sit.me_pos[1] - p[1]
            d = math.hypot(dx, dy) or 1.0
            it.goal = (sit.me_pos[0] + dx / d * 24.0, sit.me_pos[1] + dy / d * 24.0)
            it.spot = "retreat"
            it.speed = "boost" if sit.me_energy > 20 else "walk"
            it.no_laser_start = True
        else:
            it.goal = None
            it.spot = "strafe"
        it.evasive = dist > 60.0 or getattr(e, "has", lambda k: True)("gun")
        it.fire = "long"
        it.reload_ok = sit.gun_ammo is not None and sit.gun_ammo == 0
        if calc is not None and calc.finishable:
            it.label = "assault: finish"
        return it


class CornerFight:
    """Step out from a wall corner, fire, step back. Reload behind the wall. If no corner is ready
    yet (e.g. t = 0), fire full bursts immediately while strafing toward cover."""
    name = CORNER

    def act(self, sit, ctx):
        e = sit.enemy
        it = Intent(CORNER, e)
        hide = ctx.get("hide") if ctx else None
        peek = ctx.get("peek") if ctx else None
        reloading = ctx.get("reloading", False) if ctx else False
        ammo = sit.gun_ammo
        weapon_ready = ctx.get("weapon_ready", True) if ctx else True
        hurt_now = ctx.get("hurt_in_exposure", False) if ctx else False
        if reloading or (ammo is not None and ammo == 0 and "gun" in sit.me_weapons and len(sit.me_weapons) == 1) \
                or not weapon_ready or hurt_now:
            it.goal = hide
            it.spot = "hide"
            it.reload_ok = True
            it.fire = "none"
        else:
            it.goal = peek
            it.spot = "peek"
            it.fire = "long"
        if it.goal is None:
            # no corner ready yet: fire full bursts while strafing rather than throttling or waiting
            it.spot = "strafe"
            it.fire = "long" if weapon_ready else "none"
            it.evasive = True
        it.speed = "walk"
        return it


class Harass:
    """Keep a retreating enemy from healing: one hit every four seconds, from where we are."""
    name = HARASS
    PERIOD = 4.0

    def act(self, sit, ctx):
        e = sit.enemy
        it = Intent(HARASS, e)
        last = ctx.get("last_harass_hit", -1e9) if ctx else -1e9
        it.fire = "single" if sit.t - last >= self.PERIOD - 0.6 else "none"
        it.goal = ctx.get("hide") if ctx and sit.t - last < self.PERIOD - 1.5 else None
        it.spot = "hold"
        it.evasive = True
        it.reload_ok = True
        return it


class BreakToCover:
    """Fighting retreat: boost/jink to the nearest cell hidden from known threats with torso locked
    on the attacker, keeping gun/lightning firing at full movement speed until LOS is broken."""
    name = BREAK

    def act(self, sit, ctx):
        e = sit.enemy
        it = Intent(BREAK, e)
        hide = ctx.get("hide") if ctx else None
        it.goal = hide
        it.spot = "hide" if hide is not None else "retreat"
        it.speed = "boost" if sit.me_energy > 20 else "walk"
        it.evasive = True
        has_los = bool(e is not None and getattr(e, "los", False)) or bool(ctx and ctx.get("shootable", False))
        if has_los:
            it.fire = "long"
            it.no_laser_start = True
            it.reload_ok = sit.gun_ammo is not None and sit.gun_ammo == 0
            it.label = "break: fighting"
        else:
            it.fire = "none"
            it.reload_ok = True
        danger = ctx.get("danger_yaw") if ctx else None
        it.face = danger
        return it


class Reposition:
    """No threat: the tactics layer chooses where to go (cover, loot, crates, the next circle)."""
    name = REPOSITION

    def act(self, sit, ctx):
        it = Intent(REPOSITION, None)
        it.fire = "none"
        it.reload_ok = True
        return it


PLAYS = {p.name: p for p in (Assault(), CornerFight(), Harass(), BreakToCover(), Reposition())}


def retreating(sit, enemy):
    """Enemy is pulling away to heal: hurt, moving away from us or gone out of sight."""
    if enemy is None or getattr(enemy, "hp", None) is None:
        return False
    hf = enemy.hp_frac() if hasattr(enemy, "hp_frac") else 1.0
    if hf > 0.8:
        return False
    p = enemy.est_pos(sit.t) if hasattr(enemy, "est_pos") else enemy.pos
    dx, dy = p[0] - sit.me_pos[0], p[1] - sit.me_pos[1]
    d = math.hypot(dx, dy) or 1.0
    radial = (enemy.vel[0] * dx + enemy.vel[1] * dy) / d
    return radial > 2.0 or not getattr(enemy, "los", True)


def choose_play(sit, calc=None, prev=None, squad=False):
    """Pick one of the five plays for this decision. Returns (play name, FightCalc or None).
    `squad` (mech/profile.py, squad_no_break): against the practice squad, which shares vision so
    hiding buys nothing, a second mech within 160 m and HP under 30% are not reasons to break; the
    tactics layer retreats only after 1.5 s of clearly losing."""
    e = sit.enemy
    if e is None:
        if sit.unseen_hit:
            return BREAK, None
        return REPOSITION, None
    calc = calc or FightCalc(sit, e)
    # Others that are busy fighting someone else (and not shooting us) are a third-party opening, not a 2v1 on us.
    hostile_others = [
        o for o in sit.others
        if _dist(sit, o) <= NEAR_OTHERS_M
        and not ((o.engaged_elsewhere(sit.t) if hasattr(o, "engaged_elsewhere") else False)
                 and sit.t - getattr(o, "t_hit_us", -1e9) > 2.5)
    ]
    hp_frac = sit.me_hp / max(sit.me_max_hp, 1.0)
    if calc.our_dps <= 1e-6:
        return BREAK, calc
    # hit from somewhere we cannot see, and nothing in front of us to answer
    if sit.unseen_hit and not getattr(e, "los", True):
        return BREAK, calc
    if squad and PF.on("squad_no_break"):
        hostile_others = []
    if hostile_others:
        # finish a dying target fast; else isolate from a corner; else fighting retreat to cover
        if calc.finishable and calc.ratio >= 0.75 and hp_frac > 0.28:
            return ASSAULT, calc
        if calc.ratio_corner >= 0.85 and sit.corner:
            return CORNER, calc
        return BREAK, calc
    if hp_frac < 0.3 and not calc.finishable and not (squad and PF.on("squad_no_break")):
        return BREAK, calc
    a_thr = ASSAULT_RATIO - (HYST if prev == ASSAULT else 0.0) + (HYST if prev == CORNER else 0.0)
    e_thr = EVEN_RATIO - (HYST if prev in (CORNER, ASSAULT) else 0.0) + (HYST if prev == BREAK else 0.0)
    if calc.ratio >= a_thr or (calc.finishable and calc.ratio >= 0.85):
        return ASSAULT, calc
    # from cover (or open ground at >= 0.72), fight rather than showing our back
    if calc.ratio >= e_thr or (sit.corner and calc.ratio_corner >= CORNER_TAKE + (HYST if prev == BREAK else 0.0)):
        return CORNER, calc
    if retreating(sit, e) and sit.long_sight and calc.dist <= 230.0:
        return HARASS, calc
    return BREAK, calc


def _dist(sit, o):
    p = o.est_pos(sit.t) if hasattr(o, "est_pos") else o.pos
    return math.hypot(p[0] - sit.me_pos[0], p[1] - sit.me_pos[1])


def pick_target(sit_factory, enemies, t, me_pos, prev_id=None, squad=False):
    """Choose the primary enemy: weak, close, already engaged elsewhere, attacking us.
    sticky_target (mech/profile.py): score by (its DPS on us + 80) / max(HP, 120) and keep the
    current target (`prev_id`) at x2.2, x3.0 under 35% HP, so a second hunter's hit no longer
    pulls us off a nearly dead one. Counter (a free-for-all crossfire): no stickiness there while
    two or more shooters have us in sight."""
    best = None
    sticky_on = PF.on("sticky_target")
    shooters = sum(1 for e in enemies if getattr(e, "los", False) and getattr(e, "sees_us", False))
    keep = sticky_on and prev_id is not None and (squad or shooters < 2)
    for e in enemies:
        sit = sit_factory(e)
        calc = FightCalc(sit, e)
        if sticky_on:
            score = (calc.threat_dps + 80.0) / max(calc.enemy_hp, 120.0)
        else:
            score = calc.ratio
        visible = getattr(e, "los", False)
        if not visible:
            score *= 0.35            # a memory or a sound: anything we can actually shoot comes first
        if e.engaged_elsewhere(t) if hasattr(e, "engaged_elsewhere") else False:
            score *= 1.55
        if getattr(e, "t_hit_us", -1e9) >= t - 2.0:
            score *= 1.3 if sticky_on else 2.0       # whoever is hitting us now
        if calc.finishable and visible:
            score *= 2.1
        if "laser" in sit.me_weapons:
            score *= cookoff_mult(e, sit.cfg)          # mortar_priority: our laser does x1.6 to it
        if visible and reloading(e, t):
            score *= 1.4                               # punish_reload: caught reloading in the open
        score /= 1.0 + calc.dist / 250.0
        if keep and getattr(e, "id", None) == prev_id:
            hf = e.hp_frac() if hasattr(e, "hp_frac") else 1.0
            score *= STICKY_WEAK if hf < 0.35 else STICKY
        if best is None or score > best[0]:
            best = (score, e, calc)
    return (best[1], best[2]) if best else (None, None)
