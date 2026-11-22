"""Clean-room pieces, written from our behaviour spec (analysis/mech/CLEANROOM-SPEC.md, ideas 1-4 and 8).
Implemented clean-room from our behaviour spec; no other team's code was read or used.

Each piece has its own switch: `mech/cleanroom_settings.py` in free-for-all mode (which is also
every match's first 35 s) and the `cr_` keys of `mech/squad_settings.py` in squad mode.

    gear    engage a mech only when it is a real threat or an easy kill (close, hit us, charging a
            laser at us, badly hurt in sight, aiming at us, or the last few alive); otherwise loot
            (wide radii, an item chased too long is dropped), and crates/drones are farmed even
            while a harmless mech is in sight
    energy  boost only for a reason (a laser charge at us, a shell, the zone, a trap, a retreat
            while being hit, finishing a target, bringing lightning into range); idle travel in a
            free-for-all can walk under the step-sound speed (`silent`)
    heal    under half HP, retreat: hide from every mech seen lately (and a point down the bearing
            of an unseen hit) and stand still until HP is back; never fire at drones or crates while
            hurt (any damage dealt restarts the regeneration delay)
    cells   in a fight within range, choose the next cell from a ring: the preferred range for our
            loadout, in the target's sight only when a weapon is ready (out of it otherwise; always
            in sight in squad mode), out of other enemies' sight, clear of walls, inside the zone,
            with a strafe side that flips at random intervals
    home    (free-for-all only) the hold cell between fights is costed against every mech seen in
            the last 45 s and keeps clear of announced supply-drop landings; it is kept 30 s
"""
import math

from . import cleanroom_settings as CRS
from . import doctrine as D
from . import squad_settings as SS
from .geom import angdiff, bearing


def get(name, squad):
    if squad:
        v = CRS.ENV_SQ.get(name)
        return SS.get("cr_" + name) if v is None else v
    v = CRS.ENV_FFA.get(name)
    return CRS.S[name] if v is None else v


def on(name, squad):
    return bool(get(name, squad))


class State:
    """Per-match memory of the clean-room pieces."""

    def __init__(self):
        self.aim_since = {}         # mech id -> since when its torso has been on us
        self.aimed_t = {}           # mech id -> when it last held its torso on us for a second
        self.threats = []           # this decision's mechs worth fighting (gear)
        self.chase_key = None       # (kind, id) of the item being chased, and since when
        self.chase_t = 0.0
        self.bans = []              # [(x, y, until)] items given up on
        self.retreat = False        # heal: hiding to regenerate
        self.retreat_t = -1e9
        self.cell = None            # cells: the chosen cell and when
        self.cell_t = -1e9
        self.side = 1
        self.flip_t = 0.0
        self.drops = []             # home: announced supply-drop landing points


# ---------------------------------------------------------------- 1. gear first, fight only threats
def note_aim(st, me, th):
    t = me.t
    for tr in th.mechs():
        if tr.kind != "mech" or tr.dead or not tr.in_obs:
            st.aim_since.pop(tr.id, None)
            continue
        if angdiff(tr.yaw, bearing(tr.pos, me.pos)) <= 0.2:
            since = st.aim_since.setdefault(tr.id, t)
            if t - since >= 1.0:
                st.aimed_t[tr.id] = t
        else:
            st.aim_since.pop(tr.id, None)


def is_threat(tac, me, e, lt, squad):
    """A mech worth fighting now: an immediate problem or an easy kill."""
    t = me.t
    if e.dead:
        return False
    if t - e.t_hit_us <= get("hit_s", squad) or e is lt:
        return True
    alive = tac.th.alive
    if alive is not None and alive <= get("endgame_n", squad):
        return True
    if e.phantom or not (e.in_obs or t - e.t_seen <= 1.0):
        return False
    d = tac._dist(me, e)
    if d <= get("engage_m", squad):
        return True
    if e.los and e.hp_frac() < get("weak_hp", squad) and d <= get("weak_m", squad):
        return True
    return d <= get("aim_m", squad) and t - tac.cr.aimed_t.get(e.id, -1e9) <= 3.0


def threats(tac, me, enemies, lt, squad):
    note_aim(tac.cr, me, tac.th)
    tac.cr.threats = [e for e in enemies if is_threat(tac, me, e, lt, squad)]
    return tac.cr.threats


def loot_allow(st, t):
    st.bans = [b for b in st.bans if b[2] > t]
    if not st.bans:
        return None
    bans = list(st.bans)

    def allow(px, py):
        return all(math.hypot(px - bx, py - by) > 3.0 for bx, by, _ in bans)
    return allow


