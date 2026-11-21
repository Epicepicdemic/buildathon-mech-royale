"""Discrete 20 Hz jet and airborne movement model for Mech Royale.

Derived from `brief/mech-royale/protocol.md` (§5, §6, §7) and verified tick-for-tick
against the recorded real match `check/stream.jsonl.gz`.

Key findings & explicit assumptions:
1. Tick rate & action hold: The server simulates at `tick_hz = 20` (`dt = 0.05` s) and
   reads bot actions every `decide_every = 2` ticks (`0.10` s). Planned manoeuvres
   change `jump`/`boost`/`move` only on 2-tick (`0.10` s) decision boundaries.
2. Steady climb speed is 8.90 m/s, not 10.0 m/s: On each 0.05 s airborne tick with
   `boost=True` and energy available, upward velocity is first accelerated and clamped
   to `max_rise_speed` (`min(10.0, vz + 34.0 * 0.05) = 10.0` m/s), THEN gravity is
   subtracted (`10.0 - 22.0 * 0.05 = 8.90` m/s), and THEN position updates
   (`z += vz * 0.05` -> `+0.445` m/tick = `+0.89` m per 0.10 s observation).
3. Jump + jet on tick 1: Sending `jump=True, boost=True` from ground sets `vz = 11.0`,
   immediately runs the air-jet clamp (`min(10.0, 11.0 + 1.7) = 10.0`, spending 0.75
   energy), and subtracts gravity (`8.90` m/s). Holding `boost=False` for the first
   2-tick decision (`jump=True, boost=False`) gives `vz = 9.9, 8.8` m/s (`+0.935` m)
   for zero energy before igniting jets at `t = 0.10` s (+0.98 m higher max ledge).
   Default tables use standard `jump=True, boost=True` from `t = 0`, and also expose
   the delayed-jet option.
4. Partial-energy last tick (`0 < E < 0.75`): Never reached in `stream.jsonl.gz`
   (minimum energy observed is 6.9). Default model requires `E >= jet_drain * dt`
   (`0.75`) for a full jet tick (`60.77` m max apex with immediate boost, `61.70` m
   with 0.1 s delayed boost; proportional partial tick gives `60.89` / `61.83` m).
5. Horizontal motion in air: Target horizontal speed is `boost_speed` (`19.5` m/s)
   while `boost=True` with energy, else `walk_speed` (`11.7` m/s), slewing at
   `ground_accel` (`58.5` m/s^2) on the ground (including tick 1 of a jump, matching
   `|dv_h| = 2.925 + 0.600 = 3.525` m/s in `stream.jsonl.gz`) and `air_accel`
   (`12.0` m/s^2) once airborne.
6. Energy regeneration: `18.0`/s (`+0.90`/tick) begins strictly after `0.60` s without
   spending (13 zero-regen ticks, first `+0.90` on tick 14, matching every regen event
   in `stream.jsonl.gz`).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np


DEFAULT_MECH_CONFIG: Dict[str, float] = {
    "radius": 2.5,
    "height": 9.0,
    "eye_height": 8.0,
    "muzzle_height": 7.0,
    "walk_speed": 11.7,
    "boost_speed": 19.5,
    "ground_accel": 58.5,
    "air_accel": 12.0,
    "gravity": 22.0,
    "jump_speed": 11.0,
    "jet_accel": 34.0,
    "max_rise_speed": 10.0,
    "step_up": 1.2,
    "max_slope_deg": 50.0,
    "energy": 100.0,
    "energy_regen": 18.0,
    "energy_regen_delay": 0.6,
    "boost_drain": 17.0,
    "jet_drain": 15.0,
    "turn_rate": 4.0,
    "pitch_rate": 3.0,
    "min_pitch": -0.7,
    "max_pitch": 1.3,
    "tick_hz": 20.0,
    "decide_every": 2.0,
}


@dataclass(frozen=True)
class ClimbResult:
    reachable: bool
    dh: float
    time_s: float
    energy_used: float
    horiz_dist_m: float
    peak_z_m: float
    jet_ticks: int
    total_ticks: int
    start_mode: str


@dataclass(frozen=True)
class FallResult:
    dh: float
    time_s: float
    ticks: int
    impact_vz: float
    horiz_dist_m: float
    final_horiz_speed: float


def merged_config(cfg: Optional[Dict[str, float]] = None) -> Dict[str, float]:
    out = dict(DEFAULT_MECH_CONFIG)
    if cfg:
        for k, v in cfg.items():
            if k in out and isinstance(v, (int, float)):
                out[k] = float(v)
    return out


def _move_toward_2d(
    vx: float, vy: float, tx: float, ty: float, max_dv: float
) -> Tuple[float, float]:
    dx = tx - vx
    dy = ty - vy
    d = math.hypot(dx, dy)
    if d <= max_dv or d < 1e-12:
        return tx, ty
    s = max_dv / d
    return vx + dx * s, vy + dy * s


def step_tick_free(
    pos: Tuple[float, float, float],
    vel: Tuple[float, float, float],
    grounded: bool,
    energy: float,
    since_spend: float,
    move: Tuple[float, float],
    boost: bool,
    jump: bool,
    ground_z: float = -1e9,
    cfg: Optional[Dict[str, float]] = None,
    partial_jet: bool = False,
) -> Tuple[Tuple[float, float, float], Tuple[float, float, float], bool, float, float]:
    """Advance one 0.05 s simulation tick in unobstructed space over flat `ground_z`."""
    c = DEFAULT_MECH_CONFIG if cfg is None else cfg
    dt = 1.0 / c["tick_hz"]
    x, y, z = pos
    vx, vy, vz = vel
    mx, my = move
    mnorm = math.hypot(mx, my)
    if mnorm > 1.0:
        mx /= mnorm
        my /= mnorm
        mnorm = 1.0

    jet_cost = c["jet_drain"] * dt
    boost_cost = c["boost_drain"] * dt
    has_jet_e = (energy > 1e-9) if partial_jet else (energy >= jet_cost - 1e-9)
    has_boost_e = (energy > 1e-9) if partial_jet else (energy >= boost_cost - 1e-9)

    # 1. Horizontal velocity slew (uses pre-jump `grounded`, verified on ticks 278->280 & 388->390)
    will_jump = grounded and jump
    if grounded:
        use_boost_spd = boost and (has_jet_e if will_jump else (has_boost_e and mnorm > 1e-9))
        target_spd = c["boost_speed"] if use_boost_spd else c["walk_speed"]
        accel = c["ground_accel"]
    else:
        use_boost_spd = boost and has_jet_e
        target_spd = c["boost_speed"] if use_boost_spd else c["walk_speed"]
        accel = c["air_accel"]

    vx, vy = _move_toward_2d(vx, vy, mx * target_spd, my * target_spd, accel * dt)

    # 2. Jump & energy/vertical thrust
    spent = False
    if will_jump:
        vz = c["jump_speed"]
        grounded = False

    if grounded:
        if boost and mnorm > 1e-9 and has_boost_e:
            spend = min(energy, boost_cost)
            energy -= spend
            spent = spend > 0.0
    else:
        if boost and has_jet_e:
            if energy >= jet_cost - 1e-9:
                vz = min(c["max_rise_speed"], vz + c["jet_accel"] * dt)
                energy -= jet_cost
                spent = True
            elif partial_jet and energy > 1e-9:
                frac = energy / jet_cost
                vz = min(c["max_rise_speed"], vz + c["jet_accel"] * dt * frac)
                energy = 0.0
                spent = True
        vz -= c["gravity"] * dt

    # 3. Semi-implicit Euler position update
    x += vx * dt
    y += vy * dt
    if not grounded:
        z += vz * dt
        if z <= ground_z:
            z = ground_z
            vz = 0.0
            grounded = True

    # 4. Energy regeneration (starts strictly after `energy_regen_delay`, 13 delay ticks)
    if spent:
        since_spend = 0.0
    else:
        if since_spend > c["energy_regen_delay"] + 1e-9:
            energy = min(c["energy"], energy + c["energy_regen"] * dt)
        since_spend += dt

    return (x, y, z), (vx, vy, vz), grounded, energy, since_spend


def vertical_profile_for_jet_decisions(
    n_jet_decisions: int,
    delay_jet_one_decision: bool = False,
    max_energy: float = 100.0,
    cfg: Optional[Dict[str, float]] = None,
    partial_jet: bool = False,
) -> Tuple[float, int, int, float, List[Tuple[float, float, float]]]:
    """Simulate a jump + `n_jet_decisions` (2 ticks each) + upward coast to apex.

    Returns `(peak_z, total_ticks_to_apex, actual_jet_ticks, energy_used, trace)`
    where `trace` is `[(t, z, vz), ...]` at each 0.05 s tick up to apex (`vz <= 0`).
    """
    c = merged_config(cfg)
    decide_ticks = int(round(c["decide_every"]))
    pos = (0.0, 0.0, 0.0)
    vel = (0.0, 0.0, 0.0)
    grounded = True
    energy = max_energy
    since_spend = 10.0
    start_energy = energy

    trace: List[Tuple[float, float, float]] = []
    dt = 1.0 / c["tick_hz"]
    tick = 0
    actual_jet_ticks = 0
    peak_z = 0.0
    peak_tick = 0

    total_jet_window_decisions = n_jet_decisions + (1 if delay_jet_one_decision else 0)
    max_ticks = max(40, (total_jet_window_decisions + 20) * decide_ticks)

    while tick < max_ticks:
        dec_idx = tick // decide_ticks
        jump_cmd = dec_idx == 0
        if delay_jet_one_decision:
            boost_cmd = 1 <= dec_idx <= n_jet_decisions
        else:
            boost_cmd = 0 <= dec_idx < n_jet_decisions

        e_before = energy
        pos, vel, grounded, energy, since_spend = step_tick_free(
            pos,
            vel,
            grounded,
            energy,
            since_spend,
            move=(0.0, 0.0),
            boost=boost_cmd,
            jump=jump_cmd,
            ground_z=-1e9,
            cfg=c,
            partial_jet=partial_jet,
        )
        tick += 1
        if energy < e_before - 1e-9:
            actual_jet_ticks += 1
        trace.append((tick * dt, pos[2], vel[2]))
        if pos[2] >= peak_z:
            peak_z = pos[2]
            peak_tick = tick
        if tick > 2 and vel[2] <= 0.0 and dec_idx >= total_jet_window_decisions:
            break

    energy_used = start_energy - energy
    # If energy regenerated after a very long coast (never happens before apex since coast is 0.4s < 0.6s),
    # compute exact jet energy from actual_jet_ticks when not partial:
    if not partial_jet:
        energy_used = actual_jet_ticks * c["jet_drain"] * dt
    return peak_z, peak_tick, actual_jet_ticks, energy_used, trace


def max_ledge_height(
    energy: float = 100.0,
    delay_jet_one_decision: bool = False,
    wall_standoff_m: float = 0.0,
    cfg: Optional[Dict[str, float]] = None,
    partial_jet: bool = False,
) -> float:
    """Highest ledge (above takeoff feet height) reachable for available `energy`.

    - If `wall_standoff_m == 0.0`: returns maximum feet height cleared (approaching
      with running horizontal velocity so the mech crosses the edge at apex).
    - If `wall_standoff_m > 0.0`: mech starts stationary pressed against the wall
      (`v_horiz = 0` while `z < H`), and once `z >= H` must accelerate horizontally
      at `air_accel` to cover `wall_standoff_m` before descending below `H`.
    """
    c = merged_config(cfg)
    if energy <= 0.0:
        # Pure jump (0 energy)
        peak_z, _, _, _, trace = vertical_profile_for_jet_decisions(
            0, False, 0.0, c, partial_jet
        )
        if wall_standoff_m <= 0.0:
            return peak_z
        return _max_ledge_with_standoff(trace, c, wall_standoff_m)

    dt = 1.0 / c["tick_hz"]
    decide_ticks = int(round(c["decide_every"]))
    max_decisions = int(math.ceil(energy / (c["jet_drain"] * dt * decide_ticks))) + 2
    peak_z, _, _, _, trace = vertical_profile_for_jet_decisions(
        max_decisions, delay_jet_one_decision, energy, c, partial_jet
    )
    if wall_standoff_m <= 0.0:
        return peak_z
    return _max_ledge_with_standoff(trace, c, wall_standoff_m)


def _max_ledge_with_standoff(
    up_trace: List[Tuple[float, float, float]],
    c: Dict[str, float],
    standoff_m: float,
) -> float:
    """Given upward trace to apex, append downward fall ticks and find highest H
    such that horizontal air acceleration while `z >= H` covers `standoff_m`."""
    dt = 1.0 / c["tick_hz"]
    zs = [z for _, z, _ in up_trace]
    vz = up_trace[-1][2]
    z = up_trace[-1][1]
    for _ in range(40):
        vz -= c["gravity"] * dt
        z += vz * dt
        zs.append(z)
    z_arr = np.array(zs)

    # Precompute horizontal distance covered in k airborne ticks from v0 = 0
    max_k = len(zs)
    horiz_by_k = np.zeros(max_k + 1)
    vx = 0.0
    x = 0.0
    for k in range(1, max_k + 1):
        vx = min(c["walk_speed"], vx + c["air_accel"] * dt)
        x += vx * dt
        horiz_by_k[k] = x

    lo, hi = c["step_up"], float(z_arr.max())
    if hi <= lo:
        return lo
    for _ in range(32):
        mid = 0.5 * (lo + hi)
        above = np.nonzero(z_arr >= mid)[0]
        if len(above) > 0:
            ticks_above = int(above[-1] - above[0] + 1)
            if horiz_by_k[ticks_above] >= standoff_m:
                lo = mid
            else:
                hi = mid
        else:
            hi = mid
    return lo


def climb_cost(
    dh: float,
    start_mode: str = "walk",
    max_energy: float = 100.0,
    delay_jet_one_decision: bool = False,
    wall_blocked_climb: bool = False,
    step_onto_dist_m: float = 0.2,
    cfg: Optional[Dict[str, float]] = None,
    partial_jet: bool = False,
) -> ClimbResult:
    """Compute minimum energy & time to climb height `dh` above takeoff surface.

    Parameters
    ----------
    dh : float
        Height difference `target_top - start_z` (m).
    start_mode : {"standing", "walk", "boost"}
        Initial horizontal speed at takeoff (`0.0`, `walk_speed`, or `boost_speed`).
    wall_blocked_climb : bool
        If True, horizontal motion toward the roof is blocked by the vertical wall
        while `z < dh`, and only accelerates inward once `z >= dh` to cover
        `step_onto_dist_m` (e.g. `0.2` m to overhang the roof edge per §7, or `2.5` m
        for full center-on-roof without edge slide).
    """
    c = merged_config(cfg)
    dt = 1.0 / c["tick_hz"]
    decide_ticks = int(round(c["decide_every"]))

    if dh <= c["step_up"]:
        v0 = (
            c["boost_speed"]
            if start_mode == "boost"
            else (c["walk_speed"] if start_mode == "walk" else 0.0)
        )
        if wall_blocked_climb and step_onto_dist_m > 0:
            t_step = move_time_ground(step_onto_dist_m, v0=v0, boost=(start_mode == "boost"), cfg=c)
            return ClimbResult(True, dh, t_step, 0.0, step_onto_dist_m, max(0.0, dh), 0, int(math.ceil(t_step / dt)), start_mode)
        return ClimbResult(True, dh, 0.0, 0.0, 0.0, max(0.0, dh), 0, 0, start_mode)

    max_decisions = int(math.ceil(max_energy / (c["jet_drain"] * dt * decide_ticks))) + 2
    v0 = (
        c["boost_speed"]
        if start_mode == "boost"
        else (c["walk_speed"] if start_mode == "walk" else 0.0)
    )

    for n_dec in range(0, max_decisions + 1):
        pos = (0.0, 0.0, 0.0)
        vel = (0.0 if wall_blocked_climb else v0, 0.0, 0.0)
        grounded = True
        energy = max_energy
        since_spend = 10.0
        jet_ticks = 0
        peak_z = 0.0
        total_window = n_dec + (1 if (delay_jet_one_decision and n_dec > 0) else 0)

        for tick in range(1, (total_window + 25) * decide_ticks + 1):
            dec_idx = (tick - 1) // decide_ticks
            jump_cmd = dec_idx == 0
            if n_dec == 0:
                boost_cmd = False
            elif delay_jet_one_decision:
                boost_cmd = 1 <= dec_idx <= n_dec
            else:
                boost_cmd = 0 <= dec_idx < n_dec

            # If blocked by wall below dh, horizontal velocity stays 0 until z >= dh
            can_move_h = (not wall_blocked_climb) or (pos[2] >= dh)
            move_cmd = (1.0, 0.0) if can_move_h else (0.0, 0.0)
            if not can_move_h:
                vel = (0.0, 0.0, vel[2])

            e_before = energy
            pos, vel, grounded, energy, since_spend = step_tick_free(
                pos,
                vel,
                grounded,
                energy,
                since_spend,
                move=move_cmd,
                boost=boost_cmd,
                jump=jump_cmd,
                ground_z=-1e9,
                cfg=c,
                partial_jet=partial_jet,
            )
            if energy < e_before - 1e-9:
                jet_ticks += 1
            peak_z = max(peak_z, pos[2])

            if not wall_blocked_climb:
                if pos[2] >= dh:
                    e_used = jet_ticks * c["jet_drain"] * dt if not partial_jet else (max_energy - min(max_energy, energy))
                    return ClimbResult(
                        True, dh, round(tick * dt, 4), round(e_used, 4),
                        round(pos[0], 3), round(peak_z, 3), jet_ticks, tick, start_mode
                    )
            else:
                # Cleared wall and moved horizontally onto roof while still at z >= dh
                if pos[2] >= dh and pos[0] >= step_onto_dist_m:
                    e_used = jet_ticks * c["jet_drain"] * dt
                    return ClimbResult(
                        True, dh, round(tick * dt, 4), round(e_used, 4),
                        round(pos[0], 3), round(peak_z, 3), jet_ticks, tick, start_mode
                    )

            if tick > 2 and vel[2] < 0.0 and pos[2] < dh and dec_idx >= total_window:
                break

        # If we already spent all available energy, larger n_dec won't help
        if jet_ticks * c["jet_drain"] * dt >= max_energy - (0.01 if partial_jet else c["jet_drain"] * dt):
            break

    return ClimbResult(False, dh, float("inf"), float("inf"), 0.0, round(peak_z, 3), 0, 0, start_mode)


def fall_metrics(
    dh: float,
    start_mode: str = "walk",
    move_in_air: float = 1.0,
    cfg: Optional[Dict[str, float]] = None,
) -> FallResult:
    """Discrete 20 Hz free-fall from height `dh` (`jump=False, boost=False`)."""
    c = merged_config(cfg)
    if dh <= 0.0:
        return FallResult(0.0, 0.0, 0, 0.0, 0.0, 0.0)

    dt = 1.0 / c["tick_hz"]
    v0 = (
        c["boost_speed"]
        if start_mode == "boost"
        else (c["walk_speed"] if start_mode == "walk" else 0.0)
    )
    vx = v0
    vz = 0.0
    x = 0.0
    z = dh
    ticks = 0
    mv = max(-1.0, min(1.0, move_in_air))
    target_vx = mv * c["walk_speed"]

    while z > 0.0:
        vx, _ = _move_toward_2d(vx, 0.0, target_vx, 0.0, c["air_accel"] * dt)
        vz -= c["gravity"] * dt
        x += vx * dt
        z += vz * dt
        ticks += 1

    return FallResult(
        dh=dh,
        time_s=round(ticks * dt, 4),
        ticks=ticks,
        impact_vz=round(vz, 3),
        horiz_dist_m=round(x, 3),
        final_horiz_speed=round(vx, 3),
    )


def air_lateral_distance(
    air_time_s: float,
    v0_xy: Tuple[float, float] = (0.0, 0.0),
    move_dir: Tuple[float, float] = (1.0, 0.0),
    boost: bool = False,
    energy: float = 100.0,
    cfg: Optional[Dict[str, float]] = None,
) -> Tuple[float, float, float]:
    """Compute `(dx, dy, final_speed)` travelled horizontally during `air_time_s`."""
    c = merged_config(cfg)
    dt = 1.0 / c["tick_hz"]
    n_ticks = max(0, int(round(air_time_s / dt)))
    vx, vy = v0_xy
    mx, my = move_dir
    mn = math.hypot(mx, my)
    if mn > 1.0:
        mx /= mn
        my /= mn
    x, y = 0.0, 0.0
    e = energy
    jet_cost = c["jet_drain"] * dt
    for _ in range(n_ticks):
        can_jet = boost and (e >= jet_cost - 1e-9)
        t_spd = c["boost_speed"] if can_jet else c["walk_speed"]
        vx, vy = _move_toward_2d(vx, vy, mx * t_spd, my * t_spd, c["air_accel"] * dt)
        if can_jet:
            e -= jet_cost
        x += vx * dt
        y += vy * dt
    return round(x, 3), round(y, 3), round(math.hypot(vx, vy), 3)


def move_time_ground(
    dist_m: float,
    v0: float = 0.0,
    boost: bool = False,
    cfg: Optional[Dict[str, float]] = None,
) -> float:
    """Discrete 20 Hz time to move `dist_m` across a flat roof/ground surface."""
    c = merged_config(cfg)
    if dist_m <= 0.0:
        return 0.0
    dt = 1.0 / c["tick_hz"]
    vmax = c["boost_speed"] if boost else c["walk_speed"]
    acc = c["ground_accel"]
    v = v0
    x = 0.0
    ticks = 0
    while x < dist_m:
        v = min(vmax, v + acc * dt)
        x += v * dt
        ticks += 1
    return round(ticks * dt, 4)
