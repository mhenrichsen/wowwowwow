"""Flight times from the client's own taxi tables.

Whether flying beats running is an arithmetic question, not a guess: the
flight path is a polyline in TaxiPathNode.dbc, the taxi flies it at a fixed
speed, and running covers ground at a fixed speed too. Compare seconds.

Tables (1.12.1 / build 5875):
  TaxiNodes.dbc     id, mapId, x, y, z, name...
  TaxiPath.dbc      id, fromNode, toNode, cost
  TaxiPathNode.dbc  id, pathId, index, mapId, x, y, z, actionFlag, delay
"""

from __future__ import annotations

import heapq
import math
import os
import struct

DBC_DIR = r"C:\Users\MadsG\Desktop\wow-forever-server\server\data\5875\dbc"

# Vanilla movement constants. FLIGHT_SPEED is what the server moves a taxi
# passenger at and is exact; the fixed costs below are measured generously.
FLIGHT_SPEED = 32.0      # yards/second
TAKEOFF = 6.0            # seconds of talk + mount + lift-off, measured generously
HOP_DELAY = 3.0          # seconds lost at each intermediate node

# RUN_SPEED is the theoretical top speed of an unbuffed player in a straight
# line, and it is NOT how fast the bot covers ground: it detours round terrain,
# stops to fight, and from level 40 it rides. Replaying 556 clean legs out of
# data/events.jsonl gives 5.26 yd/s overall and 4.81 on the 600-1500 yd legs
# where flying starts to compete. So this is only the cold-start seed for
# bot.SpeedModel, which measures the real figure per continent and per mount
# state; nothing should price a trip with it directly.
RUN_SPEED = 7.0          # yards/second, theoretical


def _rows(name: str, fmt: str, fields: int):
    data = open(os.path.join(DBC_DIR, name), "rb").read()
    magic, n, nf, size, _ = struct.unpack_from("<4s4I", data)
    assert magic == b"WDBC", name
    assert nf == fields, (name, nf, fields)
    return [struct.unpack_from(fmt, data, 20 + i * size) for i in range(n)]


def _strings(name: str) -> bytes:
    data = open(os.path.join(DBC_DIR, name), "rb").read()
    _, n, _, size, strsz = struct.unpack_from("<4s4I", data)
    off = 20 + n * size
    return data[off:off + strsz]


class _Data:
    def __init__(self):
        strs = _strings("TaxiNodes.dbc")

        def s(off):
            end = strs.find(b"\0", off)
            return strs[off:end].decode("utf-8", "replace")

        self.nodes = {}
        for r in _rows("TaxiNodes.dbc", "<2I3f11I", 16):
            self.nodes[r[0]] = {"map": r[1], "pos": (r[2], r[3], r[4]), "name": s(r[5])}
        self.paths = {}                      # (from, to) -> path id
        for pid, a, b, cost in _rows("TaxiPath.dbc", "<4I", 4):
            self.paths[(a, b)] = pid
        pts = {}
        for r in _rows("TaxiPathNode.dbc", "<4I3f2I", 9):
            _id, pid, idx, mp, x, y, z, flag, delay = r
            pts.setdefault(pid, []).append((idx, mp, x, y, z, delay))
        self.points = {p: sorted(v) for p, v in pts.items()}

    def hop_seconds(self, a: int, b: int) -> float | None:
        """Flight time for one A->B taxi path, or None if there is no such path."""
        pid = self.paths.get((a, b))
        if pid is None:
            return None
        pts = self.points.get(pid) or []
        if len(pts) < 2:
            return None
        length = sum(math.dist(pts[i][2:5], pts[i + 1][2:5]) for i in range(len(pts) - 1))
        delay = sum(p[5] for p in pts)
        return length / FLIGHT_SPEED + delay


_D = None


def _data() -> _Data:
    global _D
    if _D is None:
        _D = _Data()
    return _D


def node_by_name(name: str) -> int | None:
    """Node id by the name the bot uses ("Thelsamar"). The DBC stores
    "Thelsamar, Loch Modan", so match on the part before the comma too."""
    want = name.strip().lower()
    for nid, n in _data().nodes.items():
        full = n["name"].strip().lower()
        if full == want or full.split(",")[0].strip() == want:
            return nid
    return None


def node_pos(nid: int):
    n = _data().nodes.get(nid)
    return None if n is None else n["pos"]


def route_seconds(a: int, b: int, known: set[int] | None = None) -> float | None:
    """Total air time from node a to node b, chaining hops when there is no
    direct path. `known` limits the graph to flight points we have learned.
    Returns None when no route exists."""
    d = _data()
    if a == b:
        return 0.0
    dist = {a: 0.0}
    q = [(0.0, a)]
    seen = set()
    while q:
        cost, cur = heapq.heappop(q)
        if cur in seen:
            continue
        seen.add(cur)
        if cur == b:
            return cost
        for (x, y), _pid in d.paths.items():
            if x != cur:
                continue
            if known is not None and y not in known and y != b:
                continue
            hop = d.hop_seconds(x, y)
            if hop is None:
                continue
            nxt = cost + hop + (HOP_DELAY if cur != a else 0.0)
            if nxt < dist.get(y, 1e18):
                dist[y] = nxt
                heapq.heappush(q, (nxt, y))
    return None


def walk_seconds(yards: float) -> float:
    """Straight-line seconds at the theoretical run speed. For anything the
    bot decides on, use MageBot.ground_seconds, which uses measured speed."""
    return yards / RUN_SPEED


def nearest_node(pos, max_yards: float = 150.0, map_id: int = 0) -> int | None:
    """Taxi node closest to a position (a flight master stands on his node)."""
    best, bd = None, max_yards
    for nid, n in _data().nodes.items():
        if n["map"] != map_id:
            continue
        d = math.dist(n["pos"][:2], pos[:2])
        if d < bd:
            best, bd = nid, d
    return best