def note_chase(st, t, kind, lid, pos, squad):
    """Drop an item chased longer than chase_s without reaching it."""
    key = (kind, lid)
    if key != st.chase_key:
        st.chase_key, st.chase_t = key, t
        return
    if t - st.chase_t > get("chase_s", squad):
        st.bans.append((pos[0], pos[1], t + get("chase_ban_s", squad)))
        st.chase_key = None


# ---------------------------------------------------------------- 2. energy and noise
BOOST_LABELS = (">zone", "mortar", "isolate", "duck the laser", "short its laser", "kite")


def energy(tac, me, dec, play, calc, lt, target, squad, slack):
    t = me.t
    th = tac.th
    if dec.speed == "boost":
        lab = (dec.intent.label if dec.intent is not None else "") or ""
        ok = lt is not None or any(k in lab for k in BOOST_LABELS) or not me.in_zone
        if not ok and (play == D.BREAK or tac.healing or tac.cr.retreat):
            ok = t - th.t_taken <= get("boost_hit_s", squad)
        if not ok and play == D.ASSAULT and target is not None:
            if (calc is not None and calc.finishable) or target.hp_frac() < 0.3:
                ok = True
            else:
                light = me.by_kind.get("lightning")
                d = tac._dist(me, target)
                if light is not None and light.stat("range", 45.0) < d <= get("boost_short_m", squad):
                    ok = True
        if not ok:
            dec.speed = "walk"
    if not squad and on("silent", False) and dec.speed == "walk" and play == D.REPOSITION \
            and (dec.label or dec.intent.label or "") in ("hold", "to hold") and slack > get("silent_slack_s", False) \
            and not th.active_mechs(t, 6.0) and t - th.t_taken > 5.0:
        dec.speed = "silent"


# ---------------------------------------------------------------- 3. hide to heal
def update_retreat(tac, me, calc, squad):
    st = tac.cr
    t = me.t
    hp = me.hp_frac
    th = tac.th
    if not st.retreat:
        finishing = calc is not None and calc.finishable
        if hp < get("retreat_hp", squad) and not finishing:
            st.retreat, st.retreat_t = True, t
    else:
        last = max([tr.t_seen for tr in th.mechs() if tr.kind == "mech"] + [th.t_taken, st.retreat_t])
        if hp >= get("back_hp", squad) or (t - last >= get("quiet_s", squad) and hp >= get("ok_hp", squad)):
            st.retreat = False
    return st.retreat


def hold_fire(tac, me, squad):
    """No drones or crates while hurt or retreating (damage dealt restarts the regen clock), unless
    a drone is hitting us anyway."""
    return on("heal", squad) and (tac.cr.retreat or me.hp_frac < get("hold_fire_hp", squad)) \
        and drone_on_us(tac, me) is None


def drone_on_us(tac, me):
    """A drone in sight that hit us in the last 3 s (its fire stops our regeneration anyway)."""
    t = me.t
    if not any(t - tt <= 3.0 and w == "npc" for (tt, a, w) in tac.th.dmg_taken):
        return None
    best = None
    for tr in tac.th.npcs():
        if not tr.in_obs or not tr.los:
            continue
        d = math.hypot(tr.pos[0] - me.pos[0], tr.pos[1] - me.pos[1])
        if d <= 140.0 and (best is None or d < best[0]):
            best = (d, tr)
    return best[1] if best else None


def answer_drone(tac, me, dec):
    """Shoot back at a drone that is hitting us, strafing."""
    tr = drone_on_us(tac, me)
    if tr is None:
        return False
    from .gunner import Target
    dec.target = Target.from_track(tr, me.t)
    dec.intent.fire = "long"
    dec.intent.label = "cr drone"
    dec.evasive = True
    tac.mv.clear()
    return True


def hide_points(tac, me, squad):
    th = tac.th
    pts = th.threat_points(me, 30.0 if squad else get("threat_window_s", squad))
    ld = th.last_damage
    if ld is not None and ld[1] is not None and me.t - ld[0] <= 3.0 and th.unseen_attacker(me.t, 3.0) is not None:
        d = get("unseen_m", squad)
        x, y = me.pos[0] + math.cos(ld[1]) * d, me.pos[1] + math.sin(ld[1]) * d
        pts.append((x, y, tac.w.floor(x, y) + 8.0))
    return pts


