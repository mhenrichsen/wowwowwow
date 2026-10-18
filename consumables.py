"""What we can actually eat and drink, straight from the world DB.

Matching food against a hand-written list of item entries kept failing. The
vendor ladder names six breads; the bags hold Tough Jerky, a looted haunch
and whatever the last quest paid out, and anything the ladder did not name
was invisible - the warrior sat on natural regen with nine rations in its
bags. The server already knows the answer: an item that restores health
casts a spell called "Food", one that restores mana casts "Drink", and that
spell's base points rank the ranks against each other. So ask it once.

"restore" is the spell's effectBasePoints1, not a health total - the real
tooltip number is a multiple of it. It is monotonic within a kind, which is
all we need to answer "which of these is the best thing to eat".
"""

from __future__ import annotations

import worlddb

CLIENT_BUILD = 5875          # 1.12.1, the only build this bot talks to


class Consumable:
    __slots__ = ("entry", "name", "kind", "restore", "req_level")

    def __init__(self, entry, name, kind, restore, req_level):
        self.entry, self.name, self.kind = entry, name, kind
        self.restore, self.req_level = restore, req_level

    def __repr__(self):
        return f"{self.name} ({self.kind} {self.restore}, lvl {self.req_level})"


_table: dict[int, Consumable] | None = None
_rejected: dict[int, str] = {}      # entries the game refused, this session only


def table() -> dict[int, Consumable]:
    """Every food and drink item in the world DB, by entry. Queried once.

    Both item_template and spell_template carry one row per content patch,
    so an undeduplicated join returns several rows per item with different
    numbers in them - that is how a drink came back as both 734 and 799.
    Take the newest row of each.
    """
    global _table
    if _table is None:
        rows = worlddb._query(f"""
            SELECT it.entry, it.name, sp.name, sp.effectBasePoints1, it.required_level
            FROM item_template it
            JOIN (SELECT entry, MAX(patch) p FROM item_template GROUP BY entry) mi
              ON mi.entry = it.entry AND mi.p = it.patch
            JOIN (SELECT entry, MAX(build) b FROM spell_template
                  WHERE build <= {CLIENT_BUILD} GROUP BY entry) ms
              ON ms.entry = it.spellid_1
            JOIN spell_template sp ON sp.entry = ms.entry AND sp.build = ms.b
            WHERE sp.name IN ('Food', 'Drink')
              AND it.class = 0 AND it.spelltrigger_1 = 0
        """)
        _table = {int(r[0]): Consumable(int(r[0]), r[1], r[2], int(r[3]) + 1, int(r[4]))
                  for r in rows}
    return _table


_ladder: dict[str, list[tuple[int, int, int]]] = {}


