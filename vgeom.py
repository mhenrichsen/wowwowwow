"""Real collision geometry from the server's vmap model files (.vmo): the triangles the client and server collide
with - building walls, ledges, stair risers, rails, tables - in WoW coordinates.

The navmesh is an approximation of this: its generator fuses furniture and ledges up to its step height into the
floor and keeps polygon edges up to a voxel off the real walls. The bot uses this module to see what the character's
body would actually touch along a planned path (navmesh.NavMesh.clear_path).

File format (TrinityCore src/common/Collision/Models/WorldModel.cpp, WorldModel::readFile / GroupModel::readFromFile):
  "VMAP_4.E" magic (8), "WMOD" u32 chunkSize u32 flags u32 rootWmoId, "GMOD" u32 nGroups, per group:
  6f bound, u32 mogpFlags, u32 groupWmoId, "VERT" u32 size u32 n n*3f, "TRIM" u32 size u32 n n*3u32,
  "MBIH" BIH (6f bounds, u32 treeSize, treeSize u32, u32 count, count u32), "LIQU" u32 size [liquid]
  then "GBIH" BIH.
Placement (ModelInstance.cpp): world = pos + R * (scale * v), R = fromEulerAnglesZYX(ry, rx, rz) in degrees
(G3D: Rz(ry) * Ry(rx) * Rx(rz)), all in assembler space; WoW x = 17066.666 - X, y = 17066.666 - Y.
"""
from __future__ import annotations

import math
import os
import struct

import numpy as np

import vmap

_models: dict[str, list] = {}        # vmo path -> [(verts (n,3) f32, tris (m,3) i32)]
_placed: dict[tuple, np.ndarray] = {}   # (root, uid) -> (m,3,3) triangles in WoW coords
WMO_GROUP_ALL = None


def _bih_skip(data, pos):
    pos += 24
    (n,) = struct.unpack_from("<I", data, pos)
    pos += 4 + 4 * n
    (n,) = struct.unpack_from("<I", data, pos)
    return pos + 4 + 4 * n


def read_vmo(path) -> list:
    """[(verts, tris)] per group of one model file."""
    m = _models.get(path)
    if m is not None:
        return m
    out = []
    with open(path, "rb") as f:
        data = f.read()
    pos = 8
    if data[pos:pos + 4] != b"WMOD":
        _models[path] = out
        return out
    (size,) = struct.unpack_from("<I", data, pos + 4)
    pos += 8 + size
    if data[pos:pos + 4] == b"GMOD":
        (ng,) = struct.unpack_from("<I", data, pos + 4)
        pos += 8
        for _ in range(ng):
            pos += 24 + 8
            assert data[pos:pos + 4] == b"VERT", (path, data[pos:pos + 4])
            nv = struct.unpack_from("<I", data, pos + 8)[0]
            pos += 12
            if not nv:
                continue                     # a group without collision geometry ends here
            v = np.frombuffer(data, "<f4", nv * 3, pos).reshape(nv, 3)
            pos += 12 * nv
            assert data[pos:pos + 4] == b"TRIM"
            nt = struct.unpack_from("<I", data, pos + 8)[0]
            pos += 12
            t = np.frombuffer(data, "<u4", nt * 3, pos).reshape(nt, 3).astype(np.int32)
            pos += 12 * nt
            assert data[pos:pos + 4] == b"MBIH"
            pos = _bih_skip(data, pos + 4)
            assert data[pos:pos + 4] == b"LIQU"
            (ls,) = struct.unpack_from("<I", data, pos + 4)
            pos += 8 + ls
            if nt:
                out.append((v, t))
    _models[path] = out
    return out


def _rot(rx, ry, rz):
    a, b, c = math.radians(ry), math.radians(rx), math.radians(rz)   # fromEulerAnglesZYX(y, x, z)
    ca, sa, cb, sb, cc, sc = math.cos(a), math.sin(a), math.cos(b), math.sin(b), math.cos(c), math.sin(c)
    Rz = np.array([[ca, -sa, 0], [sa, ca, 0], [0, 0, 1]])
    Ry = np.array([[cb, 0, sb], [0, 1, 0], [-sb, 0, cb]])
    Rx = np.array([[1, 0, 0], [0, cc, -sc], [0, sc, cc]])
    return Rz @ Ry @ Rx


def placed(sp) -> np.ndarray:
    """(m,3,3) world triangles (WoW x, y, z) of one spawn; empty when its model has no collision."""
    key = (sp.root, sp.uid)
    t = _placed.get(key)
    if t is not None:
        return t
    tris = np.zeros((0, 3, 3), np.float32)
    if sp.raw and sp.root and sp.rot is not None:
        path = os.path.join(sp.root, sp.raw + ".vmo")
        if os.path.exists(path):
            R = _rot(*sp.rot)
            pos = np.array([vmap.OFF - sp.x, vmap.OFF - sp.y, sp.z])
            parts = []
            for v, t_ in read_vmo(path):
                w = (v.astype(np.float64) * sp.scale) @ R.T + pos
                w[:, 0] = vmap.OFF - w[:, 0]
                w[:, 1] = vmap.OFF - w[:, 1]
                parts.append(w[t_].astype(np.float32))
            if parts:
                tris = np.concatenate(parts)
    _placed[key] = tris
    return tris


