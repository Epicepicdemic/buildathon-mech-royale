"""Compare sandbox batches (analysis/mech/sim/runner.py --trace output) as one markdown table row each.

    python3 tools/sim_compare.py "v2=DIR[,DIR...]" "v3=DIR[,DIR...]" [--peer-slot 0]

Per batch: matches, deaths before 60 s, share of the first 30 s in which any opponent could see us
(from the traces' `directly_seen_by`), seconds stuck and outside the zone per match, kills per
match, wins, mean place, and (with --peer-slot) the mean place of the peer bot in that slot.
"""
import argparse
import gzip
import json
import os
import statistics


def load(dirs):
    out = []
    for d in dirs:
        s = json.load(open(os.path.join(d, "summary.json")))
        for m in s["matches"]:
            m["_dir"] = d
            out.append(m)
    return out


def vis30(m):
    f = os.path.join(m["_dir"], "match-%03d.jsonl.gz" % m["match"])
    if not os.path.exists(f):
        return None
    n = seen = 0
    with gzip.open(f, "rt") as fh:
        next(fh)
        for line in fh:
            r = json.loads(line)
            if r["obs"]["t"] > 30.0:
                break
            n += 1
            seen += bool(r.get("directly_seen_by"))
    return seen / n if n else None


def row(name, ms, peer_slot=None):
    early = sum(1 for m in ms if m["survival_s"] < 60 and m["cause_of_death"] not in ("last_alive", "time_limit"))
    v = [x for x in (vis30(m) for m in ms) if x is not None]
    cells = [name, str(len(ms)), str(early), f"{statistics.mean(v):.3f}" if v else "—",
             f"{statistics.mean(m['stuck_s'] for m in ms):.2f}", f"{statistics.mean(m['outside_zone_s'] for m in ms):.2f}",
             f"{statistics.mean(m['kills'] for m in ms):.2f}", str(sum(1 for m in ms if m["place"] == 1)),
             f"{statistics.mean(m['place'] for m in ms):.2f}"]
    if peer_slot is not None:
        pp = []
        for m in ms:
            for e in m.get("final_mechs", []):
                if e["id"] == peer_slot:
                    pp.append(e["place"])
        cells.append(f"{statistics.mean(pp):.2f}" if pp else "—")
        cells.append(str(sum(1 for m in ms for e in m.get("final_mechs", []) if e["id"] == peer_slot and e["place"] == 1)))
    return "| " + " | ".join(cells) + " |"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("batches", nargs="+", help="name=dir[,dir]")
    ap.add_argument("--peer-slot", type=int, default=None)
    a = ap.parse_args()
    head = ["bot", "matches", "deaths before 60 s", "share of first 30 s visible", "stuck s/match",
            "outside zone s/match", "kills/match", "wins", "mean place"]
    if a.peer_slot is not None:
        head += ["peer mean place", "peer wins"]
    print("| " + " | ".join(head) + " |")
    print("|" + "---|" * len(head))
    for b in a.batches:
        name, dirs = b.split("=", 1)
        print(row(name, load(dirs.split(",")), a.peer_slot))


if __name__ == "__main__":
    main()
