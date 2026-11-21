"""Replay a sandbox trace's observations through a fresh bot and time each decision.

    python3 tools/trace_timing.py ../../analysis/mech/sim/reports/v2-c/match-004.jsonl.gz [profile_tick]
"""
import cProfile
import gzip
import json
import os
import pstats
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mech.brain import Bot  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(path, prof_tick=None):
    rows = [json.loads(l) for l in gzip.open(path, "rt")][1:]
    with gzip.open(os.path.join(ROOT, "check", "stream.jsonl.gz"), "rt") as f:
        cfg = json.loads(f.readline())["config"]
    bot = Bot({"type": "hello", "config": cfg, "seed": 1, "slot": 3})
    times = []
    for r in rows:
        o = r["obs"]
        if prof_tick is not None and o["tick"] == prof_tick:
            pr = cProfile.Profile()
            pr.enable()
            bot.act(o)
            pr.disable()
            pstats.Stats(pr).sort_stats("cumulative").print_stats(18)
            continue
        t0 = time.perf_counter()
        bot.act(o)
        times.append(((time.perf_counter() - t0) * 1000, o["tick"], o["t"]))
    times.sort(reverse=True)
    print("decisions", len(times), "worst", [(round(a, 2), b, c) for a, b, c in times[:8]])


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else None)