class Patch:
    """All collision triangles in a box round a walk, for fast repeated queries."""

    def __init__(self, x0, y0, x1, y1, z0, z1, map_id=None):
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        r = math.hypot(x1 - x0, y1 - y0) / 2 + 2
        parts = []
        for sp in vmap.spawns_near(mx, my, r, map_id):
            if sp.hi[2] < z0 or sp.lo[2] > z1:
                continue
            t = placed(sp)
            if not len(t):
                continue
            lo, hi = t.min(axis=1), t.max(axis=1)
            keep = ((hi[:, 0] >= x0) & (lo[:, 0] <= x1) & (hi[:, 1] >= y0) & (lo[:, 1] <= y1)
                    & (hi[:, 2] >= z0) & (lo[:, 2] <= z1))
            if keep.any():
                parts.append(t[keep])
        self.t = np.concatenate(parts) if parts else np.zeros((0, 3, 3), np.float32)
        self.lo, self.hi = self.t.min(axis=1), self.t.max(axis=1)
        n = np.cross(self.t[:, 1] - self.t[:, 0], self.t[:, 2] - self.t[:, 0])
        ln = np.linalg.norm(n, axis=1)
        self.nz = np.where(ln > 0, n[:, 2] / np.maximum(ln, 1e-9), 0.0)

    def floor(self, x, y, z_from, z_to) -> float | None:
        """Highest surface under (x, y) between z_to and z_from (a downward ray from z_from)."""
        sel = ((self.lo[:, 0] <= x) & (self.hi[:, 0] >= x) & (self.lo[:, 1] <= y) & (self.hi[:, 1] >= y)
               & (self.lo[:, 2] <= z_from) & (self.hi[:, 2] >= z_to) & (np.abs(self.nz) > 0.05))
        if not sel.any():
            return None
        t = self.t[sel].astype(np.float64)
        a, b, c = t[:, 0], t[:, 1], t[:, 2]
        d = (b[:, 1] - c[:, 1]) * (a[:, 0] - c[:, 0]) + (c[:, 0] - b[:, 0]) * (a[:, 1] - c[:, 1])
        ok = np.abs(d) > 1e-12
        d = np.where(ok, d, 1.0)
        l1 = ((b[:, 1] - c[:, 1]) * (x - c[:, 0]) + (c[:, 0] - b[:, 0]) * (y - c[:, 1])) / d
        l2 = ((c[:, 1] - a[:, 1]) * (x - c[:, 0]) + (a[:, 0] - c[:, 0]) * (y - c[:, 1])) / d
        l3 = 1 - l1 - l2
        inside = ok & (l1 >= -1e-6) & (l2 >= -1e-6) & (l3 >= -1e-6)
        zs = l1 * a[:, 2] + l2 * b[:, 2] + l3 * c[:, 2]
        zs = zs[inside & (zs <= z_from) & (zs >= z_to)]
        return float(zs.max()) if len(zs) else None

    def body_d(self, x, y, zlo, zhi, r) -> float:
        """2D distance from (x, y) to the nearest triangle part between heights zlo and zhi (r when none within r):
        what a body standing at (x, y) whose feet clear zlo would touch."""
        sel = ((self.lo[:, 0] <= x + r) & (self.hi[:, 0] >= x - r) & (self.lo[:, 1] <= y + r)
               & (self.hi[:, 1] >= y - r) & (self.lo[:, 2] <= zhi) & (self.hi[:, 2] >= zlo))
        if not sel.any():
            return r
        best = r
        for tri in self.t[sel].astype(np.float64):
            poly = _clip_z(tri, zlo, zhi)
            if len(poly) < 2:
                if len(poly) == 1:
                    best = min(best, math.hypot(poly[0][0] - x, poly[0][1] - y))
                continue
            d = _pt_poly2d(x, y, poly)
            if d < best:
                best = d
                if best <= 0:
                    return 0.0
        return best


def _clip_z(tri, zlo, zhi):
    pts = [tuple(p) for p in tri]
    for lim, keep_above in ((zlo, True), (zhi, False)):
        out = []
        n = len(pts)
        for i in range(n):
            p, q = pts[i], pts[(i + 1) % n]
            pin = p[2] >= lim if keep_above else p[2] <= lim
            qin = q[2] >= lim if keep_above else q[2] <= lim
            if pin:
                out.append(p)
            if pin != qin and q[2] != p[2]:
                f = (lim - p[2]) / (q[2] - p[2])
                out.append((p[0] + (q[0] - p[0]) * f, p[1] + (q[1] - p[1]) * f, lim))
        pts = out
        if not pts:
            return []
    return pts


def _pt_seg(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    u = 0.0 if L == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L))
    return math.hypot(px - ax - u * dx, py - ay - u * dy)


def _pt_poly2d(x, y, poly):
    n = len(poly)
    inside = False
    best = 1e9
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        best = min(best, _pt_seg(x, y, a[0], a[1], b[0], b[1]))
        if n >= 3 and ((a[1] > y) != (b[1] > y)):
            xi = a[0] + (y - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
            if xi > x:
                inside = not inside
    return 0.0 if inside else best
