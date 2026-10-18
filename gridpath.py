"""Indoor path refinement on the real collision geometry (vgeom.py).

The navmesh fuses ledges up to its climb height and furniture into the floor and keeps its edges up to a voxel off the
real walls; inside buildings that is where the character wedges (Anvilmar: a 0.84 yd riser round the sunken table
floor that the mesh calls flat - 60+ logged stucks on one line, x = -6064.3). Here the indoor stretches of a planned
path are re-planned on a fine grid built from the triangles the client collides with:

  floors   every walkable surface over a cell (several levels: floor, table top, a stair above), within a band of the
           coarse path's height;
  links    8-neighbour cells whose floors differ by at most STEP_MAX (stairs yes, a sheer riser no);
  blocked  a floor with any triangle in the body band above it (STEP_MAX .. BODY_HI): table under its top, the low
           side of a riser, a rail, a wall;
  clear    distance from each floor node to the nearest blocked node or floor edge (Dijkstra over the links).

A* runs on the nodes with clearance >= AGENT_R and pays for clearance under CLEAR_WANT, then the result is pulled
straight wherever a direct line keeps the clearance of the stretch it replaces. Outdoors (no building) nothing
changes.
"""
from __future__ import annotations

import heapq
import math
import os
import time
from collections import OrderedDict

import numpy as np

import terrain
import vgeom
import vmap

CELL = 0.4          # yd grid step
STEP_MAX = 0.6      # yd: the highest rise between neighbour cells (Anvilmar's stair steps 0.44 + 0.33; its riser 0.84)
BODY_HI = 1.7       # yd above the floor a body occupies
PROP_MAX = 3.0      # yd: an M2 smaller than this across is an obstacle, never a floor
LOW_ROOF = 3.0      # yd: a model floor closer than this above the terrain makes the terrain there solid
AGENT_R = 0.8       # yd: no node closer than this to an obstacle or floor edge (0.55 wedged on a table edge)
END_R = 0.4         # yd: ... except within END_ZONE of the stretch's start / goal: the walker (or the NPC) already
GOAL_SLACK = 4.0    # yd: a stretch whose end node is unreachable (an NPC behind its counter) ends on the reachable floor this near it
SQUEEZE_R = 0.2     # yd: the last tier - half a cell, the grid's own resolution; the gap from Jarven's corner to the
#                     Thunderbrew cellar stairs reads 0.2 on the 0.4 yd lattice and the bot walks it (a dwarf is ~0.35 yd
#                     round), while the coarse stretch it fell back to cut through the stairwell (rxp5 01:46:00)
END_ZONE = 3.0      # stands there, in a nook between props (Gilded Rose start: 9 stucks, no grid path at 0.8)
CLEAR_WANT = 1.6    # yd: clearance below this costs extra (the follower strays ~0.5 yd in a hairpin)
CLEAR_W = 6.0       # cost per yd of path per yd of missing clearance
BAND = 7.0          # yd round the coarse path the grid covers
BANDS = (7.0, 14.0, 21.0)   # tried in turn when no path: the way round (a ramp's top end) can lie wide
HINT_R = 3.0        # yd: guide points this close to a cell set its floor height band (Grid.hlo / hhi)
START_DZ = 1.5      # yd: a fallback start floor's height off ours (a counter top 1.1 yd up is one)
Z_BELOW, Z_ABOVE = 4.0, 4.0   # floors searched round the coarse path's height (its z can be 3-4 yd off at a door)
SAMPLE = 0.2        # yd: triangle sampling for the body test
MAX_CELLS = 120_000
LAYERS = 6


NEAR = 2.0          # yd: a path point this close to collision geometry at body height is re-planned
# the client's walkable limit, on the terrain triangle stood on (terrain.grade): 50 deg, the mesh's walkableSlopeAngle.
# 5060 uphill steps walked on the Spirit Stone hill and the roads: p99 1.18, max 1.22; wedged on 1.25 (-6285,179).
# (A smoothed 0.75 yd finite-difference grade read up to 1.36 on that hill: it mixes triangles, hence the old 'off'.)
SLOPE_MAX = math.tan(math.radians(50.0))
FLOOR_NZ = math.cos(math.radians(50.0))   # a model triangle is floor up to the same 50 deg (0.6 = 53 deg let the
#                                           Gnomeregan-side ice cave's rock count as floor: rxp9 02:49:46)
STEEP_PROBE = 0.8   # yd: the triangles this far beside a path point count too (the walker strays)
SLOPE_SOFT = 0.9    # terrain triangle grade above which a step costs extra
STEEP_UP = 1.0      # yd climbed on terrain past SLOPE_FLAG that a stretch reports to the navmesh
SLOPE_FLAG = 1.0    # a path point with terrain this steep near it gets the grid (and the soft cost)
SLOPE_W = 8.0       # ... (1 + SLOPE_W * excess) times its length
SLOPE_HARD = 99.0   # off (terrain grade a step may not climb)
RUN_MAX = 60        # points (yd) per grid: longer stretches are split
OCC_XY, OCC_Z = 0.5, 0.25


def _okey(kx, ky, kz):
    return ((kx.astype(np.int64) + 200000) * 400000 + (ky.astype(np.int64) + 200000)) * 20000 \
        + (kz.astype(np.int64) + 10000)


