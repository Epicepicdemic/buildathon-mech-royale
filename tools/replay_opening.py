"""What did the mechs that lived through the first minute do differently from those that died?

For every mech in every recorded match, over its first 60 s (or its life, if shorter):
when it first fired, when it was first hit, how far and how fast it moved, and, frame by frame,
whether any living opponent could see it under the rulebook's vision rules (eye 8 m up; within
45 m in any direction with canopy ignored, or within 200 m inside the 120 degree yaw wedge of the
viewer's torso aim with canopy blocking; buildings and terrain always block). Sight uses the bot's
own world tables (mech/world.py).

Usage: python3 tools/replay_opening.py RECORDINGS_DIR [--json OUT]
"""
import argparse
import json
import math
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import replays as R                      # noqa: E402
from mech.world import World            # noqa: E402

STEP_TICKS = 4            # sample every 0.2 s
NEAR, RANGE, HALF_FOV = 45.0, 200.0, math.radians(60.0)


def wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


def sees(world, viewer, target):
    """Does mech row `viewer` see mech row `target`?"""
    s_v = 1 + 0.05 * viewer[R.CUBES]
    s_t = 1 + 0.05 * target[R.CUBES]
    eye = (viewer[R.X], viewer[R.Y], viewer[R.Z] + 8.0 * s_v)
    dx, dy = target[R.X] - eye[0], target[R.Y] - eye[1]
    dh = math.hypot(dx, dy)
    if dh > RANGE + 15:
        return False
    in_wedge = abs(wrap(math.atan2(dy, dx) - viewer[R.YAW])) <= HALF_FOV
    for h in (9.0 * s_t, 4.5 * s_t):
        tz = target[R.Z] + h
        d3 = math.sqrt(dh * dh + (tz - eye[2]) ** 2)
        near = d3 <= NEAR
        if not near and (d3 > RANGE or not in_wedge):
            continue
        if world.line_clear(eye[0], eye[1], eye[2], target[R.X], target[R.Y], tz, canopy=not near):
            return True
    return False


def first_event_times(m):
    fire, hit_out, hit_in, dmg_in_30, dmg_out_30 = {}, {}, {}, {}, {}
    mech_ids = set(m.roster)
    for e in m.events:
        t = e.get("t", 0) / m.hz
        typ = e.get("e")
        if typ in ("fire", "laser", "zap") and e.get("id") in mech_ids:
            fire.setdefault(e["id"], t)
        elif typ == "hit" and e.get("src") in mech_ids and e.get("dst") in mech_ids and e["src"] != e["dst"]:
            hit_out.setdefault(e["src"], t)
            hit_in.setdefault(e["dst"], t)
            if t <= 30.0:
                dmg_in_30[e["dst"]] = dmg_in_30.get(e["dst"], 0) + e.get("amt", 0)
                dmg_out_30[e["src"]] = dmg_out_30.get(e["src"], 0) + e.get("amt", 0)
    return fire, hit_out, hit_in, dmg_in_30, dmg_out_30


