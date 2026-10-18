"""Offline tests for nb/store.py (shared JSON state files), the stores built on it (bad spots, climb votes / blocks,
death spots, dead talks, step deaths), nb/util.py and the order of move.Mover's path strategies.
Touches no game window; every file lives in a temp dir.

Run: python -m nb.test_store   (from the wowbot folder).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from nb import store as S            # noqa: E402
from nb import util                  # noqa: E402


def _tmp(name: str) -> str:
    return os.path.join(tempfile.mkdtemp(prefix="nbstore_"), name)


def _dump(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------------------------ Store
def test_atomic_write_and_read_back():
    p = _tmp("a.json")
    st = S.Store(p, default=list)
    assert st.read() == []                                   # no file: the default
    assert st.update(lambda d: d.append(1))
    assert _load(p) == [1] and st.read() == [1]
    assert not [f for f in os.listdir(os.path.dirname(p)) if f.endswith(".tmp")]     # no temp file left behind


def test_update_merges_with_disk():
    """Two stores (two bots) on one file: each update starts from the newest file, nobody's entry is lost."""
    p = _tmp("m.json")
    a, b = S.Store(p, default=dict), S.Store(p, default=dict)
    a.read(), b.read()
    assert a.update(lambda d: d.__setitem__("a", 1))
    assert b.update(lambda d: d.__setitem__("b", 2))         # b never re-read before writing
    assert _load(p) == {"a": 1, "b": 2}
    assert a.read() == {"a": 1, "b": 2}                      # a sees b's write (mtime changed)


def test_concurrent_processes_do_not_clobber():
    """4 processes x 15 appends each on one file at once: all 60 land (the bots run 4 at a time)."""
    p = _tmp("c.json")
    code = ("import sys; sys.path.insert(0, sys.argv[1]); from nb.store import Store\n"
            "st = Store(sys.argv[2], default=list)\n"
            "for i in range(15):\n"
            "    while not st.update(lambda d: d.append([sys.argv[3], i])):\n"
            "        pass\n")
    procs = [subprocess.Popen([sys.executable, "-c", code, ROOT, p, str(k)]) for k in range(4)]
    for pr in procs:
        assert pr.wait(timeout=120) == 0
    d = _load(p)
    assert len(d) == 60 and len({(a, b) for a, b in d}) == 60, len(d)


def test_ttl_read_and_prune():
    p = _tmp("t.json")
    now = time.time()
    _dump(p, [[1, 1, now - 10], [2, 2, now - 5000], [3, 3, None]])
    st = S.Store(p, default=list, ttl=100.0, stamp=lambda e: e[2])
    assert [e[0] for e in st.read()] == [1, 3]               # a stamp of None never expires
    assert st.update(lambda d: d.append([4, 4, now]))
    assert [e[0] for e in _load(p)] == [1, 3, 4]             # the expired entry is dropped on write
    d = S.Store(_tmp("td.json"), default=dict, ttl=100.0, stamp=lambda v: v)
    _dump(d.path, {"old": now - 500, "new": now})
    assert d.read() == {"new": now}


def test_mtime_reload_is_cheap_and_sees_changes():
    p = _tmp("r.json")
    _dump(p, [1])
    st = S.Store(p, default=list)
    n = [0]
    real = st._load

    def counting():
        n[0] += 1
        return real()
    st._load = counting
    assert st.read() == [1] and st.read() == [1] and n[0] == 1          # unchanged: parsed once
    time.sleep(0.02)
    _dump(p, [1, 2, 3])
    assert st.changed() and st.read() == [1, 2, 3] and n[0] == 2


def test_torn_file_keeps_last_good_copy_and_update_rebuilds():
    p = _tmp("b.json")
    _dump(p, [1, 2])
    st = S.Store(p, default=list)
    assert st.read() == [1, 2]
    with open(p, "w") as f:
        f.write("[1, 2, 3")                                  # half a file
    assert st.read() == [1, 2]
    assert st.update(lambda d: d.append(9))                  # rebuilt from our copy
    assert _load(p) == [1, 2, 9]