def collision_runs(pts, pad=3):
    """[(i, j)]: stretches of the densified path pts ((n, 3), 1 yd apart) that need the collision grid: a point on
    model geometry (a vmap floor within 1 yd of the path's height - inside a building, on a bridge) or within NEAR yd
    of any model's triangles at body height (walls, door frames, tents, fences, planks on a trail). Gaps under
    2 * pad points are bridged; runs longer than RUN_MAX are split."""
    n = len(pts)
    flags = np.zeros(n, bool)
    for c0 in range(0, n, 40):
        q = np.array(pts[c0:c0 + 40], float)
        # the ground, not the mesh polygon's height: this client's mesh floats 0.4-3.3 yd over the grass at the
        # Amberstill Ranch fence (1.4 yd tall), so the body band measured from the mesh cleared the fence and the
        # stretch was never refined - the route crossed it (rxp8 02:39:18). Terrain 0.3-4 yd under the path: use it
        tz = terrain.heights(q[:, 0], q[:, 1])
        dz_ = q[:, 2] - tz
        low = ~np.isnan(tz) & (dz_ > 0.3) & (dz_ < 4.0)
        q[low, 2] = tz[low]
        x0, y0 = q[:, 0].min() - NEAR - 1, q[:, 1].min() - NEAR - 1
        x1, y1 = q[:, 0].max() + NEAR + 1, q[:, 1].max() + NEAR + 1
        zlo, zhi = q[:, 2].min() - 1.5, q[:, 2].max() + BODY_HI + 0.5
        sps = [sp for sp in vmap.spawns_near((x0 + x1) / 2, (y0 + y1) / 2, math.hypot(x1 - x0, y1 - y0) / 2)
               if sp.hi[2] >= zlo and sp.lo[2] <= zhi and sp.lo[0] <= x1 and sp.hi[0] >= x0
               and sp.lo[1] <= y1 and sp.hi[1] >= y0]
        G_ = _gobj_body(x0, y0, x1, y1)
        if not sps and not len(G_):
            continue
        S = np.concatenate([_in_box(_spawn_body, sp, x0, y0, x1, y1) for sp in sps] + [G_])
        S = S[(S[:, 0] >= x0) & (S[:, 0] <= x1) & (S[:, 1] >= y0) & (S[:, 1] <= y1)
              & (S[:, 2] >= zlo) & (S[:, 2] <= zhi)]
        F = np.concatenate([_in_box(_spawn_floor, sp, x0, y0, x1, y1) for sp in sps] + [np.zeros((0, 3))])
        if len(F):
            fx, fy = (F[:, 0] + 0.5) * CELL, (F[:, 1] + 0.5) * CELL
            keep = (fx >= x0) & (fx <= x1) & (fy >= y0) & (fy <= y1)
            F, fx, fy = F[keep], fx[keep], fy[keep]
        # occupancy hash: (0.5 yd cell, 0.25 yd height slot) of every body point, tested for all points at once
        hit = np.zeros(len(q), bool)
        if len(S):
            sk = _okey(np.floor(S[:, 0] / OCC_XY), np.floor(S[:, 1] / OCC_XY), np.floor(S[:, 2] / OCC_Z))
            sk = np.unique(sk)
            r = int(math.ceil(NEAR / OCC_XY))
            ox, oy = np.meshgrid(np.arange(-r, r + 1), np.arange(-r, r + 1), indexing="ij")
            m = np.hypot(ox, oy) * OCC_XY <= NEAR
            ox, oy = ox[m], oy[m]
            z0 = np.ceil((q[:, 2] + 0.3) / OCC_Z).astype(np.int64)
            nz = int(math.floor((BODY_HI - 0.3) / OCC_Z))
            dz = np.arange(nz)
            kx = np.floor(q[:, 0] / OCC_XY)[:, None, None] + ox[None, :, None]
            ky = np.floor(q[:, 1] / OCC_XY)[:, None, None] + oy[None, :, None]
            kz = (z0[:, None, None] + dz[None, None, :]).astype(float)
            qk = _okey(np.broadcast_to(kx, kz.shape[:1] + (len(ox), nz)), np.broadcast_to(ky, kz.shape[:1] + (len(ox), nz)),
                       np.broadcast_to(kz, kz.shape[:1] + (len(ox), nz)))
            hit = np.isin(qk, sk).reshape(len(q), -1).any(axis=1)
        if len(F):
            for k, p in enumerate(q):
                if not hit[k]:
                    m = (np.abs(fx - p[0]) <= CELL) & (np.abs(fy - p[1]) <= CELL) & (np.abs(F[:, 2] - p[2]) <= 1.0)
                    hit[k] = bool(m.any())
                if not hit[k]:
                    # a model floor 0.6-3 yd over the ground: right overhead, the inside of a solid base (the grid's
                    # LOW_ROOF rule); near a point where the mesh floats over the ground, a ledge the mesh smoothed
                    # into a ramp (both at the Algaz Station tower base, 2.5 yd, rxp15/16 #415)
                    up = F[:, 2] - p[2]
                    r_ = NEAR if low[k] else CELL
                    m = (np.abs(fx - p[0]) <= r_) & (np.abs(fy - p[1]) <= r_) & (up > STEP_MAX) & (up < LOW_ROOF)
                    hit[k] = bool(m.any())
        # a terrain face past the client's walkable slope on or beside the path: the mesh's big polygons average it away
        g = np.zeros(len(q))
        for ox, oy in ((0, 0), (STEEP_PROBE, 0), (-STEEP_PROBE, 0), (0, STEEP_PROBE), (0, -STEEP_PROBE)):
            g = np.maximum(g, np.nan_to_num(terrain.grade(q[:, 0] + ox, q[:, 1] + oy), nan=0.0))
        hit |= g > SLOPE_FLAG
        flags[c0:c0 + len(q)] = hit
    wins, i = [], 0
    while i < n:
        if flags[i]:
            j = i
            while j + 1 < n and (flags[j + 1] or flags[j + 2:j + 1 + 2 * pad].any()):
                j += 1
            i0, j0 = max(0, i - pad), min(n - 1, j + pad)
            # padded windows that touch are one stretch: refined apart, the coarse hop between them went unchecked,
            # and each grid's start skipped its first blocked points, so the joint crossed a wall of the
            # Dun Morogh south gate tower (-4795,-2679) and the bot wedged 9 times (rxp14 04:13-04:14, #415)
            if wins and i0 <= wins[-1][1] + 1:
                wins[-1] = (wins[-1][0], j0)
            else:
                wins.append((i0, j0))
            i = j + 1
        else:
            i += 1
    runs = []
    for i0, j0 in wins:
        while j0 - i0 > RUN_MAX:
            runs.append((i0, i0 + RUN_MAX))
            i0 += RUN_MAX
        runs.append((i0, j0))
    return runs


indoor_runs = collision_runs


