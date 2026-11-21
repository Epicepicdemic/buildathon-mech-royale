"""Squad-practice profile (recon/rea-mech-royale.md sections 2 and 6), as small switchable pieces.

The practice board scores us against a house squad that shares vision and hunts only us; the final
is a free-for-all. The hello message does not say which, so the bot reads it from what it observes.
Free-for-all is the default and becomes permanent on any evidence of one. Only the play choice
(doctrine.choose_play) changes in squad mode; the damage pieces apply in both modes.

    mode_detector      decide the mode from observation (off: always free-for-all)
    squad_no_break     squad mode: no break for a second mech within 160 m or for HP under 30%;
                       retreat only after 1.5 s of clearly losing, never while finishing a kill
    gun_during_charge  keep the gun firing while the laser charges (both modes)
    partial_release    release the laser early when its partial charge already kills (both modes)
    finish_close       close to 20 m to finish a weak target (both modes)
"""
import math

PROFILE = {
    "mode_detector": True,
    "squad_no_break": True,
    "gun_during_charge": True,
    "partial_release": True,
    "finish_close": True,
}

SQUAD_AFTER_S = 20.0        # no squad decision before this
CONVERGE_SPEED = 2.0        # a hunter closes on us faster than this (m/s) ...
CONVERGE_M = 200.0          # ... from within this
AIMED_RAD = 0.45            # ... or has its torso this close to us
FIRE_EVIDENCE_S = 0.25      # fire between two other mechs seen this recently is evidence


def on(name):
    return PROFILE.get(name, True)


def _angdiff(a, b):
    return abs((a - b + math.pi) % (2.0 * math.pi) - math.pi)


class ModeDetector:
    """Free-for-all unless the match looks like the practice squad.

    Evidence of a free-for-all (any one makes it permanent):
      - fire between two other mechs: a beam ending on one, or a shot whose path passes one
        (Track.t_fired_mech);
      - another mech losing HP not to us while inside the zone and with no drone within 60 m;
      - a death we did not cause before the zone starts closing (after that it may be the zone).
    Squad: from 20 s on, no such evidence, and a hunter converging on us (it hit us, it closes on
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
        if self.mode == "ffa" and t >= SQUAD_AFTER_S and self._converging(me, th):
            self.mode = "squad"
            self.t_squad = t

    def _evidence(self, me, th, zone):
        t = me.t
        npcs = [n.pos for n in th.npcs()]
        for tr in th.mechs():
            if tr.kind != "mech":
                continue
            if t - getattr(tr, "t_fired_mech", -1e9) <= FIRE_EVIDENCE_S:
                return "fire between mechs (%d)" % tr.id
            if tr.in_obs and tr.t_hp_drop_other == t:
                inside = zone.margin_at(tr.pos[0], tr.pos[1], t) >= 2.0
                near_drone = any(math.hypot(p[0] - tr.pos[0], p[1] - tr.pos[1]) < 60.0 for p in npcs)
                if inside and not near_drone:
                    return "mech %d hurt by another" % tr.id
        unc = getattr(th, "uncredited", 0)
        if unc > self.seen_uncredited:
            self.seen_uncredited = unc
            if t < zone.shrink_start:
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