def test_replace_retries_while_target_is_held():
    """os.replace raises PermissionError while another process holds the target open on Windows: retried."""
    p = _tmp("h.json")
    calls = []
    real = os.replace

    def flaky(a, b):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError("in use")
        return real(a, b)
    S.os.replace = flaky
    try:
        assert S.atomic_write(p, {"x": 1})
    finally:
        S.os.replace = real
    assert len(calls) == 3 and _load(p) == {"x": 1}


def test_busy_lock_gives_up():
    if S.msvcrt is None:
        return
    p = _tmp("l.json")
    held = S.FileLock(p + ".lock")
    assert held.acquire()
    old = S.LOCK_WAIT
    try:
        S.FileLock.__init__.__defaults__ = (0.2,)
        assert not S.Store(p, default=list).update(lambda d: d.append(1))
    finally:
        S.FileLock.__init__.__defaults__ = (old,)
        held.release()
    assert S.Store(p, default=list).update(lambda d: d.append(1))


# ------------------------------------------------------------------ legacy files through the migrated stores
def test_bad_spots_legacy_file_and_layout():
    from nb import move
    p = _tmp("bad_spots.json")
    old = {"spots": [{"x": -6055.3, "y": 382.9, "z": 392.8, "in": True, "n": 3,
                      "eps": ["1-1791999992.1@-6055,383", "2-1792009075.5@-6000,380"], "t": 1792009075.5}]}
    with open(p, "w", encoding="utf-8") as f:
        json.dump(old, f, indent=0)                          # the old writer's layout
    bs = move.BadSpots(p, p + ".lock")
    assert bs.load() == old["spots"] and len(bs.active()) == 1
    sp, new = bs.hit(-6055.0, 383.0, 392.8, True, "3-1792010000.0@-6100,400")
    assert sp["n"] == 4 and not new
    d = _load(p)
    assert set(d) == {"spots"} and d["spots"][0]["n"] == 4 and bs.spots[0]["n"] == 4


def test_climb_votes_merge_two_bots():
    from nb import move
    p = _tmp("climb_votes.json")
    old_cv = move.CLIMB_VOTES
    move.CLIMB_VOTES = p
    try:
        frm, to = ((9, 21), 10), ((9, 21), 13)
        assert move.climb_vote(frm, to, "1-100.0@a") == 1
        assert move.climb_vote(frm, to, "2-200.0@b") == 2
        assert move.climb_vote(frm, to, "2-200.0@b") == 2    # the same walk counts once
    finally:
        move.CLIMB_VOTES = old_cv
    assert _load(p)["spots"][0]["k"] == "(9, 21):10>(9, 21):13"


def test_blocked_climbs_legacy_file_merge_and_retract():
    import navmesh
    p = _tmp("blocked_climbs_000_tcf.json")
    _dump(p, [[[9, 21, 10], [9, 21, 13]]])                    # written by the old _save_climbs
    nm = navmesh.NavMesh.__new__(navmesh.NavMesh)
    nm._climbs_file = lambda: p
    nm.blocked_climbs, nm.soft_climbs = nm._load_climbs(), {}
    k1 = (((9, 21), 10), ((9, 21), 13))
    assert nm.blocked_climbs == {k1}
    nm.blocked_climbs.add("not in the cache")                # _load_climbs hands out a new set each call
    assert nm._load_climbs() == {k1}
    nm.blocked_climbs.discard("not in the cache")
    other = navmesh.NavMesh.__new__(navmesh.NavMesh)          # another bot adds a block meanwhile
    other._climbs_file = lambda: p
    other.blocked_climbs, other.soft_climbs = other._load_climbs(), {}
    k2 = (((9, 21), 8), ((9, 21), 13))
    assert other.block_climb(k2[0], k2[1], proven=True)
    assert nm.block_climb(((8, 20), 1), ((8, 20), 2), proven=True)     # ours keeps theirs
    assert len(_load(p)) == 3
    assert nm.reload_climbs() == 1 and k2 in nm.blocked_climbs
    assert nm.unblock_climb(k1)
    assert {tuple(map(tuple, e)) for e in _load(p)} == {((9, 21, 8), (9, 21, 13)), ((8, 20, 1), (8, 20, 2))}
    assert nm.block_climb(((7, 7), 1), ((7, 7), 2), proven=False)      # a prediction: never on disk
    assert len(_load(p)) == 2


