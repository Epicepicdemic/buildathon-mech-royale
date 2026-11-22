"""Squad-practice profile (recon/rea-mech-royale.md sections 2 and 6), as small switchable pieces.

The practice board scores us against a house squad that shares vision and hunts only us; the final
is a free-for-all. The hello message does not say which, so the bot reads it from what it observes.
Free-for-all is the default and becomes permanent on any evidence of one. Only the play choice
(doctrine.choose_play) and the opening pacing change in squad mode; the damage pieces apply in both
modes. Pieces switched off here are kept for the next candidates (round table 12, brainstorm/r12/).

    mode_detector      decide the mode from observation (off: always free-for-all)
    gear_first         squad mode, until 50 s and one kill: keep a lone hunter at 120-145 m without
                       shooting it while we loot toward a laser and 6 cubes, then fight (R12 A)
    squad_no_break     squad mode: no break for a second mech within 160 m or for HP under 30%;
                       retreat only after 1.5 s of clearly losing, never while finishing a kill
    lose_isolate       ... and while two or more shooters see us, step (boost) to a cell within
                       24 m hidden from all but the target first (R12 B, Carbon)
    sticky_target      target by (its DPS on us + 80) / max(HP, 120), keeping the current one at
                       x2.2 (x3.0 under 35% HP); in a free-for-all not with two shooters in sight
    airborne_aim       a mech 0.45 m off the ground: no dodge discount in FightCalc, gravity in
                       the gun's lead, long bursts to 165 m (R12 B, Carbon)
    gun_during_charge  keep the gun firing while the laser charges, the torso on the laser's line:
                       only when the gun's lead is within 0.12 rad of it (both modes)
    partial_release    release the laser early when its partial charge already kills (both modes)
    finish_close       close to 20 m to finish a weak target (both modes), never inside 58 m of
                       its lightning (unless ours is ready) or 75 m of its mortar. Off from R12 B
                       (analysis/mech/HOUSE-BOTS.md: 84% of expert kills are under 78 m)
    house_aim          squad mode: what the house bots land on us by range, as measured over 42
                       expert matches (HOUSE-BOTS.md section 1), and floors of 70 m from a
                       lightning holder (kiting back) and 95 m from a mortar holder
    mortar_priority    our laser does x1.6 to a mortar holder (the cook-off): counted in the
                       fight calculation, the partial release and the target choice (both modes)
    punish_reload      a target heard reloading: x1.4 in the target choice (both modes), and in
                       squad mode its gun counts a quarter for 2.2 s in the fight calculation
                       (house experts reload in the open 92% of the time; in a free-for-all that
                       would close us into the 45-78 m band where top teams kill most)

Aim branch (both modes; measured with analysis/mech/sim/shotlog.py, every shot's error at release):
    accel_lead         the gun leads with the target's measured acceleration, speed-capped, and a
                       braking target is predicted to stop rather than reverse. Off: no measured
                       gain (unblocked hits on accelerating mechs 0.82 -> 0.78 squad, flat ffa)
    laser_aim          the laser aims one tick (0.05 s) ahead, from where our muzzle will be, at
                       where the target will be (was 0.1 s ahead, from where the muzzle is)
    clear_aim          the gun fires only when the line to the lead point (not to where the target
                       is now) is clear; else it aims at the head or legs if those lines are clear
    laser_hold         a charge whose target the decision dropped stays on that target's estimated
                       position for 2 s and is released when it is back on and clear, instead of
                       firing along the torso's facing
    reengage           after a hit trade while the laser recovers (or the gun reloads), move 15-30 m
                       to a cell hidden from everywhere the target stood in the last 5 s, then fight.
                       Off: measured worse alone (squad place +1.60, free-for-all +0.85 on 20
                       seeds each; moving off the line costs gun hits and tempo)
    gun_popup          jet-peek with the gun over low cover (8 m), cut the jets at the top and fall
                       (no hover), only with 35 energy left after the climb. Off: free-for-all
                       place +1.20 (1 better, 7 worse of 20), squad +0.90

Experiments only: MECH_PROFILE="name=0,other=1" overrides these in the sandbox.
"""
import math
import os

PROFILE = {
    "mode_detector": True,
    "gear_first": True,
    "squad_no_break": True,
    "lose_isolate": True,
    "sticky_target": True,
    "airborne_aim": True,
    "gun_during_charge": True,
    "partial_release": True,
    "finish_close": False,
    "house_aim": True,
    "mortar_priority": True,
    "punish_reload": True,
    "accel_lead": False,
    "laser_aim": True,
    "clear_aim": True,
    "laser_hold": True,
    "reengage": False,
    "gun_popup": False,
    "clear_aim_squad": True,    # each aim piece also in squad mode (off = free-for-all mode only)
    "laser_aim_squad": True,
    "laser_hold_squad": True,
}

SQUAD_AFTER_S = 35.0        # no squad decision before this. The first death came by 29.9 s in all 67
                            # recorded top-team free-for-alls (recon/replays/measurements.json); with
                            # 20 s, 180 of their 1,072 seats switched to squad mode before it
