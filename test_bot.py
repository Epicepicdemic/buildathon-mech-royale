import gzip, json, math, random, time, unittest
from bot import Bot, lead
from navigation import Grid

class Checks(unittest.TestCase):
    def test_lead(self):
        origin=[0,0,0]; target=[100,0,0]; velocity=[0,10,0]; yaw,pitch=lead(origin,target,velocity,252)
        t=100/(252*math.cos(yaw)); self.assertAlmostEqual(252*math.sin(yaw)*t,10*t,places=8); self.assertEqual(pitch,0)
    def test_routes(self):
        g=Grid(); rng=random.Random(7828); free=[n for n in range(g.nx*g.ny) if not g.value(n,0)]; passed=0; disconnected=0; missed=0
        for _ in range(200):
            start=g.xy(rng.choice(free)); goal=g.xy(rng.choice(free))
            shortest,dist=g.route(start,goal)
            if not shortest: disconnected+=1; continue
            route,cost=g.route(start,goal,.45)
            self.assertTrue(route)
            for a,b in zip(route,route[1:]): self.assertIn(b,dict(g.neighbors(a)))
            # A deadline between the shortest and preferred route exposes cases
            # where a safety-weighted route is too slow despite a feasible one.
            length=sum(math.dist(g.xy(a),g.xy(b)) for a,b in zip(route,route[1:])); deadline=(dist+length)/2/19.5
            if length/19.5>deadline+1e-6: missed+=1
            passed+=1
        print('Routes:',passed,'connected,',disconnected,'disconnected,',missed,'preferred routes miss constructed feasible deadlines')
        self.assertEqual(missed,0, 'controller does not yet select shortest route using exact deadline')
    def test_replay(self):
        with gzip.open('check/stream.jsonl.gz','rt') as f: rows=[json.loads(line) for line in f]
        bot=Bot(rows[0]); durations=[]
        for row in rows:
            if row.get('type')=='obs':
                start=time.perf_counter(); bot.act(row); durations.append((time.perf_counter()-start)*1000)
        print('Replay:',len(durations),'observations; worst decision %.3f ms'%max(durations)); self.assertLess(max(durations),20)
if __name__=='__main__': unittest.main()
