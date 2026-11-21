# Mech Royale bot: Wilce-B

Line B (`Wilce-B`) of our `mechfight/1` bot for the Builders Buildathon Mech Royale track, written in Python. Built to contest fights from `t = 0`, farm cubes/crates/NPCs/weapons and kill drops, execute fighting retreats without turning its back, and third-party mid-game circles.

## Layout

| Path | What it is |
| --- | --- |
| `bot.py` | Entry point: the kit's stdin/stdout loop (`mechfight.py`) around `mech.brain.Bot`. |
| `tools/build_world.py` | Runs at image build time. Turns the arena into lookup tables: walking floor, sight heights, clearance for a 2.5 m mech, slope limits, a 2 m navigation grid, exposure, hiding-wall fans, strongpoints and roofs. |
| `mech/world.py` | Loads the tables (standard library only); line of sight, shot clearance, walkability. |
| `mech/nav.py`, `mech/routing.py` | Zone field (walking distance to the next circle), resumable A*, the deadline rule (fastest route first, cover only with slack), jump-and-jet route edges onto and over low buildings, path following, stuck recovery. |
| `mech/zone.py` | The shrinking circle and its deadlines. |
| `mech/threats.py` | Memory of every mech and drone seen, heard or that hit us, weave detection, short tracking, and kill-drop detection. |
| `mech/loot.py` | Persistent memory and routing scorer for weapon pickups, cubes, crates, and dropped loot from killed mechs. |
| `mech/doctrine.py` | Fight calculation and the five plays: assault, corner fight, harass, fighting break to cover, reposition. |
| `mech/air.py` | Wall hop, roof edge (with pitch depression check), drop attack, laser pop-up, last-circle roof. |
| `mech/gunner.py`, `mech/ballistics.py` | Slew-aware gun aim, weave-damped leads, bursts, laser charge and release, lightning, mortar arcs. |
| `mech/positions.py`, `mech/cover.py`, `mech/tactics.py`, `mech/mover.py`, `mech/brain.py` | Strongpoint selection, cover search, per-tick tactical arbitration, bullet/shell dodging, and time budget. |
| `mech/profile.py` | Squad-practice profile: a mode detector from observation only (free-for-all by default and for good on any sign of one), the squad play choice, and switches for each piece. |
| `mech/dodge.py` | Long-leg jinking across an attacker's line of fire (0.6-1.4 s legs, measured on recorded bullets). |
| `tests/` | Unit tests, Line B regression tests (`test_line_b.py`), the ported-from-main tests (`test_port.py`), the 500-case route test and the recorded-stream replay. |
| `tools/replays.py`, `tools/replay_shooting.py`, `tools/sim_compare.py` | Offline analysis: recorded match parsing, shooting and dodging figures, sandbox batch comparison. |
| `tools/route_test.py`, `tools/kinesim.py` | Route test harness and the approximate movement model it drives. |

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
