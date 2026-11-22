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
| `mech/brawler.py` | Candidate D, squad mode only: the close-quarters brawler and counter-attacker (lightning-first loadout, commit, attack, doorway counter, mortar handling, energy floor). |
| `mech/profile.py` | Squad-practice profile: a mode detector from observation only (free-for-all by default and for good on any sign of one), squad-only gear-before-kill pacing (round table 12), the squad play choice with isolation from all but one shooter, sticky DPS/HP targeting, airborne aim, the damage pieces, and switches for each piece. |
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

### Candidate D: squad-mode brawler (`mech/brawler.py`)

Squad mode only: every entry point checks the detector above, so none of this runs before the switch
(at the earliest 35 s) or at all in a free-for-all. Free-for-all play is unchanged: `tests/test_brawler.py`
checks that a free-for-all close fight and the kit's recorded stream give identical actions with the
pieces on and off. Switches in `PROFILE`, all on by default: `brawl_loadout`, `brawl_commit`,
`brawl_attack`, `brawl_counter`, `brawl_mortar`. Each decision tries commit, then attack, then counter;
none applies with under 4 s of zone slack, and air plays are skipped while one does.

A hunter here is a mech that hit us in the last 10 s, held its torso within 0.2 rad of us for 1 s in
the last 10 s, or is in view, sees us and has its torso within 0.45 rad of us.

- Loadout (`brawl_loadout`): lightning is sought before the laser. A held laser that is not charging
  is dropped for lightning, and the gun is kept. A laser is no longer taken once we hold lightning, and
  a mortar is still swapped out for either. With lightning held:
  - Upgrades go lightning first: efficiency, then range, ahead of plating; chain, reactor and capacitor
    come after plating.
  - Lightning counts as geared, so `gear_first` ends.
  - Energy floor: no ground boost under 55 energy, so a fight starts with zaps in hand. Jumps, flight,
    air plays, being outside the zone and zone runs keep their boost.
- Commit (`brawl_commit`): this fires with lightning held, at least 12 energy, and a hunter in view
  inside lightning range minus 2 m (43 m at the base 45 m range).
  - Target: the visible mech in that range that the arc chains through most (other mechs within
    14 m of it, at most 2), then the nearest. Without a laser, a mortar holder beyond 20 m comes last.
  - Gun and lightning both fire on it. We walk in to about 28 m and strafe from there.
  - No break, unless we are under 20% HP against a target over 50%.
- Attack (`brawl_attack`): this needs lightning held, at least 55 energy, at least 55% HP, and at most
  two mechs seeing us.
  - Target: a hunter seen in the last 3 s within 160 m, with no other hunter within 75 m of it or of
    us. Mechs within 14 m of it are allowed (at most 2: the chain hits them), and more of them is
    preferred, then the nearer target.
  - We close on it, through cover when more than 60 m out, toward a point 20 m behind it when it is
    not looking at us. The gun fires while it is in sight; commit takes over inside lightning range.
- Counter (`brawl_counter`): with lightning held and a hunter between lightning range and 140 m, we go
  to a cell within 40 m of walking that meets all of these:
  - hidden from every known threat point;
  - a wall within 8 m;
  - open ground of at least 18 m in two directions at least 1.6 rad apart (a second way out);
  - not within 7 m of anywhere a mech saw us in the last 5 s.

  There we hold, face the hunter and stay silent unless it sees us. Commit fires when it walks within
  lightning range minus 2 m (43 m at the base 45 m range). Corner peeks in squad mode also skip spots seen in the last 5 s.
- Mortar holders (`brawl_mortar`): with a laser (lightning not yet found), a visible mortar holder in
  laser range scores x2.5 as a target, on top of the x1.6 cook-off (`pick_target` in
  `mech/doctrine.py`; `MORTAR_LASER_MULT` in `mech/brawler.py` names the same number but is not read). Without a laser, mortar holders and
  any hunter within 45 m of one are never attacked. During a commit, a mortar holder beyond 20 m is the
  last choice. The doorway search hides from mortar holders like any other watcher.
- Crossfire: none. There is no positioning to make a mortar's splash hit its teammate and no body
  blocking. Practice-squad friendly-fire rules are unrecorded (analysis/mech/CROSSFIRE.md).

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