def hide_and_hold(tac, me, dec, squad, slack):
    """Go to the nearest cell hidden from every recent threat and stand still there. Returns
    False when the zone does not allow it."""
    t = me.t
    z = tac.zone
    if not me.in_zone or slack < 12.0:
        return False
    if answer_drone(tac, me, dec):
        return True
    tac.healing = True
    intent = dec.intent
    intent.fire = "none"
    intent.reload_ok = True
    intent.label = "cr heal"
    dec.target = None
    pts = hide_points(tac, me, squad)
    if pts:
        tac.cov.want_hide(me, pts, t, max_cost=90.0)
    hide = tac.cov.hide_xy()
    if hide is not None and math.hypot(hide[0] - me.pos[0], hide[1] - me.pos[1]) > 2.0:
        tac.mv.go(hide, me, z, prefer_cover=False, tag="cr-hide", replan_dist=4.0)
        dec.speed = "walk"
        intent.label = "cr heal: hide"
        return True
    if hide is None and pts:
        goal = tac.hold_spot(me)
        if math.hypot(goal[0] - me.pos[0], goal[1] - me.pos[1]) > 3.0:
            tac.mv.go(goal, me, z, prefer_cover=True, tag="hold")
            dec.speed = "walk"
            return True
    if t - tac.th.t_taken <= 1.0 and hide is not None:
        tac.cov.reset()                 # hit at our "hidden" cell: look again
    tac.mv.clear()
    dec.hold = t - tac.th.t_taken > 1.0
    dec.evasive = not dec.hold
    dec.speed = "walk"
    dec.face = tac.danger_yaw(me) if tac.danger_yaw(me) is not None else tac.lookout_yaw(me)
    intent.label = "cr heal: still"
    return True


# ---------------------------------------------------------------- 4. fight cells by readiness
def want_range(me, squad):
    if me.has("lightning"):
        return get("want_light_m", squad)
    if me.has("laser"):
        return get("want_laser_m", squad)
    return get("want_gun_m", squad)


def choose_cell(tac, me, target, others, ready, squad):
    """The best cell of a ring around us (our own included), or None."""
    st = tac.cr
    t = me.t
    w = tac.w
    z = tac.zone
    if t >= st.flip_t:
        st.side = -st.side
        st.flip_t = t + tac.rng.uniform(get("flip_min_s", squad), get("flip_max_s", squad))
    if st.cell is not None and t - st.cell_t < 0.4 and math.hypot(st.cell[0] - me.pos[0], st.cell[1] - me.pos[1]) > 1.5:
        return st.cell
    tp = target.est_pos(t)
    teye = (tp[0], tp[1], tp[2] + 8.0)
    want = want_range(me, squad)
    dangers = []
    if not me.has("lightning"):
        for e in [target] + list(others):
            if e.has("lightning"):
                dangers.append((e.est_pos(t), get("avoid_light_m", squad)))
    for e in [target] + list(others):
        if e.has("mortar"):
            dangers.append((e.est_pos(t), get("avoid_mortar_m", squad)))
    watch = []
    for o in others:
        if o is target or o.dead or t - o.t_seen > 3.0:
            continue
        q = o.est_pos(t)
        if math.hypot(q[0] - me.pos[0], q[1] - me.pos[1]) <= 200.0:
            watch.append((q[0], q[1], q[2] + 8.0))
    watch = watch[:3]
    base = bearing(me.pos, tp)
    r = get("ring_m", squad)
    rw, sw = get("range_w", squad), get("seen_w", squad)
    want_sight = ready or squad
    best = None
    for k in range(-1, 8):
        if k < 0:
            cx, cy = me.pos[0], me.pos[1]
        else:
            a = base + k * math.pi / 4.0
            cx, cy = me.pos[0] + math.cos(a) * r, me.pos[1] + math.sin(a) * r
            if not w.walkable(me.pos[0], me.pos[1], cx, cy, need=2.7, skip_start=1.0):
                continue
        feet = (cx, cy, w.floor(cx, cy))
        dc = math.hypot(cx - tp[0], cy - tp[1])
        sc = -rw * abs(dc - want)
        for p, m in dangers:
            dd = math.hypot(cx - p[0], cy - p[1])
            if dd < m:
                sc -= (m - dd) * 1.5
        los = w.sees(teye, feet, height=me.height)
        sc += 30.0 if los == want_sight else -30.0
        for q in watch:
            if w.sees(q, feet, height=me.height):
                sc -= sw
        c = w.clearance(cx, cy)
        if c < 4.0:
            sc -= (4.0 - c) * 3.0
        if z.margin_at(cx, cy, t + 3.0) < 2.0:
            sc -= 40.0
        if k >= 0:
            lat = math.sin(k * math.pi / 4.0)
            if lat * st.side > 0.3:
                sc += 8.0
        if best is None or sc > best[0]:
            best = (sc, (cx, cy))
    st.cell = best[1] if best is not None else None
    st.cell_t = t
    return st.cell


def fight_cell(tac, me, dec, target, others, ready, squad):
    cell = choose_cell(tac, me, target, others, ready, squad)
    if cell is None:
        return False
    if math.hypot(cell[0] - me.pos[0], cell[1] - me.pos[1]) <= 1.5:
        tac.mv.clear()
    else:
        tac._go_instant_or_plan(cell, me, tac.zone, "cr-cell")
    dec.evasive = False
    dec.speed = "walk"
    dec.intent.label = (dec.intent.label or "") + " cell"
    return True