def analyse_match(world, m, horizon=60.0):
    fire, hit_out, hit_in, din, dout = first_event_times(m)
    out = []
    end_tick = min(m.ticks[-1], int(horizon * m.hz))
    sample = [t for t in m.ticks if t % STEP_TICKS == 0 and t <= end_tick]
    seen_by = {i: [] for i in m.roster}        # (t, n_viewers, nearest viewer distance)
    for t in sample:
        tt = t / m.hz
        live = [i for i in m.roster if m.alive_at(i, tt) and t in m.rows.get(i, {})]
        for i in live:
            ri = m.rows[i][t]
            n = 0
            for j in live:
                if j == i:
                    continue
                if sees(world, m.rows[j][t], ri):
                    n += 1
            seen_by[i].append((tt, n))
    for i, p in m.roster.items():
        rows = m.rows.get(i, {})
        if not rows:
            continue
        death = m.death_t(i)
        life = min(death, horizon)
        ts = sorted(t for t in rows if t / m.hz <= life)
        if not ts:
            continue
        path = 0.0
        travel = {10: 0.0, 20: 0.0, 30: 0.0}
        fast = 0.0
        prev = None
        spawn = rows[ts[0]]
        for t in ts:
            r = rows[t]
            if prev is not None:
                d = math.hypot(r[R.X] - prev[R.X], r[R.Y] - prev[R.Y])
                dt = (t - prev_t) / m.hz
                path += d
                for k in travel:
                    if t / m.hz <= k:
                        travel[k] += d
                if dt > 0 and d / dt > 5.0 and t / m.hz <= 30:
                    fast += dt
            prev, prev_t = r, t
        def at(sec):
            k = max([t for t in ts if t / m.hz <= sec] or [ts[0]])
            return rows[k]
        r30 = at(30.0)
        disp30 = math.hypot(r30[R.X] - spawn[R.X], r30[R.Y] - spawn[R.Y])
        sb = seen_by.get(i, [])
        s30 = [n for (tt, n) in sb if tt <= min(30.0, death)]
        s60 = [n for (tt, n) in sb if tt <= min(60.0, death)]
        first_seen = next((tt for (tt, n) in sb if n > 0), None)
        n_expo = world.nearest_free(spawn[R.X], spawn[R.Y], 4)
        energy_min10 = min((rows[t][R.ENERGY] for t in ts if t / m.hz <= 10), default=100)
        out.append(dict(
            match=m.seed, kind=m.kind, id=i, name=p.get("name"), place=p.get("place"), death=death,
            killed_with=p.get("killed_with"), travel10=travel[10], travel20=travel[20], travel30=travel[30],
            disp30=disp30, fast30=fast, first_fire=fire.get(i), first_hit_dealt=hit_out.get(i),
            first_hit_taken=hit_in.get(i), dmg_in_30=din.get(i, 0), dmg_out_30=dout.get(i, 0),
            seen30=(sum(1 for n in s30 if n > 0) / len(s30)) if s30 else None,
            seen60=(sum(1 for n in s60 if n > 0) / len(s60)) if s60 else None,
            viewers30=(statistics.mean(s30) if s30 else None),
            first_seen=first_seen,
            spawn_expo=(world.exposure(n_expo) if n_expo is not None else None),
            energy_min10=energy_min10, spawn_z=spawn[R.Z]))
    return out


def summarise(rows, label):
    def col(key, sub, fn=statistics.median):
        v = [r[key] for r in sub if r[key] is not None]
        return (fn(v) if v else None), len(v)

    groups = [
        ("died before 30 s", [r for r in rows if r["death"] < 30]),
        ("died 30-60 s", [r for r in rows if 30 <= r["death"] < 60]),
        ("alive at 60 s", [r for r in rows if r["death"] >= 60]),
        ("alive at 90 s", [r for r in rows if r["death"] >= 90]),
    ]
    keys = ["travel10", "travel30", "disp30", "fast30", "first_fire", "first_hit_dealt", "first_hit_taken",
            "dmg_out_30", "dmg_in_30", "seen30", "viewers30", "first_seen", "spawn_expo", "energy_min10"]
    print(f"\n## {label}: {len(rows)} mechs")
    print("| group | n | " + " | ".join(keys) + " |")
    print("|---|---|" + "---|" * len(keys))
    for name, sub in groups:
        cells = []
        for k in keys:
            v, n = col(k, sub)
            cells.append("—" if v is None else (f"{v:.2f}" if isinstance(v, float) else str(v)) + ("" if n == len(sub) else f" ({n})"))
        print(f"| {name} | {len(sub)} | " + " | ".join(cells) + " |")
    # share who fired / were hit / were seen early
    print()
    for name, sub in groups:
        if not sub:
            continue
        f10 = sum(1 for r in sub if r["first_fire"] is not None and r["first_fire"] <= 10) / len(sub)
        s5 = sum(1 for r in sub if r["first_seen"] is not None and r["first_seen"] <= 5) / len(sub)
        h_first = sum(1 for r in sub if r["first_hit_dealt"] is not None and
                      (r["first_hit_taken"] is None or r["first_hit_dealt"] < r["first_hit_taken"])) / len(sub)
        print(f"- {name}: fired by 10 s {f10:.0%}; seen by 5 s {s5:.0%}; landed a hit before taking one {h_first:.0%}")


def showdown_hours(recordings):
    """{seed: 'E1' | 'E2' | ...} from the scout's tables.md (E1.x = 10 PM, E2.x = 11 PM, E3.x = midnight)."""
    out = {}
    p = os.path.join(recordings, "tables.md")
    if not os.path.exists(p):
        return out
    for line in open(p, encoding="utf-8"):
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 3 and cells[0].startswith("E") and "." in cells[0] and cells[2].isdigit():
            out[int(cells[2])] = cells[0].split(".")[0]
    return out