def test_death_spots_legacy_file_and_ttl():
    from nb import deaths
    p = _tmp("death_spots.json")
    now = time.time()
    _dump(p, [[-5041.0, -2900.0, now - 60, 4], [-5000.0, -2900.0, now - 4000, 2], [-5010.0, -2910.0, now - 30, 1]])
    assert deaths.shared_deaths(p, who=1) == [(-5041.0, -2900.0)]       # not our own, not expired
    deaths.note_shared_death(-6000.04, 400.06, path=p, now=now, who=1)
    d = _load(p)
    assert [e[3] for e in d] == [4, 1, 1] and d[-1][:2] == [-6000.0, 400.1]    # the old one dropped on write
    assert deaths.shared_deaths(p, who=4) == [(-5010.0, -2910.0), (-6000.0, 400.1)]


def test_dead_talk_and_death_skips_legacy_files():
    from nb import rxp_follow as rf
    p = _tmp("dead_talk.json")
    now = time.time()
    _dump(p, {"Father Gavin": {"2": now - 100, "3": now - 7 * 3600}})
    old = rf.PERSIST
    rf.PERSIST = True
    try:
        assert rf._dead_talk_bots("Father Gavin", p) == {"2"}
        assert rf._dead_talk_bots("nobody", p) == set()
        rf._dead_talk_note("Father Gavin", p)
        assert rf._dead_talk_bots("Father Gavin", p) == {"2", str(rf.inst.INST)}
    finally:
        rf.PERSIST = old
    d = _load(p)
    assert set(d["Father Gavin"]) == {"2", "3", str(rf.inst.INST)}      # layout kept, nothing pruned


def test_rx_deaths_legacy_file_round_trip():
    from nb import rxp_follow as rf
    p = _tmp("rx_deaths.json")
    real_now = time.time()
    _dump(p, [{"key": ["05-11 Dun Morogh", 272, 3], "n": 2, "t": real_now - 60},
              {"key": ["old", 1, 1], "n": 5, "t": real_now - 9000}])
    b = rf.RxpFollow()
    b._rx_deaths_file = lambda: p
    b._rx = {"key": ("05-11 Dun Morogh", 300, 4)}
    b._rx_deaths = b._rx_deaths_load()
    assert b._rx_deaths == {("05-11 Dun Morogh", 272, 3): 2}
    b._rx_deaths[("05-11 Dun Morogh", 300, 4)] = 1
    b._rx_deaths_save()
    d = {tuple(e["key"]): e for e in _load(p)}
    assert set(d) == {("05-11 Dun Morogh", 272, 3), ("05-11 Dun Morogh", 300, 4)}
    assert abs(d[("05-11 Dun Morogh", 272, 3)]["t"] - (real_now - 60)) < 1e-6    # kept its time
    assert d[("05-11 Dun Morogh", 300, 4)]["n"] == 1


# ------------------------------------------------------------------ util
def test_swallow_logs_once_per_tag_and_keeps_flow():
    logs = []
    for _ in range(3):
        with util.swallow("test.swallow.once", logs.append) as sw:
            raise KeyError("x")
        assert isinstance(sw.error, KeyError)
    assert len(logs) == 1 and "KeyError" in logs[0] and "test.swallow.once" in logs[0]
    try:
        with util.swallow("test.swallow.narrow", logs.append, exc=OSError):
            raise ValueError("not mine")
        assert False
    except ValueError:
        pass
    assert util.swallowed("test.swallowed", RuntimeError("a"), logs.append)
    assert not util.swallowed("test.swallowed", RuntimeError("b"), logs.append)


