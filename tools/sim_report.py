"""Summarise a sandbox batch (analysis/mech/sim/runner.py --trace output) for our bot.

Adds what the sandbox summary does not report: the share of the first 30 s in which any opponent
could see us, shots, hits, damage dealt and taken, kills, and the plays we used.

    python3 tools/sim_report.py ../../analysis/mech/sim/reports/v2-c
"""
import gzip
import json
import os
import sys
from collections import Counter


def main(d):
    summ = json.load(open(os.path.join(d, "summary.json")))
    rows_out = []
    tot = Counter()
    for m in summ["matches"]:
        k = m["match"]
        f = os.path.join(d, "match-%03d.jsonl.gz" % k)
        if not os.path.exists(f):
            continue
        rows = [json.loads(l) for l in gzip.open(f, "rt")][1:]
        early = [r for r in rows if r["obs"]["t"] <= 30.0]
        vis30 = sum(1 for r in early if r.get("directly_seen_by")) / max(1, len(early))
        fires = sum(1 for r in rows if r["action"] and any(r["action"].get("fire", [False, False])))
        hits = sum(1 for r in rows for e in r["obs"]["events"] if e["type"] == "hit")
        dealt = sum(e.get("amount", 0) for r in rows for e in r["obs"]["events"] if e["type"] == "hit")
        taken = sum(e.get("amount", 0) for r in rows for e in r["obs"]["events"]
                    if e["type"] == "damaged" and e.get("by") is not None)
        labels = Counter()
        lab = ""
        for r in rows:
            a = r["action"] or {}
            if "debug" in a and a["debug"].get("label") is not None:
                lab = a["debug"]["label"].split(":")[0].split(" ")[0]
            labels[lab] += 1
        rows_out.append((k, m["place"], m["kills"], m["cause_of_death"], round(m["survival_s"]), m["outside_zone_s"],
                         m["stuck_s"], round(vis30, 3), round(m["visible_to_opponent_fraction"], 3), fires, hits,
                         round(dealt), round(taken), dict(labels.most_common(4))))
        tot["vis30"] += vis30
        tot["n"] += 1
        tot["kills"] += m["kills"]
        tot["hits"] += hits
        tot["dealt"] += dealt
    print("match place kills cause surv outside stuck vis30 vis fires hits dealt taken plays")
    for r in rows_out:
        print(*r)
    n = max(1, tot["n"])
    print("mean place %.2f  outside %.2fs  stuck %.2fs  visible %.3f  visible-first-30s %.3f  kills/match %.2f  "
          "dealt/match %.0f  worst %.1f ms" % (summ["mean_place"], summ["mean_outside_zone_s"], summ["mean_stuck_s"],
                                               summ["mean_visible_fraction"], tot["vis30"] / n, tot["kills"] / n,
                                               tot["dealt"] / n, summ["worst_decision_ms"]))


if __name__ == "__main__":
    main(sys.argv[1])