class Grid:
    def __init__(self, guide, band=BAND):
        """guide: (n, 3) points of the coarse path stretch to cover; band: yd round it."""
        g = np.asarray(guide, float)
        BAND = band                                       # noqa: N806 - the module default, widened on a retry
        # on the global CELL lattice, so the per-model caches (_spawn_floor / _spawn_body) line up with every grid
        self.i_off = int(math.floor((g[:, 0].min() - BAND) / CELL))
        self.j_off = int(math.floor((g[:, 1].min() - BAND) / CELL))
        self.x0, self.y0 = self.i_off * CELL, self.j_off * CELL
        x1, y1 = g[:, 0].max() + BAND, g[:, 1].max() + BAND
        self.nx = int((x1 - self.x0) / CELL) + 1
        self.ny = int((y1 - self.y0) / CELL) + 1
        if self.nx * self.ny > MAX_CELLS:
            raise ValueError(f"grid too large {self.nx}x{self.ny}")
        cx = self.x0 + (np.arange(self.nx) + 0.5) * CELL
        cy = self.y0 + (np.arange(self.ny) + 0.5) * CELL
        X, Y = np.meshgrid(cx, cy, indexing="ij")
        # height hint per cell: the nearest guide point's z; cells farther than BAND from the guide are left out
        dense = _densify(g, 1.0)
        best = np.full(X.shape, np.inf)
        hint = np.zeros(X.shape)
        for p in dense:
            d = (X - p[0]) ** 2 + (Y - p[1]) ** 2
            m = d < best
            best[m] = d[m]
            hint[m] = p[2]
        self.use = best <= BAND * BAND
        self.hint = hint
        # the height band per cell spans every guide point within HINT_R yd, not just the nearest one: on a spiral
        # stair the nearest point to a cell under the landing is on the flight above it (7 yd up), and the cellar
        # floor below fell out of the band - a closed gap at the Thunderbrew cellar stairs (rxp5 01:46:00)
        lo, hi = hint.copy(), hint.copy()
        for p in dense:
            m = (X - p[0]) ** 2 + (Y - p[1]) ** 2 <= HINT_R * HINT_R
            lo[m] = np.minimum(lo[m], p[2])
            hi[m] = np.maximum(hi[m], p[2])
        self.hlo, self.hhi = lo, hi
        zlo, zhi = float(g[:, 2].min()) - Z_BELOW - 1, float(g[:, 2].max()) + Z_ABOVE + BODY_HI + 1
        self.box = (self.x0, self.y0, x1, y1, zlo, zhi)
        self.spawns = [sp for sp in vmap.spawns_near((self.x0 + x1) / 2, (self.y0 + y1) / 2,
                                                     math.hypot(x1 - self.x0, y1 - self.y0) / 2 + 2)
                       if sp.hi[2] >= zlo and sp.lo[2] <= zhi and sp.lo[0] <= x1 and sp.hi[0] >= self.x0
                       and sp.lo[1] <= y1 and sp.hi[1] >= self.y0]
        self._floors()
        self._block()
        self._links()
        self._clearance()

    # ---- build
    def _floors(self):
        bx = self.box
        parts = [_in_box(_spawn_floor, sp, *bx[:4]) for sp in self.spawns]
        mparts = list(parts)                               # model floors only (the terrain is added below)
        # the ground: terrain height at every cell centre (nan in a terrain hole, e.g. under a building)
        ii, jj = np.nonzero(self.use)
        tz = terrain.heights(self.x0 + (ii + 0.5) * CELL, self.y0 + (jj + 0.5) * CELL)
        ok = ~np.isnan(tz)
        parts.append(np.stack([ii[ok] + self.i_off, jj[ok] + self.j_off, tz[ok]], axis=1).astype(float))
        fp = np.concatenate(parts) if parts else np.zeros((0, 3))
        # holes in the collision floors: a used cell with no model or terrain floor near the path's height takes the
        # navmesh polygon's height there (Stormwind's Trade District shop door: a 5 yd gap in the city model's floor
        # that the server mesh walks across - no grid path, the coarse stretch kept, 7 stucks on the shop's steps)
        mf = self._mesh_floors(fp)
        if len(mf):
            fp = np.concatenate([fp, mf])
        i = fp[:, 0].astype(np.int64) - self.i_off
        j = fp[:, 1].astype(np.int64) - self.j_off
        ok = (i >= 0) & (i < self.nx) & (j >= 0) & (j < self.ny)
        i, j, z = i[ok], j[ok], fp[ok, 2]
        ok = self.use[i, j] & (z >= self.hlo[i, j] - Z_BELOW) & (z <= self.hhi[i, j] + Z_ABOVE)
        cell, z = i[ok] * self.ny + j[ok], z[ok]
        lz = np.full((self.nx * self.ny, LAYERS), np.nan)
        if len(z):
            o = np.lexsort((z, cell))
            cell, z = cell[o], z[o]
            start = np.ones(len(z), bool)
            # surfaces under a step apart in one cell are one floor, the top one (the floor running on under a stair
            # tread is no place to stand, and as a floor edge it took the tread's clearance away)
            start[1:] = (cell[1:] != cell[:-1]) | (z[1:] - z[:-1] > STEP_MAX)
            idx = np.nonzero(start)[0]
            cc, cz = cell[idx], np.maximum.reduceat(z, idx)     # one floor per cluster: its top
            first = np.ones(len(cc), bool)
            first[1:] = cc[1:] != cc[:-1]
            fidx = np.nonzero(first)[0]
            grp = np.cumsum(first) - 1
            pos = np.arange(len(cc)) - fidx[grp]
            cnt = np.diff(np.append(fidx, len(cc)))[grp]
            k = pos - np.maximum(0, cnt - LAYERS)               # the top LAYERS floors of each cell
            keep = k >= 0
            lz[cc[keep], k[keep]] = cz[keep]
        self.lz = lz                                       # (cells, LAYERS) floor heights, nan = none
        # model[cell, layer]: a model surface (a floor, a stair, a bridge) makes that floor; else it is the terrain
        model = np.zeros(lz.shape, bool)
        mp = np.concatenate(mparts) if mparts else np.zeros((0, 3))
        mi = mp[:, 0].astype(np.int64) - self.i_off
        mj = mp[:, 1].astype(np.int64) - self.j_off
        ok = (mi >= 0) & (mi < self.nx) & (mj >= 0) & (mj < self.ny)
        mc, mz = mi[ok] * self.ny + mj[ok], mp[ok, 2]
        for k in range(LAYERS):
            m = np.abs(mz - lz[mc, k]) <= STEP_MAX
            np.logical_or.at(model[:, k], mc[m], True)
        self.model = model

    def _mesh_floors(self, fp):
        """(n, 3) [i, j, z] global lattice rows: navmesh heights for the used cells without a floor in the path's
        height band (MESH: the NavMesh navmesh.find_path set; none: nothing)."""
        nm = MESH
        if nm is None:
            return np.zeros((0, 3))
        i = fp[:, 0].astype(np.int64) - self.i_off
        j = fp[:, 1].astype(np.int64) - self.j_off
        ok = (i >= 0) & (i < self.nx) & (j >= 0) & (j < self.ny)
        i, j, z = i[ok], j[ok], fp[ok, 2]
        good = (z >= self.hlo[i, j] - Z_BELOW) & (z <= self.hhi[i, j] + Z_ABOVE)
        has = np.zeros((self.nx, self.ny), bool)
        has[i[good], j[good]] = True
        ei, ej = np.nonzero(self.use & ~has)
        if not len(ei):
            return np.zeros((0, 3))
        try:
            import navmesh
        except Exception:                                 # noqa: BLE001
            return np.zeros((0, 3))
        wx = self.x0 + (ei + 0.5) * CELL
        wy = self.y0 + (ej + 0.5) * CELL
        hz = self.hint[ei, ej]
        # recast: rx = wow y, rz = wow x
        rx, rz = wy, wx
        out_z = np.full(len(ei), np.nan)
        seen = set()
        for tk in {(math.floor((float(a) - nm.orig[0]) / nm.tile_w), math.floor((float(b) - nm.orig[2]) / nm.tile_h))
                   for a, b in ((rx.min(), rz.min()), (rx.min(), rz.max()), (rx.max(), rz.min()), (rx.max(), rz.max()))}:
            tl = nm.tiles.get(tk)
            if tl is None or tk in seen:
                continue
            seen.add(tk)
            m = ((tl.pmax[:, 0] >= rx.min()) & (tl.pmin[:, 0] <= rx.max()) &
                 (tl.pmax[:, 2] >= rz.min()) & (tl.pmin[:, 2] <= rz.max()) &
                 (tl.pmax[:, 1] >= hz.min() - Z_BELOW) & (tl.pmin[:, 1] <= hz.max() + Z_ABOVE))
            for pi in np.nonzero(m)[0]:
                pi = int(pi)
                if not tl.usable(pi) or nm._island((tl.key, pi)):
                    continue
                vs = tl.verts[tl.pverts[pi, :tl.pcount[pi]]].astype(float)
                sel = np.nonzero((rx >= vs[:, 0].min()) & (rx <= vs[:, 0].max()) &
                                 (rz >= vs[:, 2].min()) & (rz <= vs[:, 2].max()))[0]
                if not len(sel):
                    continue
                # point in the convex polygon (either winding): all cross products one sign
                px, pz = rx[sel], rz[sel]
                sgn = None
                inside = np.ones(len(sel), bool)
                for k in range(len(vs)):
                    a, b = vs[k], vs[(k + 1) % len(vs)]
                    cr = (b[0] - a[0]) * (pz - a[2]) - (b[2] - a[2]) * (px - a[0])
                    s_ = np.sign(cr)
                    sgn = s_ if sgn is None else np.where(sgn == 0, s_, sgn)
                    inside &= (s_ == 0) | (s_ == sgn)
                if not inside.any():
                    continue
                sel = sel[inside]
                hh = np.array([navmesh.poly_height(float(rx[q]), float(rz[q]), vs) for q in sel])
                fit = (hh >= hz[sel] - Z_BELOW) & (hh <= hz[sel] + Z_ABOVE)
                sel, hh = sel[fit], hh[fit]
                better = np.isnan(out_z[sel]) | (np.abs(hh - hz[sel]) < np.abs(out_z[sel] - hz[sel]))
                out_z[sel[better]] = hh[better]
        k = ~np.isnan(out_z)
        return np.stack([ei[k] + self.i_off, ej[k] + self.j_off, out_z[k]], axis=1).astype(float)

    def _block(self):
        """blocked[cell, layer]: a triangle point in the body band over that floor."""
        x0, y0, x1, y1, zlo, zhi = self.box
        parts = [_in_box(_spawn_body, sp, x0, y0, x1, y1) for sp in self.spawns] + [_gobj_body(x0, y0, x1, y1)]
        pts = np.concatenate(parts) if parts else np.zeros((0, 3))
        i = np.floor(pts[:, 0] / CELL).astype(np.int64) - self.i_off
        j = np.floor(pts[:, 1] / CELL).astype(np.int64) - self.j_off
        ok = (i >= 0) & (i < self.nx) & (j >= 0) & (j < self.ny) & (pts[:, 2] >= zlo) & (pts[:, 2] <= zhi)
        cell = i[ok] * self.ny + j[ok]
        z = pts[ok, 2]
        blocked = np.zeros(self.lz.shape, bool)
        for k in range(LAYERS):
            f = self.lz[cell, k]
            hit = (z > f + STEP_MAX) & (z < f + BODY_HI)
            np.logical_or.at(blocked[:, k], cell[hit], True)
        # terrain under a model floor less than LOW_ROOF up is inside a solid block, not headroom: the vmaps hold a
        # platform's top and sides but the terrain runs on beneath, so the grid walked under the Dun Morogh south
        # gate tower's base (top 2.5 yd up) through a gap in its side walls (rxp15 04:19, #415, (-4792,-2666))
        model = getattr(self, "model", None)
        if model is not None:
            lz = self.lz
            for k in range(LAYERS):
                terr = ~np.isnan(lz[:, k]) & ~model[:, k]
                for m in range(LAYERS):
                    up = lz[:, m] - lz[:, k]
                    np.logical_or(blocked[:, k], terr & model[:, m] & (up > STEP_MAX) & (up < LOW_ROOF),
                                  out=blocked[:, k])
        self.blocked = blocked & ~np.isnan(self.lz)

    def _links(self):
        """Node ids and the 8-neighbour links between floors within STEP_MAX."""
        has = ~np.isnan(self.lz)
        self.nid = np.full(self.lz.shape, -1, int)
        self.nid[has] = np.arange(int(has.sum()))
        cells, layers = np.nonzero(has)
        self.ncell, self.nlay = cells, layers
        self.nz = self.lz[cells, layers]
        self.terr = ~self.model[cells, layers]             # node stands on terrain (the slope rules apply)
        # grade of the terrain triangle under each terrain node: no step UP onto one past SLOPE_MAX (down is a slide)
        self.tg = np.zeros(len(cells))
        if self.terr.any():
            tx = self.x0 + (cells[self.terr] // self.ny + 0.5) * CELL
            ty = self.y0 + (cells[self.terr] % self.ny + 0.5) * CELL
            self.tg[self.terr] = np.nan_to_num(terrain.grade(tx, ty), nan=0.0)
        n = len(cells)
        self.adj = [[] for _ in range(n)]
        self.edge = np.zeros(n, bool)                       # a floor edge: some neighbour direction has no link
        ny = self.ny
        for d_i, d_j in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
            w = CELL * (math.sqrt(2) if d_i and d_j else 1.0)
            ci, cj = cells // ny, cells % ny
            ti, tj = ci + d_i, cj + d_j
            inb = (ti >= 0) & (ti < self.nx) & (tj >= 0) & (tj < ny)
            tc = np.where(inb, ti * ny + tj, 0)
            dz = np.abs(self.lz[tc] - self.nz[:, None])         # (n, LAYERS)
            dz = np.where(inb[:, None], dz, np.inf)
            dz = np.nan_to_num(dz, nan=np.inf)
            # every floor of the neighbour within a step: a stair-edge cell holds both the tread and the floor below
            # it, and a link to only the nearer one lost the way up
            gk = dz <= STEP_MAX                                  # (n, LAYERS)
            if d_i and d_j:                                  # a diagonal needs both side cells linked too
                for a_i, a_j in ((d_i, 0), (0, d_j)):
                    si, sj = ci + a_i, cj + a_j
                    sin = (si >= 0) & (si < self.nx) & (sj >= 0) & (sj < ny)
                    sc = np.where(sin, si * ny + sj, 0)
                    sdz = np.nan_to_num(np.abs(self.lz[sc] - self.nz[:, None]), nan=np.inf)
                    gk &= (sin & (sdz.min(axis=1) <= STEP_MAX))[:, None]
            if not (d_i and d_j):
                self.edge |= ~gk.any(axis=1)
            src, kk = np.nonzero(gk)
            dst = self.nid[tc[src], kk]
            for s, t in zip(src.tolist(), dst.tolist()):
                self.adj[s].append((t, w))

    def _clearance(self):
        n = len(self.nz)
        blk = self.blocked[self.ncell, self.nlay]
        clr = np.full(n, np.inf)
        h = []
        for v in np.nonzero(blk)[0].tolist():
            clr[v] = 0.0
            h.append((0.0, v))
        # a terrain face past SLOPE_MAX is a wall to keep off like a floor's edge: the follower strays ~1 yd, and off
        # a 1 yd ramp between 52 deg faces it wedged (the Tower of Althalaxx hill, #1168-#1169, rxp36 11:53-11:54)
        for v in np.nonzero((self.edge | (self.terr & (self.tg > SLOPE_MAX))) & ~blk)[0].tolist():
            clr[v] = CELL * 0.5
            h.append((CELL * 0.5, v))
        heapq.heapify(h)
        while h:
            d, v = heapq.heappop(h)
            if d > clr[v] or d > CLEAR_WANT + 1:
                continue
            for t, w in self.adj[v]:
                nd = d + w
                if nd < clr[t]:
                    clr[t] = nd
                    heapq.heappush(h, (nd, t))
        self.clr = np.minimum(clr, CLEAR_WANT + 1)
        self.blk = blk

    # ---- queries
    def set_ends(self, ends):
        """[(x, y, z)]: nodes within END_ZONE yd of one need only END_R clearance (for the next searches)."""
        self._need = None
        if not ends:
            return
        xs = self.x0 + (self.ncell // self.ny + 0.5) * CELL
        ys = self.y0 + (self.ncell % self.ny + 0.5) * CELL
        need = np.full(len(self.nz), AGENT_R)
        for x, y, z in ends:
            m = (np.hypot(xs - x, ys - y) <= END_ZONE) & (np.abs(self.nz - z) <= Z_ABOVE)
            need[m] = END_R
        self._need = need

    def nodes_near(self, p, r):
        """Usable nodes within r yd across and Z_ABOVE in height of p, best (node_at's score) first, one per floor."""
        i0, j0 = int((p[0] - self.x0) / CELL), int((p[1] - self.y0) / CELL)
        k = int(r / CELL) + 1
        cand = []
        for i in range(i0 - k, i0 + k + 1):
            for j in range(j0 - k, j0 + k + 1):
                if not (0 <= i < self.nx and 0 <= j < self.ny):
                    continue
                for L in range(LAYERS):
                    v = self.nid[i * self.ny + j, L]
                    if v < 0 or self.clr[v] < self.need_of(v) or abs(self.nz[v] - p[2]) > Z_ABOVE:
                        continue
                    d = math.hypot(self.x0 + (i + 0.5) * CELL - p[0], self.y0 + (j + 0.5) * CELL - p[1])
                    if d <= r:
                        cand.append((d + 0.5 * abs(self.nz[v] - p[2]) - 0.1 * self.clr[v], round(float(self.nz[v]) / STEP_MAX), v))
        cand.sort()
        out, seen = [], set()
        for _sc, fl, v in cand:
            if fl not in seen:
                seen.add(fl)
                out.append(v)
        return out

    def reachable_near(self, s, p, r):
        """The node reachable from s (each node's own clearance need) nearest p = (x, y, z): within r yd across and
        Z_ABOVE in height, scored like node_at; None if none."""
        if s is None:
            return None
        seen = {s}
        st = [s]
        while st:
            v = st.pop()
            for t, _w in self.adj[v]:
                if t not in seen and self.clr[t] >= self.need_of(t):
                    seen.add(t)
                    st.append(t)
        best, bv = None, 1e9
        for v in seen:
            x, y, z = self.xyz(v)
            d = math.hypot(x - p[0], y - p[1])
            if d <= r and abs(z - p[2]) <= Z_ABOVE:
                sc = d + 0.5 * abs(z - p[2])
                if sc < bv:
                    best, bv = v, sc
        return best

    def set_all(self, r):
        self._need = np.full(len(self.nz), float(r))

    def need_of(self, v) -> float:
        nd = getattr(self, "_need", None)
        return AGENT_R if nd is None else float(nd[v])

    def node_at(self, x, y, z, r=1.6):
        """The usable node nearest (x, y, z): within r yd and 1.5 yd of height, the most clearance on ties."""
        best, bv = None, 1e9
        i0, j0 = int((x - self.x0) / CELL), int((y - self.y0) / CELL)
        k = int(r / CELL) + 1
        for i in range(i0 - k, i0 + k + 1):
            for j in range(j0 - k, j0 + k + 1):
                if not (0 <= i < self.nx and 0 <= j < self.ny):
                    continue
                for L in range(LAYERS):
                    v = self.nid[i * self.ny + j, L]
                    if v < 0 or self.clr[v] < self.need_of(v) or abs(self.nz[v] - z) > Z_ABOVE:
                        continue
                    cx, cy = self.x0 + (i + 0.5) * CELL, self.y0 + (j + 0.5) * CELL
                    d = math.hypot(cx - x, cy - y)
                    sc = d + 0.5 * abs(self.nz[v] - z) - 0.1 * self.clr[v]
                    if d <= r and sc < bv:
                        best, bv = v, sc
        return best

    def xyz(self, v):
        c = self.ncell[v]
        return (self.x0 + (c // self.ny + 0.5) * CELL, self.y0 + (c % self.ny + 0.5) * CELL, float(self.nz[v]))

    def astar(self, s, g):
        gx, gy, _ = self.xyz(g)
        cost = {s: 0.0}
        came = {}
        h = [(0.0, s)]
        while h:
            f, v = heapq.heappop(h)
            if v == g:
                out = [v]
                while v in came:
                    v = came[v]
                    out.append(v)
                return out[::-1]
            cv = cost[v]
            for t, w in self.adj[v]:
                c = self.clr[t]
                if c < self.need_of(t):
                    continue
                if self.tg[t] > SLOPE_MAX and self.nz[t] > self.nz[v] + 0.02:
                    continue                                 # up onto a face the client will not climb
                fac = 1.0 + CLEAR_W * max(0.0, CLEAR_WANT - c)
                if self.terr[v] and self.terr[t]:
                    grade = abs(float(self.nz[t] - self.nz[v])) / w
                    if grade > SLOPE_HARD:
                        continue
                if self.tg[t] > SLOPE_SOFT:
                    # the triangle stood on (not the step's finite difference, which mixes triangles): a climb near
                    # the limit leaves the walker no room to stray (Loch Modan ridge, 1.06-1.14 route, wedged 3x on
                    # the 1.37 faces beside it, rxp22 #524)
                    fac += SLOPE_W * (float(self.tg[t]) - SLOPE_SOFT)
                nc = cv + w * fac
                if nc < cost.get(t, 1e18):
                    cost[t] = nc
                    came[t] = v
                    x, y, _ = self.xyz(t)
                    heapq.heappush(h, (nc + math.hypot(gx - x, gy - y), t))
        return None

    def line_ok(self, a, b, need):
        """Walk the straight line between nodes a and b cell by cell, staying on linked floors with clearance."""
        ax, ay, az = self.xyz(a)
        bx, by, _ = self.xyz(b)
        L = math.hypot(bx - ax, by - ay)
        n = max(1, int(L / (CELL * 0.5)))
        cur = a
        for s in range(1, n + 1):
            f = s / n
            x, y = ax + (bx - ax) * f, ay + (by - ay) * f
            i, j = int((x - self.x0) / CELL), int((y - self.y0) / CELL)
            if not (0 <= i < self.nx and 0 <= j < self.ny):
                return False
            cell = i * self.ny + j
            if cell == self.ncell[cur]:
                continue
            nxt = None
            cz = self.nz[cur]
            for t, _w in self.adj[cur]:              # the target itself, else the floor nearest our height
                if self.ncell[t] == cell and (nxt is None or t == b or
                                              (nxt != b and abs(self.nz[t] - cz) < abs(self.nz[nxt] - cz))):
                    nxt = t
            if nxt is None:                              # a diagonal step the walk skipped: through a side cell
                for t, _w in self.adj[cur]:
                    for t2, _w2 in self.adj[t]:
                        if self.ncell[t2] == cell:
                            nxt = t2
                            break
                    if nxt is not None:
                        if self.clr[t] < need:
                            return False
                        break
            if nxt is None or self.clr[nxt] < need:
                return False
            if self.tg[nxt] > SLOPE_MAX and self.nz[nxt] > self.nz[cur] + 0.02:
                return False
            # a straightened line must not climb what A* paid to avoid: the pull cut the Loch Modan ridge path back
            # across its 1.14 faces (rxp25 #646 wedged on 1.22 beside it, the same spot as #524 before the soft cost)
            if self.tg[nxt] > SLOPE_SOFT and self.nz[nxt] > self.nz[cur] + 0.02 and self.tg[nxt] > self.tg[cur]:
                return False
            if self.terr[cur] and self.terr[nxt]:
                cx_, cy_, _ = self.xyz(cur)
                nx_, ny_, _ = self.xyz(nxt)
                run = math.hypot(nx_ - cx_, ny_ - cy_)
                if run > 0 and abs(float(self.nz[nxt] - self.nz[cur])) / run > SLOPE_HARD:
                    return False
            cur = nxt
        return cur == b

    def blocking_index(self, s, seg):
        """Index into seg of blocking_point (None: all reachable)."""
        self._bi = None
        self.blocking_point(s, seg)
        return self._bi

    def blocking_point(self, s, seg):
        """The first point of the coarse stretch seg past the floor reachable from node s (None if all of it is
        reachable): where the coarse path goes through something the body cannot pass."""
        if s is None:
            return None
        seen = {s}
        st = [s]
        while st:
            v = st.pop()
            for t, _w in self.adj[v]:
                if t not in seen and self.clr[t] >= self.need_of(t):
                    seen.add(t)
                    st.append(t)
        cells = {}
        for v in seen:
            cells.setdefault(int(self.ncell[v]), []).append(float(self.nz[v]))
        for qi, q in enumerate(seg):
            i, j = int((q[0] - self.x0) / CELL), int((q[1] - self.y0) / CELL)
            ok = False
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    for z in cells.get((i + di) * self.ny + (j + dj), ()):
                        if abs(z - q[2]) <= Z_ABOVE:
                            ok = True
            if not ok:
                self._bi = qi
                return (float(q[0]), float(q[1]), float(q[2]))
        return None

    def pull(self, nodes):
        """String-pull: the farthest node reachable by a straight line that keeps the stretch's clearance."""
        out = [nodes[0]]
        i = 0
        while i < len(nodes) - 1:
            j_best = i + 1
            lo = min(CLEAR_WANT, float(self.clr[nodes[i + 1]]))
            for j in range(i + 2, len(nodes)):
                lo = min(lo, float(self.clr[nodes[j]]))
                need = max(min(AGENT_R, *(self.need_of(v) for v in nodes[i:j + 1])), lo - 0.05)
                if self.line_ok(nodes[i], nodes[j], need):
                    j_best = j
                elif math.dist(self.xyz(nodes[i])[:2], self.xyz(nodes[j])[:2]) > 25:
                    break
            out.append(nodes[j_best])
            i = j_best
        return out


LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nb", "gridpath.log")
_grids: "OrderedDict" = OrderedDict()   # (stretch key, band) -> Grid: a replan in the same place reuses the grid
GRID_CACHE = 32         # grids kept: every band of every stretch over the replan rounds (6: 36 rebuilds, 47 s in Booty Bay)


def _file_log(msg):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + f"[{os.getpid()}] " + msg + "\n")
    except OSError:
        pass


def _grid(seg, band):
    key = (round(float(seg[:, 0].min()) / 2), round(float(seg[:, 1].min()) / 2), round(float(seg[:, 0].max()) / 2),
           round(float(seg[:, 1].max()) / 2), round(float(seg[:, 2].mean())), band)
    G = _grids.get(key)
    if G is None:
        G = _grids[key] = Grid(seg, band)
        while len(_grids) > GRID_CACHE:
            _grids.popitem(last=False)
    else:
        _grids.move_to_end(key)
    return G


def refine(pts, log=None, fails=None):
    """pts: [(x, y, z)] path starting at the walker. The stretches inside buildings re-planned on the collision grid
    (see the module doc); the rest unchanged. Any failure keeps the stretch as it was (and is written to
    nb/gridpath.log). fails (a list): gets (x, y, z) of each coarse stretch's first point the grid cannot reach
    from the stretch's start - where the coarse path crosses something solid (navmesh replans round it)."""
    if len(pts) < 2:
        return pts
    log = log or _file_log
    dense = _densify(np.asarray([(p[0], p[1], p[2] or 0.0) for p in pts], float), 1.0)
    runs = indoor_runs(dense)
    if not runs:
        return pts
    out = []
    last = 0
    for i, j in runs:
        if j - i < 2:
            continue
        seg = dense[i:j + 1]
        nodes, G, si = None, None, None
        near_end = False                                   # ended on the reachable floor nearest an unreachable goal
        ends = [(float(p[0]), float(p[1]), float(p[2] or 0.0)) for p in (pts[0], pts[-1])
                if min(math.dist(p[:2], seg[0][:2]), math.dist(p[:2], seg[-1][:2])) < END_ZONE]
        # tiers: AGENT_R clearance (END_R at the walker's own spot and the goal: a nook it already stands in / an NPC
        # stands in), every band; then END_R all along - a tight way through beats the coarse stretch through props
        for tier, band in ([("ends", b) for b in BANDS] + [("tight", b) for b in BANDS]
                           + [("squeeze", b) for b in BANDS]):
            try:
                G = _grid(seg, band)
                if tier == "ends":
                    G.set_ends(ends)
                else:
                    G.set_all(END_R if tier == "tight" else SQUEEZE_R)
                # the ends: the first / last points with a usable grid node (a door threshold borders the outdoor
                # ground, which the vmaps do not hold: its edge clearance is too small for a node)
                si = next((k for k in range(min(8, len(seg))) if G.node_at(*seg[k]) is not None), None)
                gi = next((k for k in range(len(seg) - 1, max(-1, len(seg) - 9), -1)
                           if G.node_at(*seg[k]) is not None), None)
                if si is not None and gi is not None and gi > si:
                    nodes = G.astar(G.node_at(*seg[si]), G.node_at(*seg[gi]))
                    if not nodes:
                        # an end node may sit on a floor the other end cannot reach (a counter top the mesh fused into
                        # the shop floor, 1.1 yd up: Trade District shop; our own floor guess can be that top too):
                        # try the other floors at the start, and end on the reachable floor nearest the goal
                        for s2 in G.nodes_near(seg[si], 1.6):
                            if abs(float(G.nz[s2]) - float(seg[si][2])) > START_DZ:
                                continue
                            g2 = G.reachable_near(s2, seg[gi], GOAL_SLACK)
                            if g2 is not None:
                                nodes = G.astar(s2, g2)
                                if nodes:
                                    near_end = True
                                    break
                        if not nodes:
                            # the start in a pocket the grid calls closed (we walked in, behind a counter): start
                            # from the floor nearest us that does reach the goal, a straight step away
                            gn = G.node_at(*seg[gi])
                            s3 = G.reachable_near(gn, seg[si], GOAL_SLACK) if gn is not None else None
                            # ... a step away on our own level: not the floor over our head (the Auberdine inn's
                            # spiral stair: the grid had no way up it, s3 was the landing 3.8 yd above us and the
                            # route ran into the stair's underside, 8 stucks, #1128 rxp35 11:33)
                            if s3 is not None and abs(float(G.nz[s3]) - float(seg[si][2])) > START_DZ:
                                s3 = None
                            if s3 is not None:
                                nodes = G.astar(s3, gn)
            except Exception as e:                         # noqa: BLE001 - keep the coarse stretch
                if log:
                    log(f"gridpath: {e!r}")
                break
            if nodes:
                if tier != "ends" and log:
                    log(f"gridpath: ({seg[0][0]:.1f},{seg[0][1]:.1f}) -> ({seg[-1][0]:.1f},{seg[-1][1]:.1f}) only at "
                        f"{END_R if tier == 'tight' else SQUEEZE_R} yd clearance")
                break
        if not nodes and G is not None and si is not None:
            # refine what the grid does reach: from the start to the last coarse point before the first one it
            # cannot (Booty Bay: one bogus straight leg up to a deck 4.8 yd higher sank the whole stretch, and with it
            # the corner past a post where the bot wedged); the coarse path goes on from there
            s0 = G.node_at(*seg[si])
            k = G.blocking_index(s0, seg[si:])
            if k is not None and k >= 3:
                gk = si + k - 1
                g3 = G.reachable_near(s0, seg[gk], 1.6)
                if g3 is not None:
                    part = G.astar(s0, g3)
                    if part:
                        if log:
                            log(f"gridpath: ({seg[0][0]:.1f},{seg[0][1]:.1f}) -> ({seg[-1][0]:.1f},{seg[-1][1]:.1f}): "
                                f"refined the first {k - 1} yd only")
                        nodes, gi = part, gk
                        # the rest still crosses what the grid cannot pass: navmesh replans round it too (the coarse
                        # rest climbed the 2 yd Algaz Station ledge, rxp16 #415)
                        if fails is not None:
                            fails.append(tuple(float(v) for v in seg[si + k][:3]))
        if not nodes:
            where = None
            if fails is not None and G is not None and si is not None:
                with_block = G.blocking_point(G.node_at(*seg[si]), seg[si:])
                # a terrain face past SLOPE_MAX: blocking_point's 3x3-cell, Z_ABOVE test passes every point on it
                # (a lower neighbour cell is always close enough), so the replan never heard of it and the bot
                # wedged 9 times on the 1.4 face south of Gol'Bolar Quarry (-5670,-1642) (rxp13 03:33-03:35).
                # The first steep point wins over a later solid one: the bot wedges where the face starts (a 54 deg
                # Stonetalon slope reported 12 yd further up, at its top, rxp45 #1512)
                sq = np.array([p[:2] for p in seg[si:]], float)
                g = np.nan_to_num(terrain.grade(sq[:, 0], sq[:, 1]), nan=0.0)
                steep = np.nonzero(g > SLOPE_MAX)[0]
                if len(steep):
                    k0 = int(steep[0])
                    kb = (min(range(len(sq)), key=lambda k: math.dist(sq[k], with_block[:2]))
                          if with_block is not None else len(sq))
                    if k0 < kb:
                        with_block = tuple(float(v) for v in seg[si + k0][:3])
                if with_block is not None:
                    where = with_block
                    fails.append(where)
            if log:
                log(f"gridpath: no grid path from ({seg[0][0]:.1f},{seg[0][1]:.1f}) to ({seg[-1][0]:.1f},"
                    f"{seg[-1][1]:.1f}); coarse stretch kept"
                    + (f"; it crosses something solid at ({where[0]:.1f},{where[1]:.1f})" if where else ""))
            continue
        # up a steep face (terrain past SLOPE_FLAG, 45 deg, climbed for STEEP_UP yd or more): the navmesh hears of it
        # and looks round - the gentle way can lie outside the grid's band (15 yd south of the Tower of Althalaxx
        # ramp, 1 yd wide between 52 deg faces; the bot strayed onto one and wedged, #1168-#1169 rxp36 11:53)
        if fails is not None and len(nodes) > 1:
            up = [(a, b) for a, b in zip(nodes, nodes[1:]) if G.tg[b] > SLOPE_FLAG and G.nz[b] > G.nz[a] + 0.02]
            if sum(math.dist(G.xyz(a)[:2], G.xyz(b)[:2]) for a, b in up) >= STEEP_UP:
                v = max((b for _, b in up), key=lambda b: float(G.tg[b]))
                fails.append(tuple(float(c) for c in G.xyz(v)))
                if log:
                    log(f"gridpath: ({seg[0][0]:.1f},{seg[0][1]:.1f}) -> ({seg[-1][0]:.1f},{seg[-1][1]:.1f}) climbs a "
                        f"{float(G.tg[v]):.2f} face ({tier} tier)")
        # the joint with the stretch before: the coarse hop from where that stretch's refined part ended to this
        # stretch's first node is spliced in unrefined (a long window split at RUN_MAX, a start that skipped its first
        # blocked points). Walk it on this grid: a hop through blocked cells or up a sheer rise crosses something
        # solid - the joint of two windows split in a tower's 2.8 yd base wall, the route went through it and the bot
        # wedged on its face
        bridged = False
        if out and G is not None and math.dist(out[-1][:2], seg[si][:2]) <= BAND:
            pa = out[-1]
            a = None
            ia, ja = int((pa[0] - G.x0) / CELL), int((pa[1] - G.y0) / CELL)
            if 0 <= ia < G.nx and 0 <= ja < G.ny:
                cand = [v for v in G.nid[ia * G.ny + ja] if v >= 0 and abs(G.nz[v] - pa[2]) <= STEP_MAX]
                a = min(cand, key=lambda v: abs(G.nz[v] - pa[2])) if cand else None
            if a is not None and a != nodes[0] and not G.line_ok(a, nodes[0], 1e-6):
                # walk the joint on this grid when it can: the replans can run out with the bad hop still in (up the
                # 2 yd side of a ramp to Wilder Thistlenettle's shop, Stormwind - the same wedge in two runs, #1221
                # and #1290); the grid's way goes round to the ramp's foot
                bridge = G.astar(a, nodes[0])
                if bridge and len(bridge) > 1:
                    if log:
                        log(f"gridpath: the joint ({pa[0]:.1f},{pa[1]:.1f}) -> ({seg[si][0]:.1f},{seg[si][1]:.1f}) "
                            f"walked on the grid ({len(bridge)} nodes)")
                    nodes = list(bridge[:-1]) + list(nodes)
                    bridged = True
                else:
                    hop = dense[max(0, last - 1):i + si + 1]
                    q = hop[len(hop) // 2]
                    if log:
                        log(f"gridpath: the joint ({pa[0]:.1f},{pa[1]:.1f}) -> ({seg[si][0]:.1f},{seg[si][1]:.1f}) "
                            f"crosses something solid at ({q[0]:.1f},{q[1]:.1f})")
                    if fails is not None:
                        fails.append(tuple(float(v) for v in q[:3]))
        i, j = i + si, i + gi
        nodes = G.pull(nodes)
        if not bridged:                                    # a bridged joint replaces the coarse hop
            out.extend(tuple(p) for p in dense[last:i])
        out.extend(G.xyz(v) for v in nodes)
        last = j + 1
        if near_end and j + GOAL_SLACK + 2 >= len(dense) - 1:
            # the walk's goal (an NPC behind its counter) is not reachable: the route ends here, where the walker
            # arrives (goto's arrival is the route's last point), instead of pushing on into the counter
            last = len(dense)
            break
    out.extend(tuple(p) for p in dense[last:])
    return _simplify(out)


def _grade(x, y, h=0.75):
    """Terrain grade (rise per yd, the gradient's length) at arrays of points; nan off the map / in holes."""
    x, y = np.asarray(x, float), np.asarray(y, float)
    zx = terrain.heights(x + h, y) - terrain.heights(x - h, y)
    zy = terrain.heights(x, y + h) - terrain.heights(x, y - h)
    return np.hypot(zx, zy) / (2 * h)


BUCKET = 8.0        # yd: spatial buckets of the per-spawn caches (a big building holds millions of body points)
TILE = 32.0         # yd: a spawn's floor / body points are built per tile, on demand (Stormwind is one model ~1 km
                    # across: its whole body was 286M points, 6.4 GB - MemoryError, grid refine off)
_bucket_cache: dict = {}
MESH = None         # the NavMesh whose polygons fill holes in the collision floors (navmesh sets it)
_tri_cache: dict = {}


def _gobj_body(x0, y0, x1, y1):
    """Surface points of the solid spawned gameobjects in the box (campfires, crates, carts: gobj.py). Not in the
    vmap tiles, so without them a grid re-plan drops the coarse path's detour round one (Slaughtered Lamb bonfire)."""
    try:
        import gobj
        return gobj.bodies_in(x0, y0, x1, y1)
    except Exception as e:                                # noqa: BLE001 - no gameobject data: models only
        if not getattr(_gobj_body, "warned", False):
            _gobj_body.warned = True
            print(f"gridpath: gameobjects off: {e!r}")
        return np.zeros((0, 3))


def _tile_tris(sp, tx, ty, pad):
    """The spawn's placed triangles whose xy box overlaps tile (tx, ty) grown by pad yd."""
    key = (sp.root, sp.uid)
    c = _tri_cache.get(key)
    if c is None:
        T = vgeom.placed(sp).astype(np.float64)
        c = (T, T[:, :, 0].min(axis=1), T[:, :, 0].max(axis=1), T[:, :, 1].min(axis=1), T[:, :, 1].max(axis=1))
        _tri_cache[key] = c
    T, x0, x1, y0, y1 = c
    lx, ly = tx * TILE - pad, ty * TILE - pad
    hx, hy = lx + TILE + 2 * pad, ly + TILE + 2 * pad
    return T[(x1 >= lx) & (x0 <= hx) & (y1 >= ly) & (y0 <= hy)]


def _in_box(fn, sp, x0, y0, x1, y1):
    """The rows of fn(sp, tile) (body points in world x, y; floor rows in lattice i, j) in buckets touching the box."""
    out = []
    for tx in range(int(math.floor(x0 / TILE)), int(math.floor(x1 / TILE)) + 1):
        for ty in range(int(math.floor(y0 / TILE)), int(math.floor(y1 / TILE)) + 1):
            key = (fn.__name__, sp.root, sp.uid, tx, ty)
            ix = _bucket_cache.get(key)
            if ix is None:
                a = fn(sp, tx, ty)
                if fn is _spawn_floor:
                    wx, wy = (a[:, 0] + 0.5) * CELL, (a[:, 1] + 0.5) * CELL
                else:
                    wx, wy = a[:, 0], a[:, 1]
                keep = ((np.floor(wx / TILE) == tx) & (np.floor(wy / TILE) == ty))
                a, wx, wy = a[keep], wx[keep], wy[keep]
                bk = np.floor(wx / BUCKET).astype(np.int64) * 100003 + np.floor(wy / BUCKET).astype(np.int64)
                o = np.argsort(bk, kind="stable")
                a, bk = a[o], bk[o]
                u, st = np.unique(bk, return_index=True)
                en = np.append(st[1:], len(bk))
                ix = (a, {int(k): (int(s), int(e)) for k, s, e in zip(u, st, en)})
                _bucket_cache[key] = ix
            a, d = ix
            bx0, bx1 = max(int(math.floor(x0 / BUCKET)), int(tx * TILE / BUCKET)), min(int(math.floor(x1 / BUCKET)), int((tx + 1) * TILE / BUCKET) - 1)
            by0, by1 = max(int(math.floor(y0 / BUCKET)), int(ty * TILE / BUCKET)), min(int(math.floor(y1 / BUCKET)), int((ty + 1) * TILE / BUCKET) - 1)
            for bx in range(bx0, bx1 + 1):
                for by in range(by0, by1 + 1):
                    se = d.get(bx * 100003 + by)
                    if se:
                        out.append(a[se[0]:se[1]])
    return np.concatenate(out) if out else np.zeros((0, 3))


def _spawn_floor(sp, tx, ty):
    """(n, 3) [i, j, z] of one model spawn in one tile: the global CELL-lattice cells whose centre lies on (or within
    half a cell of) one of its walkable triangles, with that surface's height there. A small prop (an M2 under
    PROP_MAX yd across: a chair, a table, a crate) is no floor: the client does not walk onto it, and as one the grid
    stepped the route over the Thelsamar inn's chair and table (rxp21 #517)."""
    if (str(getattr(sp, "name", "")).lower().endswith(".m2")
            and max(sp.hi[0] - sp.lo[0], sp.hi[1] - sp.lo[1]) < PROP_MAX):
        return np.zeros((0, 3))
    T = _tile_tris(sp, tx, ty, CELL)
    out = []
    if len(T):
        n = np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0])
        ln = np.linalg.norm(n, axis=1)
        walk = (ln > 0) & (n[:, 2] / np.maximum(ln, 1e-12) > FLOOR_NZ)
        tol = CELL * 0.5
        li, hi_ = int(math.floor(tx * TILE / CELL)) - 1, int(math.floor((tx + 1) * TILE / CELL)) + 1
        lj, hj = int(math.floor(ty * TILE / CELL)) - 1, int(math.floor((ty + 1) * TILE / CELL)) + 1
        for tri in T[walk]:
            lo, hi = tri.min(axis=0), tri.max(axis=0)
            i0, i1 = max(li, int(math.floor((lo[0] - tol) / CELL))), min(hi_, int(math.floor((hi[0] + tol) / CELL)))
            j0, j1 = max(lj, int(math.floor((lo[1] - tol) / CELL))), min(hj, int(math.floor((hi[1] + tol) / CELL)))
            if i1 < i0 or j1 < j0:
                continue
            ii = np.repeat(np.arange(i0, i1 + 1), j1 - j0 + 1)
            jj = np.tile(np.arange(j0, j1 + 1), i1 - i0 + 1)
            px, py = (ii + 0.5) * CELL, (jj + 0.5) * CELL
            a, b, c = tri
            d = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
            if abs(d) < 1e-9:
                continue
            l1 = ((b[1] - c[1]) * (px - c[0]) + (c[0] - b[0]) * (py - c[1])) / d
            l2 = ((c[1] - a[1]) * (px - c[0]) + (a[0] - c[0]) * (py - c[1])) / d
            l3 = 1 - l1 - l2
            inside = (l1 >= 0) & (l2 >= 0) & (l3 >= 0)
            near = ~inside
            if near.any():                                # small triangles (stair treads): half a cell of slack
                inside[near] = _tri2d_dist(px[near], py[near], a, b, c) <= tol
            if inside.any():
                z = np.clip(l1 * a[2] + l2 * b[2] + l3 * c[2], lo[2], hi[2])
                out.append(np.stack([ii[inside], jj[inside], z[inside]], axis=1))
    return np.concatenate(out) if out else np.zeros((0, 3))


def _spawn_body(sp, tx, ty):
    """(n, 3) points every SAMPLE yd over one spawn's triangles that reach into one tile (the body test). A long
    triangle is sampled only along its part in the tile (rows outside are dropped by _in_box)."""
    T = _tile_tris(sp, tx, ty, SAMPLE)
    if not len(T):
        return np.zeros((0, 3))
    lx, ly = tx * TILE - SAMPLE, ty * TILE - SAMPLE
    hx, hy = lx + TILE + 2 * SAMPLE, ly + TILE + 2 * SAMPLE
    L = np.max(np.stack([np.linalg.norm(T[:, 1] - T[:, 0], axis=1), np.linalg.norm(T[:, 2] - T[:, 0], axis=1),
                         np.linalg.norm(T[:, 2] - T[:, 1], axis=1)]), axis=0)
    ns = np.maximum(1, np.ceil(L / SAMPLE)).astype(int)
    out = []
    for n in np.unique(ns):                               # one barycentric pattern per subdivision count
        u, v = np.meshgrid(np.arange(n + 1), np.arange(n + 1), indexing="ij")
        m = (u + v) <= n
        u, v = u[m] / n, v[m] / n
        S = T[ns == n]
        # in chunks: a huge triangle (a city floor) has n^2/2 samples
        step = max(1, int(2_000_000 // max(1, len(u))))
        for k in range(0, len(S), step):
            Sk = S[k:k + step]
            a = Sk[:, 0][:, None, :]
            P = (a + u[None, :, None] * (Sk[:, 1] - Sk[:, 0])[:, None, :]
                 + v[None, :, None] * (Sk[:, 2] - Sk[:, 0])[:, None, :]).reshape(-1, 3)
            out.append(P[(P[:, 0] >= lx) & (P[:, 0] <= hx) & (P[:, 1] >= ly) & (P[:, 1] <= hy)])
    return np.concatenate(out) if out else np.zeros((0, 3))


def _simplify(pts, tol=0.05):
    """Drop points on a straight line between their neighbours (the densified outdoor stretches)."""
    if len(pts) < 3:
        return list(pts)
    out = [pts[0]]
    for k in range(1, len(pts) - 1):
        a, b, c = out[-1], pts[k], pts[k + 1]
        dx, dy = c[0] - a[0], c[1] - a[1]
        L = math.hypot(dx, dy)
        if L > 1e-6 and abs((b[0] - a[0]) * dy - (b[1] - a[1]) * dx) / L <= tol and abs(b[2] - (a[2] + c[2]) / 2) < 0.3:
            continue
        out.append(b)
    out.append(pts[-1])
    return out


def _densify(g, step):
    out = [g[0]]
    for a, b in zip(g, g[1:]):
        L = math.hypot(b[0] - a[0], b[1] - a[1])
        n = max(1, int(math.ceil(L / step)))
        for k in range(1, n + 1):
            out.append(a + (b - a) * (k / n))
    return np.array(out)


def _sample_tri(tri, step):
    a, b, c = tri
    L = max(np.linalg.norm(b - a), np.linalg.norm(c - a), np.linalg.norm(c - b))
    n = max(1, int(math.ceil(L / step)))
    u, v = np.meshgrid(np.arange(n + 1), np.arange(n + 1), indexing="ij")
    m = (u + v) <= n
    u, v = u[m] / n, v[m] / n
    return a + np.outer(u, b - a) + np.outer(v, c - a)


def _tri2d_dist(px, py, a, b, c):
    def seg(ax, ay, bx, by):
        dx, dy = bx - ax, by - ay
        L = dx * dx + dy * dy
        u = np.clip(((px - ax) * dx + (py - ay) * dy) / L, 0, 1) if L > 0 else np.zeros_like(px)
        return np.hypot(px - ax - u * dx, py - ay - u * dy)
    return np.minimum(np.minimum(seg(a[0], a[1], b[0], b[1]), seg(b[0], b[1], c[0], c[1])), seg(c[0], c[1], a[0], a[1]))
