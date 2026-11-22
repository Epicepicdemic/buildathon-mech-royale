"""Feed a recorded observation stream to this build with a fixed fake clock and write its actions,
so two builds can be compared decision by decision (the switchboard proof: the default
mech/squad_settings.py must play exactly like B2).

The clock advances 0.2 ms per read, so the background searches inside the time budget do the same
work in every build that reads the clock the same way. Observations come from a sandbox trace
(analysis/mech/sim/reports/*/match-*.jsonl.gz, rows with "obs"); recordings are data only.

usage (from the build's directory): python3 tools/replay_actions.py <trace.jsonl.gz> <out.jsonl>
"""
import gzip
import itertools
import json
import os
import sys
import time

_tick = itertools.count()
time.perf_counter = lambda: next(_tick) * 0.0002
time.time = time.perf_counter
sys.path.insert(0, os.getcwd())

from mech.brain import Bot          # noqa: E402
from tests.test_controller import hello   # noqa: E402


def main(trace, out):
    bot = Bot(hello())
    n = 0
    with gzip.open(trace, "rt") as f, open(out, "w") as w:
        for line in f:
            row = json.loads(line)
            obs = row.get("obs")
            if not obs:
                continue
            act = bot.act(obs)
            w.write(json.dumps(act, sort_keys=True) + "\n")
            n += 1
    print(n, "decisions; squad mode ever:", bot.tactics.mode.t_squad is not None)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