def vendor_ladder(kind: str, min_share: float = 0.5) -> list[tuple[int, int, int]]:
    """[(required_level, entry, buy_price)] - what to buy to eat or drink, by
    the level it becomes usable, cheapest tier first.

    The same derivation as table(), plus the vendor side: an item is only on
    the ladder if the vendors who sell both food and drink carry it, because a
    town trip buys both from one npc. Within a required_level tier the items
    are interchangeable (the 1.12 tiers are flat: every level 15 food restores
    115 and costs 500), so the tie-break is availability, then price.

    One query for both kinds, cached for the session (about two seconds the
    first time), so ask for it once at start-up, not on every town trip.

    This replaces two hand-written lists. The food one happened to be complete;
    the drink one stopped at required_level 25, so every mana class drank rank
    3 Sweet Nectar from level 25 to 60 against a mana pool that grows about
    sixfold on the way - Moonberry Juice (35) and Morning Glory Dew (45) are on
    this server and were simply never typed in. Nothing in the code could know
    that, which is the reason for deriving it instead of listing it.
    """
    kind = kind.title()
    if kind not in _ladder:
        rows = worlddb._query(f"""
            SELECT it.entry, it.required_level, it.buy_price,
                   sp.name, sp.effectBasePoints1,
                   (SELECT COUNT(*) FROM npc_vendor v
                     WHERE v.item = it.entry AND EXISTS (
                       SELECT 1 FROM npc_vendor v2
                        JOIN item_template d ON d.entry = v2.item
                        JOIN (SELECT entry, MAX(build) b FROM spell_template
                               WHERE build <= {CLIENT_BUILD} GROUP BY entry) md
                          ON md.entry = d.spellid_1
                        JOIN spell_template ds ON ds.entry = md.entry AND ds.build = md.b
                       WHERE v2.entry = v.entry AND d.class = 0
                         AND d.spelltrigger_1 = 0 AND ds.name = 'Drink'))
            FROM item_template it
            JOIN (SELECT entry, MAX(patch) p FROM item_template GROUP BY entry) mi
              ON mi.entry = it.entry AND mi.p = it.patch
            JOIN (SELECT entry, MAX(build) b FROM spell_template
                  WHERE build <= {CLIENT_BUILD} GROUP BY entry) ms
              ON ms.entry = it.spellid_1
            JOIN spell_template sp ON sp.entry = ms.entry AND sp.build = ms.b
            WHERE sp.name IN ('Food', 'Drink')
              AND it.class = 0 AND it.spelltrigger_1 = 0 AND it.buy_price > 0
        """)
        tiers: dict[str, dict[int, list]] = {}
        for e, req, price, k, restore, vendors in rows:
            if int(vendors) <= 0:
                continue
            tiers.setdefault(k, {}).setdefault(int(req), []).append(
                (int(restore) + 1, int(vendors), int(price), int(e)))
        for k, by_req in tiers.items():
            out = []
            for req in sorted(by_req):
                cands = by_req[req]
                # keep what a fair share of those vendors carry: a drink one
                # innkeeper in Booty Bay stocks is not a ladder rung
                top = max(c[1] for c in cands)
                cands = [c for c in cands if c[1] >= max(2, top * min_share)]
                if not cands:
                    continue
                restore, vendors, price, entry = sorted(
                    cands, key=lambda c: (-c[0], -c[1], c[2], c[3]))[0]
                if out and restore <= out[-1][0]:
                    continue            # a tier that is not an improvement
                out.append((restore, req, entry, price))
            _ladder[k] = [(req, entry, price) for _, req, entry, price in out]
        _ladder.setdefault(kind, [])
    return list(_ladder.get(kind, []))


def quest_items(quest_ids) -> set[int]:
    """Items the quests in our log want handed in.

    Eighteen ordinary foods are also quest objectives - Refreshing Spring
    Water among them, which is the drink every vendor sells. Blanket-banning
    them would starve us, so only refuse the ones a quest we are ON needs.
    """
    ids = [int(q) for q in quest_ids]
    if not ids:
        return set()
    cols = "ReqItemId1, ReqItemId2, ReqItemId3, ReqItemId4"
    rows = worlddb._query(
        f"SELECT {cols} FROM quest_template "
        f"WHERE entry IN ({','.join(map(str, ids))})")
    return {int(v) for r in rows for v in r if v and int(v) > 0}


def reject(entry: int, why: str) -> None:
    """Stop offering an item the game would not let us consume.

    The DB says an item is food; the client is the one that decides. When a
    press produces no sitting and no aura, believe the client and move to
    the next best thing instead of pressing the same slot every 27 seconds.
    """
    _rejected[int(entry)] = why


def rejected() -> dict[int, str]:
    return dict(_rejected)


def best(items, level: int, kind: str, avoid=()) -> list[Consumable]:
    """Usable food (or drink) from these bags, richest first.

    "items" is {entry: count} as game.bag_items returns it.
    """
    t = table()
    if not isinstance(items, dict):
        items = {int(e): 1 for e in items}
    avoid = {int(a) for a in avoid}
    out = [c for e, n in items.items()
           if n > 0 and (c := t.get(int(e))) is not None
           and c.kind == kind and c.req_level <= level
           and int(e) not in avoid and int(e) not in _rejected]
    # richest bite first, and on a tie the stack we have most of: a single
    # Healing Herb should not outrank nine rations of the same value
    out.sort(key=lambda c: (-c.restore, -items[c.entry]))
    return out
