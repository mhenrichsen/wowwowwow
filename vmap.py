"""Read server vmap tiles: which models (fences, barrels, tables, houses)
stand where. Used to explain why the navmesh wedges us and to keep paths
clear of doodads the mesh calls walkable.

Two formats are read; read_tile() picks by the file magic.

NEW - TrinityCore "forever" branch (client 70338), D:\\tcforever\\server\\data\\vmaps
(src/common/Collision/Models/ModelInstance.cpp ModelSpawn::readFromFile,
src/tools/vmap4_assembler/TileAssembler.cpp convertMap):
  <dir>/<map:04>/<map:04>_<tx:02>_<ty:02>.vmtile, tx = floor(32 - wx/533.33),
  ty = floor(32 - wy/533.33). "VMAP_4.E", u32 count, then per spawn
  u8 flags, u8 adtId, u32 uniqueId, 3f pos, 3f rot, f scale,
  [6f bound lo/hi if flags & 1 - the assembler sets it on every spawn,
  M2s included], u32 nameLen, name. No trailing tree index (that lives in
  the .vmtileidx). Flags: 1 HAS_BOUND, 2 PARENT_SPAWN, 4 PATH_ONLY
  (destructible WMO). A spawn is listed in every tile its box overlaps.
  WMO doodads are their own spawns, like the old data.
  Names are FileDataIDs ("FILE0001B9FD.xxx"); vmap_names.tsv (built by
  `python vmap.py --build-names` from the community listfile) maps them to
  "Wet_Inn.wmo"-style names and the M2/WMO kind. Unknown ids fall back to the
  model's .vmo header (WMOD chunk flags & 1 = IsM2) and keep the FILE name.

OLD - VMaNGOS-style flat dir (wow-forever-server), "VMAP_7.0":
  <dir>/<map:03>_<ty:02>_<tx:02>.vmtile, per spawn u32 flags (1 M2,
  4 HAS_BOUND), u16 adtId, u32 id, 3f pos, 3f rot, f scale, [6f bounds],
  u32 nameLen, name, u32 treeIndex.

Both use the assembler's coordinates: wow_x = 17066.666 - x,
wow_y = 17066.666 - y, z unchanged.
"""
import math
import os
import struct

HERE = os.path.dirname(os.path.abspath(__file__))
NEW_VMAP_DIR = r"D:\tcforever\server\data\vmaps"
OLD_VMAP_DIR = os.path.join(HERE, "..", "wow-forever-server", "server", "data", "vmaps")
VMAP_DIR = NEW_VMAP_DIR if os.path.isdir(NEW_VMAP_DIR) else OLD_VMAP_DIR
NAMES_FILE = os.path.join(HERE, "vmap_names.tsv")
LISTFILE = r"D:\wowserver\dl\listfile.csv"
OFF = 17066.666
TILE = 533.3333
MOD_M2 = 1            # old format
MOD_HAS_BOUND = 4     # old format
TC_HAS_BOUND = 1
TC_PARENT_SPAWN = 2
TC_PATH_ONLY = 4


class Spawn:
    __slots__ = ("name", "m2", "x", "y", "z", "lo", "hi", "scale", "uid", "rot", "raw", "root")

    def __init__(self, name, m2, x, y, z, lo, hi, scale, uid=None, rot=None, raw=None, root=None):
        self.name, self.m2, self.x, self.y, self.z = name, m2, x, y, z
        self.lo, self.hi, self.scale = lo, hi, scale   # WoW-space AABB (x, y, z)
        self.uid = uid
        self.rot, self.raw, self.root = rot, raw, root  # assembler rotation (deg), model file name, vmap dir (vgeom.py)

    @property
    def height(self):
        return self.hi[2] - self.lo[2]

    @property
    def width(self):
        return min(self.hi[0] - self.lo[0], self.hi[1] - self.lo[1])

    def dist2d(self, x, y):
        dx = max(self.lo[0] - x, 0, x - self.hi[0])
        dy = max(self.lo[1] - y, 0, y - self.hi[1])
        return math.hypot(dx, dy)

    def __repr__(self):
        return (f"Spawn({self.name!r}, {'M2' if self.m2 else 'WMO'}, "
                f"x {self.lo[0]:.1f}..{self.hi[0]:.1f} y {self.lo[1]:.1f}..{self.hi[1]:.1f} "
                f"z {self.lo[2]:.1f}..{self.hi[2]:.1f})")


