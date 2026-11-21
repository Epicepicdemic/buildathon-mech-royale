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
