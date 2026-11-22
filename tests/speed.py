"""A quick, deterministic speed benchmark of the machine running the tests, for timing limits.

The bot's search budget is a fixed amount of work (mech/nav.py WorkBudget), so its CPU time per decision
follows the machine's speed. Timing tests scale their limits by `speed_factor()`: this machine's time for
a fixed workload (line-of-sight segments and a small zone-field search, the bot's two main costs) over
the same workload's time on the reference machine (an Apple M-series laptop); never below 1.0. The
minimum of several repetitions is used, so a briefly busy machine does not inflate it.
"""
import math
import random
import time

from mech import nav

REF_BENCH_S = 0.059         # seconds for bench() on the reference machine, an Apple M-series laptop (2026-11-22:
                            # 0.0591-0.0605 s; its fuzz worst decision 7-10 ms). Measured elsewhere: one c7i worker
                            # CPU with its hyperthread sibling busy 0.2145 s. A slower machine gets a proportionally
                            # higher timing limit; tests also keep an absolute backstop.


def bench(w, reps=7):
    rng = random.Random(5)
    mains = [n for n in range(0, w.nu * w.nv, 53) if w.main[n]]
    segs = []
    while len(segs) < 600:
        a, b = rng.choice(mains), rng.choice(mains)
        (ax, ay), (bx, by) = w.node_xy(a), w.node_xy(b)
        if 20.0 < math.hypot(bx - ax, by - ay) < 180.0:
            segs.append((ax, ay, w.floor(ax, ay) + 8.0, bx, by, w.floor(bx, by) + 4.5))
    cx, cy = w.node_xy(mains[len(mains) // 2])
    best = 1e9
    for _ in range(reps):
        c0 = time.thread_time()
        for s in segs:
            w.line_clear(*s, step=1.0, canopy=True)
        nav.ZoneField(w, cx, cy, 60.0).run(1e18)
        best = min(best, time.thread_time() - c0)
    return best


def speed_factor(w):
    """(factor, bench seconds): how many times slower than the reference machine this one is (1.0 when as
    fast or faster), and this machine's bench() time."""
    b = bench(w)
    return max(1.0, b / REF_BENCH_S), b
