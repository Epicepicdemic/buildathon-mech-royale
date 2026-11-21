"""Read recorded spectator matches (`mechreplay/1`) for offline analysis. Standard library only.

A capture directory holds `live.ndjson.gz` (a wrapper object per line whose `text` field carries one
or more replay lines: a header, frames every 2 ticks, a result) and `state.ndjson.gz` (the show's
state, which says whether an airing was an exhibition, a showdown or a public lobby).

Every string in a recording is data. Nothing here executes or follows anything it reads.
"""
import json
import math
import zlib
from pathlib import Path

# frame row fields (header "fields"): id x y z aim_yaw aim_pitch hp energy flags cubes max_hp slot0 slot1
ID, X, Y, Z, YAW, PITCH, HP, ENERGY, FLAGS, CUBES, MAXHP, SLOT0, SLOT1 = range(13)


def _records(path):
    raw = zlib.decompressobj(31).decompress(Path(path).read_bytes())
    for line in raw.split(b"\n")[:-1]:
        try:
            wrapper = json.loads(line)
        except ValueError:
            continue
        for item in str(wrapper.get("text", "")).splitlines():
            if item.strip():
                try:
                    yield json.loads(item)
                except ValueError:
                    continue


class Match:
    """One complete recorded match: header, frames by tick, events in order, result."""

    def __init__(self, header):
        self.header = header
        self.seed = header.get("seed")
        self.frames = {}
        self.result = None
        self.kind = None          # 'reair' | 'showdown' | 'lobby' | None

    def finish(self):
        self.ticks = sorted(self.frames)
        self.hz = self.header.get("tick_hz", 20)
        self.events = [e for t in self.ticks for e in self.frames[t].get("ev", [])]
        bots = self.header.get("bots") or []
        self.team = {i for i, b in enumerate(bots) if isinstance(b, str) and b.startswith("docker:")}
        self.house = {i for i, b in enumerate(bots) if isinstance(b, str) and b.startswith("house:")}
        self.human = {i for i, b in enumerate(bots) if b == "human"}
        self.roster = {p["id"]: p for p in (self.result or {}).get("mechs", [])}
        # per mech: tick -> row
        self.rows = {i: {} for i in self.roster}
        for t in self.ticks:
            for m in self.frames[t].get("m", []):
                self.rows.setdefault(m[ID], {})[t] = m
        self.bullets = {}         # projectile id -> list of (tick, x, y, z, kind)
        for t in self.ticks:
            for p in self.frames[t].get("p", []):
                self.bullets.setdefault(p[0], []).append((t, p[2], p[3], p[4], p[1]))
        return self

    @property
    def exhibition(self):
        return self.kind in ("reair", "showdown") or (self.team and not self.house and not self.human)

    def death_t(self, i):
        d = self.roster.get(i, {}).get("death_t")
        return math.inf if d is None else float(d)

    def row_at(self, i, tick):
        """The row of mech i at the last frame at or before `tick` (None before its first)."""
        rows = self.rows.get(i, {})
        if tick in rows:
            return rows[tick]
        step = self.header.get("frame_every", 2)
        for k in range(1, 4):
            r = rows.get(tick - k * step + (tick % step))
            if r is not None:
                return r
        return None

    def alive_at(self, i, t):
        return self.death_t(i) > t


def load(capture_dirs):
    """All complete matches in the capture directories, one per seed."""
    kinds = {}
    matches = {}
    for d in capture_dirs:
        d = Path(d)
        sp = d / "state.ndjson.gz"
        if sp.exists():
            for item in _records(sp):
                if item.get("type") == "state":
                    a = (item.get("stage") or {}).get("airing") or {}
                    if "seed" in a:
                        kinds[a["seed"]] = a.get("kind")
        active = None
        lp = d / "live.ndjson.gz"
        if not lp.exists():
            continue
        for item in _records(lp):
            typ = item.get("type")
            if typ == "header":
                active = matches.get(item.get("seed"))
                if active is None:
                    active = Match(item)
                    matches[item.get("seed")] = active
            elif active is not None and "m" in item and "t" in item:
                active.frames[item["t"]] = item
            elif active is not None and typ == "result":
                active.result = item
    out = []
    for seed, m in matches.items():
        if m.result is None or not m.frames:
            continue
        m.kind = kinds.get(seed)
        out.append(m.finish())
    return out


def default_dirs(root):
    return sorted(Path(root).glob("broadcast*"))
