# Mech Royale bot: Wilce-B

Line B (`Wilce-B`) of our `mechfight/1` bot for the Builders Buildathon Mech Royale track, written in Python. Built to contest fights from `t = 0`, farm cubes/crates/NPCs/weapons and kill drops, execute fighting retreats without turning its back, and third-party mid-game circles.

## Layout

| Path | What it is |
| --- | --- |
| `bot.py` | Entry point: the kit's stdin/stdout loop (`mechfight.py`) around `mech.brain.Bot`. |
| `tools/build_world.py` | Runs at image build time. Turns the arena into lookup tables: walking floor, sight heights, clearance for a 2.5 m mech, slope limits, a 2 m navigation grid, exposure, hiding-wall fans, strongpoints and roofs. |
| `mech/world.py` | Loads the tables (standard library only); line of sight, shot clearance, walkability. |
| `mech/nav.py`, `mech/routing.py` | Zone field (walking distance to the next circle), resumable A*, the deadline rule (fastest route first, cover only with slack), path following, stuck recovery. |
| `mech/zone.py` | The shrinking circle and its deadlines. |
| `mech/threats.py` | Memory of every mech and drone seen, heard or that hit us, weave detection, short tracking, and kill-drop detection. |
| `mech/loot.py` | Persistent memory and routing scorer for weapon pickups, cubes, crates, and dropped loot from killed mechs. |
| `mech/doctrine.py` | Fight calculation and the five plays: assault, corner fight, harass, fighting break to cover, reposition. |
| `mech/air.py` | Wall hop, roof edge (with pitch depression check), drop attack, laser pop-up, last-circle roof. |
| `mech/gunner.py`, `mech/ballistics.py` | Slew-aware gun aim, weave-damped leads, bursts, laser charge and release, lightning, mortar arcs. |
| `mech/positions.py`, `mech/cover.py`, `mech/tactics.py`, `mech/mover.py`, `mech/brain.py` | Strongpoint selection, cover search, per-tick tactical arbitration, bullet/shell dodging, and time budget. |
| `mech/profile.py` | Squad-practice profile: a mode detector from observation only (free-for-all by default and for good on any sign of one), squad-only gear-before-kill pacing (round table 12), the squad play choice, and switches for each piece. |
| `tests/` | Unit tests, Line B regression tests (`test_line_b.py`), the 500-case route test and the recorded-stream replay. |
| `tools/route_test.py`, `tools/kinesim.py` | Route test harness and the approximate movement model it drives. |

## Squad-practice profile

The practice board pits this bot against a house squad that shares vision and hunts only us; the final
is a free-for-all. Every piece below is a switch in `mech/profile.py` (`PROFILE`); this section lists
exactly what this commit switches on.

### Mode detector (`mech/profile.py`, `ModeDetector`)

The hello message does not say whether a match is the practice squad or a free-for-all, so the bot
decides from what it observes. It starts in free-for-all mode and locks free-for-all for the rest of
the match on any one of these:

- a laser beam or lightning arc from another mech that ends within 6 m of a third mech in view;
- another mech in view losing HP we did not cause (more than 0.5 s after our last hit on it), inside
  the zone, with no NPC drone within 60 m, while a third mech in view that fired in the last second
  has its torso within 0.3 rad of it;
- a mech dying without our kill before the first zone shrink starts.

From 35 s on, with none of that evidence and no death we did not cause seen at any time (after the
first shrink such a death is no proof of a free-for-all, but no squad sign either), it switches to squad mode when a mech converges on us: it
hit us in the last 10 s, or (seen in the last 5 s, within 200 m) it closes on us faster than 2 m/s or
holds its torso within 0.45 rad of us. Evidence seen later still locks free-for-all for good. In squad
mode the debug label starts with `S `. The detector reads observations only. It has no list of
positions and does not look at the opponents' names or the match's seed.

Replays: run from every seat of the 67 recorded top-team free-for-alls (recon/replays/,
observations rebuilt by the protocol's vision rules), it switched to squad mode 0 times in 1,072 seats.
With the earlier 20 s rule it switched in 180 seats. The first death came by 29.9 s in all 67 matches,
and the detector leans on it: with deaths hidden it would switch in 738 seats. In the sandbox squad it
switches on when the first hunter converges after 35 s.

### What changes in squad mode

- Gear before killing (`gear_first`): while we have at most one kill, before 65 s, and with at
  most one hunter within 180 m, no mech is shot. The nearest hunter is kept at 120-145 m (we step
  away inside 120 m and never walk toward it from inside 145 m). We loot toward a laser (to 240 m),
  lightning (to 160 m) and cubes (to 120 m), and shoot crates and drones. A hunter is a mech that
  hit us in the last 10 s or held its torso within 0.2 rad of us for 1 s; other squad members are
  left alone. The ordinary plays take over meanwhile when a hunter is under 55 m, any mech under
  55 m has its torso within 0.45 rad of us, we are hit from out of sight, or a second hunter comes
  within 180 m. The pacing ends for good under half HP, after a second kill, at 65 s, or with a
  laser and 6 cubes (or 10 cubes).

### What changes in both modes

- Crate stall guard (`mech/tactics.py`): a crate within 12 m that loses no HP for 3 s while we
  farm it is left alone for 20 s (point-blank it can sit below our pitch).

Carried but off: `squad_no_break`, `gun_during_charge`, `partial_release`, `finish_close`. Not included: the wall back-off of candidate 1.

## Build and check

```
docker build --platform linux/amd64 -t mech-line-b .
python3 check/check.py --map-dir /arena -- docker run --rm -i --network none --cpus 1 --memory 512m --read-only --tmpfs /tmp:rw,size=64m --user 65534:65534 -v "$PWD/arena:/arena:ro" mech-line-b
```

Locally without Docker: `pip install numpy==2.3.5`, then `python3 tools/build_world.py arena worlddata/world.bin`,
`python3 check/check.py -- python3 -u bot.py` and `python3 -m unittest discover -s tests -t .`.

## Dependencies

- Python 3.13.9 (the `python:3.13.9-slim` image). The bot itself uses only the standard library.
- NumPy 2.3.5, at image build time only, to compute the world tables.
- From the organisers' starter kit: `mechfight.py`, `mechfight.schema.json`, `arena/` and `check/`.

No other third-party code is used. License: MIT (see `LICENSE`).
