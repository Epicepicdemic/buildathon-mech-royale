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

from .ballistics import reversal_range
from .geom import angdiff, bearing

ASSAULT, CORNER, HARASS, BREAK, REPOSITION = "assault", "corner", "harass", "break", "reposition"

ASSAULT_RATIO = 1.4
EVEN_RATIO = 0.6        # version 2 broke below 0.8 and then held fire: version 3 fights to 0.6
NEAR_OTHERS_M = 160.0
HYST = 0.2
CORNER_TAKE = 0.7       # from a corner we fight unless the calculation says we clearly lose
FINISH_RATIO = 0.7      # a target we can finish with one burst is attacked unless we clearly lose
LOW_HP = 0.25           # below this we break only if we are also losing


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
    """Everything the doctrine needs about one moment, as plain numbers.

    me_*: our hp, max hp, cubes, energy, armor, weapons {kind: stats}, position, yaw, airborne
    enemy: the primary enemy (a threats.Track or any object with the same fields)
    others: other hostile mechs that are close and active
    corner: we have a hide spot and a peek spot near us against this enemy
    unseen_hit: something we cannot see hit us in the last 2 s
    long_sight: we have a line of fire to the enemy (or had one a moment ago)
    """

    def __init__(self, t, me_pos, me_yaw, me_hp, me_max_hp, me_weapons, enemy=None, others=(),
                 me_cubes=0, me_energy=100.0, me_armor=0.0, corner=False, unseen_hit=False,
                 long_sight=True, cfg=None, airborne=False, healing=False, gun_ammo=None):
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
        cfg = sit.cfg
        ours = 0.0
        for kind, st in sit.me_weapons.items():
            ours += weapon_dps(kind, st, sit.me_cubes, self.dist, dodging and not self.unaware, still,
                               sit.me_energy, cfg)
        if self.rear:
            ours *= float((cfg.get("mech") or {}).get("rear_mult", 1.5))
        self.our_dps = ours
        hp = e.hp if getattr(e, "hp", None) is not None else 1400.0
        self.enemy_hp = hp
        self.ttk_us = hp / ours if ours > 1e-6 else math.inf
        # what it lands on us: we keep moving and dodge beyond the reversal range
        theirs = self.their_dps(e, self.dist)
        if e.engaged_elsewhere(sit.t) if hasattr(e, "engaged_elsewhere") else False:
            theirs *= 0.5
        if self.unaware:
            theirs *= 0.6
        self.their_dps_on_us = theirs
        extra = 0.0
        for o in sit.others:
            po = o.est_pos(sit.t) if hasattr(o, "est_pos") else o.pos
            d = math.hypot(po[0] - sit.me_pos[0], po[1] - sit.me_pos[1])
            f = 1.0 if getattr(o, "sees_us", True) else 0.5
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
        if "laser" in sit.me_weapons:
            burst += float(sit.me_weapons["laser"].get("damage", 315.0)) * cube_mult(sit.me_cubes, cfg)
        if "lightning" in sit.me_weapons and self.dist <= float(sit.me_weapons["lightning"].get("range", 45.0)):
            burst += 60.0 * 10 * cube_mult(sit.me_cubes, cfg)
        if self.rear:
            burst *= 1.5
        self.burst = burst
        self.finishable = hp <= burst

    def their_dps(self, e, dist):
        sit = self.sit
        cfg = sit.cfg
        m = cube_mult(getattr(e, "cubes", 0), cfg)
        wpn = getattr(e, "weapons", ["gun"]) or ["gun"]
        tot = 0.0
        we_dodge = not sit.airborne
        for kind in wpn:
            st = _wstats(cfg, kind)
            if kind == "gun":
                _, sus = gun_dps(st or {}, getattr(e, "cubes", 0), cfg)
                rr = reversal_range(float((st or {}).get("speed", 252.0)), accel=12.0 if sit.airborne else 58.5)
                if dist > float((st or {}).get("range", 230.0)):
                    p = 0.0
                elif dist <= rr or not we_dodge:
                    p = 0.8
                else:
                    p = max(0.15, 0.8 * rr / dist * 0.6)
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
        self.label = play


def rear_point(enemy_pos, enemy_yaw, dist):
    """A point `dist` behind the enemy's facing."""
    return (enemy_pos[0] - math.cos(enemy_yaw) * dist, enemy_pos[1] - math.sin(enemy_yaw) * dist)


class Assault:
    """Close under cover to 45 m with lightning (else about 70 m), arrive on the rear arc when the
    approach allows, and fire long bursts."""
    name = ASSAULT

    def act(self, sit, ctx):
        e = sit.enemy
        it = Intent(ASSAULT, e)
        p = e.est_pos(sit.t) if hasattr(e, "est_pos") else e.pos
        calc = ctx.get("calc") if ctx else None
        dist = math.hypot(p[0] - sit.me_pos[0], p[1] - sit.me_pos[1])
        want = 38.0 if "lightning" in sit.me_weapons else 65.0
        if "laser" in sit.me_weapons and "lightning" not in sit.me_weapons:
            want = 90.0
        if getattr(e, "has", lambda k: False)("mortar"):
            # its blast throws us back and drains the energy lightning needs: stay at gun/laser range
            want = max(want, 75.0)
        if dist > want:
            # approach: toward the rear side when it is not looking at us
            if not getattr(e, "sees_us", True):
                it.goal = rear_point(p, getattr(e, "yaw", 0.0), want * 0.7)
            else:
                it.goal = (p[0], p[1])
            it.spot = "approach"
            it.speed = "boost" if dist > want + 30 and sit.me_energy > 40 else "walk"
        else:
            it.goal = None
            it.spot = "strafe"
        it.evasive = dist > 70.0 or getattr(e, "has", lambda k: True)("gun")
        it.fire = "long"
        it.reload_ok = sit.gun_ammo is not None and sit.gun_ammo == 0
        if calc is not None and calc.finishable:
            it.label = "assault: finish"
        return it