_is_new: dict[str, bool] = {}


def is_new_dir(vmap_dir) -> bool:
    k = _is_new.get(vmap_dir)
    if k is None:
        k = _is_new[vmap_dir] = os.path.isfile(os.path.join(vmap_dir, "0000", "0000.vmtree"))
    return k


def tile_name(map_id, wx, wy, vmap_dir=None):
    """Tile file path relative to vmap_dir (new layout unless vmap_dir is old)."""
    a = math.floor(32 - wy / TILE)
    b = math.floor(32 - wx / TILE)
    if vmap_dir is None or is_new_dir(vmap_dir):
        return f"{map_id:04d}/{map_id:04d}_{b:02d}_{a:02d}.vmtile"
    return f"{map_id:03d}_{a:02d}_{b:02d}.vmtile"


# --- FileDataID -> name / kind ------------------------------------------------

def _pretty(base):
    """'wet_inn.wmo' -> 'Wet_Inn.wmo', the old extractor's spelling."""
    out, up = [], True
    for ch in base.lower():
        out.append(ch.upper() if up else ch)
        up = ch in "_ "
    return "".join(out)


_names = None          # fid -> (name, m2)
_vmo_kind: dict[str, bool] = {}


def _load_names():
    global _names
    if _names is None:
        _names = {}
        try:
            with open(NAMES_FILE, encoding="utf-8") as f:
                for line in f:
                    p = line.rstrip("\n").split("\t")
                    if len(p) == 3:
                        _names[int(p[0])] = (p[1], p[2] == "1")
        except OSError:
            pass
    return _names


def _vmo_is_m2(root, raw):
    k = _vmo_kind.get(raw)
    if k is None:
        k = False
        try:
            with open(os.path.join(root, raw + ".vmo"), "rb") as f:
                h = f.read(20)
            if h[8:12] == b"WMOD":
                k = bool(struct.unpack_from("<I", h, 16)[0] & 1)
        except OSError:
            pass
        _vmo_kind[raw] = k
    return k


def _resolve(raw, root):
    if raw.startswith("FILE") and raw.endswith(".xxx"):
        try:
            fid = int(raw[4:12], 16)
        except ValueError:
            fid = None
        hit = _load_names().get(fid)
        if hit:
            return hit
        return raw, _vmo_is_m2(root, raw)
    return raw, raw.lower().endswith((".m2", ".mdx"))


def build_names(vmap_dir=NEW_VMAP_DIR, listfile=LISTFILE, out=NAMES_FILE):
    """Write vmap_names.tsv for every model the new vmaps reference."""
    want = {}
    for fn in os.listdir(vmap_dir):
        if fn.startswith("FILE") and fn.endswith(".xxx.vmo"):
            want[int(fn[4:12], 16)] = fn[:-4]
    found = {}
    with open(listfile, encoding="utf-8", errors="replace") as f:
        for line in f:
            i = line.find(";")
            if i <= 0:
                continue
            try:
                fid = int(line[:i])
            except ValueError:
                continue
            if fid in want:
                found[fid] = line[i + 1:].strip().replace("\\", "/").rsplit("/", 1)[-1]
    rows = []
    for fid, raw in sorted(want.items()):
        m2 = _vmo_is_m2(vmap_dir, raw)
        name = _pretty(found[fid]) if fid in found else raw
        rows.append(f"{fid}\t{name}\t{int(m2)}\n")
    with open(out, "w", encoding="utf-8") as f:
        f.writelines(rows)
    global _names
    _names = None
    return len(rows), len(found)


# --- tile readers ---------------------------------------------------------------

_interned: dict[tuple, Spawn] = {}     # (root, map dir, uid) -> one object across tiles


