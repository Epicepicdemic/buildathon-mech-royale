"""Route tests on the real map (500 random starts and circles; see tools/route_test.py)."""
import json
import os
import unittest

from tests.helpers import ROOT  # noqa: F401  (puts the repo on sys.path)
from tools import route_test

N = int(os.environ.get("ROUTE_CASES", "500"))


class RouteTests(unittest.TestCase):
    def test_random_routes_valid_and_on_time(self):
        stats, failures = route_test.run(N, seed=7828, sim=True)
        print("\nroutes:", json.dumps(stats))
        for f in failures[:10]:
            print("  failure:", json.dumps(f))
        self.assertEqual(stats["cases"], N)
        self.assertEqual(stats["invalid_paths"], 0)
        self.assertEqual(stats["unreached"], 0)
        self.assertEqual(stats["missed"], 0)
        self.assertEqual(stats["stuck_events"], 0)


class HopTests(unittest.TestCase):
    """The dead end beside RPAC (sandbox seed 4): the next circle is 90 m south behind the PAES/RPAC
    complex and the only walk out is a 1.5 km loop north. Version 2 took it and died in the zone.
    With hop edges the route goes up onto the 6.5 m roof, across and off it."""

    def test_rpac_pocket_hops_and_arrives(self):
        import math
        from tests.helpers import world
        from mech import nav, routing
        w = world()
        circle = (-84.75, -188.44, 22.0)
        stay = (-77.84, -149.69, 66.0)
        zf = nav.ZoneField(w, *circle, stay=stay)
        zf.run(1e18)
        walk = nav.ZoneField(w, *circle, stay=stay, use_hops=False)
        walk.run(1e18)
        s = w.nearest_free(-101.3, -98.7, 6)
        self.assertGreater(walk.cost(s), 1000.0)
        self.assertLess(zf.cost(s), 200.0)
        nodes = zf.path(s)
        self.assertEqual(route_test.validate_nodes(w, nodes), [])
        e = routing.enter_length(w, nodes, *circle)
        route = routing.Route(w, nodes, "zone", False, e, e)
        self.assertTrue(route.hops)
        mob = routing.Mobility(route_test.load_config()["mech"])
        ph = dict(center=stay[:2], radius=70.0, next_center=circle[:2], next_radius=25.0,
                  shrink_start=350.0, shrink_end=375.0)
        arrive, outside, stuck, _ = route_test.simulate(w, route, mob, 327.0, 375.0, circle, ph, energy=80.0)
        self.assertIsNotNone(arrive)
        self.assertLess(arrive, 350.0)
        self.assertEqual(outside, 0.0)


class FollowerTests(unittest.TestCase):
    def test_follower_cuts_corners_where_walkable(self):
        from tests.helpers import world
        from mech import nav, routing
        w = world()
        a = w.nearest_free(300.0, -60.0)       # open lawn of the Oval
        b = w.nearest_free(340.0, -20.0)
        s = nav.AStar(w, a, b)
        s.run(1e18)
        pts = nav.path_points(w, s.result)
        fol = routing.Follower(w, pts)
        tx, ty, rem, off = fol.target(*pts[0])
        # on open ground it aims well past the next grid node
        self.assertGreater(((tx - pts[0][0]) ** 2 + (ty - pts[0][1]) ** 2) ** 0.5, 10.0)


if __name__ == "__main__":
    unittest.main()
