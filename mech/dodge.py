"""Jinking under fire: irregular sidesteps across the line of fire.

A led gun shot is aimed at where the target will be if it keeps its velocity. The bullet takes
0.12 s to cross 30 m and 0.8 s to cross 200 m, so what beats it is a change of direction during
the flight that the shooter cannot predict. Each leg runs across the line of fire (perpendicular to
the bearing of the threat) for a random time, then usually reverses.

The pattern is a pure function of time and the seeded RNG, so `tools/replay_shooting.py` can fly
it against bullets that real bots fired in recorded matches (see that tool for the figures).

Version 2 used legs of 0.30-0.55 s. At 58.5 m/s^2 a reversal from walking speed takes 0.4 s, so
those legs barely reached speed before turning back: the mech wobbled around a point that was
easy to hit. Version 3's legs (LEG_S) were chosen from the recorded bullets.
"""
import math

V2_LEG_S = (0.30, 0.55)
LEG_S = (0.45, 1.05)        # version 3 (chosen in tools/replay_shooting.py: real bullets, real ranges)
P_FLIP = 0.8                # chance a new leg reverses the last one


class Jinker:
    def __init__(self, rng, leg_s=LEG_S, p_flip=P_FLIP, base_w=0.6, perp_w=0.8):
        self.rng = rng
        self.leg_s = leg_s
        self.p_flip = p_flip
        self.base_w = base_w
        self.perp_w = perp_w
        self.sign = 1
        self.until = -1e9

    def direction(self, t, move, threat_yaw):
        """Unit move direction for this decision, or None when there is nothing to jink across.
        `move`: where the legs were going anyway (may be zero). `threat_yaw`: bearing to the threat."""
        if t >= self.until:
            if self.rng.random() < self.p_flip:
                self.sign = -self.sign
            self.until = t + self.rng.uniform(*self.leg_s)
        if threat_yaw is not None:
            px, py = -math.sin(threat_yaw), math.cos(threat_yaw)
        elif math.hypot(move[0], move[1]) > 0.1:
            px, py = -move[1], move[0]
        else:
            return None
        s = self.sign
        mx = move[0] * self.base_w + px * s * self.perp_w
        my = move[1] * self.base_w + py * s * self.perp_w
        n = math.hypot(mx, my)
        if n < 1e-9:
            return None
        return (mx / n, my / n)

    def flip(self):
        """A wall in the way: reverse now."""
        self.sign = -self.sign
