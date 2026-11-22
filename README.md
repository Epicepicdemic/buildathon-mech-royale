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
| `mech/profile.py` | Squad-practice profile: a mode detector from observation only (free-for-all by default and for good on any sign of one), squad-only gear-before-kill pacing (round table 12), the squad play choice with isolation from all but one shooter, sticky DPS/HP targeting, airborne aim, the damage pieces, and switches for each piece. |
| `mech/cleanroom.py`, `mech/cleanroom_settings.py` | Clean-room pieces from our behaviour spec (gear first, energy, hide to heal, fight cells, home spot), each behind a switch. |
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
- No break (`squad_no_break`): a second mech within 160 m and HP under 30% are not reasons to
  break. A break stands only after 1.5 s of clearly losing (two or more mechs within 160 m and HP
  under 55%, three or more and under 80%, or under 35% against a healthier target), and never while
  finishing (one burst kills, or the target is at or under 30% HP). Otherwise we fight from the
  corner if one is ready, else assault.
- Isolation (`lose_isolate`): while two or more other shooters within 160 m see us, the target
  sees us, and we are not finishing, we boost to a cell within 24 m hidden from all of them but the
  target, the gun firing on the way.
- House-bot numbers (`house_aim`): the fight calculation uses the hit rates measured for house
  experts (analysis/mech/HOUSE-BOTS.md): gun 87.5% under 45 m down to 17% beyond 150 m, laser 12% at
  105-135 m and 6.7% at 135-150 m (our fit elsewhere: 50% under 45 m down to 4% beyond 150 m), zap 83%,
  mortar 38%. Assault keeps a floor of 70 m from a lightning holder (kiting back inside
  it) and 95 m from a mortar holder.
- Reloads (`punish_reload`): a target heard reloading in the last 2.2 s counts its gun at a quarter in
  the fight calculation.

### What changes in both modes (free-for-all play too)

- Sticky target (`sticky_target`): targets are scored by (their damage per second on us + 80) /
  max(HP, 120), and the current target is kept at x2.2 (x3.0 under 35% HP). In a free-for-all there
  is no stickiness while two or more shooters have us in sight.
- Airborne aim (`airborne_aim`): a mech more than 0.45 m above the ground or roof under it counts as
  airborne. The fight calculation then gives it no dodge discount, and the gun's lead adds gravity
  (22 m/s^2, never below that ground) while it climbs slower than 6 m/s. There is no weave damping,
  and long bursts go to 165 m.
- Gun during the laser charge (`gun_during_charge`): the torso stays on the laser's line; the gun fires
  only when its own lead is within 0.12 rad of that line and its aim test passes.
- Early release (`partial_release`): the laser is released as soon as 70% of a release now (with the
  cook-off) would kill the target.
- Cook-off (`mortar_priority`): our laser does x1.6 to a mortar holder (the config's `cookoff`). This
  counts in the fight calculation, the one-burst kill test, the early release, and the target score
  (x1.6 when we hold a laser).
- Reloads (`punish_reload`): a target heard reloading in the last 2.2 s scores x1.4 as a target
  (squad mode also changes the fight calculation, above).
- `finish_close` is off.
- Crate stall guard (`mech/tactics.py`): a crate within 12 m that loses no HP for 3 s while we
  farm it is left alone for 20 s (point-blank it can sit below our pitch).

Not included: the wall back-off of candidate 1.

### Switchboard (`mech/squad_settings.py`)

Every number that shapes squad-mode play is a knob in `mech/squad_settings.py`. The defaults are B2's
values, so the default build plays exactly like 8d7d9cb: replaying five recorded streams (9,615
decisions, three squad matches, two free-for-alls) gives identical actions (`tools/replay_actions.py`).
A variant is `mech/squad_settings.json`, written and committed by `tools/variant.py key=value ...`. An
unknown key or a wrong type stops the bot at start. Free-for-all play reads none of the knobs
(`tests/test_switchboard.py`), and the mode detector's thresholds are not knobs.

## Clean-room pieces (`mech/cleanroom.py`)

Implemented clean-room from our behaviour spec (`analysis/mech/CLEANROOM-SPEC.md` in our private
workspace, which describes behaviours and our own sandbox measurements only). No other team's code
was read or used; every number was set by us. Each piece has its own switch: free-for-all mode (which
is also every match's first 35 s, before the squad detector can decide) reads
`mech/cleanroom_settings.py`, squad mode reads the same names with a `cr_` prefix from
`mech/squad_settings.py`. All are off by default, so the default build plays exactly like f90bf0a
(identical actions on recorded streams, `tools/replay_actions.py`). A variant sets them in
`mech/cleanroom_settings.json` / `mech/squad_settings.json`.

**This commit switches on:** `gear`, `energy`, `silent` and `heal` in both modes (`silent` acts only in
free-for-all mode), through `mech/cleanroom_settings.json` and `mech/squad_settings.json`; `cells` and `home` are
off. Sandbox, 40 seeds paired with 780bef0: free-for-all +1.10 +- 0.68 places, squad +0.10 +- 0.41 (survival
+45 +- 10 s).

- `gear`: a mech is fought only when it is a real threat or an easy kill: within 60 m (70 m in
  squad mode) in sight, it hit us in the last 4 s, it is charging a laser at us, it held its torso on
  us for 1 s within 110 m, it is under 35% HP in sight within 130 m, or four or fewer mechs are
  alive. Otherwise we loot with the wide radii, an item chased 22 s without reaching it is dropped
  for 45 s, and a mech merely in sight does not stop looting or farming.
- `energy`: boost only for a laser charge at us, a mortar shell, the zone, an isolation step, a
  retreat while hit in the last 2 s, finishing a target, or bringing lightning into range; loot and
  travel walk. `silent`: idle travel in a free-for-all walks under the 5 m/s step-sound speed.
- `heal`: under 50% HP we retreat to the nearest cell hidden from every mech seen in the last 8 s
  (30 s in squad mode) and from a point 70 m down the bearing of an unseen hit, and stand still
  there until 92% HP (or 70% after 12 s with nothing seen or hitting us). No fire at drones or
  crates under 50% HP; a drone that is hitting us is shot back; hit at the cell, we move.
- `cells`: in range of the target with no corner ready, each 0.4 s pick the best of our cell and
  eight cells 7 m out: preferred range for the loadout, in the target's sight only when a weapon is
  ready (always in squad mode), out of other enemies' sight, clear of walls and lightning/mortar
  holders, inside the zone, on a strafe side that flips every 1.2-3 s.
- `home` (free-for-all only): the hold cell between fights is costed against every mech seen in the
  last 45 s, keeps clear of announced supply-drop landings, and is kept 30 s.

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
