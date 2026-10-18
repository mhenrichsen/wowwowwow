"""Pathfinding on the server's own Detour navmeshes (.mmtile files).

The tiles are produced by VMaNGOS' MoveMapGenerator from the client's terrain,
buildings (vmaps) and liquids - the same data the server uses to path
creatures. We read the raw Detour tile format directly and run A* over the
polygons, then straighten the corridor with the funnel algorithm.

Coordinates: Detour/Recast is (x, y-up, z) = WoW (Y, Z, X).
"""

from __future__ import annotations

import glob
import heapq
import math
import os
import time
import struct

import roads

import numpy as np

from nb.store import Store
from nb.util import swallow, swallowed

MMAP_DIR = r"C:\Users\MadsG\Desktop\wow-forever-server\server\data_r3\mmaps"
# The bot's own mesh, built with the player's real walking limits: MoveMapGenerator
# patched to a 50 deg slope (the stock build calls terrain walkable up to 52 deg and
# marks 52-75 deg "steep" instead of removing it) and 1.2 yd terrain steps (stock
# 1.8), plus walkableSlopeAngle 50 in its config.json. Over the logged stuck spots in
# Coldridge and Kharanos, 80% of those that sat on walkable mesh are off this one -
# the bot stops planning up faces it then has to learn one bump at a time.
# Used for a map only once the build is verified (a READY file next to mmaps/),
# so a half-finished build is never loaded.
BOT_MMAP_DIR = r"C:\Users\MadsG\Desktop\wow-forever-server\server\data_r4\mmaps"
# The Forever server's own mesh, generated (2026-10-13) from the Forever client's
# terrain. Both meshes above were built from the 1.12 client's maps, and Coldridge
# Valley is reshaped in this client: on data_r3 the Spirit Stone hill trail the
# human walked (~170 yd at a gentle grade) did not exist, and the planned route went
# 413 yd over a ridge the character kept sliding off. Newer TrinityCore format:
# "0000.mmap" with an 8-byte magic/version prefix, "0000_XX_YY.mmtile", and the
# poly flags renumbered (see TCF_FLAGS).
TCF_MMAP_DIR = r"D:\tcforever\server\data\mmaps"
# newer TrinityCore NavTerrainFlag bit -> the NAV_* bit this module tests
TCF_FLAGS = {0x01: 0x01, 0x02: 0x10, 0x04: 0x08, 0x08: 0x02}


TILE_CACHE_VER = 1                   # bump when Tile's parsed fields change (NavMesh tile pickle)


def is_tcf(mmap_dir: str, map_id: int) -> bool:
    return os.path.exists(os.path.join(mmap_dir, f"{map_id:04d}.mmap"))


def mmap_dir_for(map_id: int) -> tuple[str, str]:
    """(mmaps directory, cache tag) for a map: the bot mesh when it is ready."""
    if (os.path.exists(os.path.join(os.path.dirname(BOT_MMAP_DIR), "READY"))
            and os.path.exists(os.path.join(BOT_MMAP_DIR, f"{map_id:03d}.mmap"))):
        return BOT_MMAP_DIR, "_r4"
    return MMAP_DIR, ""

DT_EXT_LINK = 0x8000
DT_POLYTYPE_OFFMESH = 1
# NavTerrain bits (MoveMapSharedDefines.h). These live in the polygon FLAGS;
# the polygon AREA is a separate enum (ground, steep, model-ground, ...) that
# the generator maps onto these flags (TileWorker.cpp), so never test bits on
# the area - that marked every building floor and staircase as unwalkable.
NAV_GROUND, NAV_MAGMA, NAV_SLIME, NAV_WATER, NAV_STEEP = 0x01, 0x02, 0x04, 0x08, 0x10
BLOCKED_FLAGS = NAV_MAGMA | NAV_SLIME
DANGER_COST = 60.0                   # extra "yards" per polygon step inside an aggro zone
SLOPE_FACE = math.tan(math.radians(50.0))   # terrain past this (the client's walk limit) is a face
FACE_STEP = 1.5                      # yd lattice the face test samples a tile on
FACE_SPAN = 8.3                      # yd: polestar's fallOf - the gradient across 2 + 2 of its 2.08 yd columns
FACE_RISE = 1.6                      # yd: steep triangles in a polygon rising more than this (a jump) make a face
FACE_DZ = 2.0                        # yd: terrain this far outside a polygon's height span is not under it
FACE_COST = 250.0                    # yd added to a step up into a face polygon
OCEAN_ENTRY = 1250                   # Forever LiquidType id of the sea (the .map files carry no ocean/deep flag)
DEEP_D = 40.0                        # yd of sea under a swim polygon that make it open sea (the Stormwind harbour: 33)
DEEP_FREE = 60.0                     # yd round the start / goal where open sea may still be swum
WATER_COST = 25.0                    # per-step cost of swimming polys (no ground flag):
                                     # at 8.0 the straight line across the loch still won
STEEP_COST = 4.0                     # per-step cost of steep polys
HOLE_POLY_MAX = 4.0                  # a hole only deletes polys up to this extent (yd)
AGENT_RADIUS = 0.9                   # portals shrink by this on each side (0.6 still clipped fence posts)
SLOPE_SOFT = 0.7                     # Forever mesh: polys steeper than this (rise/run of the surface) ...
SLOPE_COST = 15.0                    # ... cost x(1 + SLOPE_COST * excess) per step
SLIVER_W = 1.0                       # yd: a polygon narrower than this (2 x area / longest edge) in a building has no
                                     # trustworthy plane: its slope is its height span over its longest edge instead.
                                     # A 0.27 yd sliver along the Algaz Station tower's hall floor (span 1.3 yd over
                                     # 6.4 yd: one lifted wall vertex) read 3.34 rise/run and cost x40, so the plan
                                     # left the door and went 120 yd round the outside wall
GRADE_COST = 60.0                   # find_path(grade_soft=...): extra yd per yd run x excess grade^2
# clearance: the funnel path touches the mesh's walls at inner corners (portal shrinking only pads door ends): Teo's
# stair route ran 0.2-0.6 yd from the rail edge, the human's line 1.4-2.2 yd, and every bot wedged there. clear_path
# pushes the path off mesh walls (boundary edges) to CLEARANCE yd, staying on the mesh; narrow ways end up centred
CLEARANCE = 1.5
CLEAR_STEP = 0.75                    # yd between the points the push works on
CLEAR_ITERS = 15
CLEAR_DZ = 2.5                       # yd: a wall edge counts only near our height (floors of a building overlap)
# on steep ground the taut funnel line climbs straight up the face (Spirit Stone hill: polygons at 1.08-1.11 rise/run,
# the line ran 10 yd off the human's diagonal and every bot slid back at the same two spots). Portals touching a
# polygon steeper than STEEP_FOLLOW are narrowed to their middle STEEP_PORTAL yd: the path follows the corridor
STEEP_FOLLOW = 0.9
# furniture (vmap doodads the mesh calls floor) costs FURN_COST yd for a step whose portal middle lies within
# CLEARANCE of it: the search takes the open aisle over the 3.4 yd gap between Anvilmar's tables (4 of the 4 hall
# stucks left after the clearance pass were in that gap)
FURN_COST = 12.0
# furniture is kept FURN_CLEAR yd away (its collision reaches past the vmap box: the bots wedged in the 3.4 yd gap
# between two of Anvilmar's tables with 1.7 yd to each box; the human walks the open aisle beside them)
FURN_CLEAR = 2.0
STEEP_PORTAL = 1.0
# indoor stretches re-planned on the real collision triangles (gridpath.py): the mesh fuses ledges up to its climb
# height into the floor (Anvilmar's 0.84 yd riser round the sunken table floor: 60+ stucks on one line)
GRID_INDOOR = True
GRID_REPLANS = 3        # rounds of 'the grid says the corridor is blocked here: plan round it'
GRID_BLOCK_R = 3.0      # yd: corridor portals this near the blocking point get the cost
GRID_BLOCK_COST = 400.0  # yd added to a corridor step through it (a cost: a dead end still finds the old way)


def _cross2d(ax, ay, bx, by, cx, cy, dx, dy) -> bool:
    """Segments a-b and c-d cross (proper intersection, 2D)."""
    def o(px, py, qx, qy, rx, ry):
        return (qx - px) * (ry - py) - (qy - py) * (rx - px)
    d1, d2 = o(cx, cy, dx, dy, ax, ay), o(cx, cy, dx, dy, bx, by)
    d3, d4 = o(ax, ay, bx, by, cx, cy), o(ax, ay, bx, by, dx, dy)
    return d1 * d2 < 0 and d3 * d4 < 0

