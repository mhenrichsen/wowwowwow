"""Solid gameobjects the navmesh does not know about (2026-10-15).

The server's mmaps are built from terrain and the static map models (vmap tiles) only: campfires, crates, sacks of
oats, ore veins, carts and the like are spawned gameobjects and the mesh runs straight through them. The client
collides with them, so a path through one wedges the character.

  GameObjectModels.dtree   the vmap extractor's list of gameobject models WITH collision geometry: displayId -> name
                           and the model-space bounding box (z up). Models without collision (herbs) are not in it.
  tcf_world.gameobject     the spawns: position, rotation quaternion (or yaw), the template's size (scale)

boxes_near() hands navmesh._avoid_doodads world AABBs of the rotated, scaled boxes, padded by the caller.
"""

from __future__ import annotations

import math
import os
import struct

DTREE = r"D:\tcforever\server\data\vmaps\GameObjectModels.dtree"
MAP = 0
CELL = 32.0
# doors (state changes), buttons, traps, transports, portals, fishing holes, destructibles: no fixed solid box
SKIP_TYPES = {0, 1, 6, 11, 15, 22, 25, 33}
MIN_HEIGHT = 0.6      # yd above its base: lower ones are stepped over
MIN_FOOT = 0.4        # yd: smaller footprint is a twig
MAX_FOOT = 30.0       # yd: bigger is a building-sized object we walk on or in (docks, ships), left to the mesh
BODY_LO, BODY_HI = 0.5, 2.0   # yd above our feet: a box overlapping this band is in our way

_grid = None          # (cx, cy) -> [(x0, y0, x1, y1, zlo, zhi, name)]


def read_models(path=DTREE) -> dict:
    """displayId -> (name, (x0, y0, z0, x1, y1, z1)) model-space box."""
    d = open(path, "rb").read()
    if d[:4] != b"VMAP":
        raise ValueError(f"{path}: not a vmap file")
    o, out = 8, {}
    while o + 8 <= len(d):
        did, nl = struct.unpack_from("<II", d, o)
        o += 8
        name = d[o:o + nl].decode("latin1")
        o += nl
        out[did] = (name, struct.unpack_from("<6f", d, o))
        o += 24
    return out


def world_box(box, x, y, z, q, scale):
    """Model box -> world AABB (x0, y0, x1, y1, zlo, zhi): corners scaled, rotated by unit quaternion q (x, y, z, w),
    moved to (x, y, z)."""
    qx, qy, qz, qw = q
    # rotation matrix of the quaternion
    r = ((1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - qz * qw), 2 * (qx * qz + qy * qw)),
         (2 * (qx * qy + qz * qw), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz - qx * qw)),
         (2 * (qx * qz - qy * qw), 2 * (qy * qz + qx * qw), 1 - 2 * (qx * qx + qy * qy)))
    xs, ys, zs = [], [], []
    for cx in (box[0], box[3]):
        for cy in (box[1], box[4]):
            for cz in (box[2], box[5]):
                v = (cx * scale, cy * scale, cz * scale)
                xs.append(x + r[0][0] * v[0] + r[0][1] * v[1] + r[0][2] * v[2])
                ys.append(y + r[1][0] * v[0] + r[1][1] * v[1] + r[1][2] * v[2])
                zs.append(z + r[2][0] * v[0] + r[2][1] * v[1] + r[2][2] * v[2])
    return min(xs), min(ys), max(xs), max(ys), min(zs), max(zs)


def quat(rot, yaw):
    """The spawn's rotation: its quaternion when set, else the yaw about z."""
    n = math.sqrt(sum(c * c for c in rot))
    if n < 1e-6:
        return 0.0, 0.0, math.sin(yaw / 2), math.cos(yaw / 2)
    return tuple(c / n for c in rot)


def solid(b) -> bool:
    w, h = b[2] - b[0], b[3] - b[1]
    return b[5] - b[4] >= MIN_HEIGHT and max(w, h) >= MIN_FOOT and max(w, h) <= MAX_FOOT


CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gobj_boxes4.pkl")   # 2: + the OBB, 3: + teleport triggers, 4: + big models by triangles
CACHE_TTL = 86400.0   # s: spawns change with server DB updates only


_grids: dict = {}     # map -> its _grid


def _cache_file(m):
    return CACHE if m == 0 else CACHE.replace(".pkl", f"_m{m}.pkl")