def _read_new(data, path) -> list[Spawn]:
    out = []
    mapdir = os.path.dirname(os.path.abspath(path))
    root = os.path.dirname(mapdir)
    (n,) = struct.unpack_from("<I", data, 8)
    pos = 12
    unpack = struct.unpack_from
    for _ in range(n):
        flags, adt, uid, x, y, z, rx, ry, rz, scale = unpack("<BBI7f", data, pos)
        pos += 34
        b = None
        if flags & TC_HAS_BOUND:
            b = unpack("<6f", data, pos)
            pos += 24
        (nl,) = unpack("<I", data, pos)
        pos += 4
        raw = data[pos:pos + nl].decode("latin1")
        pos += nl
        if b is None:
            continue
        key = (mapdir, uid)
        s = _interned.get(key)
        if s is None:
            name, m2 = _resolve(raw, root)
            s = Spawn(name, m2, OFF - x, OFF - y, z,
                      (OFF - b[3], OFF - b[4], b[2]), (OFF - b[0], OFF - b[1], b[5]),
                      scale, uid, (rx, ry, rz), raw, root)
            _interned[key] = s
        out.append(s)
    return out


def _read_old(data, path) -> list[Spawn]:
    out = []
    pos = 8
    (n,) = struct.unpack_from("<I", data, pos)
    pos += 4
    for _ in range(n):
        flags, adt, sid, x, y, z, rx, ry, rz, scale = struct.unpack_from("<IHI7f", data, pos)
        pos += 4 + 2 + 4 + 28
        lo = hi = None
        if flags & MOD_HAS_BOUND:
            b = struct.unpack_from("<6f", data, pos)
            pos += 24
            # x/y flip, so lo/hi swap on those axes
            lo = (OFF - b[3], OFF - b[4], b[2])
            hi = (OFF - b[0], OFF - b[1], b[5])
        (nl,) = struct.unpack_from("<I", data, pos)
        pos += 4
        # WMO doodad names carry a trailing NUL in the old files
        name = data[pos:pos + nl].decode("latin1").rstrip("\x00")
        pos += nl + 4
        if lo is not None:
            out.append(Spawn(name, bool(flags & MOD_M2), OFF - x, OFF - y, z, lo, hi, scale, sid))
    return out


def read_tile(path) -> list[Spawn]:
    with open(path, "rb") as f:
        data = f.read()
    magic = data[:8]
    if magic == b"VMAP_4.E":
        return _read_new(data, path)
    if magic == b"VMAP_7.0":
        return _read_old(data, path)
    raise ValueError(f"{path}: unknown vmap magic {magic!r}")


_cache: dict[tuple, list[Spawn]] = {}


def _tile(name, vmap_dir):
    key = (vmap_dir, name)
    t = _cache.get(key)
    if t is None:
        p = os.path.join(vmap_dir, name)
        t = _cache[key] = read_tile(p) if os.path.exists(p) else []
    return t


_box_idx: dict[tuple, dict] = {}
BOX_CELL = 32.0                      # yards per index cell
# WMO boxes wider than this are whole cities / cave systems, not buildings:
# Ironforge.wmo's 910x911 yd box covers the open mountainside south of the
# gate, so every hillside poly there was "indoor stairs", exempt from the
# navmesh cliff rule and block_steep_patch - the bot climbed the face on
# guide step 79 "Travel to Ironforge" and stuck 3 x 9 times at (-5280,-660).
# The cap drops Stormwind, Ironforge, Undercity(wall), Orgrimmar, Darnassis,
# Caverns of Time, Timbermaw Hold, Aerie Peak, Stromgarde and the big caves,
# mines and instances (Maraudon, Dire Maul, Razorfen Downs/Kraul, Barrowdens,
# Md_Goldmine_Variantc/d/e, Silithus hives, the Winterspring cave). If a
# mine ramp stucks later, this is why it is no longer exempt.
BIG_BOX = 500.0
# Md_Icecave04.wmo is the rock bowl of Gol'Bolar Quarry, open to the sky: its
# 314x257 yd box made every quarry wall "indoors", which turned off the
# MAX_CLIMB cliff rule and block_steep_patch. 29 stucks and 3 give-ups in
# 20 min, each learning one poly pair up the same wall (bot_20260922_071711.log).
# The other Md_Icecave* models are real roofed caves (Coldridge, Frostmane
# Hold, Grizzled Den) and keep the exemption.
OPEN_AIR_WMOS = {"md_icecave04.wmo"}