def _seg_dist2d(x, y, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    L = dx * dx + dy * dy
    u = 0.0 if L == 0 else max(0.0, min(1.0, ((x - a[0]) * dx + (y - a[1]) * dy) / L))
    return math.hypot(x - a[0] - u * dx, y - a[1] - u * dy)
CLIMB_BLOCK_COST = 200.0             # yd a PROVEN climb block adds to a route that crosses it (a cost, not a wall)
UPHILL_COST = 3.0                   # extra cost per yard of height gained between polygons
STEEP_CLIMB = 1.2                    # max up-grade onto a STEEP poly. At 1.5 the mesh offered a
                                     # route south out of Gol'Bolar up a cliff the client will not
                                     # climb (worst waypoint grade 1.49); 1.2 routes round it at 1.04
                                     # and leaves Menethil->Ironforge/Whelgar/Rhapsody unchanged.
HEURISTIC_WEIGHT = 1.25              # weighted A*: <=25% longer paths, far fewer nodes
ROAD_DISCOUNT = 0.77                 # multiplier on a step onto a road poly (roads.py).
                                     # Measured over 9 trips: routes +1.6% long (worst 1.11x),
                                     # 27% fewer hostile spawns passed, and the search runs
                                     # 8-30% FASTER because the discount focuses the frontier.
                                     # 1.0 disables it. Do NOT express this as an off-road
                                     # penalty: same routes, but the heuristic deflates and
                                     # the search explodes (15.8 s -> 25.0 s measured).
SEAL_CAP = 1500                      # polygons a backwards flood from the goal may grow before "not sealed" (_goal_sealed)
TIME_BUDGET = 30.0                 # seconds an A* search may take in the bot (None = unlimited, the audit)
MAX_CLIMB = 1.0                      # rise/run the character can walk up (45 deg; 0.75 walled off the Redridge hills); steeper edges
                                     # of a "ground" poly are a cliff face - unless inside a building
# find_path(spot_pen=...): circles (x, y, z, r, cost) the STRAIGHTENED path must not pass through. A graph cost cannot
# see them on big polygons: the 122-hit Algaz rise wedge (-4793.8,-2631.2) sits 0.6 yd from the 18 yd portal
# (9,21) 10 -> 13 but 5 yd from its midpoint and from the centre line, so it cost nothing and every plan from the
# bunker went up the rise (nb inst4 2026-10-15 06:59-07:58, 247+ stucks). After a search, every circle the funnel
# path passes through charges its cost on the corridor step whose portal lies nearest it, and the
# search runs again (SPOT_PEN_ROUNDS times at most). Works on any polygon size and any mesh (plain coordinates).
SPOT_PEN_ROUNDS = 3

# Detour side index -> neighbour tile offset (dx, dy) in the tile grid.
SIDE_OFFSET = {0: (1, 0), 1: (1, 1), 2: (0, 1), 3: (-1, 1),
               4: (-1, 0), 5: (-1, -1), 6: (0, -1), 7: (1, -1)}


def _climbs_decode(d) -> set:
    return {(((a[0], a[1]), a[2]), ((b[0], b[1]), b[2])) for a, b in d}


def _climbs_encode(hard) -> list:
    return [[[a[0][0], a[0][1], a[1]], [b[0][0], b[0][1], b[1]]] for a, b in hard]


def wow_to_rc(x, y, z):
    return (y, z, x)


def rc_to_wow(rx, ry, rz):
    return (rz, rx, ry)


def _a4(n):
    return (n + 3) & ~3


class Tile:
    def __init__(self, path: str, tcf: bool = False):
        d = open(path, "rb").read()
        magic, dtver, mver, size = struct.unpack_from("<4I", d, 0)
        assert magic == 0x4D4D4150 and dtver == 7, path
        o = 20
        h = struct.unpack_from("<15i3f3f3ff", d, o)
        (_, _, self.tx, self.ty, _, _, npoly, nvert, nlink, ndm, ndv, ndt, nbv,
         noff, _) = h[:15]
        self.bmin, self.bmax = h[18:21], h[21:24]
        o += _a4(100)
        self.verts = np.frombuffer(d, np.float32, nvert * 3, o).reshape(-1, 3).astype(np.float64)
        o += _a4(12 * nvert)
        praw = np.frombuffer(d, np.uint8, 32 * npoly, o).reshape(-1, 32)
        o += _a4(32 * npoly)
        self.pverts = praw[:, 4:16].copy().view(np.uint16)       # (n, 6)
        self.pneis = praw[:, 16:28].copy().view(np.uint16)       # (n, 6)
        self.pflags = praw[:, 28:30].copy().view(np.uint16)[:, 0]
        if tcf:
            f = np.zeros_like(self.pflags)
            for src, dst in TCF_FLAGS.items():
                f |= np.where(self.pflags & src, dst, 0).astype(f.dtype)
            self.pflags = f
        self.pcount = praw[:, 30].astype(int)
        self.parea = praw[:, 31] & 0x3F
        self.ptype = praw[:, 31] >> 6
        # per-poly 2D bounds (recast x/z) and heights, for point queries.
        # This was a Python loop calling min/max/mean once per polygon.
        # Eastern Kingdoms has 3.3 million of them, so startup spent 48 s
        # here before the bot could take a single step. Same numbers, done
        # as three array operations: polygons have 3-6 vertices, so gather
        # a fixed 6 and mask the slots that do not exist.
        cnt = self.pcount
        idx = np.asarray(self.pverts, dtype=np.intp)
        used = np.arange(idx.shape[1])[None, :] < cnt[:, None]
        vs = self.verts[np.where(used, idx, 0)]          # (npoly, 6, 3)
        m = used[..., None]
        self.pmin = np.where(m, vs, np.inf).min(1)
        self.pmax = np.where(m, vs, -np.inf).max(1)
        self.center = np.where(m, vs, 0.0).sum(1) / np.maximum(cnt, 1)[:, None]
        self.key = (self.tx, self.ty)
        # surface slope (rise/run of the polygon's own plane, Newell normal)
        nxt = np.where(np.arange(6)[None, :] + 1 < cnt[:, None], np.roll(idx, -1, 1), idx[:, :1])
        a, b = vs, self.verts[np.where(used, nxt, 0)]
        mm = used.astype(np.float64)
        nx = ((a[..., 1] - b[..., 1]) * (a[..., 2] + b[..., 2]) * mm).sum(1)
        ny = ((a[..., 2] - b[..., 2]) * (a[..., 0] + b[..., 0]) * mm).sum(1)
        nz = ((a[..., 0] - b[..., 0]) * (a[..., 1] + b[..., 1]) * mm).sum(1)
        self.pslope = np.hypot(nx, nz) / np.maximum(np.abs(ny), 1e-9)

    def usable(self, i) -> bool:
        # Water-only polys lie on the lake bed and a path across Loch Modan
        # walked the mage along the bottom (drowned next to a Loch Frenzy).
        # Making them UNWALKABLE was wrong: it also cut the fords and the
        # shoreline strips, isolating a third of Loch Modan (the whole
        # Bingles valley). They stay walkable at WATER_COST instead, so a
        # 10 yd ford is taken and a 200 yd swim never is.
        return (self.ptype[i] != DT_POLYTYPE_OFFMESH and self.pflags[i] != 0
                and not (self.pflags[i] & BLOCKED_FLAGS))

    def thin_slope(self) -> np.ndarray:
        """Per polygon: height span / longest edge for polygons narrower than SLIVER_W yd, inf for the rest (cached;
        built on first use so pickled tiles need no rebuild)."""
        s = self.__dict__.get("_thin_slope")
        if s is None:
            cnt = self.pcount
            idx = np.asarray(self.pverts, dtype=np.intp)
            used = np.arange(idx.shape[1])[None, :] < cnt[:, None]
            a = self.verts[np.where(used, idx, 0)]
            nxt = np.where(np.arange(6)[None, :] + 1 < cnt[:, None], np.roll(idx, -1, 1), idx[:, :1])
            b = self.verts[np.where(used, nxt, 0)]
            area = 0.5 * np.abs(((a[..., 0] * b[..., 2] - b[..., 0] * a[..., 2]) * used).sum(1))   # recast x/z
            elen = np.where(used, np.hypot(b[..., 0] - a[..., 0], b[..., 2] - a[..., 2]), 0.0).max(1)
            elen = np.maximum(elen, 1e-9)
            s = np.where(2 * area / elen < SLIVER_W, (self.pmax[:, 1] - self.pmin[:, 1]) / elen, np.inf)
            self.__dict__["_thin_slope"] = s
        return s

    def step_cost(self, i) -> float:
        f = self.pflags[i]
        if f & NAV_WATER and not f & NAV_GROUND:
            return WATER_COST              # swimming is slow and banks are cliffs; prefer land
        if f & NAV_STEEP:
            return STEEP_COST              # walkable downhill, poor uphill
        return 1.0

    def edge(self, i, j):
        c = self.pcount[i]
        return self.verts[self.pverts[i, j]], self.verts[self.pverts[i, (j + 1) % c]]


class NavMesh:
    def __init__(self, mmap_dir=None, map_id=0):
        self.map_id = map_id
        # poly ids differ between meshes: anything cached by poly id (learned
        # climbs, the component map) is kept per mesh
        if mmap_dir is None:
            mmap_dir, self.mesh_tag = mmap_dir_for(map_id)
        else:
            self.mesh_tag = "" if os.path.normcase(mmap_dir) == os.path.normcase(MMAP_DIR) else "_r4"
        tcf = is_tcf(mmap_dir, map_id)
        if tcf:
            self.mesh_tag = "_tcf"
        self.mmap_dir = mmap_dir
        if tcf:
            prm = open(os.path.join(mmap_dir, f"{map_id:04d}.mmap"), "rb").read()[8:]
            pattern = f"{map_id:04d}_*.mmtile"
        else:
            prm = open(os.path.join(mmap_dir, f"{map_id:03d}.mmap"), "rb").read()
            pattern = f"{map_id:03d}????.mmtile"
        self.orig = struct.unpack_from("<3f", prm, 0)
        self.tile_w, self.tile_h = struct.unpack_from("<2f", prm, 12)
        self.tiles: dict[tuple[int, int], Tile] = {}
        files = sorted(glob.glob(os.path.join(mmap_dir, pattern)))
        # parsed tiles cached beside the climb file, keyed by the newest tile's mtime: parsing the Forever mesh's
        # 736 map-0 tiles took 18 s of every bot restart (idle probe: 50-70 s standing per restart, 2026-10-14)
        cache = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data",
                             f"tiles_{map_id:03d}{self.mesh_tag}.pkl")
        stamp = (len(files), max((os.path.getmtime(f) for f in files), default=0.0), TILE_CACHE_VER)
        try:
            import pickle
            with open(cache, "rb") as fh:
                st, tiles = pickle.load(fh)
            if st == stamp:
                self.tiles = tiles
        except FileNotFoundError:
            pass                                 # first start on this mesh: parse below
        except Exception as e:
            swallowed("navmesh.tile_cache_read", e)
        if not self.tiles:
            for f in files:
                t = Tile(f, tcf=tcf)
                self.tiles[t.key] = t
            with swallow("navmesh.tile_cache_write"):
                import pickle
                os.makedirs(os.path.dirname(cache), exist_ok=True)
                tmp = cache + f".{os.getpid()}.tmp"
                with open(tmp, "wb") as fh:
                    pickle.dump((stamp, self.tiles), fh, protocol=pickle.HIGHEST_PROTOCOL)
                os.replace(tmp, cache)
        self._ext_cache: dict = {}
        self.blocked_climbs = self._load_climbs()
        # Predicted (not yet proven) climb blocks: a SUBSET of blocked_climbs,
        # key -> time.time() when it was predicted. The search tests
        # blocked_climbs and nothing else, so the hot loop is unchanged; what
        # soft_climbs adds is provenance. A PROVEN block (the character really
        # failed to walk up that step) is permanent and is written to disk. A
        # PREDICTED one (check_climbs read the terrain and expects a slide, or
        # block_steep_patch's blanket rule painted a whole hillside) lives for
        # this session only, is never persisted, expires (expire_soft) and can
        # be set aside for one search (find_path(relax_soft=True)) when the
        # honest answer would otherwise be "no route at all".
        self.soft_climbs: dict = {}
        # session-only extra cost per step (yd): the uphill spread round a proven wedge and repeat wedges on a
        # proven step. A proven block used to remove the step outright and the spread removed every uphill entry
        # into the polygon, for good: 72 such walls turned Teo -> the chest hill (-5941,459) from 460 into 1381 yd
        # and bot 2 lost 57 min (run 6). As costs, a long detour is only taken when it is really cheaper.
        self.climb_pen: dict = {}

    # ----------------------------------------------------------- learned climbs
    def _climbs_file(self):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data",
                            f"blocked_climbs_{self.map_id:03d}{self.mesh_tag}.json")

    def _climb_store(self) -> Store:
        """The shared climb file as an nb.store.Store (same JSON layout: [[[tx, ty, poly], [tx, ty, poly]], ...])."""
        st = self.__dict__.get("_climbs_st")
        if st is None:
            st = self._climbs_st = Store(self._climbs_file(), default=set, decode=_climbs_decode,
                                         encode=_climbs_encode)
        return st

    def _load_climbs(self) -> set:
        """Poly -> poly steps the character failed to walk up (a steep hill
        the mesh calls walkable). Outdoor hillside polys are far bigger than
        HOLE_POLY_MAX, so a learned hole never took them out and the bot
        kept planning up the same climb; this blocks just that one step.
        A new set each call (the file is re-parsed only when it changed)."""
        with swallow("navmesh.load_climbs"):
            return set(self._climb_store().read())
        return set()

    def _save_climbs(self) -> None:
        """Write the PROVEN blocks (blocked_climbs minus the predicted ones).

        The live bot reads this file at startup, so a half-written file must
        never exist on disk: nb.store writes a temp file beside it and
        os.replace's it, under the file's lock."""
        with swallow("navmesh.save_climbs"):
            hard = self.blocked_climbs - set(self.soft_climbs)
            unblocked = getattr(self, "_unblocked", set())
            # several bots share the file: keep what the others wrote since our load (minus our retractions)
            if not self._climb_store().update(lambda disk: (hard | disk) - unblocked):
                swallowed("navmesh.save_climbs_write", OSError(f"could not write {self._climbs_file()}"))

    def block_climb(self, frm, to, proven: bool = True) -> bool:
        """Block the step frm -> to. `proven` means the character really tried
        it and failed (permanent, persisted); proven=False is a prediction
        (session-only, relaxable, expires)."""
        key = ((tuple(frm[0]), int(frm[1])), (tuple(to[0]), int(to[1])))
        if key in self.blocked_climbs:
            # a prediction that later gets proven must be promoted, or the
            # one step we actually failed on would be forgotten at expiry
            if proven and key in self.soft_climbs:
                del self.soft_climbs[key]
                self._save_climbs()
            return False
        self.blocked_climbs.add(key)
        if proven:
            self._save_climbs()
        else:
            self.soft_climbs[key] = time.time()
        return True

    def block_uphill_into(self, node, proven: bool = True) -> int:
        """Block every step up into `node` from a lower neighbour (the steep
        face is inside one big polygon, so there is no single step to name)."""
        cz = float(self.tiles[node[0]].center[node[1]][1])
        n = 0
        for a, _, _ in self.neighbours(node):
            if float(self.tiles[a[0]].center[a[1]][1]) < cz - 0.3 and self.block_climb(a, node, proven=proven):
                n += 1
        return n

    def penalize_uphill_into(self, node, cost: float) -> int:
        """Add `cost` yd (session-only) to every step up into `node` from a lower neighbour."""
        cz = float(self.tiles[node[0]].center[node[1]][1])
        n = 0
        for a, _, _ in self.neighbours(node):
            if float(self.tiles[a[0]].center[a[1]][1]) < cz - 0.3:
                key = ((tuple(a[0]), int(a[1])), (tuple(node[0]), int(node[1])))
                self.climb_pen[key] = self.climb_pen.get(key, 0.0) + cost
                n += 1
        return n

    def crossed(self, frm, to) -> bool:
        """The character really walked frm -> to: whatever we learned against that step was wrong. True if
        something was lifted."""
        key = ((tuple(frm[0]), int(frm[1])), (tuple(to[0]), int(to[1])))
        hit = self.climb_pen.pop(key, None) is not None
        return self.unblock_climb(key) or hit

    def block_steep_patch(self, start, radius=10.0, slope=0.7, proven: bool = False) -> int:
        """Block every uphill step onto a steep face within `radius` yd of
        `start`. One stuck report used to learn one polygon step, and a
        hillside is dozens: nine stucks and three minutes below MacGrann's
        camp before the route finally went round.

        This is the blanket rule, and it is what produced most of the 1,732
        entries in the climb file - so it now defaults to proven=False: it
        still blocks, it just stops being permanent. Only the exact step the
        character was proved unable to walk (block_climb from
        learn_blocked_climb) is written to disk.

        Re-measured 2026-09. This docstring used to say a path cost for steep
        faces "made that search 11x slower", with no number attached and no
        record of which search. It does not reproduce. Consulting the real
        terrain gradient per polygon, memoised, inside the A* loop costs
        x1.01-1.04 with a warm cache and x1.37-1.55 cold, measured over 28
        real legs replayed from events.jsonl (0-981 yd) and 6 cross-zone trips
        (828-2453 yd, the max_nodes=1500000/budget=150.0 case). The size of
        the penalty barely moves it: x8 -> worst single leg x1.61, x60 ->
        worst leg x1.82, and not one journey lost its path. The slowest real
        search in the set is 1.2 s against a 150 s budget, so cost was never
        the reason not to do this.

        The reason is that the gradient is a poor discriminator. At the bot's
        STEEP_GROUND=1.3, 33.5% of the map-0 polygons that sit on the
        heightmap are "steep" (20,000-poly sample), a further 13.7% are off
        the heightmap entirely and can only count as unknown -> allow, while
        just 16.5% of 479 logged stucks are on ground that steep (median
        1.08, p90 1.39). Banning on it seals real routes: a direction-blind
        ban at 1.3 lost 1 of those 28 legs, and the one it lost was the way
        out of the very hillside the rule exists to fix. So the blanket rule
        stays here, as a PREDICTION that expires, and the search keeps
        knowing nothing about terrain.
        """
        c0 = self.tiles[start[0]].center[start[1]]
        seen, todo, new = {start}, [start], []
        while todo:
            a = todo.pop()
            ca = self.tiles[a[0]].center[a[1]]
            for b, _, _ in self.neighbours(a):
                cb = self.tiles[b[0]].center[b[1]]
                if (float(cb[1]) - float(ca[1]) > 0.5 and self.max_edge_slope(b) > slope
                        and not self.in_building(b)):
                    key = ((tuple(a[0]), int(a[1])), (tuple(b[0]), int(b[1])))
                    if key not in self.blocked_climbs:
                        self.blocked_climbs.add(key)
                        if not proven:
                            self.soft_climbs[key] = time.time()
                        new.append(key)
                if b not in seen and math.hypot(float(cb[0]) - float(c0[0]),
                                                float(cb[2]) - float(c0[2])) < radius:
                    seen.add(b)
                    todo.append(b)
        if new and proven:
            self._save_climbs()
        return len(new)

    def unblock_climb(self, key) -> bool:
        """Retract one blocked step (proven or predicted). True if it was there."""
        if key not in self.blocked_climbs:
            self.soft_climbs.pop(key, None)
            return False
        self.blocked_climbs.discard(key)
        was_soft = self.soft_climbs.pop(key, None) is not None
        if not was_soft:
            if not hasattr(self, "_unblocked"):
                self._unblocked = set()
            self._unblocked.add(key)
            self._save_climbs()          # it was on disk; take it off
        return True

    def reload_climbs(self) -> int:
        """Merge the proven blocks other bots wrote to the shared file since we loaded it."""
        new = self._load_climbs() - self.blocked_climbs - getattr(self, "_unblocked", set())
        self.blocked_climbs |= new
        return len(new)

    def expire_soft(self, ttl: float = 1800.0) -> int:
        """Drop predicted blocks older than `ttl`. A prediction is a guess
        about terrain we have not tested; left to pile up they turn a hillside
        into a maze with no way out. Proven blocks and the file are untouched."""
        if not self.soft_climbs:
            return 0
        cut = time.time() - ttl
        old = [k for k, t in self.soft_climbs.items() if t < cut]
        for k in old:
            del self.soft_climbs[k]
            self.blocked_climbs.discard(k)
        return len(old)

    # ----------------------------------------------------------- queries
    def _zone_of(self, node):
        """Zone name under a polygon's centre (cached per tile)."""
        t = self.tiles[node[0]]
        zc = t.__dict__.setdefault("_zone", {})
        z = zc.get(node[1], 0)
        if z == 0:
            import zones as zonemod
            c = t.center[node[1]]
            z = zc[node[1]] = zonemod.zone_name(float(c[2]), float(c[0]), self.map_id)
        return z

    def in_pocket(self, x, y, z, polys=5000) -> bool:
        """The polygon at WoW (x, y, z) lies in a mesh component of fewer than `polys` polygons (polestar's region rule:
        the terrain under a city model, a cellar - no start or goal floor while another floor is there)."""
        node = self.nearest_poly(x, y, z)[0]
        if node is None:
            return False
        comps = self.components()
        k = comps.get(node)
        if k is None:
            return False
        if self.__dict__.get("_comp_sizes_of") is not comps:
            from collections import Counter
            self._comp_sizes, self._comp_sizes_of = Counter(comps.values()), comps
        return self._comp_sizes.get(k, 0) < polys

    def _face(self, node) -> bool:
        """A ground polygon over a terrain face (polestar's held-back face links): terrain under it steeper than 50 deg,
        by the steepest triangle with a rise over FACE_RISE yd among the steep samples, or by the gradient across
        FACE_SPAN yd (a face of mixed triangles). Not the polygon's own plane, which averages the face away."""
        t = self.tiles[node[0]]
        f = t.__dict__.get("_face")
        if f is None:
            f = np.zeros(len(t.pflags), bool)
            try:
                f = self._faces_of(t)
            except Exception:                                 # noqa: BLE001
                pass
            t.__dict__["_face"] = f
        return bool(f[node[1]])

    def _faces_of(self, t):
        import terrain
        n = len(t.pflags)
        out = np.zeros(n, bool)
        ground = np.array([bool(fl & NAV_GROUND) for fl in t.pflags.tolist()], bool) & (t.ptype != DT_POLYTYPE_OFFMESH)
        if not ground.any():
            return out
        # sample the tile on a FACE_STEP lattice (recast x = wow y, recast z = wow x)
        x0, x1 = float(t.pmin[ground, 2].min()), float(t.pmax[ground, 2].max())
        y0, y1 = float(t.pmin[ground, 0].min()), float(t.pmax[ground, 0].max())
        gx, gy = np.meshgrid(np.arange(x0, x1 + FACE_STEP, FACE_STEP), np.arange(y0, y1 + FACE_STEP, FACE_STEP),
                             indexing="ij")
        wx, wy = gx.ravel(), gy.ravel()
        tri = np.nan_to_num(terrain.grade(wx, wy, self.map_id), nan=0.0)
        h = FACE_SPAN / 2
        zx = terrain.heights(wx + h, wy, self.map_id) - terrain.heights(wx - h, wy, self.map_id)
        zy = terrain.heights(wx, wy + h, self.map_id) - terrain.heights(wx, wy - h, self.map_id)
        span = np.nan_to_num(np.hypot(zx, zy) / FACE_SPAN, nan=0.0)
        st = (tri > SLOPE_FACE) | (span > SLOPE_FACE)
        if not st.any():
            return out
        wx, wy, tri, span = wx[st], wy[st], tri[st], span[st]
        wz = terrain.heights(wx, wy, self.map_id)
        # polygons by 8 yd bucket of their bounds
        B = 8.0
        bk: dict = {}
        for i in np.nonzero(ground)[0].tolist():
            for a in range(int(t.pmin[i, 2] // B), int(t.pmax[i, 2] // B) + 1):
                for b in range(int(t.pmin[i, 0] // B), int(t.pmax[i, 0] // B) + 1):
                    bk.setdefault((a, b), []).append(i)
        zlo: dict = {}
        zhi: dict = {}
        for k in range(len(wx)):
            px, py, pz = float(wx[k]), float(wy[k]), float(wz[k])
            if pz != pz:
                continue
            for i in bk.get((int(px // B), int(py // B)), ()):
                if not (t.pmin[i, 2] <= px <= t.pmax[i, 2] and t.pmin[i, 0] <= py <= t.pmax[i, 0]):
                    continue
                if not (t.pmin[i, 1] - FACE_DZ <= pz <= t.pmax[i, 1] + FACE_DZ):
                    continue                                  # the polygon is a floor above / below this terrain
                vs = t.verts[t.pverts[i, :t.pcount[i]]]
                if not _point_in_poly_2d(py, px, vs):
                    continue
                if span[k] > SLOPE_FACE:
                    out[i] = True
                if tri[k] > SLOPE_FACE:
                    zlo[i] = min(zlo.get(i, pz), pz)
                    zhi[i] = max(zhi.get(i, pz), pz)
        for i, lo in zlo.items():
            if zhi[i] - lo > FACE_RISE:
                out[i] = True
        return out

    def _deep(self, node) -> bool:
        """A swim-only polygon over open sea: ocean (liquid entry OCEAN_ENTRY) deeper than DEEP_D yd under it."""
        t = self.tiles[node[0]]
        d = t.__dict__.get("_deep")
        if d is None:
            d = np.zeros(len(t.pflags), bool)
            sw = np.array([(f & NAV_WATER) and not (f & NAV_GROUND) for f in t.pflags.tolist()], bool)
            if sw.any():
                try:
                    import terrain
                    c = t.center[sw]
                    wx, wy = c[:, 2], c[:, 0]
                    ent, lev = terrain.liquid(wx, wy, self.map_id)
                    lev = np.where(lev <= -1000, 0.0, lev)            # no-height sentinel: the sea at 0
                    floor = terrain.heights(wx, wy, self.map_id)
                    floor = np.where(np.isnan(floor), c[:, 1], floor)
                    d[np.nonzero(sw)[0]] = (ent == OCEAN_ENTRY) & (lev - floor > DEEP_D)
                except Exception:                                 # noqa: BLE001
                    pass
            t.__dict__["_deep"] = d
        return bool(d[node[1]])

    def max_edge_slope(self, node) -> float:
        """Steepest edge of a polygon: rise over horizontal run (cached)."""
        cache = self.__dict__.setdefault("_slope_cache", {})
        s = cache.get(node)
        if s is None:
            t = self.tiles[node[0]]
            vs = t.verts[t.pverts[node[1], :t.pcount[node[1]]]]
            s = 0.0
            for k in range(len(vs)):
                a, b = vs[k], vs[(k + 1) % len(vs)]
                run = max(math.hypot(b[0] - a[0], b[2] - a[2]), 1.0)
                s = max(s, abs(b[1] - a[1]) / run)
            cache[node] = s
        return s

    def in_building(self, node) -> bool:
        """Is the polygon's centre inside a WMO (building) box? Stairs and
        ramps in buildings are steep but walkable (cached)."""
        cache = self.__dict__.setdefault("_wmo_cache", {})
        r = cache.get(node)
        if r is None:
            r = False
            try:
                import vmap
                c = self.tiles[node[0]].center[node[1]]
                x, y, z = rc_to_wow(*c)
                r = vmap.inside_building(x, y, z, map_id=self.map_id)
            except Exception as e:
                swallowed("navmesh.in_building", e)
                r = False
            cache[node] = r
        return r

    def tile_at(self, rx, rz) -> Tile | None:
        tx = math.floor((rx - self.orig[0]) / self.tile_w)
        ty = math.floor((rz - self.orig[2]) / self.tile_h)
        return self.tiles.get((tx, ty))

    def reachable_near(self, start, target, radius=12.0, tries=6, require=0, exclude=0,
                       dz: float = 6.0):
        """The polygon centre nearest to `target` (within `radius`) that a
        path from `start` reaches, or None. Used when the target itself is
        walled off from the mesh (a doorway gap).

        `require` is a poly-flag mask every candidate must carry. Pass
        NAV_GROUND to find dry land: water polys are walkable (they have to
        be, or the Loch Modan fords vanish), so a plain search for the
        nearest poly while swimming returns the lake bed you are floating
        over. That is how the mage drowned trying to sit down to eat.

        `dz` is how far above or below `target` a candidate may sit. The
        default 6.0 is what every existing caller got; a caller answering
        "there is no honest route up there at all" can widen it to find an
        APPROACH point on a lower shelf (the goal above tonight's stuck
        hillside is 44 yd above the only usable ground near it, so at 6.0
        there is nothing to offer). Widening it does not make the last gap
        walkable - that is the caller's own check."""
        cands = []
        for t in self.tiles.values():
            lo, hi = t.verts.min(axis=0), t.verts.max(axis=0)       # recast: x=wow y, z=wow x
            if not (lo[0] - radius <= target[1] <= hi[0] + radius
                    and lo[2] - radius <= target[0] <= hi[2] + radius):
                continue
            for i in range(len(t.pcount)):
                if not t.usable(i):
                    continue
                if require and not (t.pflags[i] & require):
                    continue
                if exclude and (t.pflags[i] & exclude):
                    continue
                cx, cy, cz = rc_to_wow(*t.center[i])
                d = math.hypot(cx - target[0], cy - target[1])
                if d <= radius and abs(cz - target[2]) < dz and d > 0.3:
                    cands.append((d, (float(cx), float(cy), float(cz))))
        cands.sort()
        if tries == 0:                       # just the nearest centre, no path check
            return cands[0][1] if cands else None
        # only candidates on OUR patch of mesh: the six nearest to Razzle
        # Sprysprocket were all on the depot's unlinked floor, so all six
        # path searches failed and the quest was skipped
        with swallow("navmesh.reachable_near_island"):
            comp = self.components()
            s_poly, _ = self.nearest_poly(*start)
            mine = comp.get(s_poly) if s_poly is not None else None
            if mine is not None:
                same = []
                for d, p in cands:
                    pp, _ = self.nearest_poly(*p, max_dist=1.0)
                    if pp is not None and comp.get(pp) == mine:
                        same.append((d, p))
                cands = same or cands
        # Each failed try is a full search (7-8 s where learned climb blocks
        # wall a hill in), and callers widen the radius 12 -> 25 -> 40 yd,
        # which re-tries the same nearest centres: 46 s per call, 153 s
        # standing still for Tundra MacGrann (08:51). Cap every try and skip
        # centres that just failed from about here.
        failed = self.__dict__.setdefault("_rn_failed", {})
        now = time.time()
        skey = (round(start[0] / 10), round(start[1] / 10))
        for _, p in cands[:tries]:
            if time.time() - now > 6.0:
                break                      # the whole call, not only each try
            fk = (skey, round(p[0], 1), round(p[1], 1))
            if now - failed.get(fk, 0) < 120:
                continue
            if self.find_path(start, p, max_nodes=150000, budget=2.0):
                return p
            failed[fk] = time.time()
        return None

    def nearest_poly(self, x, y, z, max_dist=8.0):
        """Poly containing (or closest to) a WoW position: ((tile_key, idx), rc_point)."""
        rx, ry, rz = wow_to_rc(x, y, z)
        t = self.tile_at(rx, rz)
        if t is None:
            return None, None
        m = ((t.pmin[:, 0] - max_dist <= rx) & (t.pmax[:, 0] + max_dist >= rx) &
             (t.pmin[:, 2] - max_dist <= rz) & (t.pmax[:, 2] + max_dist >= rz) &
             (t.pmin[:, 1] - 6 <= ry) & (t.pmax[:, 1] + 6 >= ry))
        best, best_d = None, 1e9
        for i in np.nonzero(m)[0]:
            if not t.usable(i):
                continue
            if self._island((t.key, int(i))):
                continue           # table tops, crates, roof bits: not the floor
            vs = t.verts[t.pverts[i, :t.pcount[i]]]
            # Height counts double: the floors of a building are 6+ yd apart
            # while a horizontal miss (an NPC in a nook without polygons) is
            # 1-2 yd - the ground floor under an upstairs NPC must never win.
            if _point_in_poly_2d(rx, rz, vs):
                dh = abs(poly_height(rx, rz, vs) - ry)
                d = dh * 2.0                     # inside: only height matters
            else:
                d = math.hypot(t.center[i, 0] - rx, t.center[i, 2] - rz) + \
                    abs(t.center[i, 1] - ry) * 2.0 + 2.0
            if d < best_d:
                best, best_d = (t.key, int(i)), d
        return best, (rx, ry, rz)

    def ground_z(self, x, y, hint=None, reach=8.0) -> float | None:
        """Floor height under a 2D point, for a position that comes without one.

        A guide waypoint is a map percentage: x and y only. nearest_poly
        cannot help, it searches 6 yd around a height it is given. Where
        floors stack (a bridge over a road, a building's storeys) take the
        one nearest `hint` - the caller's own height - or else the lowest,
        which is the open ground a travel waypoint means.
        """
        rx, _, rz = wow_to_rc(x, y, 0.0)
        t = self.tile_at(rx, rz)
        if t is None:
            return None
        m = ((t.pmin[:, 0] - reach <= rx) & (t.pmax[:, 0] + reach >= rx) &
             (t.pmin[:, 2] - reach <= rz) & (t.pmax[:, 2] + reach >= rz))
        inside, near = [], []
        for i in np.nonzero(m)[0]:
            if not t.usable(i) or self._island((t.key, int(i))):
                continue
            vs = t.verts[t.pverts[i, :t.pcount[i]]]
            if _point_in_poly_2d(rx, rz, vs):
                inside.append(poly_height(rx, rz, vs))
            else:
                d = math.hypot(t.center[i, 0] - rx, t.center[i, 2] - rz)
                if d <= reach:
                    near.append((d, float(t.center[i, 1])))
        if hint is not None:
            # A point can sit inside only a far-away surface (a roof or a ledge
            # 30 yd up, 426.7 over an Anvilmar floor at 392.8) while the real
            # floor polygon starts a hand's width beside it: near floors compete
            # too, the one closest to the caller's own height wins.
            # ...but a polygon we stand IN within 3 yd of our height wins outright: else the height never climbs a
            # stair (Anvilmar: believed 392.8 on Teo's 399.4 platform, planned off the edge into the curb, 15:10/15:13)
            close = [(abs(h - hint), h) for h in inside if abs(h - hint) <= 3.0]
            if close:
                return min(close)[1]
            pool = [(abs(h - hint), h) for h in inside] + [(abs(h - hint) + 0.5, h) for _, h in near]
            return min(pool)[1] if pool else None
        if not inside:
            # a waypoint on a rock or a fence post: the floor just beside it
            return min(near)[1] if near else None
        # no hint: the lowest surface wins, and a floor polygon within 3 yd
        # counts as under the point (the point can sit on a polygon edge while
        # the only polygon that contains it is a roof 30 yd up)
        return min(inside + [h for d, h in near if d <= 3.0])

    def _island(self, node, min_size=40) -> bool:
        """True if the polygon belongs to a tiny disconnected patch.

        Furniture and roof tops get their own little navmesh islands; the
        nearest polygon to an NPC standing next to a table is often the table.
        """
        cache = self.__dict__.setdefault("_island_cache", {})
        if node in cache:
            return cache[node]
        seen = {node}
        stack = [node]
        while stack and len(seen) < min_size:
            n = stack.pop()
            for m, _, _ in self.neighbours(n):
                if m not in seen:
                    seen.add(m)
                    stack.append(m)
        small = len(seen) < min_size
        if small:
            for n in seen:
                cache[n] = True
        cache[node] = small
        return small

    # ------------------------------------------------------- neighbours
    def _external_edges(self, tile: Tile, side: int):
        k = (tile.key, side)
        if k not in self._ext_cache:
            out = []
            for i in range(len(tile.pcount)):
                for j in range(tile.pcount[i]):
                    nei = tile.pneis[i, j]
                    if nei & DT_EXT_LINK and (nei & 0xFF) == side:
                        a, b = tile.edge(i, j)
                        out.append((i, a, b))
            self._ext_cache[k] = out
        return self._ext_cache[k]

    def components(self):
        """Polygon -> connected-component id, ignoring the directional
        walking rules (steep/cliff are one-way, so this over-connects).

        Used only to reject a goal that is definitely unreachable: finding
        that out by exhausting the search cost 160 s per attempt, three
        times per quest step.
        """
        comp = self.__dict__.get("_components")
        if comp is not None:
            return comp
        import pickle
        cache = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data",
                             f"components_{self.map_id:03d}{self.mesh_tag}.pkl")
        try:
            with open(cache, "rb") as f:
                sig, comp = pickle.load(f)
            if sig == len(self.tiles):
                self._components = comp
                return comp
        except FileNotFoundError:
            pass                                 # not built for this mesh yet: below
        except Exception as e:
            swallowed("navmesh.components_cache_read", e)
        comp, cid = {}, 0
        for tk, t in self.tiles.items():
            for i in range(len(t.pcount)):
                if (tk, i) in comp or not t.usable(i):
                    continue
                cid += 1
                stack = [(tk, i)]
                comp[(tk, i)] = cid
                while stack:
                    cur = stack.pop()
                    for nb, _, _ in self.neighbours(cur):
                        if nb not in comp:
                            comp[nb] = cid
                            stack.append(nb)
        self._components = comp
        with swallow("navmesh.components_cache_write"):
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            with open(cache, "wb") as f:
                pickle.dump((len(self.tiles), comp), f, protocol=4)
        return comp

    def island_exit(self, pos, radius=40.0, big=5000):
        """Standing on a small unlinked patch of mesh (Steelgrill's Depot
        floor): the nearest polygon centre of a big patch within radius -
        somewhere a straight walk can get us back onto the real mesh - or
        None when we are on a big patch already (or no exit is near)."""
        comp = self.components()
        here, _ = self.nearest_poly(*pos)
        sizes = self.__dict__.get("_comp_sizes")
        if sizes is None:
            from collections import Counter
            sizes = self._comp_sizes = Counter(comp.values())
        if here is not None and sizes.get(comp.get(here), 0) >= big:
            return None
        ex = self.island_exits(pos, radius, big, 1)
        return ex[0] if ex else None

    def on_big_patch(self, pos, big=5000) -> bool:
        """The polygon under pos (within 1.5 yd of its height) belongs to a patch of big+ polygons (the real mesh)."""
        comp = self.components()
        sizes = self.__dict__.get("_comp_sizes")
        if sizes is None:
            from collections import Counter
            sizes = self._comp_sizes = Counter(comp.values())
        here, rp = self.nearest_poly(*pos)
        if here is None or rp is None:
            return False
        wx, wy, wz = rc_to_wow(*rp)
        return (math.hypot(wx - pos[0], wy - pos[1]) < 0.5 and abs(wz - pos[2]) < 1.5
                and sizes.get(comp.get(here), 0) >= big)

    def island_exits(self, pos, radius=40.0, big=5000, n=8):
        """The n nearest polygon centres of big patches within radius (island_exit's candidates), nearest first."""
        comp = self.components()
        sizes = self.__dict__.get("_comp_sizes")
        if sizes is None:
            from collections import Counter
            sizes = self._comp_sizes = Counter(comp.values())
        out = []
        for t in self.tiles.values():
            lo, hi = t.verts.min(axis=0), t.verts.max(axis=0)
            if not (lo[0] - radius <= pos[1] <= hi[0] + radius and lo[2] - radius <= pos[0] <= hi[2] + radius):
                continue
            for i in range(len(t.pcount)):
                if not t.usable(i) or sizes.get(comp.get((t.key, i)), 0) < big:
                    continue
                cx, cy, cz = rc_to_wow(*t.center[i])
                d = math.hypot(cx - pos[0], cy - pos[1])
                if d < radius and abs(cz - pos[2]) < 8.0:
                    out.append((d, (float(cx), float(cy), float(cz))))
        out.sort()
        return [p for _, p in out[:n]]

    def roads(self):
        """Road-cell set for this map (roads.py), loaded once."""
        r = self.__dict__.get("_roads")
        if r is None:
            try:
                import roads as _r
                r = _r.load(self.map_id)
            except Exception as e:
                swallowed("navmesh.roads", e)
                r = frozenset()
            self._roads = r
        return r

    NBR_CACHE_MAX = 120000           # ~300 MiB of portal arrays; cleared when full

    def neighbours(self, node):
        """(node, portal_left, portal_right) for each walkable neighbour.

        Cached: the adjacency is static, but matching collinear edges across
        a tile border was 6 s of a 20 s Thelsamar -> Ironforge search, and
        every search paid it again.
        """
        cache = self.__dict__.setdefault("_nbr_cache", {})
        out = cache.get(node)
        if out is None:
            out = list(self._neighbours(node))
            if len(cache) > self.NBR_CACHE_MAX:
                cache.clear()
            cache[node] = out
        return out

    def _neighbours(self, node):
        """Yield (node, portal_left, portal_right) for each walkable neighbour."""
        tk, i = node
        t = self.tiles[tk]
        for j in range(t.pcount[i]):
            nei = int(t.pneis[i, j])
            if nei == 0:
                continue
            a, b = t.edge(i, j)
            if not nei & DT_EXT_LINK:
                k = nei - 1
                if t.usable(k):
                    yield (tk, k), a, b
                continue
            side = nei & 0xFF
            dx, dy = SIDE_OFFSET[side]
            nt = self.tiles.get((t.tx + dx, t.ty + dy))
            if nt is None:
                continue
            # Match collinear, overlapping border edges in the neighbour tile.
            axis = 2 if side in (2, 6) else 0          # coordinate along the border
            other = 0 if axis == 2 else 2
            for k, c, d in self._external_edges(nt, (side + 4) % 8):
                if abs(c[axis] - a[axis]) > 0.1 or abs(d[axis] - a[axis]) > 0.1:
                    continue
                lo = max(min(a[other], b[other]), min(c[other], d[other]))
                hi = min(max(a[other], b[other]), max(c[other], d[other]))
                if hi - lo < 0.05 or not nt.usable(k):
                    continue
                if abs((a[1] + b[1]) / 2 - (c[1] + d[1]) / 2) > 3.0:
                    continue
                # clip the portal to the shared stretch, keeping a->b order
                pa, pb = a.copy(), b.copy()
                if a[other] <= b[other]:
                    pa[other], pb[other] = lo, hi
                else:
                    pa[other], pb[other] = hi, lo
                yield (nt.key, k), pa, pb

    def _goal_sealed(self, s, e, blocks, start, free_start=0.0) -> bool:
        """The goal sits in a pocket nothing outside can walk into: a backwards flood from e over the steps the A*
        below allows (learned climb blocks, the STEEP up-rule, the cliff-lip rule) closes within SEAL_CAP polygons
        without meeting s. components() cannot tell: it ignores direction, so a ledge you can only drop off is on
        the same island, and A* proved "no path" by exhausting 1.4 M polygons - 40-93 s with the character standing
        still (nb inst2 15:47: (-5675,-2871) on a 429-poly ledge above Loch Modan; this check: 0.01 s). An open
        flood (cap reached) says nothing and is remembered per goal until the block set changes."""
        key = (e, len(blocks))
        open_ = self.__dict__.setdefault("_seal_open", {})
        if open_.get(e) == key:
            return False
        ctr = self.__dict__.setdefault("_ctr_cache", {})

        def centre(n):
            c = ctr.get(n)
            if c is None:
                v = self.tiles[n[0]].center[n[1]]
                c = ctr[n] = (float(v[0]), float(v[1]), float(v[2]))
            return c
        cliff_rule = self.map_id in (0, 1) and self.mesh_tag != "_tcf"
        seen, stack = {e}, [e]
        while stack:
            x = stack.pop()
            cx = centre(x)
            steep = x != e and self.tiles[x[0]].pflags[x[1]] & NAV_STEEP
            for p, _, _ in self.neighbours(x):
                if p in seen:
                    continue
                cp = centre(p)
                # the A* loop's rules for the step p -> x (keep in step with it)
                if (p, x) in blocks and not (
                        free_start and math.hypot(cp[2] - start[0], cp[0] - start[1]) < free_start):
                    continue
                rise = cx[1] - cp[1]
                if rise > 0.3 and (steep or (cliff_rule and rise > 1.0)):
                    run = max(math.hypot(cx[0] - cp[0], cx[2] - cp[2]), 0.5)
                    if steep and rise / run > STEEP_CLIMB:
                        continue
                    if (cliff_rule and rise > 1.0 and rise / run > MAX_CLIMB and self.max_edge_slope(x) > MAX_CLIMB
                            and not self.in_building(x)):
                        continue
                if p == s:
                    return False
                seen.add(p)
                if len(seen) > SEAL_CAP:
                    open_[e] = key
                    if len(open_) > 4096:
                        open_.clear()
                    return False
                stack.append(p)
        return True

    # ------------------------------------------------------------ path
    def find_path(self, start, end, avoid=(), dangers=(), agent_radius=AGENT_RADIUS, max_nodes=200000,
                  holes=(), budget=None, relax_soft=False, detours=(), grade_soft=None, spot_pen=()):
        """See _find_path (spot_pen: see SPOT_PEN_ROUNDS). When learned climb blocks wall in the spot we stand
        on, ignore them near the start: we are already here, so there is a
        way out (at Amberstill Ranch the only step off our polygon had been
        blocked, and the bot sat for minutes on "no navmesh path").

        relax_soft: set aside the PREDICTED blocks (soft_climbs) for this one
        search and keep the proven ones. The caller asks for this only after
        the ordinary search has already failed, so that "no way round" means
        either a face we really failed to walk, or nothing at all - never a
        guess about terrain we never tested."""
        pen: dict = {}
        free = 0.0
        path = self._find_path(start, end, avoid, dangers, agent_radius, max_nodes, holes, budget,
                               relax_soft=relax_soft, detours=detours, grade_soft=grade_soft)
        if path is None and self.blocked_climbs:
            free = 25.0
            path = self._find_path(start, end, avoid, dangers, agent_radius, max_nodes, holes, budget,
                                   free_start=free, relax_soft=relax_soft, detours=detours,
                                   grade_soft=grade_soft)
        if not (path and spot_pen):
            return path
        # each round charges the steps this route passes wedge circles on, then searches again; the route with the
        # smallest length + circle bill wins (a later round can find a route whose own circles were never charged)
        hits, bill = self._pen_hits(start, path, spot_pen, end)
        best = (bill, path)
        for _ in range(SPOT_PEN_ROUNDS):
            new = [(st, c) for st, c in hits if st not in pen]
            if not new:
                break
            for st, c in new:
                pen[st] = c
            p2 = self._find_path(start, end, avoid, dangers, agent_radius, max_nodes, holes, budget,
                                 free_start=free, relax_soft=relax_soft, detours=detours,
                                 grade_soft=grade_soft, step_pen=pen)
            if not p2:
                break                        # keep the route we had: a penalty is a preference
            hits, bill = self._pen_hits(start, p2, spot_pen, end)
            if bill < best[0]:
                best = (bill, p2)
        return best[1]

    def _pen_hits(self, start, path, circles, end):
        """([(corridor step, cost)], bill) for the waypoint list `path` (the last search's result): one entry per
        circle of `circles` it passes through, on the step of the last search's corridor whose portal lies nearest
        the circle centre; bill = path length + the costs. Circles within r + 1.5 yd of the start or the goal are
        skipped (we stand there / must get there)."""
        steps = getattr(self, "_last_steps", None) or []
        pts = [tuple(map(float, start[:3]))] + [tuple(map(float, q[:3])) for q in path]
        bill = sum(math.dist(a[:2], b[:2]) for a, b in zip(pts, pts[1:]))
        out = {}
        for cx, cy, cz, r, cost in circles:
            if math.hypot(cx - start[0], cy - start[1]) < r + 1.5 or math.hypot(cx - end[0], cy - end[1]) < r + 1.5:
                continue
            hit = False
            for a, b in zip(pts, pts[1:]):
                dx, dy = b[0] - a[0], b[1] - a[1]
                n2 = dx * dx + dy * dy
                t = 0.0 if n2 == 0 else max(0.0, min(1.0, ((cx - a[0]) * dx + (cy - a[1]) * dy) / n2))
                if math.hypot(a[0] + t * dx - cx, a[1] + t * dy - cy) < r and (
                        cz is None or abs(a[2] + t * (b[2] - a[2]) - cz) < 6.0):
                    hit = True
                    break
            if not hit:
                continue
            bill += float(cost)
            best = None
            for st, pa, pb in steps:
                # portals are recast (wowY, wowZ, wowX)
                ax, ay, bx, by = float(pa[2]), float(pa[0]), float(pb[2]), float(pb[0])
                dx, dy = bx - ax, by - ay
                n2 = dx * dx + dy * dy
                t = 0.0 if n2 == 0 else max(0.0, min(1.0, ((cx - ax) * dx + (cy - ay) * dy) / n2))
                d = math.hypot(ax + t * dx - cx, ay + t * dy - cy)
                if best is None or d < best[0]:
                    best = (d, st)
            if best is not None:
                out[best[1]] = out.get(best[1], 0.0) + float(cost)
        return list(out.items()), bill

    def _find_path(self, start, end, avoid=(), dangers=(), agent_radius=AGENT_RADIUS, max_nodes=200000,
                   holes=(), budget=None, free_start=0.0, relax_soft=False, detours=(), grade_soft=None,
                   step_pen=None):
        """WoW (x,y,z) -> WoW (x,y,z) waypoint list, or None if unreachable.
        step_pen: {(poly, poly): extra cost} for single corridor steps (find_path's spot_pen rounds).
        The found corridor is left in self._last_steps as [((from, to), pa, pb), ...].

        avoid: WoW points (x, y, z, radius) that turned out to be blocked by
        something the navmesh does not know about (small doodads such as a
        pile of books). Crossing near them is made very expensive.
        dangers: WoW (x, y, z, radius) aggro zones of mobs that could kill us
        (dangers.py). Every step inside one costs DANGER_COST extra yards, so
        the path detours around camps unless there is no other way through.
        agent_radius: portals are narrowed by this much on each side so the
        straightened path keeps clear of walls, railings and door frames.
        detours: WoW (x, y, z, r) spots the straightened path only bends round
        (a box like a hole's; no polygons removed, no cost). nb's outdoor wedge
        spots: an avoid circle only costs a step whose portal MIDPOINT is inside
        it, and on open ground the portals are long - an r 2.5 avoid 0.6 yd off
        the Felix -> Talin Keeneye path at (-6171,397) left the path unchanged.
        """
        self._last_steps = []
        zones = _ZoneIndex(avoid, 500.0)
        # holes: WoW (x, y, z, r) spots where we got wedged on furniture the
        # mesh calls walkable (inn tables, railings). Polygons centred inside
        # one are simply not walkable - a cost is not enough in a small room
        # where every route pays it and the same path wins again.
        hole_idx = _ZoneIndex(holes, 1.0) if holes else None
        # per-polygon extent/height span, and only the holes bucketed near a
        # polygon: with 550 learned wedge spots the slope test below walked
        # every one of them for every neighbour expanded - 14.6 s for a
        # 500 yd Coldridge -> Kharanos plan that takes 0.1 s without holes,
        # and the bot stood 40-80 s before every town trip
        pext: dict = {}
        danger = _ZoneIndex(dangers, DANGER_COST)
        s, sp = self.nearest_poly(*start)
        e, ep = self.nearest_poly(*end)
        if s is None or e is None:
            return None
        if s == e:
            return _avoid_doodads(start, [end])     # one polygon can still hold a campfire or a cart
        try:
            comp = self.components()
            if comp.get(s) != comp.get(e):
                return None          # different island: no walk exists at all
        except Exception as ex:
            swallowed("navmesh.find_path_islands", ex)
        et = self.tiles[e[0]]
        gv = et.center[e[1]]
        goal = (float(gv[0]), float(gv[1]), float(gv[2]))

        # plain-float centres: numpy row views and np.linalg.norm on 3-vectors
        # cost more than the arithmetic they do (1.8 M calls per long search)
        ctr = self.__dict__.setdefault("_ctr_cache", {})
        road = self.roads() if ROAD_DISCOUNT != 1.0 else None
        road_of = self.__dict__.setdefault("_road_cache", {})

        def centre(n):
            c = ctr.get(n)
            if c is None:
                v = self.tiles[n[0]].center[n[1]]
                c = (float(v[0]), float(v[1]), float(v[2]))
                ctr[n] = c
            return c

        # the set the loop tests, bound once: with relax_soft the predicted
        # blocks are set aside and only the proven ones remain. Same object as
        # self.blocked_climbs in the normal case, so the hot loop is unchanged.
        # steep polygons cost more, up or down or across (the character slides
        # off them): the Forever mesh only, where the generator's slope limit
        # is loose. Over the human's level 1-7 run 1.9% of positions were on
        # polygons steeper than 0.8 rise/run; 31% of 298 bot stuck spots were,
        # and every Spirit Stone hill stuck spot measured 1.09-1.40
        slope_soft = SLOPE_SOFT if self.mesh_tag == "_tcf" else None
        face_rule = FACE_COST > 0 and self.mesh_tag == "_tcf"
        # polestar's zone danger: a zone whose mobs start well over our level costs x3 / x20 per step; the start's
        # and the goal's zones are exempt (we are already there / must go there)
        zfac = None
        lvl = getattr(self, "level", None)
        if lvl:
            try:
                import zones as zonemod
                skip = {zonemod.zone_name(start[0], start[1], self.map_id),
                        zonemod.zone_name(end[0], end[1], self.map_id)}
                zfac = {zn: zonemod.danger_factor_of(lv, lvl) for zn, lv in zonemod.zone_levels().items()
                        if zn not in skip}
                zfac = {k: v for k, v in zfac.items() if v > 1.0} or None
            except Exception:                         # noqa: BLE001
                zfac = None
        furn = {}                            # 4 yd grid cell -> [box] of furniture inflated by CLEARANCE
        if FURN_COST and math.hypot(start[0] - end[0], start[1] - end[1]) < 400:
            with swallow("navmesh.furniture"):
                pad = 30.0
                for b in _doodad_boxes(min(start[0], end[0]) - pad, min(start[1], end[1]) - pad,
                                       max(start[0], end[0]) + pad, max(start[1], end[1]) + pad, start[2]) +                         (_doodad_boxes(min(start[0], end[0]) - pad, min(start[1], end[1]) - pad,
                                       max(start[0], end[0]) + pad, max(start[1], end[1]) + pad, end[2])
                         if abs(end[2] - start[2]) > 2.5 else []):
                    c_ = CLEARANCE - DOODAD_CLEARANCE
                    bx = (b[0] - c_, b[1] - c_, b[2] + c_, b[3] + c_)
                    for gx in range(int(bx[0] // 4), int(bx[2] // 4) + 1):
                        for gy in range(int(bx[1] // 4), int(bx[3] // 4) + 1):
                            furn.setdefault((gx, gy), []).append(bx)
        # only the PREDICTIONS (soft_climbs) are walls; a proven block is a CLIMB_BLOCK_COST yd cost and
        # climb_pen adds its session costs on top
        blocks = set() if relax_soft else set(self.soft_climbs)
        proven = self.blocked_climbs - set(self.soft_climbs) if self.blocked_climbs else ()
        cpen = self.climb_pen

        if self._goal_sealed(s, e, blocks, start, free_start):
            return None                      # the goal's pocket has no way in: no search can find one

        # teleport triggers (instance doors, Champions' Hall in Old Town) are closed doors: a step whose portal
        # midpoint lies in one is never taken - unless the start or the goal is in it (we were sent there)
        trig = []
        for e_ in teleport_boxes():
            if any(trigger_inside(e_, p_[0], p_[1], p_[2] or 0.0, 0.0) for p_ in (start, end)):
                continue
            trig.append((e_[0] - TRIG_PAD - 1.0, e_[1] - TRIG_PAD - 1.0, e_[2] + TRIG_PAD + 1.0,
                         e_[3] + TRIG_PAD + 1.0, e_))

        openq = [(0.0, 0, s)]
        g = {s: 0.0}
        came: dict = {}
        portals: dict = {}
        tie = 0
        found = False
        t_budget = time.time() + (budget if budget is not None else (TIME_BUDGET or 1e9))
        dl = getattr(self, "deadline", None)       # the caller's limit for a whole multi-search plan
        if dl:
            t_budget = min(t_budget, dl)
        stop = getattr(self, "interrupt", None)    # the caller's "something attacks us": give up the plan now
        n_pop = 0
        while openq and len(g) < max_nodes:
            if time.time() > t_budget:
                break                       # 19 three-minute searches today: ~1 h of standing still
            n_pop += 1
            if stop is not None and n_pop % 2000 == 0 and stop():
                break
            _, _, cur = heapq.heappop(openq)
            if cur == e:
                found = True
                break
            cc = centre(cur)
            for nb, pa, pb in self.neighbours(cur):
                if (cur, nb) in blocks and not (
                        free_start and math.hypot(float(cc[2]) - start[0], float(cc[0]) - start[1]) < free_start):
                    continue                 # a climb we got stuck on before
                c_nb = centre(nb)
                # open sea (polestar's deep-water rule): no swim across deep ocean; a crossing is a boat. Free near
                # the start and goal, so a walk out of the sea (or to a wreck) still plans
                if self._deep(nb) and math.hypot(float(c_nb[2]) - start[0], float(c_nb[0]) - start[1]) > DEEP_FREE                         and math.hypot(float(c_nb[2]) - end[0], float(c_nb[0]) - end[1]) > DEEP_FREE:
                    continue
                if hole_idx is not None and nb != e:
                    # a 1.5 yd hole must not delete a 17 yd room polygon (two
                    # holes removed the whole inn taproom, stairs included);
                    # big polys keep the hole as a detour box instead
                    tb = self.tiles[nb[0]]
                    es = pext.get(nb)
                    if es is None:
                        es = (float(max(tb.pmax[nb[1], 0] - tb.pmin[nb[1], 0], tb.pmax[nb[1], 2] - tb.pmin[nb[1], 2])),
                              float(tb.pmax[nb[1], 1] - tb.pmin[nb[1], 1]))
                        pext[nb] = es
                    ext, span = es
                    # ... unless the polygon is a slope (height span > 4 yd):
                    # a wedge spot on a "ground" poly rising 6 yd over 5 yd
                    # under Sentinel Hill means the climb is not walkable
                    if ext <= HOLE_POLY_MAX and hole_idx.cost(c_nb[2], c_nb[0], c_nb[1]):
                        continue
                    if span > 4.0:
                        near_h = hole_idx.near(c_nb[2], c_nb[0], ext * 0.5 + 2.0)
                        if near_h and any(abs(c_nb[1] - h[2]) < 15
                                          and _point_in_poly_2d(h[1], h[0], tb.verts[tb.pverts[nb[1], :tb.pcount[nb[1]]]])
                                          for h in near_h):
                            continue
                # STEEP polys (52-75 deg) are for NPCs: a player cannot climb
                # them and just slides. Going down is fine, going up is not.
                if self.tiles[nb[0]].pflags[nb[1]] & NAV_STEEP and nb != e \
                        and c_nb[1] > cc[1] + 0.3:
                    # ... unless the step between the two centres is itself a
                    # walkable grade: the Dun Algaz road, the Whelgar dig site
                    # and the Hinterlands slopes are all flagged STEEP and
                    # were walled off (Menethil -> Ironforge had no path)
                    run = max(math.hypot(c_nb[0] - cc[0], c_nb[2] - cc[2]), 0.5)
                    if (c_nb[1] - cc[1]) / run > STEEP_CLIMB:
                        continue
                # a "ground" poly with an edge steeper than MAX_CLIMB is a cliff
                # face when entered from below (the 6-over-5 yd lip under
                # Sentinel Hill wedged the character in five runs); stairs
                # and ramps inside buildings are exempt
                # (continents only: the Deeprun Tram map is one big building
                # whose track bed ramps would be "cliffs")
                # (not on the Forever server's mesh: its generator already cut
                # the faces the client cannot walk, and this rule removed the
                # Spirit Stone hill trail a human walks up - no path at all)
                if self.map_id in (0, 1) and self.mesh_tag != "_tcf" and c_nb[1] > cc[1] + 1.0 \
                        and self.max_edge_slope(nb) > MAX_CLIMB and not self.in_building(nb):
                    # ... but only when the step itself is steep: a poly with
                    # one cliff edge still has a walkable side (the Dun Algaz
                    # road, the Hinterlands slopes: no path at all before)
                    run = max(math.hypot(c_nb[0] - cc[0], c_nb[2] - cc[2]), 0.5)
                    if (c_nb[1] - cc[1]) / run > MAX_CLIMB:
                        continue
                mx = (float(pa[0]) + float(pb[0])) * 0.5
                my = (float(pa[1]) + float(pb[1])) * 0.5
                mz = (float(pa[2]) + float(pb[2])) * 0.5
                if trig and any(b_[0] <= mz <= b_[2] and b_[1] <= mx <= b_[3]
                                and trigger_inside(b_[4], mz, mx, my, TRIG_PAD) for b_ in trig):
                    continue                 # (mx, my, mz) is recast: wow (mz, mx, my)
                step = (math.dist((mx, my, mz), cc) + math.dist(c_nb, (mx, my, mz)))
                step *= self.tiles[nb[0]].step_cost(nb[1])
                if zfac:
                    f_ = zfac.get(self._zone_of(nb))
                    if f_:
                        step *= f_
                # up into a face polygon: held back, not shut (polestar: shutting every face cut 84 guide goals off; a
                # face that is the only way still opens). The cost makes any gentler way round under FACE_COST win
                if face_rule and nb != e and c_nb[1] > cc[1] + 0.3 and self._face(nb):
                    step += FACE_COST
                if slope_soft is not None:
                    sl = float(self.tiles[nb[0]].pslope[nb[1]])
                    if sl > slope_soft:
                        # a sliver's plane tilt is noise (one lifted edge vertex); in a building its span/length
                        # is the climb. Not on open terrain: contour slivers on a hill keep their tilt
                        th = float(self.tiles[nb[0]].thin_slope()[nb[1]])
                        if th < sl and self.in_building(nb):
                            sl = th
                    if sl > slope_soft:
                        step *= 1.0 + SLOPE_COST * (sl - slope_soft)
                if road:
                    # c_nb is recast (wowY, wowZ, wowX)
                    on = road_of.get(nb)
                    if on is None:
                        on = roads.key(c_nb[2], c_nb[0]) in road
                        road_of[nb] = on
                    if on:
                        step *= ROAD_DISCOUNT
                # climbing costs: a route over a 20 yd hill is only taken when
                # the flat way round is much longer (the bot "ran up mountains")
                step += UPHILL_COST * max(0.0, float(c_nb[1] - cc[1]))
                # grade_soft: climbs steeper than this rise/run cost extra per
                # yard, growing with the square of the excess, so a gentler
                # trail wins over the straight line up the face. The server
                # mesh keeps terrain to ~52 deg and its Spirit Stone hill route
                # ran at 0.75-1.18 the whole way: the character slid back
                # ~15 times a minute
                if grade_soft is not None and c_nb[1] > cc[1] + 0.3:
                    run = max(math.hypot(c_nb[0] - cc[0], c_nb[2] - cc[2]), 0.5)
                    over = (c_nb[1] - cc[1]) / run - grade_soft
                    if over > 0:
                        step += GRADE_COST * run * over * over
                # mid is in recast coords: (wowY, wowZ, wowX)
                step += zones.cost(mz, mx, my) + danger.cost(mz, mx, my)
                if furn:
                    for bx in furn.get((int(mz // 4), int(mx // 4)), ()):
                        if bx[0] <= mz <= bx[2] and bx[1] <= mx <= bx[3]:
                            step += FURN_COST
                            break
                if step_pen:
                    step += step_pen.get((cur, nb), 0.0)
                if proven and (cur, nb) in proven:
                    step += CLIMB_BLOCK_COST
                if cpen:
                    step += cpen.get((cur, nb), 0.0)
                ng = g[cur] + step
                if ng < g.get(nb, 1e18):
                    g[nb] = ng
                    came[nb] = cur
                    portals[nb] = (pa, pb)
                    tie += 1
                    h = math.dist(c_nb, goal)
                    heapq.heappush(openq, (ng + h * HEURISTIC_WEIGHT, tie, nb))
        if not found:
            return None
        # corridor of portals from start to end
        chain = []
        steps = []
        n = e
        while n != s:
            chain.append(portals[n])
            steps.append(((came[n], n),) + tuple(portals[n]))
            n = came[n]
        chain.reverse()
        steps.reverse()
        self._last_steps = steps
        chain = [_shrink_portal(pa, pb, agent_radius) for pa, pb in chain]
        if STEEP_FOLLOW:
            for k, st_ in enumerate(steps):
                (a_, b_) = st_[0]
                if (float(self.tiles[a_[0]].pslope[a_[1]]) > STEEP_FOLLOW
                        or float(self.tiles[b_[0]].pslope[b_[1]]) > STEEP_FOLLOW):
                    pa, pb = chain[k]
                    w_ = math.hypot(pb[0] - pa[0], pb[2] - pa[2])
                    if w_ > STEEP_PORTAL:
                        chain[k] = _shrink_portal(pa, pb, (w_ - STEEP_PORTAL) / 2)
        pts = _funnel(np.array(sp), np.array(ep), chain)
        out = []
        for p in pts[1:]:
            w = rc_to_wow(*p)
            if not out or math.dist(out[-1], w) > 0.5:
                out.append(w)
        # remembered wedge spots also become boxes to walk round: a 1.5 yd
        # hole only removes polygons centred inside it, which on open
        # terrain (large polygons) removes nothing
        hole_boxes = [(h[0] - h[3] - DOODAD_CLEARANCE, h[1] - h[3] - DOODAD_CLEARANCE,
                       h[0] + h[3] + DOODAD_CLEARANCE, h[1] + h[3] + DOODAD_CLEARANCE, h[2])
                      for h in tuple(holes) + tuple(detours)]
        if CLEARANCE > 0:
            with swallow("navmesh.clear_path"):
                # furniture the mesh calls floor (vmap doodads, _doodad_boxes) is a wall to keep clear of too: its
                # real outline (the boxes carry DOODAD_CLEARANCE) at the path's height
                segs = []
                if out:
                    xs = [start[0]] + [p[0] for p in out]
                    ys = [start[1]] + [p[1] for p in out]
                    zs = sorted({round(z_ or 0.0) for z_ in [start[2]] + [p[2] for p in out]})
                    c = DOODAD_CLEARANCE
                    if max(xs) - min(xs) + max(ys) - min(ys) < 400:
                        for zz in zs[::max(1, len(zs) // 4)]:
                            for b in _doodad_boxes(min(xs), min(ys), max(xs), max(ys), zz):
                                segs.append((b[0] + c, b[1] + c, b[2] - c, b[3] - c, float(zz)))
                        # spawned gameobjects and teleport triggers (gobj.py): this branch returns without
                        # _avoid_doodads, so without them a path ran through Champions' Hall's portal door (SI:7 leg)
                        # and the Slaughtered Lamb's bonfire. One holding the start or the goal is left out (the
                        # chest we loot, the fire we cook at)
                        with swallow("navmesh.clear_gobj"):
                            import gobj
                            ends = (tuple(start), tuple(out[-1]))
                            for e in gobj.boxes_near(min(xs) - 2, min(ys) - 2, max(xs) + 2, max(ys) + 2):
                                if any(e[0] - c <= p[0] <= e[2] + c and e[1] - c <= p[1] <= e[3] + c for p in ends):
                                    continue
                                for zz in zs:
                                    if gobj.blocks_at(e, zz):
                                        segs.append((e[0], e[1], e[2], e[3], float(zz)))
                self._clear_boxes = segs
                try:
                    out = self.clear_path(start, out)
                finally:
                    self._clear_boxes = ()
                if GRID_INDOOR:
                    with swallow("navmesh.gridpath"):
                        import gridpath
                        gridpath.MESH = self
                        fails = []
                        ref = gridpath.refine([tuple(start)] + [tuple(p) for p in out], fails=fails)
                        # the coarse corridor goes through something solid (a railing the mesh calls open ground,
                        # Coldridge's Thalos road): make the corridor steps there expensive and plan again
                        rnd = getattr(self, "_grid_round", 0)
                        if fails and rnd < GRID_REPLANS:
                            pen = dict(step_pen or {})
                            added = 0
                            for fx, fy, fz in fails:
                                for st_ in steps:
                                    pa, pb = rc_to_wow(*st_[1]), rc_to_wow(*st_[2])
                                    if (abs((pa[2] + pb[2]) / 2 - fz) <= 4.0
                                            and _seg_dist2d(fx, fy, pa, pb) <= GRID_BLOCK_R):
                                        pen[st_[0]] = pen.get(st_[0], 0.0) + GRID_BLOCK_COST
                                        added += 1
                            if added:
                                self._grid_round = rnd + 1
                                try:
                                    p2 = self._find_path(start, end, avoid, dangers, agent_radius, max_nodes, holes,
                                                         budget, free_start, relax_soft, detours, grade_soft, pen)
                                finally:
                                    self._grid_round = rnd
                                if p2:
                                    return p2
                        out = [tuple(float(v) for v in p) for p in ref[1:]]
                with swallow("navmesh.detour_triggers"):
                    out = self._detour_triggers(start, out)
                return out                           # furniture handled here: no 0.7 yd detour corners on top
        return _avoid_doodads(start, out, extra_boxes=hole_boxes)

    # ------------------------------------------------------------ clearance
    def _walls(self, tile) -> dict:
        """Boundary edges (no neighbour) of a tile's usable polygons in WoW coords, grid-indexed: {(gx, gy): [seg]},
        seg = (ax, ay, az, bx, by, bz). 4 yd cells; built once per tile."""
        cache = self.__dict__.setdefault("_wall_cache", {})
        w = cache.get(tile.key)
        if w is not None:
            return w
        w = {}
        for i in range(len(tile.pcount)):
            if not tile.usable(i) or self._island((tile.key, int(i))):
                continue
            n = int(tile.pcount[i])
            for k in range(n):
                if tile.pneis[i, k] != 0:
                    continue
                a = tile.verts[tile.pverts[i, k]]
                b = tile.verts[tile.pverts[i, (k + 1) % n]]
                seg = (float(a[2]), float(a[0]), float(a[1]), float(b[2]), float(b[0]), float(b[1]))
                x0, x1 = sorted((seg[0], seg[3]))
                y0, y1 = sorted((seg[1], seg[4]))
                for gx in range(int(math.floor(x0 / 4)), int(math.floor(x1 / 4)) + 1):
                    for gy in range(int(math.floor(y0 / 4)), int(math.floor(y1 / 4)) + 1):
                        w.setdefault((gx, gy), []).append(seg)
        cache[tile.key] = w
        return w

    def walls_near(self, x, y, z, r):
        rx, ry, rz = wow_to_rc(x, y, z)
        t = self.tile_at(rx, rz)
        out = []
        if t is None:
            return out
        w = self._walls(t)
        for gx in range(int(math.floor((x - r) / 4)), int(math.floor((x + r) / 4)) + 1):
            for gy in range(int(math.floor((y - r) / 4)), int(math.floor((y + r) / 4)) + 1):
                for sg in w.get((gx, gy), ()):
                    if min(abs(sg[2] - z), abs(sg[5] - z)) <= CLEAR_DZ or (sg[2] - z) * (sg[5] - z) < 0:
                        out.append(sg)
        return out

    def clear_path(self, start, pts, clear=None):
        """Push a planned path (WoW points, `start` excluded) off the mesh's walls to `clear` yd (CLEARANCE): densify
        to CLEAR_STEP, move each inner point along the summed push of the walls closer than `clear` (a point between
        two walls settles in the middle), keep it on the mesh, then drop the points a straight line does not need."""
        clear = CLEARANCE if clear is None else clear
        if len(pts) < 1:
            return pts
        seq = [tuple(start)] + [tuple(p) for p in pts]
        dense = [seq[0]]
        for a, b in zip(seq, seq[1:]):
            d = math.dist(a[:2], b[:2])
            n = max(1, int(d / CLEAR_STEP))
            for i in range(1, n + 1):
                f = i / n
                dense.append((a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f,
                              (a[2] or 0.0) + ((b[2] or 0.0) - (a[2] or 0.0)) * f))
        moved = False
        pts2 = [list(p) for p in dense]
        # pushes are sideways to the path only: a push along it let neighbouring points overtake each other in a
        # doorway pinched by walls and furniture, folding the path back and forth (Trade District shop door: the route
        # went south, north, south again over 6 yd and wedged the bot there both ways)
        tang = [None] * len(dense)
        for i in range(1, len(dense) - 1):
            tx, ty = dense[i + 1][0] - dense[i - 1][0], dense[i + 1][1] - dense[i - 1][1]
            tl = math.hypot(tx, ty)
            tang[i] = (tx / tl, ty / tl) if tl > 1e-6 else None
        box_side = {}                        # furniture box -> the side this path passes it on (decided once)
        last_side = None                     # (side, dense index) of the last box decision: a fence of boxes in a row
        for _ in range(CLEAR_ITERS):
            any_push = False
            for i in range(1, len(pts2) - 1):
                px, py, pz = pts2[i]
                fx = fy = 0.0
                for sg in self.walls_near(px, py, pz, clear):
                    ax, ay, bx, by = sg[0], sg[1], sg[3], sg[4]
                    dx, dy = bx - ax, by - ay
                    L = dx * dx + dy * dy
                    u = 0.0 if L == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L))
                    cx, cy = ax + u * dx, ay + u * dy
                    d = math.hypot(px - cx, py - cy)
                    if 1e-3 < d < clear:
                        fx += (px - cx) / d * (clear - d)
                        fy += (py - cy) / d * (clear - d)
                fcl = max(clear, FURN_CLEAR)
                for x0, y0, x1, y1, bz in getattr(self, "_clear_boxes", ()):
                    if abs(bz - pz) > CLEAR_DZ or px < x0 - fcl or px > x1 + fcl or py < y0 - fcl or py > y1 + fcl:
                        continue
                    if x0 <= px <= x1 and y0 <= py <= y1:
                        # inside the furniture: out by the side with the most room (the open aisle, not the gap to the
                        # next table), the same side for every point of this path
                        key = (x0, y0, x1, y1)
                        sd = box_side.get(key)
                        if sd is None and last_side is not None and abs(i - last_side[1]) <= SIDE_KEEP:
                            # the next box of a row (fence segments): pass it on the side the path took past the one
                            # before - per-box choices alternated and slalomed the path through the Amberstill fence
                            # (rxp7 02:32:59)
                            ux, uy = last_side[0]
                            tx, ty = dense[i + 1][0] - dense[i - 1][0], dense[i + 1][1] - dense[i - 1][1]   # the original line's way
                            tl = math.hypot(tx, ty) or 1.0
                            if abs(ux * tx + uy * ty) / tl <= 0.7:
                                sd = box_side[key] = last_side[0]
                        if sd is None:
                            best = None
                            tx, ty = dense[i + 1][0] - dense[i - 1][0], dense[i + 1][1] - dense[i - 1][1]   # the original line's way
                            tl = math.hypot(tx, ty) or 1.0
                            for depth, ux, uy in (((px - x0), -1, 0), ((x1 - px), 1, 0), ((py - y0), 0, -1),
                                                  ((y1 - py), 0, 1)):
                                if abs(ux * tx + uy * ty) / tl > 0.7:
                                    continue                 # out sideways, never along the way we walk
                                ex, ey = px + ux * (depth + fcl), py + uy * (depth + fcl)
                                room = min(self._wall_d(ex, ey, pz, fcl + 1.0), fcl + 1.0)
                                score = room - 0.15 * depth
                                if best is None or score > best[0]:
                                    best = (score, ux, uy)
                            if best is None:
                                continue
                            sd = box_side[key] = (best[1], best[2])
                        last_side = (sd, i)
                        depth = {(-1, 0): px - x0, (1, 0): x1 - px, (0, -1): py - y0, (0, 1): y1 - py}[sd]
                        fx += sd[0] * (depth + fcl)
                        fy += sd[1] * (depth + fcl)
                        continue
                    cx, cy = min(max(px, x0), x1), min(max(py, y0), y1)
                    d = math.hypot(px - cx, py - cy)
                    sd = box_side.get((x0, y0, x1, y1))
                    if sd is not None and d < fcl and (
                            (sd[0] and (px - (x0 + x1) / 2) * sd[0] < 0) or (sd[1] and (py - (y0 + y1) / 2) * sd[1] < 0)):
                        # beside a box this path passes on the other side: over to that side too, not away from the
                        # box's near face (that sent the points beside a fence one way and the ones in it the other:
                        # a slalom through the Amberstill fence, rxp7 02:32:59)
                        depth = {(-1, 0): px - x0, (1, 0): x1 - px, (0, -1): py - y0, (0, 1): y1 - py}[sd]
                        fx += sd[0] * (depth + fcl)
                        fy += sd[1] * (depth + fcl)
                        continue
                    if 1e-3 < d < fcl:
                        fx += (px - cx) / d * (fcl - d)
                        fy += (py - cy) / d * (fcl - d)
                if tang[i] is not None:
                    along = fx * tang[i][0] + fy * tang[i][1]
                    fx, fy = fx - along * tang[i][0], fy - along * tang[i][1]
                m = math.hypot(fx, fy)
                if m < 0.05:
                    continue
                if m > 0.8:
                    fx, fy = fx / m * 0.8, fy / m * 0.8
                nx, ny = px + fx, py + fy
                # never through a wall: a stair's side edge (the cellar floor below it) is a mesh wall too, and pushes
                # crossed it onto the floor 1-2 yd down beside the Thunderbrew cellar stairs (rxp4 01:41:12)
                if any(_cross2d(px, py, nx, ny, sg[0], sg[1], sg[3], sg[4])
                       for sg in self.walls_near(px, py, pz, math.hypot(fx, fy) + 0.1)):
                    continue
                node, rc = self.nearest_poly(nx, ny, pz, max_dist=0.5)
                if node is None:
                    continue
                w = rc_to_wow(*rc)
                if math.hypot(w[0] - nx, w[1] - ny) > 0.3 or abs(w[2] - pz) > 2.0:
                    continue                         # off the mesh (or onto another floor): stay
                pts2[i] = [nx, ny, w[2]]
                any_push = moved = True
            if not any_push:
                break
        if not moved:
            return pts
        # smooth the pushed points a little (pushes alone leave kinks), keeping the ends
        for _ in range(2):
            for i in range(1, len(pts2) - 1):
                a, b, c = pts2[i - 1], pts2[i], pts2[i + 1]
                nb_ = [b[0] * 0.5 + (a[0] + c[0]) * 0.25, b[1] * 0.5 + (a[1] + c[1]) * 0.25, b[2]]
                if self._wall_d(nb_[0], nb_[1], nb_[2], clear) >= self._wall_d(b[0], b[1], b[2], clear) - 0.05:
                    pts2[i] = nb_                    # smoothing never brings a point closer to a wall
        # no folds: a pushed point must lie further along the original line than the one before it (pushes from walls
        # on both sides of a stair turn moved neighbours past each other: a 2 yd hairpin at the Thunderbrew Distillery
        # cellar stairs, rxp4 01:41:12, the bot wedged in it twice)
        cum = [0.0]
        for a_, b_ in zip(dense, dense[1:]):
            cum.append(cum[-1] + math.dist(a_[:2], b_[:2]))

        def along(i, q):
            best, bt = None, 0.0
            for j in range(max(0, i - FOLD_WIN), min(len(dense) - 1, i + FOLD_WIN)):
                ax, ay = dense[j][0], dense[j][1]
                dx, dy = dense[j + 1][0] - ax, dense[j + 1][1] - ay
                L2 = dx * dx + dy * dy
                u = 0.0 if L2 == 0 else max(0.0, min(1.0, ((q[0] - ax) * dx + (q[1] - ay) * dy) / L2))
                d = math.hypot(q[0] - ax - u * dx, q[1] - ay - u * dy)
                if best is None or d < best:
                    best, bt = d, cum[j] + u * math.sqrt(L2)
            return bt
        kept, last_t = [pts2[0]], 0.0
        for i in range(1, len(pts2) - 1):
            t = along(i, pts2[i])
            if t > last_t + 0.05:
                kept.append(pts2[i])
                last_t = t
        kept.append(pts2[-1])
        pts2 = kept
        # simplify: keep a point when the straight line past it would come closer to a wall than it does
        out = []
        k = 0
        while k < len(pts2) - 1:
            j = len(pts2) - 1
            while j > k + 1 and not self._line_clear(pts2[k], pts2[j], pts2[k + 1:j], clear):
                j -= 1
            out.append(tuple(pts2[j]))
            k = j
        return out

    def _wall_d(self, x, y, z, r) -> float:
        """Distance to the nearest mesh wall within r (r when none)."""
        best = r
        for x0, y0, x1, y1, bz in getattr(self, "_clear_boxes", ()):
            if abs(bz - z) <= CLEAR_DZ:
                best = min(best, math.hypot(max(x0 - x, 0.0, x - x1), max(y0 - y, 0.0, y - y1)))
        for sg in self.walls_near(x, y, z, r):
            ax, ay, bx, by = sg[0], sg[1], sg[3], sg[4]
            dx, dy = bx - ax, by - ay
            L = dx * dx + dy * dy
            u = 0.0 if L == 0 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / L))
            best = min(best, math.hypot(x - ax - u * dx, y - ay - u * dy))
        return best

    def _on_mesh_line(self, a, b, need=None) -> bool:
        """Every TRIG_STEP yd of a -> b stands on the mesh, need (TRIG_WALL) yd off its walls."""
        need = TRIG_WALL if need is None else need
        L = math.dist(a[:2], b[:2])
        n = max(1, int(math.ceil(L / TRIG_STEP)))
        for i in range(n + 1):
            f = i / n
            x, y, z = a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, (a[2] or 0.0) + ((b[2] or 0.0) - (a[2] or 0.0)) * f
            pk, rc = self.nearest_poly(x, y, z, max_dist=2.0)
            if pk is None:
                return False
            tl = self.tiles[pk[0]]
            vs = tl.verts[tl.pverts[pk[1], :tl.pcount[pk[1]]]]
            if not _point_in_poly_2d(rc[0], rc[2], vs) or abs(poly_height(rc[0], rc[2], vs) - rc[1]) > 3.0:
                return False
            if self._wall_d(x, y, poly_height(rc[0], rc[2], vs), need + 0.1) < need:
                return False
        return True

    def _detour_triggers(self, start, out):
        """Teleport triggers are hard walls (walking into one loads another map: the SI:7 leg ended in Champions'
        Hall). Each leg that crosses one (its box grown by TRIG_PAD) is bent round the box's corners (grown by
        TRIG_CORNER): the shortest one-corner or two-corner detour whose legs stay on the mesh and out of the box.
        A trigger holding the start or the goal is left alone (we were sent there)."""
        import gobj
        if not out:
            return out
        path = [tuple(start)] + [tuple(p) for p in out]
        xs, ys = [p[0] for p in path], [p[1] for p in path]
        trig = [e for e in gobj.boxes_near(min(xs) - 5, min(ys) - 5, max(xs) + 5, max(ys) + 5)
                if len(e) > 7 and str(e[6]).startswith("teleport trigger")]
        if not trig:
            return out

        inside = trigger_inside

        def seg_hits(e, a, b, pad):
            L = math.dist(a[:2], b[:2])
            n = max(1, int(math.ceil(L / 0.4)))
            return any(inside(e, a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n,
                              (a[2] or 0.0) + ((b[2] or 0.0) - (a[2] or 0.0)) * i / n, pad) for i in range(n + 1))

        def corners(e, pad):
            box, cx, cy, _cz, q, s = e[7]
            yaw = 2.0 * math.atan2(q[2], q[3])
            c, sn = math.cos(yaw), math.sin(yaw)
            pts = []
            for u, v in ((box[0] - pad, box[1] - pad), (box[3] + pad, box[1] - pad), (box[3] + pad, box[4] + pad),
                         (box[0] - pad, box[4] + pad)):
                pts.append((cx + u * c - v * sn, cy + u * sn + v * c))
            return pts                                   # in order round the box: k and k+1 are neighbours

        ends = (path[0], path[-1])
        trig = [e for e in trig if not any(inside(e, p[0], p[1], p[2] or 0.0, 0.0) for p in ends)]
        fixes, i = 0, 0
        while i < len(path) - 1 and fixes < 4 * len(trig) + 4:
            a, b = path[i], path[i + 1]
            e = next((e for e in trig if seg_hits(e, a, b, TRIG_PAD)), None)
            if e is None:
                i += 1
                continue
            cs = corners(e, TRIG_CORNER)
            za = lambda x, y: _z_along(a, b, x, y)       # noqa: E731
            opts = [[c] for c in cs] + [[cs[k], cs[(k + 1) % 4]] for k in range(4)] +                    [[cs[(k + 1) % 4], cs[k]] for k in range(4)]
            best = None
            for o in opts:
                seq = [a] + [(x, y, za(x, y)) for x, y in o] + [b]
                if any(seg_hits(e, p, q, TRIG_PAD * 0.5) for p, q in zip(seq, seq[1:])):
                    continue
                L = sum(math.dist(p[:2], q[:2]) for p, q in zip(seq, seq[1:]))
                if best is not None and L >= best[0]:
                    continue
                if all(self._on_mesh_line(p, q) for p, q in zip(seq, seq[1:])):
                    best = (L, seq[1:-1])
            fixes += 1
            if best is None:
                swallowed("navmesh.trigger_no_detour", RuntimeError(f"no on-mesh way round {e[6]}"))
                i += 1
                continue
            path[i + 1:i + 1] = best[1]
        return path[1:]

    def _line_clear(self, a, b, between, clear) -> bool:
        """The straight line a -> b keeps the clearance of the pushed points it would replace (less 0.15 yd, at most
        `clear`), sampled every CLEAR_STEP yd against the walls."""
        L = math.dist(a[:2], b[:2])
        if L < 1e-6:
            return True
        dx, dy = b[0] - a[0], b[1] - a[1]
        for p in between:                    # a shortcut never strays far from the path it replaces
            u = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (L * L)))
            if math.hypot(p[0] - (a[0] + u * dx), p[1] - (a[1] + u * dy)) > 1.0:
                return False
        need = max(0.3, min([clear] + [self._wall_d(p[0], p[1], p[2], clear) for p in between]) - 0.15)
        n = max(1, int(L / CLEAR_STEP))
        for i in range(1, n):
            f = i / n
            x, y, z = a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f
            if self._wall_d(x, y, z, clear) < need:
                return False
        return True


# --- furniture the mesh calls floor -----------------------------------------
# The generator fuses anything up to 1.75 yd tall standing on a building floor
# (inn tables, chairs, kegs) into the floor polygon, and outdoors it leaves
# only a 0.27 yd margin round barrels and fence posts. So after straightening
# a path, walk it against the M2 model boxes from the vmap files and bend it
# round the ones it cuts through.
DOODAD_FIXES_MIN = 12
SIDE_KEEP = 12        # clear_path: dense points within which the next box reuses the last box's side (a fence row)
FOLD_WIN = 8          # clear_path: dense points either side searched for a pushed point's place on the original line
TRIG_PAD = 1.0                  # yd round a teleport trigger's box a path may not cross
TRIG_CORNER = 1.6               # yd round it the detour corners stand
TRIG_STEP = 0.5                 # yd sampling of a detour leg's on-mesh test
TRIG_WALL = 0.3                 # yd a detour leg keeps off the mesh walls
DOODAD_FIXES_PER = 2.0
DOODAD_CLEARANCE = 0.7          # player capsule + a little
DOODAD_MAX_HEIGHT = 9.0         # a Westfall scarecrow (1 x 3.8 yd) and an outhouse (3.3 x 5)
                                # are NOT carved out of the navmesh and wedged the character;
                                # trees still drop out through the width filter (their canopy)


def _doodad_boxes(x0, y0, x1, y1, z):
    import vmap
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    r = math.dist((x0, y0), (x1, y1)) / 2 + 4
    out = []
    for sp in vmap.spawns_near(mx, my, r):
        if not sp.m2 or not (0.3 <= sp.height <= DOODAD_MAX_HEIGHT):
            continue
        if sp.width > 6 or not (z - 1.5 <= sp.lo[2] <= z + 1.5):
            continue
        c = DOODAD_CLEARANCE
        out.append((sp.lo[0] - c, sp.lo[1] - c, sp.hi[0] + c, sp.hi[1] + c))
    return _merge_boxes(out)


MERGE_CAP = 6.0     # yd: largest merged doodad box


def _merge_boxes(boxes):
    """Overlapping boxes (a table with its chairs, jars on a shelf) become
    one: a detour corner of one box used to land inside the next, and the
    path fixer inserted the same corner over and over at the inn stairs."""
    boxes = list(boxes)
    changed = True
    while changed:
        changed = False
        out = []
        for b in boxes:
            for i, o in enumerate(out):
                if b[0] <= o[2] and o[0] <= b[2] and b[1] <= o[3] and o[1] <= b[3]:
                    u = (min(b[0], o[0]), min(b[1], o[1]), max(b[2], o[2]), max(b[3], o[3]))
                    # only while the union stays furniture-sized: the inn's
                    # tables, chairs and planters fused into one 17x26 yd box
                    # that swallowed the stair foot, so no detour was possible
                    if u[2] - u[0] <= MERGE_CAP and u[3] - u[1] <= MERGE_CAP:
                        out[i] = u
                        changed = True
                        break
            else:
                out.append(b)
        boxes = out
    return boxes


def _seg_hits_rect(a, b, r) -> bool:
    """2D segment a-b crosses the axis-aligned rect r (Liang-Barsky)."""
    x0, y0, x1, y1 = r
    dx, dy = b[0] - a[0], b[1] - a[1]
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, a[0] - x0), (dx, x1 - a[0]), (-dy, a[1] - y0), (dy, y1 - a[1])):
        if p == 0:
            if q < 0:
                return False
            continue
        t = q / p
        if p < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return False
    return True


def _z_along(a, b, x, y) -> float:
    """Height of the 3D segment a-b at the projection of (x, y) onto it."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    n2 = dx * dx + dy * dy
    t = 0.0 if n2 == 0 else max(0.0, min(1.0, ((x - a[0]) * dx + (y - a[1]) * dy) / n2))
    return a[2] + t * (b[2] - a[2])


_trig_all = None


def teleport_boxes():
    """All teleport-trigger boxes of map 0 (gobj.py); [] without the data."""
    global _trig_all
    if _trig_all is None:
        try:
            import gobj
            g = gobj._load()
            seen, out = set(), []
            for v in g.values():
                for e in v:
                    if id(e) not in seen and len(e) > 7 and str(e[6]).startswith("teleport trigger"):
                        seen.add(id(e))
                        out.append(e)
            _trig_all = out
        except Exception as ex:                      # noqa: BLE001
            swallowed("navmesh.teleport_boxes", ex)
            _trig_all = []
    return _trig_all


def trigger_inside(e, x, y, z, pad) -> bool:
    """(x, y, z) inside teleport-trigger box e (gobj entry with its OBB) grown by pad yd (the height band loosely)."""
    if not (e[4] - 2.0 <= z <= e[5] + 0.5):
        return False
    box, cx, cy, _cz, q, s = e[7]
    yaw = 2.0 * math.atan2(q[2], q[3])
    dx, dy = x - cx, y - cy
    c, sn = math.cos(-yaw), math.sin(-yaw)
    u, v = dx * c - dy * sn, dx * sn + dy * c
    return box[0] - pad <= u <= box[3] + pad and box[1] - pad <= v <= box[4] + pad


def _avoid_doodads(start, pts, max_fixes=None, extra_boxes=()):
    """extra_boxes: (x0, y0, x1, y1, z) boxes from remembered wedge spots
    (not "extra": the detour-length local below shadowed it and crashed).
    max_fixes: detour points inserted at most; default DOODAD_FIXES_MIN or DOODAD_FIXES_PER per input point (a
    200 yd Stormwind route spent a flat 12 on chairs and kept a leg through Champions' Hall's portal door)."""
    if max_fixes is None:
        max_fixes = max(DOODAD_FIXES_MIN, int(DOODAD_FIXES_PER * (len(pts) + 1)))
    try:
        import vmap  # noqa: F401  (missing vmaps: keep the plain path)
    except Exception as e:
        swallowed("navmesh.avoid_doodads_vmap", e)
        vmap = None
    try:
        import gobj
    except Exception as e:
        swallowed("navmesh.avoid_doodads_gobj", e)
        gobj = None
    path = [tuple(start)] + [tuple(p) for p in pts]
    fixes = 0
    i = 0
    while i < len(path) - 1 and fixes < max_fixes:
        a, b = path[i], path[i + 1]
        z = min(a[2], b[2])
        boxes = _doodad_boxes(a[0], a[1], b[0], b[1], z) if vmap else []
        # height test against the segment's height AT the box too, not only its lower end: a 40 yd leg down the
        # Anvilmar slope has its low end 10+ yd under a box half way along (nb Felix -> Talin Keeneye, (-6171,397))
        near = [e[:4] for e in extra_boxes
                if min(a[0], b[0]) - 5 <= e[0] <= max(a[0], b[0]) + 5
                and min(a[1], b[1]) - 5 <= e[1] <= max(a[1], b[1]) + 5
                and (abs(e[4] - z) < 3.0 or abs(e[4] - _z_along(a, b, (e[0] + e[2]) / 2, (e[1] + e[3]) / 2)) < 3.0)]
        # spawned gameobjects (campfires, crates, carts, ore veins): not in the mesh at all (gobj.py). One we walk
        # to or stand in (a chest to loot, a campfire to cook at) is skipped: no detour can reach it
        go = []
        if gobj is not None:
            c = DOODAD_CLEARANCE
            for e in gobj.boxes_near(min(a[0], b[0]) - 2, min(a[1], b[1]) - 2, max(a[0], b[0]) + 2,
                                     max(a[1], b[1]) + 2):
                if not gobj.blocks_at(e, _z_along(a, b, (e[0] + e[2]) / 2, (e[1] + e[3]) / 2)):
                    continue
                r = (e[0] - c, e[1] - c, e[2] + c, e[3] + c)
                if any(r[0] <= p[0] <= r[2] and r[1] <= p[1] <= r[3] for p in (a, b, path[-1])):
                    continue
                go.append(r)
        if near or go:
            boxes = _merge_boxes(list(boxes) + near + go)
        hit = next((r for r in boxes if _seg_hits_rect(a, b, r)), None)
        if hit is None:
            i += 1
            continue
        x0, y0, x1, y1 = hit
        best = None
        for cx, cy in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
            inner = (x0 + 0.05, y0 + 0.05, x1 - 0.05, y1 - 0.05)
            if _seg_hits_rect(a, (cx, cy), inner) or _seg_hits_rect((cx, cy), b, inner):
                continue
            extra = math.dist(a[:2], (cx, cy)) + math.dist((cx, cy), b[:2]) - math.dist(a[:2], b[:2])
            if best is None or extra < best[0]:
                best = (extra, (cx, cy, _z_along(a, b, cx, cy)))     # not b's height: the leg can slope
        if best is None:
            # a leg through the middle of a box (in one side, out the opposite one) sees no single corner: go round
            # one side by its two corners (a signpost on the Stormwind road: the plain detour gave up)
            inner = (x0 + 0.05, y0 + 0.05, x1 - 0.05, y1 - 0.05)
            for p, q in (((x0, y0), (x1, y0)), ((x0, y1), (x1, y1)), ((x0, y0), (x0, y1)), ((x1, y0), (x1, y1))):
                for u, v in ((p, q), (q, p)):
                    if (_seg_hits_rect(a, u, inner) or _seg_hits_rect(u, v, inner)
                            or _seg_hits_rect(v, b, inner)):
                        continue
                    extra = math.dist(a[:2], u) + math.dist(u, v) + math.dist(v, b[:2]) - math.dist(a[:2], b[:2])
                    if best is None or extra < best[0]:
                        best = (extra, (u[0], u[1], _z_along(a, b, *u)), (v[0], v[1], _z_along(a, b, *v)))
            if best is not None and len(best) == 3:
                if math.dist(best[1][:2], a[:2]) < 0.3 or math.dist(best[2][:2], b[:2]) < 0.3:
                    i += 1
                    continue
                path[i + 1:i + 1] = [best[1], best[2]]
                fixes += 1
                continue
        if best is None or math.dist(best[1][:2], a[:2]) < 0.3 or math.dist(best[1][:2], b[:2]) < 0.3:
            i += 1
            continue
        path.insert(i + 1, best[1])
        fixes += 1
    return path[1:]


class _ZoneIndex:
    """Circles (WoW x, y, z, r) bucketed on a 32 yd grid for fast lookups."""
    CELL = 32.0

    def __init__(self, circles, cost):
        # a circle may carry its own cost as a 5th element (nb: evidence-weighted wedge spots); else `cost`
        self.cost_per_hit = cost
        self.grid: dict[tuple[int, int], list] = {}
        for circ in circles:
            x, y, z, r = circ[:4]
            hc = float(circ[4]) if len(circ) > 4 else cost
            c = self.CELL
            for gx in range(int((x - r) // c), int((x + r) // c) + 1):
                for gy in range(int((y - r) // c), int((y + r) // c) + 1):
                    self.grid.setdefault((gx, gy), []).append((x, y, z, r, hc))

    def cost(self, x, y, z) -> float:
        cell = self.grid.get((int(x // self.CELL), int(y // self.CELL)))
        if not cell:
            return 0.0
        for cx, cy, cz, r, hc in cell:
            if abs(z - cz) < 15 and (x - cx) ** 2 + (y - cy) ** 2 < r * r:
                return hc
        return 0.0

    def near(self, x, y, within) -> list:
        """The circles bucketed within `within` yd of (x, y) - a superset of
        those actually that close, cheap enough to test one polygon against."""
        c = self.CELL
        out = []
        for gx in range(int((x - within) // c), int((x + within) // c) + 1):
            for gy in range(int((y - within) // c), int((y + within) // c) + 1):
                cell = self.grid.get((gx, gy))
                if cell:
                    out.extend(cell)
        return out


def _shrink_portal(a, b, r):
    """Pull both ends of a portal inward by r (collapse to the middle if narrow)."""
    d = b - a
    n = math.hypot(d[0], d[2])
    if n <= 2 * r:
        m = (a + b) / 2
        return m, m.copy()
    k = r / n
    return a + d * k, b - d * k


def poly_height(px, pz, vs) -> float:
    """Height of polygon vs at (px, pz): its best-fit plane there, kept within the polygon's own heights. A sloped
    polygon's vertex mean is the height of its middle only (Coldridge's Spirit Stone hill: one polygon 404.9-429.2,
    mean 419.6, read as the floor all the way down it)."""
    vs = np.asarray(vs, float)
    lo, hi = float(vs[:, 1].min()), float(vs[:, 1].max())
    if hi - lo < 0.05 or len(vs) < 3:
        return float(vs[:, 1].mean())
    A = np.c_[vs[:, 0], vs[:, 2], np.ones(len(vs))]
    try:
        c = np.linalg.lstsq(A, vs[:, 1], rcond=None)[0]
    except np.linalg.LinAlgError:
        return float(vs[:, 1].mean())
    return min(hi, max(lo, float(c[0] * px + c[1] * pz + c[2])))


def _point_in_poly_2d(px, pz, vs) -> bool:
    inside = False
    n = len(vs)
    j = n - 1
    for i in range(n):
        xi, zi = vs[i, 0], vs[i, 2]
        xj, zj = vs[j, 0], vs[j, 2]
        if (zi > pz) != (zj > pz) and px < (xj - xi) * (pz - zi) / (zj - zi) + xi:
            inside = not inside
        j = i
    return inside


def _tri2(a, b, c) -> float:
    """Twice the signed area of abc in the x/z plane."""
    return (c[0] - a[0]) * (b[2] - a[2]) - (b[0] - a[0]) * (c[2] - a[2])


def _funnel(start, end, portals):
    """Simple stupid funnel algorithm (Mononen) over the portal corridor."""
    # Portals are edges of the polygon we are leaving, in Detour's vertex
    # order, which makes the first vertex the left side (getPortalPoints).
    pts = [(start, start)] + [(a, b) for a, b in portals] + [(end, end)]

    path = [start]
    apex, left, right = start, pts[0][0], pts[0][1]
    ai = li = ri = 0
    i = 1
    while i < len(pts):
        pl, pr = pts[i]
        # tighten right side
        if _tri2(apex, right, pr) <= 0.0:
            if np.allclose(apex, right) or _tri2(apex, left, pr) > 0.0:
                right, ri = pr, i
            else:
                path.append(left)
                apex, ai = left, li
                left, right = apex, apex
                li = ri = ai
                i = ai + 1
                continue
        # tighten left side
        if _tri2(apex, left, pl) >= 0.0:
            if np.allclose(apex, left) or _tri2(apex, right, pl) < 0.0:
                left, li = pl, i
            else:
                path.append(right)
                apex, ai = right, ri
                left, right = apex, apex
                li = ri = ai
                i = ai + 1
                continue
        i += 1
    if not np.allclose(path[-1], end):
        path.append(end)
    return path
