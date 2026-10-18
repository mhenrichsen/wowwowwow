"""Where does the navmesh break between two points?

Floods the polygon graph from both ends (ignoring the walking rules first,
then with them) and prints the closest polygon pair between the two
regions - the gap the character cannot cross.

    python navgap.py MAP x1,y1,z1 x2,y2,z2
"""
import math, sys
import numpy as np
import navmesh


def flood(nav, start, limit, rules, blocked=None):
    """Polygons reachable from start. With rules, the edges the walking rules
    reject are collected in `blocked` as (from_node, to_node, rule)."""
    seen = {start}
    stack = [start]
    while stack and len(seen) < limit:
        n = stack.pop()
        for m, pa, pb in nav.neighbours(n):
            if m in seen:
                continue
            if rules:
                cc, cn = nav.tiles[n[0]].center[n[1]], nav.tiles[m[0]].center[m[1]]
                t = nav.tiles[m[0]]
                if t.pflags[m[1]] & navmesh.NAV_STEEP and cn[1] > cc[1] + 0.3:
                    if blocked is not None:
                        blocked.append((n, m, "steep"))
                    continue
                if nav.map_id in (0, 1) and cn[1] > cc[1] + 1.0                         and nav.max_edge_slope(m) > navmesh.MAX_CLIMB and not nav.in_building(m):
                    if blocked is not None:
                        blocked.append((n, m, f"cliff slope {nav.max_edge_slope(m):.2f}"))
                    continue
            seen.add(m)
            stack.append(m)
    return seen


def centres(nav, nodes):
    keys = list(nodes)
    pts = np.array([nav.tiles[k[0]].center[k[1]] for k in keys])
    return keys, pts


def closest(nav, a, b, s, e, k=5, margin=400.0):
    """Closest polygon pairs between the two regions, looked at only inside a
    box around the leg (both regions can be a whole continent)."""
    lo = (min(s[0], e[0]) - margin, min(s[1], e[1]) - margin)
    hi = (max(s[0], e[0]) + margin, max(s[1], e[1]) + margin)

    def inside(pts):
        wx, wy = pts[:, 2], pts[:, 0]          # recast (x=wowY, z=wowX)
        return (wx >= lo[0]) & (wx <= hi[0]) & (wy >= lo[1]) & (wy <= hi[1])
    ka, pa = centres(nav, a)
    kb, pb = centres(nav, b)
    ma, mb = inside(pa), inside(pb)
    ka = [k_ for k_, m in zip(ka, ma) if m]; pa = pa[ma]
    kb = [k_ for k_, m in zip(kb, mb) if m]; pb = pb[mb]
    print(f"   {len(pa)} / {len(pb)} polys inside the leg's box")
    if not len(pa) or not len(pb):
        return []
    qa, qb = pa.copy(), pb.copy()                          # 3D: stacked layers (caves, bridges) are not a gap
    qa[:, 1] *= 2.0; qb[:, 1] *= 2.0                       # height counts double
    d = np.empty(len(qa)); j = np.empty(len(qa), dtype=int)
    for i0 in range(0, len(qa), 200):                       # chunked: no scipy here
        dd = np.sqrt(((qa[i0:i0 + 200, None, :] - qb[None, :, :]) ** 2).sum(-1))
        j[i0:i0 + 200] = dd.argmin(1); d[i0:i0 + 200] = dd.min(1)
    order = np.argsort(d)[:k * 20]
    out, seen = [], set()
    for i in order:
        wa = navmesh.rc_to_wow(*pa[i]); wb = navmesh.rc_to_wow(*pb[j[i]])
        key = (round(wa[0] / 15), round(wa[1] / 15))
        if key in seen:
            continue
        seen.add(key)
        nb = kb[j[i]]
        out.append((round(float(d[i]), 1), tuple(round(v, 1) for v in wa), tuple(round(v, 1) for v in wb),
                    round(nav.max_edge_slope(nb), 2), int(nav.tiles[nb[0]].pflags[nb[1]])))
        if len(out) >= k:
            break
    return out


def main():
    mid = int(sys.argv[1])
    s = tuple(float(v) for v in sys.argv[2].split(","))
    e = tuple(float(v) for v in sys.argv[3].split(","))
    nav = navmesh.NavMesh(map_id=mid)
    a, _ = nav.nearest_poly(*s, max_dist=8.0)
    b, _ = nav.nearest_poly(*e, max_dist=8.0)
    print("start poly", a, "end poly", b)
    A = flood(nav, a, 4000000, rules=False)
    if b not in A:
        B = flood(nav, b, 4000000, rules=False)
        print(f"NOT connected at all: {len(A)} polys from the start, {len(B)} from the end; closest pairs:")
        for row in closest(nav, A, B, s, e):
            print("  ", row)
        return
    blocked = []
    A2 = flood(nav, a, 4000000, rules=True, blocked=blocked)
    if b in A2:
        print(f"connected with rules too ({len(A2)} polys) - the leg should path; A* node cap?")
        return
    print(f"the break is a walking rule: {len(A2)} polys reachable, {len(blocked)} rejected edges; the ones nearest the goal:")
    goal = np.array(navmesh.wow_to_rc(*e))
    rows = []
    for n, m, rule in blocked:
        if m in A2:
            continue
        c = nav.tiles[m[0]].center[m[1]]
        d = math.dist((c[0], c[2]), (goal[0], goal[2]))
        rows.append((d, n, m, rule))
    rows.sort(key=lambda r: r[0])
    seen = set()
    for d, n, m, rule in rows:
        wa = navmesh.rc_to_wow(*nav.tiles[n[0]].center[n[1]]); wb = navmesh.rc_to_wow(*nav.tiles[m[0]].center[m[1]])
        key = (round(wb[0] / 20), round(wb[1] / 20))
        if key in seen:
            continue
        seen.add(key)
        print(f"   {d:6.0f} yd from goal: ({wa[0]:.1f}, {wa[1]:.1f}, {wa[2]:.1f}) -> ({wb[0]:.1f}, {wb[1]:.1f}, {wb[2]:.1f})  {rule}")
        if len(seen) >= 8:
            break


if __name__ == "__main__":
    main()