def _tile_boxes(name, vmap_dir):
    """Grid index of the WMO (non-M2) boxes in one vmtile: cell -> [Spawn].

    A linear scan of every spawn in the tile was 72% of a long A* search
    (58 M distance calls for one Thelsamar -> Ironforge path).
    """
    key = (vmap_dir, name)
    idx = _box_idx.get(key)
    if idx is None:
        idx = {}
        for s in _tile(name, vmap_dir):
            if s.m2 or s.name.lower().rsplit("/", 1)[-1].rsplit("\\", 1)[-1] in OPEN_AIR_WMOS:
                continue
            if max(s.hi[0] - s.lo[0], s.hi[1] - s.lo[1]) > BIG_BOX:
                continue
            # registered with the query padding baked in, so a hit is always
            # found in the point's own cell
            for cx in range(int((s.lo[0] - 2) // BOX_CELL), int((s.hi[0] + 2) // BOX_CELL) + 1):
                for cy in range(int((s.lo[1] - 2) // BOX_CELL), int((s.hi[1] + 2) // BOX_CELL) + 1):
                    idx.setdefault((cx, cy), []).append(s)
        _box_idx[key] = idx
    return idx


def inside_building(wx, wy, wz, pad_xy=1.0, pad_z=3.0, map_id=None, vmap_dir=VMAP_DIR) -> bool:
    if map_id is None:
        import curmap
        map_id = curmap.MAP
    """Is the point inside a WMO bounding box? (navmesh cliff rule)"""
    cell = (int(wx // BOX_CELL), int(wy // BOX_CELL))
    seen = set()
    for dx in (-2.0, 0.0, 2.0):
        for dy in (-2.0, 0.0, 2.0):
            name = tile_name(map_id, wx + dx, wy + dy, vmap_dir)
            if name in seen:
                continue
            seen.add(name)
            for s in _tile_boxes(name, vmap_dir).get(cell, ()):
                if (s.lo[0] - pad_xy <= wx <= s.hi[0] + pad_xy
                        and s.lo[1] - pad_xy <= wy <= s.hi[1] + pad_xy
                        and s.lo[2] - pad_z <= wz <= s.hi[2] + pad_z):
                    return True
    return False


def spawns_near(wx, wy, radius=5.0, map_id=None, vmap_dir=VMAP_DIR) -> list[Spawn]:
    if map_id is None:
        import curmap
        map_id = curmap.MAP
    """Model spawns whose bounding box comes within `radius` yd of (wx, wy).

    Each spawn once, even when its box spans several tiles."""
    found = []
    seen = set()
    got = set()
    for dx in (-radius, 0, radius):
        for dy in (-radius, 0, radius):
            name = tile_name(map_id, wx + dx, wy + dy, vmap_dir)
            if name in seen:
                continue
            seen.add(name)
            for s in _tile(name, vmap_dir):
                k = (s.uid, s.name, s.x, s.y, s.z)
                if k not in got and s.dist2d(wx, wy) <= radius:
                    got.add(k)
                    found.append(s)
    found.sort(key=lambda s: s.dist2d(wx, wy))
    return found


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] == ["--build-names"]:
        n, k = build_names()
        print(f"{NAMES_FILE}: {n} models, {k} named from {LISTFILE}")
        sys.exit()
    d = VMAP_DIR
    if "--old" in sys.argv:
        sys.argv.remove("--old")
        d = OLD_VMAP_DIR
    x, y = float(sys.argv[1]), float(sys.argv[2])
    r = float(sys.argv[3]) if len(sys.argv) > 3 else 5.0
    for s in spawns_near(x, y, r, vmap_dir=d):
        print(f"{s.dist2d(x, y):4.1f} yd  {'M2 ' if s.m2 else 'WMO'} {s.name:34s} "
              f"z {s.lo[2]:.1f}-{s.hi[2]:.1f} (h {s.height:.1f}) box "
              f"x {s.lo[0]:.1f}..{s.hi[0]:.1f} y {s.lo[1]:.1f}..{s.hi[1]:.1f}")
