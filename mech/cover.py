"""Finding cover: the nearest spot hidden from a set of watchers, and a peek spot next to it.

Searches walk outward from us over the navigation grid (walking distance), test candidates
cheaply with the hiding-wall fans first and exactly with line of sight second, and run in
slices across decisions (`run(deadline)`).
"""
import math

from . import nav


def hidden_at(world, n, watchers, height=9.0, near_range=45.0):
    """Is a mech standing on node n hidden from every watcher eye point (x, y, z)?"""
    x, y = world.node_xy(n)
    f = world.floor(x, y)
    for (wx, wy, wz) in watchers:
        d = math.hypot(wx - x, wy - y)
        if d < 6.0:
            return False
        if d > 235.0:
            continue              # beyond vision range
        yaw = math.atan2(wy - y, wx - x)
        if world.fan_dist(n, yaw) >= d:
            return False          # no hiding wall between us: treat as seen (canopy may hide, we do not count on it)
        if world.sees((wx, wy, wz), (x, y, f), height, near_range, step=2.0):
            return False
    return True


def sees_target(world, n, target_center, eye_h=8.0):
    """From node n, would our eye see the target centre (no FOV check)?"""
    x, y = world.node_xy(n)
    f = world.floor(x, y)
    return world.line_clear(x, y, f + eye_h, target_center[0], target_center[1], target_center[2])


def shot_from(world, n, target_center, muzzle_h=7.0):
    x, y = world.node_xy(n)
    f = world.floor(x, y)
    return world.shot_clear((x, y, f + muzzle_h), target_center)


class HideSearch(nav.NearestSearch):
    """Nearest walkable node hidden from all watchers (and not too exposed in general)."""

    def __init__(self, world, start, watchers, max_cost=90.0, max_expo=0.6, away_from=None, min_dist=0.0,
                 height=9.0, rng=None):
        self.watchers = list(watchers)
        self.max_expo = max_expo
        self.away_from = away_from
        self.min_dist = min_dist
        self.height = height
        self.world = world

        def pred(n):
            if not world.main[n]:
                return False
            if world.expo[n] > max_expo * 255:
                return False
            if world.clear_dm[world.vidx(*world.node_xy(n))] < 33:
                return False
            if away_from is not None and min_dist > 0:
                x, y = world.node_xy(n)
                if math.hypot(x - away_from[0], y - away_from[1]) < min_dist:
                    return False
            return hidden_at(world, n, self.watchers, height)

        # several hidden cells within a few metres of the nearest: draw one, so the spot differs by match
        super().__init__(world, start, pred, max_cost, alts=4 if rng is not None else 1, alt_slack=8.0, rng=rng)


class PeekSearch(nav.NearestSearch):
    """From a hide spot, the nearest node with a clear shot at the target, within a few steps."""

    def __init__(self, world, hide_node, target_center, max_cost=16.0, muzzle_h=7.0, eye_h=8.0, avoid=(),
                 rng=None):
        """`avoid`: (x, y) peek spots used last time; we never step out of the same edge twice running."""
        self.target_center = target_center
        avoid = list(avoid)

        def pred(n):
            if not world.main[n]:
                return False
            if avoid:
                x, y = world.node_xy(n)
                for ax, ay in avoid:
                    if math.hypot(x - ax, y - ay) < 4.5:
                        return False
            return shot_from(world, n, target_center, muzzle_h) and sees_target(world, n, target_center, eye_h)

        super().__init__(world, hide_node, pred, max_cost, alts=3 if rng is not None else 1, alt_slack=4.0, rng=rng)


class LowExposureSearch(nav.NearestSearch):
    """Nearest node with low exposure and walls close around (the opening when nothing is known)."""

    def __init__(self, world, start, max_expo=0.10, min_arc=0.25, max_cost=140.0):
        def pred(n):
            return world.main[n] and world.expo[n] <= max_expo * 255 and world.wall_arc(n) >= min_arc
        super().__init__(world, start, pred, max_cost)


class CoverState:
    """Keeps the current hide/peek spots against the current watchers and refreshes them."""

    def __init__(self, world, rng=None):
        self.w = world
        self.rng = rng
        self.last_peek = None     # (x, y) of the edge we stepped out of last time
        self.hide = None          # (node, path) result
        self.peek = None
        self.hide_search = None
        self.peek_search = None
        self.hide_key = None
        self.hide_t = -1e9
        self.peek_key = None
        self.peek_t = -1e9
        self.fail_t = -1e9
        self.extra_avoid = ()     # brawl_counter (squad): spots where a mech saw us in the last 5 s

    @staticmethod
    def _key(watchers):
        return tuple((round(w[0] / 12.0), round(w[1] / 12.0)) for w in sorted(watchers)[:4])

    def want_hide(self, me, watchers, t, max_cost=90.0, away_from=None, min_dist=0.0):
        """Start (or keep) a hide search for these watchers."""
        if not watchers:
            return
        key = self._key(watchers)
        start = self.w.nearest_free(me.pos[0], me.pos[1], 5)
        if start is None:
            return
        stale = t - self.hide_t > 2.5
        if self.hide_search is not None and key == self.hide_key and not stale:
            return
        if self.hide is not None and key == self.hide_key and not stale:
            return
        if self.hide_search is not None and not stale:
            return
        self.hide_key = key
        self.hide_t = t
        self.hide_search = HideSearch(self.w, start, watchers, max_cost, away_from=away_from, min_dist=min_dist,
                                      height=me.height, rng=self.rng)

    def want_peek(self, target_center, t):
        if self.hide is None:
            return
        key = (self.hide[0], round(target_center[0] / 8.0), round(target_center[1] / 8.0))
        if self.peek_search is not None and key == self.peek_key:
            return
        if self.peek is not None and key == self.peek_key and t - self.peek_t < 2.0:
            return
        self.peek_key = key
        self.peek_t = t
        avoid = [self.last_peek] if self.last_peek is not None else []
        avoid += list(self.extra_avoid)
        self.peek_search = PeekSearch(self.w, self.hide[0], target_center, avoid=avoid, rng=self.rng)

    def peek_used(self):
        """We stepped back from the current peek spot: the next one must be a different edge."""
        if self.peek is not None:
            self.last_peek = self.w.node_xy(self.peek[0])
            self.peek = None
            self.peek_key = None

    def run(self, deadline):
        if self.hide_search is not None:
            if self.hide_search.run(deadline):
                res = self.hide_search.result
                if res is not None:
                    self.hide = res
                    self.peek = None
                else:
                    self.fail_t = self.hide_t
                self.hide_search = None
        if self.peek_search is not None:
            if self.peek_search.run(deadline):
                self.peek = self.peek_search.result
                self.peek_search = None

    def hide_xy(self):
        return self.w.node_xy(self.hide[0]) if self.hide else None

    def peek_xy(self):
        return self.w.node_xy(self.peek[0]) if self.peek else None

    def reset(self):
        self.hide = None
        self.peek = None
        self.hide_search = None
        self.peek_search = None