def midgame(world, m, t0=90.0, t1=300.0):
    """For mechs alive at t0: share of [t0, min(t1, death)] seen by an opponent, damage dealt and
    taken, cubes gained, distance from the live circle's centre as a share of its radius."""
    hz = m.hz
    alive0 = [i for i in m.roster if m.death_t(i) > t0]
    if not alive0:
        return []
    zone_by_tick = {t: m.frames[t].get("z") for t in m.ticks}
    hits = [e for e in m.events if e.get("e") == "hit" and e.get("src") in m.roster and e.get("dst") in m.roster
            and e["src"] != e["dst"] and t0 * hz <= e["t"] <= t1 * hz]
    out = []
    for i in alive0:
        rows = m.rows.get(i, {})
        end = min(t1, m.death_t(i), m.ticks[-1] / hz)
        if end <= t0 + 5:
            continue
        seen = n = 0
        rel = []
        for t in m.ticks:
            tt = t / hz
            if tt < t0 or tt > end or t % 10 or t not in rows:
                continue
            n += 1
            ri = rows[t]
            for j in m.roster:
                if j != i and m.alive_at(j, tt) and t in m.rows.get(j, {}) and sees(world, m.rows[j][t], ri):
                    seen += 1
                    break
            z = zone_by_tick.get(t)
            if z:
                rel.append(math.hypot(ri[R.X] - z[0], ri[R.Y] - z[1]) / max(1.0, z[2]))
        k0 = max([t for t in rows if t / hz <= t0] or [min(rows)])
        k1 = max([t for t in rows if t / hz <= end] or [k0])
        out.append(dict(match=m.seed, id=i, place=m.roster[i].get("place"), death=m.death_t(i),
                        seen=seen / n if n else None,
                        dealt=sum(e["amt"] for e in hits if e["src"] == i and e["t"] <= end * hz) / max(1.0, end - t0) * 60,
                        taken=sum(e["amt"] for e in hits if e["dst"] == i and e["t"] <= end * hz) / max(1.0, end - t0) * 60,
                        cubes=(rows[k1][R.CUBES] - rows[k0][R.CUBES]) / max(1.0, end - t0) * 60,
                        cubes_at_90=rows[k0][R.CUBES], rel_r=statistics.median(rel) if rel else None))
    return out


def summarise_mid(rows, label):
    groups = [("placed 1-3", [r for r in rows if r["place"] <= 3]),
              ("placed 4-6", [r for r in rows if 4 <= r["place"] <= 6]),
              ("placed 7+", [r for r in rows if r["place"] >= 7])]
    keys = ["seen", "dealt", "taken", "cubes", "cubes_at_90", "rel_r"]
    print(f"\n## {label}: mechs alive at 90 s, 90-300 s (per minute where a rate)")
    print("| group | n | seen share | damage dealt /min | damage taken /min | cubes /min | cubes at 90 s | distance from centre / radius |")
    print("|---|---|---|---|---|---|---|---|")
    for name, sub in groups:
        if not sub:
            continue
        cells = []
        for k in keys:
            v = [r[k] for r in sub if r[k] is not None]
            cells.append(f"{statistics.median(v):.2f}" if v else "—")
        print(f"| {name} | {len(sub)} | " + " | ".join(cells) + " |")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("recordings")
    ap.add_argument("--world", default=None)
    ap.add_argument("--json", default=None)
    a = ap.parse_args()
    world = World(a.world) if a.world else World()
    ms = R.load(R.default_dirs(a.recordings))
    hours = showdown_hours(a.recordings)
    rows = []
    mids = []
    for m in ms:
        rows.extend(analyse_match(world, m))
        if m.kind in ("reair", "showdown"):
            for r in midgame(world, m):
                r["hour"] = hours.get(m.seed)
                mids.append(r)
        print(f"match {m.seed} ({m.kind}) done", file=sys.stderr, flush=True)
    for r in rows:
        r["hour"] = hours.get(r["match"])
    if a.json:
        with open(a.json, "w") as f:
            json.dump({"opening": rows, "mid": mids}, f, indent=1)
    ex = [r for r in rows if r["kind"] in ("reair", "showdown")]
    lob = [r for r in rows if r["kind"] not in ("reair", "showdown")]
    print(f"{len(ms)} matches: {len(set(r['match'] for r in ex))} between team bots, "
          f"{len(set(r['match'] for r in lob))} public lobbies")
    summarise(ex, "Team-bot matches, all")
    names = {"E1": "10 PM showdown", "E2": "11 PM showdown", "E3": "midnight showdown"}
    for h in sorted(set(r["hour"] for r in ex if r["hour"])):
        summarise([r for r in ex if r["hour"] == h], f"Team-bot matches, {names.get(h, h)}")
    summarise(lob, "Public lobbies (house bots and humans)")
    summarise_mid(mids, "Mid-game, team-bot matches, all")
    later = [r for r in mids if r.get("hour") in ("E2", "E3")]
    if later:
        summarise_mid(later, "Mid-game, 11 PM and midnight showdowns")


if __name__ == "__main__":
    main()