def _load():
    """The (cx, cy) grid of boxes on the current map (curmap.MAP), built / read once per map."""
    global _grid, MAP
    import curmap
    MAP = curmap.MAP
    if MAP in _grids:
        _grid = _grids[MAP]
        return _grid
    _grid = _grids[MAP] = {}
    if not os.path.exists(DTREE):
        return _grid
    CACHE = _cache_file(MAP)                          # noqa: N806 - this map's cache file
    import pickle
    import time
    try:
        st = os.stat(CACHE)
        if st.st_mtime > os.stat(DTREE).st_mtime and time.time() - st.st_mtime < CACHE_TTL:
            with open(CACHE, "rb") as f:
                _grid = _grids[MAP] = pickle.load(f)
            return _grid
    except (OSError, pickle.PickleError, EOFError):
        pass
    boxes = _build()
    for b in boxes:
        for cx in range(int(b[0] // CELL), int(b[2] // CELL) + 1):
            for cy in range(int(b[1] // CELL), int(b[3] // CELL) + 1):
                _grid.setdefault((cx, cy), []).append(b)
    try:
        tmp = CACHE + ".%d" % os.getpid()
        with open(tmp, "wb") as f:
            pickle.dump(_grid, f)
        os.replace(tmp, CACHE)
    except OSError:
        pass
    return _grid


def _build() -> list:
    out = []
    import worlddb
    models = read_models()
    rows = worlddb._query_db(
        "tcf_world", "SELECT t.displayId, t.size, t.type, t.name, g.position_x, g.position_y, g.position_z, "
        "g.orientation, g.rotation0, g.rotation1, g.rotation2, g.rotation3 FROM gameobject g "
        "JOIN gameobject_template t ON t.entry = g.id "
        "WHERE g.map = %d AND IFNULL(g.PhaseId, 0) = 0 AND IFNULL(g.PhaseGroup, 0) = 0 "
        # holiday decorations (signposts, banners) stand only while their event runs
        "AND g.guid NOT IN (SELECT guid FROM game_event_gameobject WHERE eventEntry > 0)" % MAP)
    for did, size, typ, name, x, y, z, o, r0, r1, r2, r3 in rows:
        if int(typ) in SKIP_TYPES:
            continue
        m = models.get(int(did))
        if m is None:
            continue
        b = world_box(m[1], float(x), float(y), float(z), quat((float(r0), float(r1), float(r2), float(r3)),
                                                                float(o)), float(size or 1.0))
        big = b[5] - b[4] >= MIN_HEIGHT and max(b[2] - b[0], b[3] - b[1]) > MAX_FOOT
        if not solid(b) and not big:
            continue
        # a building-sized one goes in by its model's triangles, not its box (a box that size is mostly air): the
        # Beached Sea Creature's bones (22 x 42 yd) on the Darkshore beach were left out and wedged the walk
        # twice (#1124, rxp34 11:28)
        out.append(b + (name, (m[1], float(x), float(y), float(z),
                              quat((float(r0), float(r1), float(r2), float(r3)), float(o)), float(size or 1.0)),
                        m[0] if big else None))
    try:
        out += teleport_triggers()
    except Exception as e:                                # noqa: BLE001 - no trigger data: gameobjects only
        print(f"gobj: teleport triggers off: {e!r}")
    return out


DB2_AREATRIGGER = "D:/tcforever/server/data/dbc/enUS/AreaTrigger.db2"
TRIGGER_MAX = 60.0    # yd: a bigger trigger (a whole zone line) is not a box to walk round


def teleport_triggers() -> list:
    """Area triggers that teleport (instance entrances, Champions' Hall's door in Old Town): walking into one loads
    another map (SI:7 leg 2026-10-16). They are boxes to keep out of, like a solid gameobject: the client's
    AreaTrigger.db2 shapes with the server's hotfix rows on top, for the ids in areatrigger_teleport."""
    import db2
    import worlddb
    tele = {int(r[0]): r[1] for r in worlddb._query_db("tcf_world", "SELECT ID, Name FROM areatrigger_teleport")}
    shapes = {}
    for rid, v in db2.read(DB2_AREATRIGGER).items():
        f = lambda i: db2.as_float(v[i][0])                # noqa: E731
        pos = [db2.as_float(u) for u in v[0]]
        shapes[rid] = (pos[0], pos[1], pos[2], v[2][0], f(6), f(7), f(8), f(9), f(10), v[11][0])
    for r in worlddb._query_db("tcf_hotfixes", "SELECT ID, PosX, PosY, PosZ, ContinentID, Radius, BoxLength, "
                               "BoxWidth, BoxHeight, BoxYaw, ShapeType FROM area_trigger"):
        rid = int(r[0])
        shapes[rid] = tuple(float(c) for c in r[1:4]) + (int(r[4]),) + tuple(float(c) for c in r[5:10]) + (int(r[10]),)
    out = []
    for rid, name in tele.items():
        s = shapes.get(rid)
        if s is None or s[3] != MAP:
            continue
        x, y, z, _m, rad, bl, bw, bh, yaw, shape = s
        if shape == 1 and bl > 0 and bw > 0:
            box = (-bl / 2, -bw / 2, -max(bh, 2.0) / 2, bl / 2, bw / 2, max(bh, 2.0) / 2)
        elif rad > 0:
            box = (-rad, -rad, -rad, rad, rad, rad)
        else:
            continue
        if max(box[3] - box[0], box[4] - box[1]) > TRIGGER_MAX:
            continue
        q = quat((0.0, 0.0, 0.0, 0.0), yaw if shape == 1 else 0.0)
        b = world_box(box, x, y, z, q, 1.0)
        out.append(b + (f"teleport trigger {rid} {name}", (box, x, y, z, q, 1.0)))
    return out


def boxes_near(x0, y0, x1, y1, big=False):
    """World boxes (x0, y0, x1, y1, zlo, zhi, name) whose footprint touches the rectangle (unpadded). big: also the
    building-sized ones kept by their triangles (their box is not a solid: only bodies_in takes them)."""
    g = _load()
    lo_x, hi_x, lo_y, hi_y = min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)
    seen, out = set(), []
    for cx in range(int(lo_x // CELL), int(hi_x // CELL) + 1):
        for cy in range(int(lo_y // CELL), int(hi_y // CELL) + 1):
            for e in g.get((cx, cy), ()):
                if id(e) in seen:
                    continue
                seen.add(id(e))
                if not big and len(e) > 8 and e[8]:
                    continue
                if e[0] <= hi_x and lo_x <= e[2] and e[1] <= hi_y and lo_y <= e[3]:
                    out.append(e)
    return out


_body_cache: dict = {}


def body_points(e, step=0.2):
    """(n, 3) world points every ~step yd over the surface of the box's rotated model box (e[7]): what the grid
    planner (gridpath) treats as solid, like a model's triangles."""
    import numpy as np
    k = id(e)
    r = _body_cache.get(k)
    if r is not None:
        return r
    box, x, y, z, q, s = e[7]
    if len(e) > 8 and e[8]:
        r = _tri_points(e[8], x, y, z, q, s, step)
        _body_cache[k] = r
        return r
    lo = np.array(box[:3]) * s
    hi = np.array(box[3:]) * s
    ax = [np.linspace(lo[i], hi[i], max(2, int(math.ceil((hi[i] - lo[i]) / step)) + 1)) for i in range(3)]
    faces = []
    for i in range(3):
        j, k2 = [a for a in range(3) if a != i]
        u, v = np.meshgrid(ax[j], ax[k2], indexing="ij")
        for c in (lo[i], hi[i]):
            f = np.zeros((u.size, 3))
            f[:, i], f[:, j], f[:, k2] = c, u.ravel(), v.ravel()
            faces.append(f)
    P = np.concatenate(faces)
    qx, qy, qz, qw = q
    R = np.array(((1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - qz * qw), 2 * (qx * qz + qy * qw)),
                  (2 * (qx * qy + qz * qw), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz - qx * qw)),
                  (2 * (qx * qz - qy * qw), 2 * (qy * qz + qx * qw), 1 - 2 * (qx * qx + qy * qy))))
    r = P @ R.T + np.array((x, y, z))
    _body_cache[k] = r
    return r


def _tri_points(name, x, y, z, q, s, step):
    """(n, 3) world points every ~step yd over a gameobject model's collision triangles (vmaps/<name>.vmo)."""
    import numpy as np
    import vgeom
    qx, qy, qz, qw = q
    R = np.array(((1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - qz * qw), 2 * (qx * qz + qy * qw)),
                  (2 * (qx * qy + qz * qw), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz - qx * qw)),
                  (2 * (qx * qz - qy * qw), 2 * (qy * qz + qx * qw), 1 - 2 * (qx * qx + qy * qy))))
    path = os.path.join(os.path.dirname(DTREE), name + ".vmo")
    out = []
    for v, t in (vgeom.read_vmo(path) if os.path.exists(path) else []):
        T = ((v.astype(np.float64) * s) @ R.T + np.array((x, y, z)))[t]
        L = np.max(np.stack([np.linalg.norm(T[:, 1] - T[:, 0], axis=1), np.linalg.norm(T[:, 2] - T[:, 0], axis=1),
                             np.linalg.norm(T[:, 2] - T[:, 1], axis=1)]), axis=0)
        ns = np.maximum(1, np.ceil(L / step)).astype(int)
        for n in np.unique(ns):
            u, w = np.meshgrid(np.arange(n + 1), np.arange(n + 1), indexing="ij")
            m = (u + w) <= n
            u, w = u[m] / n, w[m] / n
            S = T[ns == n]
            out.append((S[:, 0][:, None, :] + u[None, :, None] * (S[:, 1] - S[:, 0])[:, None, :]
                        + w[None, :, None] * (S[:, 2] - S[:, 0])[:, None, :]).reshape(-1, 3))
    return np.concatenate(out) if out else np.zeros((0, 3))


def bodies_in(x0, y0, x1, y1):
    """body_points of every solid box touching the rectangle, as one (n, 3) array."""
    import numpy as np
    parts = [body_points(e) for e in boxes_near(x0, y0, x1, y1, big=True) if len(e) > 7]
    return np.concatenate(parts) if parts else np.zeros((0, 3))


def blocks_at(e, feet_z) -> bool:
    """The box overlaps the body band of a character standing at feet_z."""
    return e[4] < feet_z + BODY_HI and e[5] > feet_z + BODY_LO


if __name__ == "__main__":
    import time
    t = time.time()
    g = _load()
    n = len({id(e) for v in g.values() for e in v})
    print(f"{n} solid gameobjects on map {MAP} in {time.time() - t:.1f}s")