class CornerFight:
    """Step out from a wall corner, fire, step back. Reload behind the wall."""
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
            # no corner known: fall back to strafing in place behind whatever we have
            it.spot = "strafe"
            it.fire = "short"
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
    """Boost to the nearest cell hidden from every known threat, changing direction irregularly,
    torso toward the most dangerous attacker. No shooting: healing needs 5 s without damage."""
    name = BREAK

    def act(self, sit, ctx):
        it = Intent(BREAK, sit.enemy)
        hide = ctx.get("hide") if ctx else None
        it.goal = hide
        it.spot = "hide" if hide is not None else "retreat"
        it.speed = "boost" if sit.me_energy > 20 else "walk"
        it.evasive = True
        it.fire = "none"
        it.reload_ok = True
        danger = ctx.get("danger_yaw") if ctx else None
        it.face = danger
        # a laser that is already charged can still be released on the way (handled by the gunner)
        return it


class Reposition:
    """No threat: the tactics layer chooses where to go (cover, loot, the next circle)."""
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


def choose_play(sit, calc=None, prev=None):
    """Pick one of the five plays for this decision. Returns (play name, FightCalc or None).
    `prev` is last decision's play: thresholds move by HYST in its favour so that a ratio sitting
    on a boundary does not flip the play every tick.

    `calc.ratio` is the open-ground ratio (their time to kill us / our time to kill them), and it
    already counts every other mech shooting at us (`others`); `calc.ratio_corner` assumes we
    fight from a corner and are exposed half the time.

    Version 3 (the red team's squad finding): this bot fights. Other mechs being near is not by
    itself a reason to break, low HP alone is not, and a break never means holding fire while
    something can still hit us (the tactics layer keeps the torso and the gun on it). We break
    when the calculation says we clearly lose, when we cannot hurt the target, or when something
    we cannot locate is hitting us."""
    e = sit.enemy
    if e is None:
        if sit.unseen_hit:
            return BREAK, None
        return REPOSITION, None
    calc = calc or FightCalc(sit, e)
    near_others = [o for o in sit.others if _dist(sit, o) <= NEAR_OTHERS_M]
    hp_frac = sit.me_hp / max(sit.me_max_hp, 1.0)
    if calc.our_dps <= 1e-6:
        return BREAK, calc
    # hit from somewhere we cannot locate, and nothing in front of us to answer
    if sit.unseen_hit and not getattr(e, "in_obs", True):
        return BREAK, calc
    if calc.finishable and calc.ratio >= FINISH_RATIO:
        return ASSAULT, calc
    a_thr = ASSAULT_RATIO - (HYST if prev == ASSAULT else 0.0) + (HYST if prev == CORNER else 0.0)
    e_thr = EVEN_RATIO - (HYST if prev in (CORNER, ASSAULT) else 0.0) + (HYST if prev == BREAK else 0.0)
    if near_others:
        # a third party: hold ground and fight (no charging in), from a corner when we have one
        if calc.ratio >= e_thr or (sit.corner and calc.ratio_corner >= CORNER_TAKE):
            return CORNER, calc
        return BREAK, calc
    if hp_frac < LOW_HP and calc.ratio < 1.0:
        return BREAK, calc
    if calc.ratio >= a_thr:
        return ASSAULT, calc
    # from cover, a roughly even fight is taken: hunters come anyway, and running shows them our back
    if calc.ratio >= e_thr or (sit.corner and calc.ratio_corner >= CORNER_TAKE + (HYST if prev == BREAK else 0.0)):
        return CORNER, calc
    if retreating(sit, e) and sit.long_sight and calc.dist <= 230.0:
        return HARASS, calc
    return BREAK, calc


def _dist(sit, o):
    p = o.est_pos(sit.t) if hasattr(o, "est_pos") else o.pos
    return math.hypot(p[0] - sit.me_pos[0], p[1] - sit.me_pos[1])


def pick_target(sit_factory, enemies, t, me_pos):
    """Choose the primary enemy: weak, close, already engaged elsewhere, attacking us. Returns the
    track with the best (ratio-weighted) score, or None."""
    best = None
    for e in enemies:
        sit = sit_factory(e)
        calc = FightCalc(sit, e)
        score = calc.ratio
        visible = e.shootable() if hasattr(e, "shootable") else getattr(e, "los", False)
        if not visible:
            score *= 0.35            # a memory or a sound: anything we can actually shoot comes first
        if e.engaged_elsewhere(t) if hasattr(e, "engaged_elsewhere") else False:
            score *= 1.3
        if getattr(e, "t_hit_us", -1e9) >= t - 2.0:
            score *= 2.0             # whoever is hitting us now
        if calc.finishable and visible:
            score *= 1.6
        score /= 1.0 + calc.dist / 250.0
        if best is None or score > best[0]:
            best = (score, e, calc)
    return (best[1], best[2]) if best else (None, None)
