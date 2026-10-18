"""nb/forecast.py: fight costs from the book, n-mob scaling, heals, verdicts, walk encounters."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nb import forecast as fc, threat  # noqa: E402


def lone(npc=100, gap=0, s=20, hp=30, mana=50, k=6):
    return [{"t": 0, "npc": npc, "gap": gap, "n": 1, "s": s, "hp": hp, "mana": mana, "out": "killed"}
            for _ in range(k)]


def test_cost_tiers():
    b = fc.Book(rows=lone(npc=7, hp=40, k=3) + lone(npc=8, hp=20, k=5))
    assert b.cost(7, 0).src == "npc" and b.cost(7, 0).hp == 40
    assert b.cost(9, 0).src == "gap"                      # 8 lone kills at gap 0
    c = b.cost(9, 2)
    assert c.src == "all" and c.hp > b.cost(9, 0).hp      # 2 levels up costs more
    assert fc.Book(rows=[]).cost(1, 0).src == "default"


def test_fight_verdicts():
    b = fc.Book(rows=lone(hp=30, mana=60))
    assert fc.fight(b, 15, 15, 100, 100, 100, 1).verdict == "ok"
    # two at once: hp x 3, mana x 2 -> 120% mana: out of it
    f2 = fc.fight(b, 15, 15, 100, 100, 100, 2)
    assert f2.n == 2 and f2.end_mana < 0 and f2.verdict != "ok"
    # low mana start: lose
    assert fc.fight(b, 15, 15, 100, 70, 20, 1).verdict == "lose"


def test_heal_spends_mana():
    b = fc.Book(rows=lone(hp=80, mana=10))
    f = fc.fight(b, 15, 15, 100, 100, 100, 1, heal_pct=17)
    assert f.heals >= 1 and f.end_hp >= fc.HEAL_FLOOR - 1 and f.end_mana < 90


def test_book_persists():
    import tempfile
    p = os.path.join(tempfile.mkdtemp(), "book.json")
    b = fc.Book(p)
    b.add(5, 0, 1, 20, 30, 50, "killed")
    assert fc.Book(p).rows[-1]["npc"] == 5


class _Sp:
    def __init__(self, pts):
        self.pts = pts

    def near(self, x, y, r, live=True):
        import math
        return sorted(((math.hypot(p.x - x, p.y - y), p) for p in self.pts
                       if math.hypot(p.x - x, p.y - y) <= r), key=lambda t: t[0])


def _pt(x, y, lv=15, fac=1):
    return threat.Point(x, y, 0.0, 0.0, frozenset({200}), lv, lv, 0, fac)


def test_walk_encounters():
    a, b2, far = _pt(60, 3), _pt(64, 6), _pt(0, 300)
    m = threat.Model(sp=_Sp([a, b2, far]))
    b = fc.Book(rows=lone(hp=30, mana=50))
    enc = fc.walk(m, b, 15, [(0, 0), (140, 0)], 100, 40)
    assert len(enc) == 1                                  # the pair is one encounter, the far spawn none
    e = enc[0]
    assert e.n == 2 and 30 < e.d < 60 and e.arrive_mana > 40
    assert fc.walk(m, b, 15, [(0, 0), (0, -100)], 100, 40) == []
    assert "lvl 15 (+1)" in fc.fmt_walk(enc)


def test_mid_two_on_us():
    b = fc.Book(rows=lone(hp=30, mana=50, s=20))
    one = fc.mid(b, 15, 90, 80, [(15, 60, 100, True)], target_hp=60)
    two = fc.mid(b, 15, 90, 80, [(15, 60, 100, True), (15, 100, 100, False)], target_hp=60)
    assert one.n == 1 and two.n == 2 and two.end_hp < one.end_hp and two.s > one.s
    # the live rates win over the book: taking 8%/s from 2 mobs at 40% hp is lost
    bad = fc.mid(b, 15, 40, 30, [(15, 90, 100, True), (15, 100, 100, False)], target_hp=90, inc=8.0, out=4.0)
    assert bad.verdict == "lose"
    assert fc.mid(b, 15, 90, 80, []).verdict == "ok"


def test_fleebook_and_decide():
    rows = [{"t": 0, "hp": 85, "n": 2, "out": "clear"}] * 8 + [{"t": 0, "hp": 65, "n": 2, "out": "dead"}] * 8
    fb = fc.FleeBook(rows=rows)
    assert fb.p_dead(85, 2)[0] < 0.3 < 0.7 < fb.p_dead(65, 2)[0]
    m = fc.Mid(2, 40, -10, -30, "lose", fc.P_DIE["lose"])
    assert fc.decide(m, fb.p_dead(85, 2)[0])[0] == "flee"       # lost fight, a flee from 85% gets away
    m_ok = fc.Mid(1, 10, 60, 50, "ok", fc.P_DIE["ok"])
    assert fc.decide(m_ok, 0.1)[0] == "stay"


def test_fleebook_shared_file():
    import tempfile
    p = os.path.join(tempfile.mkdtemp(), "flee.json")
    a, b = fc.FleeBook(p), fc.FleeBook(p)
    a.add(80, 1, "clear")
    b.add(70, 2, "dead")                                      # b reads a's row before writing
    assert len(fc.FleeBook(p).rows) == 2


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