CONVERGE_SPEED = 2.0        # a hunter closes on us faster than this (m/s) ...
CONVERGE_M = 200.0          # ... from within this
AIMED_RAD = 0.45            # ... or has its torso this close to us
FIRE_EVIDENCE_S = 0.25      # fire between two other mechs seen this recently is evidence


def _env_overrides():
    """Sandbox experiments only: MECH_PROFILE="name=0,other=1" (the match container sets nothing)."""
    for part in (os.environ.get("MECH_PROFILE") or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip() in PROFILE:
                PROFILE[k.strip()] = v.strip() not in ("0", "false", "off", "")


def on(name):
    return PROFILE.get(name, True)


def _angdiff(a, b):
    return abs((a - b + math.pi) % (2.0 * math.pi) - math.pi)


class ModeDetector:
    """Free-for-all unless the match looks like the practice squad.

    Evidence of a free-for-all (any one makes it permanent):
      - fire between two other mechs: a laser beam ending on one (Track.t_fired_mech; a shot whose
        path merely passes one was dropped: squad hunters shooting drones and crates past a teammate
        set it in 3 of 40 squad matches);
      - another mech losing HP not to us (checked after the tick's hit events) while inside the zone,
        with no drone within 60 m, and another mech in view with its torso on it that fired within 1 s;
      - a death we did not cause before the first phase's shrink starts (after that it may be the zone).
    Squad: from 35 s on, no such evidence, no death we did not cause seen at any time, and a hunter
    converging on us (it hit us, it closes on
    us faster than 2 m/s within 200 m, or its torso is on us). A quiet free-for-all opening with
    nobody coming for us therefore stays free-for-all; and any later evidence switches back for
    good (counter: treat anything unclear as a free-for-all).
    """

    def __init__(self):
        self.mode = "ffa"
        self.locked = False
        self.why = None
        self.t_squad = None
        self.seen_uncredited = 0
        self.first_shrink = None     # the first phase's shrink start: before it nobody dies to the zone

    def squad(self):
        return on("mode_detector") and self.mode == "squad"

    def update(self, me, th, zone):
        t = me.t
        if self.locked:
            return
        why = self._evidence(me, th, zone)
        if why is not None:
            self.locked = True
            self.mode = "ffa"
            self.why = (round(t, 1), why)
            return
        if self.mode == "ffa" and t >= SQUAD_AFTER_S and self.seen_uncredited == 0 and self._converging(me, th):
            # (after the first shrink a death we did not cause is no proof, since the zone kills too, but
            # it is no squad sign either: one replayed top-team seat killed every mech that died
            # before 60 s itself and was hunted at 67.7 s)
            self.mode = "squad"
            self.t_squad = t

    def _evidence(self, me, th, zone):
        t = me.t
        if self.first_shrink is None:
            self.first_shrink = zone.shrink_start
        npcs = [n.pos for n in th.npcs()]
        for tr in th.mechs():
            if tr.kind != "mech":
                continue
            if t - getattr(tr, "t_fired_mech", -1e9) <= FIRE_EVIDENCE_S:
                return "fire between mechs (%d)" % tr.id
            if tr.in_obs and tr.t_hp_drop_other == t and t - tr.t_we_hit > 0.5:
                # positive evidence only (the study's counter): another mech in view, torso on it, fired
                # within 1 s. A bare HP drop was our own hit seen before its hit event (5537640: 27 of 40
                # squad matches locked free-for-all that way), or a drone's, or the zone's
                inside = zone.margin_at(tr.pos[0], tr.pos[1], t) >= 2.0
                near_drone = any(math.hypot(p[0] - tr.pos[0], p[1] - tr.pos[1]) < 60.0 for p in npcs)
                if inside and not near_drone:
                    for o in th.mechs():
                        if o is tr or not o.in_obs or o.dead or t - getattr(o, "t_fired", -1e9) > 1.0:
                            continue
                        q = o.est_pos(t)
                        if _angdiff(o.yaw, math.atan2(tr.pos[1] - q[1], tr.pos[0] - q[0])) < 0.3:
                            return "mech %d hurt by mech %d" % (tr.id, o.id)
        unc = getattr(th, "uncredited", 0)
        if unc > self.seen_uncredited:
            self.seen_uncredited = unc
            if t < self.first_shrink:
                # (zone.shrink_start moves on to each later phase: comparing with it counted zone
                # deaths later in squad matches, 17 of 40)
                return "a death we did not cause"
        return None

    def _converging(self, me, th):
        t = me.t
        for tr in th.mechs():
            if tr.kind != "mech" or tr.dead:
                continue
            if t - tr.t_hit_us <= 10.0:
                return True
            if t - tr.t_seen > 5.0:
                continue
            q = tr.est_pos(t)
            dx, dy = me.pos[0] - q[0], me.pos[1] - q[1]
            d = math.hypot(dx, dy)
            if d < 1.0 or d > CONVERGE_M:
                continue
            closing = (tr.vel[0] * dx + tr.vel[1] * dy) / d
            if closing > CONVERGE_SPEED:
                return True
            if tr.in_obs and _angdiff(tr.yaw, math.atan2(dy, dx)) < AIMED_RAD:
                return True
        return False


_env_overrides()
