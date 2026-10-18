"""Readers for the polestar data tables in polestar_data/ (GPL-3.0, github.com/fitzforge/polestar; see
polestar_data/SOURCE_COMMIT.txt). The .lua files are generated Lua table literals: parsed here as data, never run.
  quest_spots()   {(quest, oi): {"t", "target", "spots": [(wx, wy, z|None, r, n, alt)], "spread"}} in world yards
  taxi_nodes() / flights() / inns() / lifts()   from Nav/TravelData.lua
  spirit_healers()   from QoL/Data.lua
  eval_routes()   tools/lib/nav/eval/routes.json
"""
from __future__ import annotations

import json
import os
import re

DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "polestar_data")
SPOTS_JSON = os.path.join(DIR, "quest_spots.json")


class _Lua:
    """A Lua table-literal reader: tables, strings, numbers, true/false/nil, comments."""
    _tok = re.compile(r'\s+|--\[\[.*?\]\]|--[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|'
                      r'-?\d+\.?\d*(?:[eE][-+]?\d+)?|[A-Za-z_][A-Za-z_0-9]*|\[|\]|\{|\}|=|,|;', re.S)

    def __init__(self, text):
        self.t = [m.group(0) for m in self._tok.finditer(text)
                  if not m.group(0).isspace() and not m.group(0).startswith("--")]
        self.i = 0

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else None

    def take(self):
        v = self.t[self.i]
        self.i += 1
        return v

    def value(self):
        v = self.take()
        if v == "{":
            return self._table()
        if v[0] in "\"'":
            return bytes(v[1:-1], "utf-8").decode("unicode_escape", "replace") if "\\" in v else v[1:-1]
        if v == "true":
            return True
        if v == "false":
            return False
        if v == "nil":
            return None
        try:
            return float(v) if any(c in v for c in ".eE") else int(v)
        except ValueError:
            return v

    def _table(self):
        arr, d = [], {}
        while self.peek() != "}":
            if self.peek() == "[":
                self.take()
                k = self.value()
                self.take()            # ]
                self.take()            # =
                d[k] = self.value()
            elif self.i + 1 < len(self.t) and self.t[self.i + 1] == "=" and re.match(r"[A-Za-z_]", self.peek()):
                k = self.take()
                self.take()
                d[k] = self.value()
            else:
                arr.append(self.value())
            if self.peek() in (",", ";"):
                self.take()
        self.take()
        if d and arr:
            d.update({i + 1: v for i, v in enumerate(arr)})
            return d
        return d if d else arr


def lua_table(text):
    return _Lua(text).value()


def _read(name):
    return open(os.path.join(DIR, name), encoding="utf-8").read()


def build_quest_spots():
    """Guides.lua -> quest_spots.json (world coordinates; Era-frame goals only, as worldmap's 1.12 frame)."""
    import worldmap
    src = _read("addon_Polestar_Guides_Guides.lua")
    out = {}
    for line in src.splitlines():
        line = line.strip()
        if not line.startswith("{ t = ") or "spots =" not in line or "quest =" not in line:
            continue
        g = lua_table(line.rstrip(","))
        if not isinstance(g, dict) or g.get("f") != "e" or g.get("t") not in ("kill", "collect", "use", "interact", "loot"):
            continue
        zn = worldmap.UI_MAP.get(g.get("map"))
        if not zn:
            continue
        spots = []
        for sp in g.get("spots") or []:
            w = worldmap.to_world(sp["x"], sp["y"], zn)
            if not w:
                continue
            spots.append((round(w[1], 1), round(w[2], 1), sp.get("z"), sp.get("r", 30), sp.get("n", 1),
                          bool(sp.get("alt"))))
        if not spots:
            continue
        key = f"{g['quest']}:{g.get('oi', 1)}"
        e = out.setdefault(key, {"t": g["t"], "target": g.get("target") or g.get("itemName") or g.get("title"),
                                 "map": w[0], "spots": [], "spread": bool(g.get("spread"))})
        e["spots"] += [s for s in spots if s not in e["spots"]]
    with open(SPOTS_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f)
    return out


_spots = None


def quest_spots():
    global _spots
    if _spots is None:
        if not os.path.exists(SPOTS_JSON):
            build_quest_spots()
        _spots = json.load(open(SPOTS_JSON, encoding="utf-8"))
    return _spots


def spots_for(quest, oi=1):
    return quest_spots().get(f"{quest}:{oi}")


_trav = None


def _travel():
    global _trav
    if _trav is None:
        src = _read("addon_Polestar_Nav_TravelData.lua")
        i = src.index("RegisterTravelData(") + len("RegisterTravelData(")
        _trav = lua_table(src[i:])
    return _trav


def taxi_nodes():
    """[(id, continent, wx, wy, faction mask, name)]."""
    return [(r[0], r[1], r[2], r[3], r[7], r[8]) for r in _travel().get("taxi", [])]


def flights():
    """[(from node, to node, yards flown)]."""
    f = _travel().get("flights", [])
    return [tuple(f[k:k + 3]) for k in range(0, len(f) - 2, 3)]


def inns():
    return _travel().get("inns", [])


def lifts():
    """{lift id: its service table (period ms, stops with top / bottom z)} found anywhere in TravelData."""
    out = {}

    def walk(v):
        if isinstance(v, dict):
            for k, w in v.items():
                if isinstance(k, str) and k.startswith("lift-") and isinstance(w, dict):
                    out[k] = w
                else:
                    walk(w)
        elif isinstance(v, list):
            for w in v:
                walk(w)
    src = _read("addon_Polestar_Nav_TravelData.lua")
    i = src.index("ns.Transit.Register(") + len("ns.Transit.Register(")
    walk(lua_table(src[i:]).get("routes", {}))
    return out


def lift_stops():
    """[(lift id, continent, wx, wy, z top, z bottom, period s)] - one per car position (a shaft)."""
    out = []
    for k, v in lifts().items():
        by = {}
        for st in v.get("stops", []):
            by.setdefault((st[2], round(st[3], 1), round(st[4], 1)), []).append(st[5])
        for (c, x, y), zs in by.items():
            out.append((k, c, x, y, max(zs), min(zs), v.get("period", 30000) / 1000.0))
    return out


def eval_routes():
    return json.load(open(os.path.join(DIR, "tools_lib_nav_eval_routes.json"), encoding="utf-8"))
