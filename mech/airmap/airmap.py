"""Fast (< 0.1 ms) runtime query module for 3D roof surfaces, sight, and air manoeuvres.

Usage in the bot:
    from air import AirMap
    air = AirMap()  # loads precomputed tables once (~25 ms)

    hop = air.wall_hop(x, y, pursuer_pos=(px, py))
    pop = air.popup_shot(x, y)
    da  = air.drop_attack(x, y, z)
    lc  = air.last_circle(zone_cx, zone_cy)
"""
from __future__ import annotations

import json
import math
import os
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np

from .flight import (
    ClimbResult,
    DEFAULT_MECH_CONFIG,
    FallResult,
    air_lateral_distance,
    climb_cost,
    fall_metrics,
    max_ledge_height,
    move_time_ground,
)

THIS_DIR = os.path.dirname(os.path.abspath(__file__))


class AirMap:
    """In-memory O(1) / small-slice lookup wrapper over `air_tables.npz` and `air_meta.json`."""

    def __init__(
        self,
        npz_path: Optional[str] = None,
        meta_path: Optional[str] = None,
    ) -> None:
        npz_file = npz_path or os.path.join(THIS_DIR, "air_tables.npz")
        meta_file = meta_path or os.path.join(THIS_DIR, "air_meta.json")

        with np.load(npz_file, allow_pickle=False) as z:
            gm = z["grid_meta"]
            self.min_x = float(gm[0])
            self.max_x = float(gm[1])
            self.min_y = float(gm[2])
            self.max_y = float(gm[3])
            self.res_1m = float(gm[4])
            self.res_4m = float(gm[5])

            self.surf_grid_1m = np.array(z["surf_grid_1m"], dtype=np.int16)
            self.surf_top_1m = np.array(z["surf_top_1m"], dtype=np.float32)
            self.ny1, self.nx1 = self.surf_grid_1m.shape

            self.g4_standable = np.array(z["g4_standable"], dtype=bool)
            self.g4_gz = np.array(z["g4_gz"], dtype=np.float32)
            self.ny4, self.nx4 = self.g4_standable.shape

            self.access_arr = np.array(z["access_arr"], dtype=np.float32)
            self.stepoff_arr = np.array(z["stepoff_arr"], dtype=np.float32)
            self.hop_arr = np.array(z["hop_arr"], dtype=np.float32)
            self.sight_arr = np.array(z["sight_arr"], dtype=np.float32)
            self.sight_poi_masks = np.array(z["sight_poi_masks"], dtype=np.int32)

            self.wh_surf = np.array(z["wh_surf"], dtype=np.int16)
            self.wh_pos = np.array(z["wh_pos"], dtype=np.float32)
            self.wh_cost = np.array(z["wh_cost"], dtype=np.float32)
            self.wh_oct = np.array(z["wh_oct"], dtype=np.float32)

            self.drop_attack_arr = np.array(z["drop_attack_arr"], dtype=np.float32)
            self.popup_valid = np.array(z["popup_valid"], dtype=bool)
            self.popup_data = np.array(z["popup_data"], dtype=np.float32)

            self.roof_stand_cloud = np.array(z["roof_stand_cloud"], dtype=np.float32)
            self.roof_stand_sids = np.array(z["roof_stand_sids"], dtype=np.int16)

        with open(meta_file, "r") as f:
            meta = json.load(f)

        self.counts: Dict[str, Any] = meta["counts"]
        self.da_tags: List[str] = meta["da_tags"]
        self.da_tag_to_idx: Dict[str, int] = {t: i for i, t in enumerate(self.da_tags)}
        self.poi_tags: List[str] = meta.get("poi_tags", [])
        self.surfaces: List[Dict[str, Any]] = meta["surfaces"]
        self.prism_roofs: List[Dict[str, Any]] = meta["prism_roofs"]
        self.hop_summary: Dict[str, Any] = meta["hop_summary"]
        self.sight_summary: Dict[str, Any] = meta["sight_summary"]
        self.surface_by_id: Dict[int, Dict[str, Any]] = {
            int(s["surface_id"]): s for s in self.surfaces
        }
        self.top10_roof_positions: List[Dict[str, Any]] = meta["top10_roof_positions"]
        self.overwatch_by_marker: Dict[str, List[Dict[str, Any]]] = meta["sight_summary"]["overwatch_by_marker"]
        self.last_circle_blocks: List[Dict[str, Any]] = meta["last_circle_blocks"]

        # Pre-index slices by surface_id for O(1) dictionary lookups
        self._sight_by_surf: Dict[int, Tuple[np.ndarray, np.ndarray]] = {}
        if len(self.sight_arr) > 0:
            sids = self.sight_arr[:, 0].astype(np.int32)
            for sid in np.unique(sids):
                m_s = sids == sid
                self._sight_by_surf[int(sid)] = (self.sight_arr[m_s], self.sight_poi_masks[m_s])

        self._stepoffs_by_surf: Dict[int, np.ndarray] = {}
        if len(self.stepoff_arr) > 0:
            sids = self.stepoff_arr[:, 0].astype(np.int32)
            for sid in np.unique(sids):
                self._stepoffs_by_surf[int(sid)] = self.stepoff_arr[sids == sid]

        self._hops_by_surf: Dict[int, np.ndarray] = {}
        if len(self.hop_arr) > 0:
            sids = self.hop_arr[:, 0].astype(np.int32)
            for sid in np.unique(sids):
                self._hops_by_surf[int(sid)] = self.hop_arr[sids == sid]

        self._da_by_surf: Dict[int, np.ndarray] = {}
        self._da_by_tag: Dict[str, np.ndarray] = {}
        if len(self.drop_attack_arr) > 0:
            sids = self.drop_attack_arr[:, 0].astype(np.int32)
            for sid in np.unique(sids):
                self._da_by_surf[int(sid)] = self.drop_attack_arr[sids == sid]
            tidxs = self.drop_attack_arr[:, 9].astype(np.int32)
            for ti, tname in enumerate(self.da_tags):
                self._da_by_tag[tname] = self.drop_attack_arr[tidxs == ti]

        # Pre-unpack sparse cell tuples into immutable 2D tuple grids (untracked by CPython cyclic GC)
        self._wh_any = self.wh_surf[:, :, 0] >= 0
        wh_rows: List[List[Optional[Tuple[Tuple[Any, ...], ...]]]] = [
            [None] * self.nx4 for _ in range(self.ny4)
        ]
        wh_js, wh_is = np.nonzero(self._wh_any)
        for j_c, i_c in zip(wh_js.tolist(), wh_is.tolist()):
            lx_c = self.min_x + (i_c + 0.5) * self.res_4m
            ly_c = self.min_y + (j_c + 0.5) * self.res_4m
            sids_c = self.wh_surf[j_c, i_c].tolist()
            pos_c = self.wh_pos[j_c, i_c].tolist()
            cost_c = self.wh_cost[j_c, i_c].tolist()
            oct_c = self.wh_oct[j_c, i_c].tolist()
            entries = []
            for k in range(len(sids_c)):
                sid_k = int(sids_c[k])
                if sid_k < 0:
                    break
                entries.append(
                    (
                        sid_k,
                        lx_c,
                        ly_c,
                        pos_c[k][0],
                        pos_c[k][1],
                        pos_c[k][2],
                        cost_c[k][0],
                        cost_c[k][1],
                        cost_c[k][2],
                        tuple(oct_c[k]),
                    )
                )
            wh_rows[j_c][i_c] = tuple(entries)
        self._wh_grid: Tuple[Tuple[Optional[Tuple[Tuple[Any, ...], ...]], ...], ...] = tuple(
            tuple(r) for r in wh_rows
        )

        pop_rows: List[List[Optional[Tuple[float, ...]]]] = [
            [None] * self.nx4 for _ in range(self.ny4)
        ]
        pop_js, pop_is = np.nonzero(self.popup_valid)
        for j_c, i_c in zip(pop_js.tolist(), pop_is.tolist()):
            lx_c = self.min_x + (i_c + 0.5) * self.res_4m
            ly_c = self.min_y + (j_c + 0.5) * self.res_4m
            gz_c = float(self.g4_gz[j_c, i_c])
            d_c = self.popup_data[j_c, i_c].tolist()
            pop_rows[j_c][i_c] = (lx_c, ly_c, gz_c, d_c[0], d_c[1], d_c[2], d_c[3], d_c[4], d_c[5], d_c[6], d_c[7])
        self._pop_grid: Tuple[Tuple[Optional[Tuple[float, ...]], ...], ...] = tuple(
            tuple(r) for r in pop_rows
        )

        # Precompute spatial grid (32m buckets) with pre-sliced arrays for zero-concatenation last_circle queries
        self._lc_bucket_size = 32.0
        self._lc_buckets: Dict[Tuple[int, int], Tuple[np.ndarray, np.ndarray]] = {}
        if len(self.roof_stand_cloud) > 0:
            bx = ((self.roof_stand_cloud[:, 0] - self.min_x) / self._lc_bucket_size).astype(np.int32)
            by = ((self.roof_stand_cloud[:, 1] - self.min_y) / self._lc_bucket_size).astype(np.int32)
            bucket_map: Dict[Tuple[int, int], List[int]] = {}
            for idx, (ix, iy) in enumerate(zip(bx.tolist(), by.tolist())):
                bucket_map.setdefault((ix, iy), []).append(idx)
            for key, idxs in bucket_map.items():
                idx_arr = np.array(idxs, dtype=np.int32)
                self._lc_buckets[key] = (
                    np.ascontiguousarray(self.roof_stand_cloud[idx_arr]),
                    np.ascontiguousarray(self.roof_stand_sids[idx_arr]),
                )

        self._lc_gnd_buckets: Dict[Tuple[int, int], np.ndarray] = {}
        g_js, g_is = np.nonzero(self.g4_standable)
        if len(g_is) > 0:
            g_xs = (self.min_x + (g_is.astype(np.float32) + 0.5) * self.res_4m)
            g_ys = (self.min_y + (g_js.astype(np.float32) + 0.5) * self.res_4m)
            g_pts = np.column_stack([g_xs, g_ys]).astype(np.float32)
            gbx = ((g_xs - self.min_x) / self._lc_bucket_size).astype(np.int32)
            gby = ((g_ys - self.min_y) / self._lc_bucket_size).astype(np.int32)
            gb_map: Dict[Tuple[int, int], List[int]] = {}
            for idx, (ix, iy) in enumerate(zip(gbx.tolist(), gby.tolist())):
                gb_map.setdefault((ix, iy), []).append(idx)
            for key, idxs in gb_map.items():
                self._lc_gnd_buckets[key] = np.ascontiguousarray(g_pts[np.array(idxs, dtype=np.int32)])

        self.data_bytes = os.path.getsize(npz_file) + os.path.getsize(meta_file)

    # -----------------------------------------------------------------------
    # Fast Grid Indexing Helpers
    # -----------------------------------------------------------------------

    def _ij1(self, x: float, y: float) -> Tuple[int, int]:
        i = int((x - self.min_x) / self.res_1m)
        j = int((y - self.min_y) / self.res_1m)
        if i < 0:
            i = 0
        elif i >= self.nx1:
            i = self.nx1 - 1
        if j < 0:
            j = 0
        elif j >= self.ny1:
            j = self.ny1 - 1
        return i, j

    def _ij4(self, x: float, y: float) -> Tuple[int, int]:
        i = int((x - self.min_x) / self.res_4m)
        j = int((y - self.min_y) / self.res_4m)
        if i < 0:
            i = 0
        elif i >= self.nx4:
            i = self.nx4 - 1
        if j < 0:
            j = 0
        elif j >= self.ny4:
            j = self.ny4 - 1
        return i, j

    def _cell4_xy(self, i: int, j: int) -> Tuple[float, float]:
        return self.min_x + (i + 0.5) * self.res_4m, self.min_y + (j + 0.5) * self.res_4m

    # -----------------------------------------------------------------------
    # Core Surface, Access, Hop & Sight Queries
    # -----------------------------------------------------------------------

    def surface_id_at(self, x: float, y: float, z: Optional[float] = None) -> int:
        """Return merged `surface_id` at `(x, y)` (or `-1` if on open ground)."""
        if x < self.min_x or x > self.max_x or y < self.min_y or y > self.max_y:
            return -1
        i, j = self._ij1(x, y)
        sid = int(self.surf_grid_1m[j, i])
        if sid < 0:
            return -1
        if z is not None and z < float(self.surf_top_1m[j, i]) - 2.0:
            return -1
        return sid

    def surface_at(self, x: float, y: float, z: Optional[float] = None) -> Optional[Dict[str, Any]]:
        sid = self.surface_id_at(x, y, z)
        return self.surface_by_id.get(sid) if sid >= 0 else None

    def reachable_roofs(self, x: float, y: float, max_energy: float = 100.0) -> List[Dict[str, Any]]:
        """Return reachable roofs from ground position `(x, y)` (searching 5x5 4m neighbourhood)."""
        ci, cj = self._ij4(x, y)
        j0, j1 = max(0, cj - 2), min(self.ny4, cj + 3)
        i0, i1 = max(0, ci - 2), min(self.nx4, ci + 3)
        if not self._wh_any[j0:j1, i0:i1].any():
            return []
        best_by_sid: Dict[int, Dict[str, Any]] = {}
        wh_grid = self._wh_grid
        for nj in range(j0, j1):
            row_wh = wh_grid[nj]
            for ni in range(i0, i1):
                entries = row_wh[ni]
                if entries is None:
                    continue
                lx, ly = entries[0][1], entries[0][2]
                walk_d = math.hypot(lx - x, ly - y)
                walk_t = walk_d / 11.7 if walk_d > 1.0 else 0.0
                for sid, _, _, rx, ry, rz, e_u, climb_t, _, _ in entries:
                    if e_u > max_energy:
                        continue
                    t_c = climb_t + walk_t
                    prev = best_by_sid.get(sid)
                    if prev is None or (e_u, t_c) < (prev["energy"], prev["time_s"]):
                        best_by_sid[sid] = {
                            "surface_id": sid,
                            "name": self.surface_by_id[sid]["name"],
                            "launch_xy": (round(lx, 1), round(ly, 1)),
                            "roof_pos": (round(rx, 1), round(ry, 1), round(rz, 1)),
                            "energy": round(e_u, 2),
                            "time_s": round(t_c, 2),
                        }
        res = list(best_by_sid.values())
        res.sort(key=lambda r: (r["energy"], r["time_s"]))
        return res

    def step_offs(self, surface_or_xy: Union[int, Tuple[float, float]]) -> List[Dict[str, Any]]:
        """Where stepping off each edge of a roof surface lands."""
        sid = surface_or_xy if isinstance(surface_or_xy, int) else self.surface_id_at(surface_or_xy[0], surface_or_xy[1])
        rows = self._stepoffs_by_surf.get(sid)
        if rows is None:
            return []
        return [
            {
                "surface_id": sid,
                "edge_pos": (float(r[1]), float(r[2]), float(r[3])),
                "out_dir": (float(r[4]), float(r[5])),
                "land_pos": (float(r[6]), float(r[7]), float(r[8])),
                "fall_time_s": float(r[9]),
                "land_surface_id": int(r[10]),
            }
            for r in rows
        ]

    def roof_hops(self, surface_or_xy: Union[int, Tuple[float, float]], max_energy: float = 100.0) -> List[Dict[str, Any]]:
        """Roof-to-roof airborne connections from `surface_or_xy` within `max_energy`."""
        sid = surface_or_xy if isinstance(surface_or_xy, int) else self.surface_id_at(surface_or_xy[0], surface_or_xy[1])
        rows = self._hops_by_surf.get(sid)
        if rows is None:
            return []
        out = []
        for r in rows:
            if float(r[10]) <= max_energy:
                to_sid = int(r[1])
                out.append(
                    {
                        "from_surface": sid,
                        "to_surface": to_sid,
                        "to_name": self.surface_by_id[to_sid]["name"],
                        "from_pos": (float(r[2]), float(r[3]), float(r[4])),
                        "to_pos": (float(r[5]), float(r[6]), float(r[7])),
                        "horiz_dist_m": float(r[8]),
                        "dh_m": float(r[9]),
                        "energy": float(r[10]),
                        "time_s": float(r[11]),
                    }
                )
        out.sort(key=lambda h: (h["energy"], h["time_s"]))
        return out

    def sight_positions(self, surface_or_xy: Union[int, Tuple[float, float]]) -> List[Dict[str, Any]]:
        sid = surface_or_xy if isinstance(surface_or_xy, int) else self.surface_id_at(surface_or_xy[0], surface_or_xy[1])
        pair = self._sight_by_surf.get(sid)
        if pair is None:
            return []
        rows_f, rows_m = pair
        tags = self.poi_tags
        out: List[Dict[str, Any]] = []
        for r, m in zip(rows_f, rows_m):
            fl = int(r[17])
            ov_m, fi_m = int(m[0]), int(m[1])
            out.append(
                {
                    "surface_id": sid,
                    "edge_pos": (round(float(r[1]), 2), round(float(r[2]), 2), round(float(r[3]), 2)),
                    "out_yaw_rad": round(float(r[4]), 3),
                    "vis_area_aimed_m2": round(float(r[5]), 1),
                    "vis_area_360_m2": round(float(r[6]), 1),
                    "fire_area_aimed_m2": round(float(r[7]), 1),
                    "fire_area_360_m2": round(float(r[8]), 1),
                    "min_fire_horiz_dist_m": round(float(r[9]), 1) if float(r[9]) >= 0.0 else None,
                    "edge_ground_exposure_frac": round(float(r[10]), 4),
                    "setback_pos": (round(float(r[11]), 2), round(float(r[12]), 2), round(float(r[13]), 2)),
                    "setback_dist_m": round(float(r[14]), 2),
                    "move_time_walk_s": round(float(r[15]), 2),
                    "move_time_boost_s": round(float(r[16]), 2),
                    "setback_strict_unseen_200m_hard": bool(fl & 1),
                    "setback_strict_unseen_200m_canopy": bool(fl & 2),
                    "setback_unseen_within_78m": bool(fl & 4),
                    "setback_ground_exposure_frac": round(float(r[18]), 4),
                    "setback_fwd_hidden_dist_m": round(float(r[19]), 1),
                    "overlooks_markers": [tags[i] for i in range(len(tags)) if (ov_m & (1 << i))],
                    "fires_on_markers": [tags[i] for i in range(len(tags)) if (fi_m & (1 << i))],
                }
            )
        return out

    def overwatch_for_marker(self, tag: str) -> List[Dict[str, Any]]:
        return self.overwatch_by_marker.get(tag, [])

    # -----------------------------------------------------------------------
    # The Four Tactical Air Manoeuvre Queries (< 0.05 ms each)
    # -----------------------------------------------------------------------

    def wall_hop(
        self,
        x: float,
        y: float,
        pursuer_pos: Optional[Tuple[float, float]] = None,
        max_energy: float = 100.0,
    ) -> Optional[Dict[str, Any]]:
        """Nearest reachable roof from ground `(x, y)` that breaks LOS to `pursuer_pos`."""
        ci, cj = self._ij4(x, y)
        j0, j1 = max(0, cj - 4), min(self.ny4, cj + 5)
        i0, i1 = max(0, ci - 4), min(self.nx4, ci + 5)
        if not self._wh_any[j0:j1, i0:i1].any():
            return None

        wh_grid = self._wh_grid
        best_score = 1e18
        best_item: Optional[Tuple[Any, ...]] = None

        for nj in range(j0, j1):
            row_wh = wh_grid[nj]
            for ni in range(i0, i1):
                entries = row_wh[ni]
                if entries is None:
                    continue
                lx, ly = entries[0][1], entries[0][2]
                approach_t = 0.0 if (ni == ci and nj == cj) else (math.hypot(lx - x, ly - y) / 11.7)
                for sid, _, _, rx, ry, rz, e_u, climb_t, cov_t, oct_t in entries:
                    if e_u > max_energy:
                        continue
                    tot_t = approach_t + cov_t
                    if pursuer_pos is not None:
                        px_p, py_p = pursuer_pos[0], pursuer_pos[1]
                        ang = math.atan2(py_p - ry, px_p - rx)
                        oct_i = int(((ang + 0.39269908169872414) % 6.283185307179586) // 0.7853981633974483) % 8
                        d_p = math.hypot(px_p - rx, py_p - ry)
                        min_vis_d = oct_t[oct_i]
                        breaks_los = d_p < min_vis_d or d_p > 200.0
                    else:
                        ang = math.atan2(ly - ry, lx - rx)
                        oct_i = int(((ang + 0.39269908169872414) % 6.283185307179586) // 0.7853981633974483) % 8
                        min_vis_d = oct_t[oct_i]
                        breaks_los = min_vis_d >= 25.0
                    score = (0.0 if breaks_los else 100.0) + tot_t + 0.03 * e_u
                    if score < best_score:
                        best_score = score
                        best_item = (sid, lx, ly, rx, ry, rz, e_u, climb_t, tot_t, breaks_los, min_vis_d)

        if best_item is None:
            return None
        sid, lx, ly, rx, ry, rz, e_u, climb_t, tot_t, breaks_los, min_vis_d = best_item
        return {
            "surface_id": sid,
            "surface_name": self.surface_by_id[sid]["name"],
            "launch_xy": (round(lx, 1), round(ly, 1)),
            "roof_cover_pos": (round(rx, 1), round(ry, 1), round(rz, 1)),
            "energy": round(e_u, 2),
            "climb_time_s": round(climb_t, 2),
            "total_time_s": round(tot_t, 2),
            "breaks_los": bool(breaks_los),
            "hidden_up_to_dist_m": round(min_vis_d, 1),
        }

    def drop_attack(
        self,
        x: float,
        y: float,
        z: Optional[float] = None,
        target_tag: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Best roof edge drop attack from current roof `(x, y)` (or globally for `target_tag`)."""
        if target_tag is not None:
            rows = self._da_by_tag.get(target_tag)
        else:
            sid = self.surface_id_at(x, y, z)
            if sid >= 0:
                rows = self._da_by_surf.get(sid)
            else:
                rows = self.drop_attack_arr

        if rows is None or len(rows) == 0:
            return None

        dx = rows[:, 1] - x
        dy = rows[:, 2] - y
        walk_d = np.sqrt(dx * dx + dy * dy)
        cost = (1.0 - rows[:, 10]) * 50.0 + walk_d * (1.0 / 11.7) + rows[:, 7] + 0.02 * rows[:, 8]
        best_i = int(np.argmin(cost))
        r = rows[best_i]
        sid = int(r[0])
        return {
            "surface_id": sid,
            "surface_name": self.surface_by_id[sid]["name"],
            "edge_pos": (round(float(r[1]), 1), round(float(r[2]), 1), round(float(r[3]), 1)),
            "land_pos": (round(float(r[4]), 1), round(float(r[5]), 1), round(float(r[6]), 1)),
            "fall_time_s": round(float(r[7]), 2),
            "walk_to_edge_s": round(float(walk_d[best_i]) / 11.7, 2),
            "dist_to_target_m": round(float(r[8]), 1),
            "target_tag": self.da_tags[int(r[9])],
            "lightning_los_on_landing": bool(r[10] > 0.5),
        }

    def popup_shot(
        self,
        x: float,
        y: float,
        max_energy: float = 100.0,
    ) -> Optional[Dict[str, Any]]:
        """Nearest pop-up laser/gun spot behind a wall within 16 m of `(x, y)`."""
        ci, cj = self._ij4(x, y)
        j0, j1 = max(0, cj - 4), min(self.ny4, cj + 5)
        i0, i1 = max(0, ci - 4), min(self.nx4, ci + 5)
        if not self.popup_valid[j0:j1, i0:i1].any():
            return None

        pop_grid = self._pop_grid
        best_score = 1e18
        best_tup: Optional[Tuple[float, ...]] = None

        for nj in range(j0, j1):
            row_pop = pop_grid[nj]
            for ni in range(i0, i1):
                tup = row_pop[ni]
                if tup is None or tup[4] > max_energy:
                    continue
                walk_d = math.hypot(tup[0] - x, tup[1] - y)
                score = walk_d / 11.7 + tup[6] * 2.0 + 0.02 * tup[4]
                if score < best_score:
                    best_score = score
                    best_tup = tup

        if best_tup is None:
            return None
        return {
            "launch_pos": (round(best_tup[0], 1), round(best_tup[1], 1), round(best_tup[2], 1)),
            "rise_height_m": round(best_tup[3], 2),
            "energy": round(best_tup[4], 2),
            "time_to_apex_s": round(best_tup[5], 2),
            "time_above_roofline_s": round(best_tup[6], 2),
            "target_xy": (round(best_tup[7], 1), round(best_tup[8], 1)),
            "aim_yaw_rad": round(best_tup[9], 3),
            "aim_pitch_rad": round(best_tup[10], 3),
        }

    def last_circle(
        self,
        zone_x: float,
        zone_y: float,
        zone_radius: float = 25.0,
    ) -> Dict[str, Any]:
        """Roof position staying inside a shrinking `zone_radius -> 0` circle around `(zone_x, zone_y)` longest."""
        bx = int((zone_x - self.min_x) / self._lc_bucket_size)
        by = int((zone_y - self.min_y) / self._lc_bucket_size)
        rad_b = max(1, int(math.ceil(zone_radius / self._lc_bucket_size)))

        best_roof_d2 = zone_radius * zone_radius
        best_roof_d = float("inf")
        best_roof_pos = None
        best_sid = -1

        for dy_b in range(-rad_b, rad_b + 1):
            for dx_b in range(-rad_b, rad_b + 1):
                bpair = self._lc_buckets.get((bx + dx_b, by + dy_b))
                if bpair is None:
                    continue
                pts, sids = bpair
                dx_p = pts[:, 0] - zone_x
                dy_p = pts[:, 1] - zone_y
                d2 = dx_p * dx_p + dy_p * dy_p
                k = int(np.argmin(d2))
                min_d2 = float(d2[k])
                if min_d2 <= best_roof_d2:
                    best_roof_d2 = min_d2
                    best_roof_d = math.sqrt(min_d2)
                    best_roof_pos = (round(float(pts[k, 0]), 2), round(float(pts[k, 1]), 2), round(float(pts[k, 2]), 2))
                    best_sid = int(sids[k])

        ci, cj = self._ij4(zone_x, zone_y)
        best_gnd_d = float("inf")
        best_gnd_xy = None
        if self.g4_standable[cj, ci]:
            gx_c = self.min_x + (ci + 0.5) * self.res_4m
            gy_c = self.min_y + (cj + 0.5) * self.res_4m
            best_gnd_d = math.hypot(gx_c - zone_x, gy_c - zone_y)
            if best_gnd_d <= zone_radius:
                best_gnd_xy = (round(gx_c, 2), round(gy_c, 2))
        else:
            best_gd2 = zone_radius * zone_radius
            for dy_b in range(-rad_b, rad_b + 1):
                for dx_b in range(-rad_b, rad_b + 1):
                    gpts = self._lc_gnd_buckets.get((bx + dx_b, by + dy_b))
                    if gpts is None:
                        continue
                    dx_g = gpts[:, 0] - zone_x
                    dy_g = gpts[:, 1] - zone_y
                    gd2 = dx_g * dx_g + dy_g * dy_g
                    gk = int(np.argmin(gd2))
                    min_gd2 = float(gd2[gk])
                    if min_gd2 <= best_gd2:
                        best_gd2 = min_gd2
                        best_gnd_d = math.sqrt(min_gd2)
                        best_gnd_xy = (round(float(gpts[gk, 0]), 2), round(float(gpts[gk, 1]), 2))

        def stay_s(d: float) -> float:
            return 0.0 if d > zone_radius else (15.0 + 25.0 * (1.0 - d / max(1e-6, zone_radius)))

        t_roof = stay_s(best_roof_d)
        t_gnd = stay_s(best_gnd_d)
        return {
            "zone_center": (round(zone_x, 2), round(zone_y, 2)),
            "best_roof_pos": best_roof_pos,
            "surface_id": best_sid,
            "surface_name": self.surface_by_id[best_sid]["name"] if best_sid >= 0 else None,
            "min_climb_energy": self.surface_by_id[best_sid]["min_climb_energy"] if best_sid >= 0 else None,
            "roof_dist_m": round(best_roof_d, 2) if best_roof_pos else None,
            "roof_stay_s": round(t_roof, 2),
            "best_ground_xy": best_gnd_xy,
            "ground_dist_m": round(best_gnd_d, 2) if best_gnd_xy else None,
            "ground_stay_s": round(t_gnd, 2),
            "roof_advantage_s": round(t_roof - t_gnd, 2),
        }


__all__ = [
    "AirMap",
    "ClimbResult",
    "DEFAULT_MECH_CONFIG",
    "FallResult",
    "air_lateral_distance",
    "climb_cost",
    "fall_metrics",
    "max_ledge_height",
    "move_time_ground",
]
