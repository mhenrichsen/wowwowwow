"""move.Mover.path(): a long plan is given up when combat starts or hp drops during it (peer review 2026-10-15:
40 s plans with the character standing still, inst2 died during one), but not one planned in combat (flee / chase)."""

from types import SimpleNamespace

from nb import move


class FakeMesh:
    deadline = None
    interrupt = None

    def __init__(self, states, me):
        self.states, self.me = states, me

    def run(self):
        """A search that polls interrupt() until it stops it (or finds a path after 50 polls)."""
        for _ in range(50):
            move.time.sleep(0.0)
            self.me._clock[0] += 0.6
            if self.interrupt and self.interrupt():
                return None
        return [(1.0, 1.0, 0.0)]


def mover(states):
    m = object.__new__(move.Mover)
    m._clock = [1000.0]
    seq = iter(states)
    last = [states[0]]

    def state():
        try:
            last[0] = next(seq)
        except StopIteration:
            pass
        return last[0]
    m.state = state
    m.log = lambda *_a: LOGS.append(_a[0])
    m.zones, m._nopath, m._plan_abort, m._abort = (), {}, None, None
    m.nm = FakeMesh(states, m)
    m._nopath_key = lambda *a: None
    m._path = lambda x, y, z=None: m.nm.run()
    return m


LOGS = []


def st(combat=False, hp=100.0):
    return SimpleNamespace(x=0.0, y=0.0, z=0.0, combat=combat, dead=False, hp_pct=hp, level=13)


def run(m):
    t = move.time.time
    move.time.time = lambda: m._clock[0]
    try:
        return m.path(900.0, 0.0)
    finally:
        move.time.time = t


def test_combat_stops_a_plan():
    m = mover([st(), st(), st(), st(combat=True)])
    assert run(m) is None and m.plan_hit == "combat", m.plan_hit


def test_hp_drop_stops_a_plan():
    m = mover([st(), st(hp=99), st(hp=90)])
    assert run(m) is None and m.plan_hit.startswith("hp"), m.plan_hit


def test_plan_in_combat_runs_on():
    m = mover([st(combat=True, hp=60), st(combat=True, hp=40)])
    assert run(m) == [(1.0, 1.0, 0.0)] and m.plan_hit is None


def test_sealed_goal_is_answered_at_once():
    """navmesh._goal_sealed: the ledge above Loch Modan (inst2 15:47, 40-93 s of A* for "none") is a closed pocket
    once the climbs the bots learned they cannot walk are blocked; without them, and for a goal on the open map,
    it is not (real Forever mesh; LEARNED = nb's climb file on 2026-10-15)."""
    import time as _t
    import navmesh
    nm = navmesh.NavMesh(move.NB_MESH, 0)
    pt = lambda x, y: (x, y, nm.ground_z(x, y))     # noqa: E731
    s = pt(-5300, -2950)
    a = nm.nearest_poly(*s)[0]
    ledge = nm.nearest_poly(-5675.0, -2871.0, 476.9)[0]
    t0 = _t.time()
    assert nm._goal_sealed(a, ledge, LEARNED, s) and _t.time() - t0 < 0.5
    nm._seal_open = {}
    assert not nm._goal_sealed(a, ledge, set(), s)
    assert not nm._goal_sealed(a, nm.nearest_poly(*pt(-4845, -3475))[0], LEARNED, s)


LEARNED = {(((9, 19), 5199), ((9, 19), 5202)), (((11, 20), 4751), ((11, 20), 4752)), (((11, 20), 4688), ((11, 20), 4750)),
           (((8, 19), 3414), ((8, 19), 3412)), (((11, 20), 4688), ((11, 20), 4687)), (((8, 19), 4623), ((8, 19), 4625)),
           (((11, 20), 4750), ((11, 20), 4688)), (((9, 21), 10), ((9, 21), 12)), (((8, 19), 3250), ((8, 19), 3251)),
           (((8, 20), 2094), ((8, 20), 2215)), (((8, 19), 3099), ((8, 19), 3097)), (((8, 19), 2851), ((8, 19), 2855)),
           (((9, 21), 10), ((9, 21), 13)), (((11, 20), 4750), ((11, 20), 4751))}


if __name__ == "__main__":
    n = 0
    for k, fn in list(globals().items()):
        if k.startswith("test_") and callable(fn):
            fn()
            print("ok", k)
            n += 1
    print(n, "tests passed")