def test_deadline_and_cooldown():
    clk = SimpleNamespace(t=100.0)
    clk.time = lambda: clk.t
    d = util.Deadline(1.5, clock=clk)
    assert d.running() and not d.passed()
    clk.t = 101.5
    assert not d.running() and not d.passed()                # exactly at the limit: neither (< and > as before)
    clk.t = 101.6
    assert d.passed() and abs(d.left() + 0.1) < 1e-9
    d.restart()
    assert d.running() and d.elapsed() == 0.0
    c = util.Cooldown(5.0, clock=clk)
    assert c.ready() and not c.ready()
    clk.t += 5.1
    assert c.ready()
    c.reset()
    assert c.ready()


# ------------------------------------------------------------------ move: path strategies
def _plan_mover(nm_find, zones=((-6000.0, 450.0, 400.0, 10.0),)):
    from nb import move
    m = move.Mover.__new__(move.Mover)
    m.logs, m.calls = [], []
    m.log = m.logs.append
    m.gates, m.zones, m._wedges = [], list(zones), []
    m.state = lambda: SimpleNamespace(x=-6122.0, y=491.0, z=402.0, flags=0, level=5)
    m.nm = SimpleNamespace(ground_z=lambda x, y, hint=None: 400.0, island_exit=lambda p, r: None,
                           find_path=nm_find, tile_at=lambda rx, rz: None, reload_climbs=lambda: 0)
    m.floors_at = lambda x, y: []
    m.bad = SimpleNamespace(active=lambda bbox: [])
    for name in move.Mover.PATH_STRATEGIES:
        fn = getattr(m, "_ps_" + name)

        def wrap(plan, fn=fn, name=name):
            m.calls.append(name)
            return fn(plan)
        setattr(m, "_ps_" + name, wrap)
    return m, move


def test_path_strategy_order_all_fail():
    m, move = _plan_mover(lambda a, b, **kw: None)
    old = move.NB_MESH
    move.NB_MESH = move.navmesh.TCF_MMAP_DIR                 # the Forever mesh: no server-mesh fallback
    try:
        assert m._path(-6013.0, 468.0, 400.0) is None
    finally:
        move.NB_MESH = old
    assert tuple(m.calls) == move.Mover.PATH_STRATEGIES, m.calls
    assert move.Mover.PATH_STRATEGIES == ("alt_mesh", "gate", "island_start", "main", "no_zones", "start_floor",
                                          "goal_island", "server_mesh")
    assert m.path_strategy is None


def test_path_strategy_first_success_stops_and_is_logged():
    # danger zones close every route; without them there is one: main fails, no_zones finds it
    m, move = _plan_mover(lambda a, b, **kw: None if kw.get("dangers") else [a, (b[0], b[1], 400)])
    p = m._path(-6013.0, 468.0, 400.0)
    assert m.calls == ["alt_mesh", "gate", "island_start", "main", "no_zones"], m.calls
    assert m.path_strategy == "no_zones" and p[-1] == (-6013.0, 468.0, 400.0)
    assert all(isinstance(v, float) for q in p for v in q)
    assert any("by strategy no_zones" in l for l in m.logs), m.logs
    m2, _ = _plan_mover(lambda a, b, **kw: [a, b])
    m2._path(-6013.0, 468.0, 400.0)
    assert m2.calls[-1] == "main" and m2.path_strategy == "main"
    assert not any("by strategy" in l for l in m2.logs)          # the normal case: no extra log line


def test_path_strategy_goal_island_learns_a_gate():
    gate = (-6020.0, 470.0, 401.0)
    m, move = _plan_mover(lambda a, b, **kw: [a, b] if tuple(b) == gate else None, zones=())
    m.nm.island_exit = lambda p, r: gate if abs(p[0] + 6013.0) < 1 else None
    old = move.NB_MESH
    move.NB_MESH = move.navmesh.TCF_MMAP_DIR
    try:
        p = m._path(-6013.0, 468.0, 400.0)
    finally:
        move.NB_MESH = old
    assert m.path_strategy == "goal_island" and p[-1] == gate
    assert "no_zones" in m.calls and m.calls[-1] == "goal_island"   # no zones: no_zones ran and passed
    assert m.gates == [(gate, (-6013.0, 468.0), False)]


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(f"{n} tests passed")
