"""Water and food stock (rest review 2026-10-15).

The four shaman bots spent 20-35% of three hours resting on natural regen and never drank or ate: the eat / drink keys
named level-1 items (Refreshing Spring Water, Tough Hunk of Bread) the bags did not hold, rest() looked for them on the
action bar only, and no vendor visit bought any (the addon bought RestedXP .collect items only). Now:

  BotEye (build 13)  reports the best usable water / food in the bags and their counts (blocks 166-169, eye.State
                     drink_id / drinks / drinks_cur / drink_want ...), points the eat / drink keys at the best owned
                     item, and at every merchant that sells a current tier tops the bags up to DRINK_TARGET /
                     FOOD_TARGET, keeping RESERVE_PER_LEVEL copper per level for training and repairs.
  this module        what is low (low_kinds), whether a top-up is affordable, and which vendors sell a current tier
                     (tcf_world npc_vendor x consumables.table(): an item whose use spell is "Food" / "Drink").
  Bot.vendor_step    a stock errand when a kind is under LOW and a seller is within WALK_MAX yd of walk.

"Current tier": required level within SPAN of ours (the vendor tiers are 1 / 5 / 15 / 25 / ...: at level 13 Ice Cold
Milk and Freshly Baked Bread count, Tough Jerky (level 1) is eaten but not counted as stock).
"""

from __future__ import annotations

BUILD = 13                       # BotEye build that paints blocks 166-169 (BotEye.lua BOTEYE_BUILD)
DRINK_TARGET, FOOD_TARGET = 40, 20   # (BotEye.lua SUP_TARGET) - 20 / 10 lasted under an hour of mob country
LOW = 4                          # current-tier items under this: a stock errand
SPAN = 10                        # levels: an item this far under ours still counts as stock (BotEye.lua SUP_SPAN)
SHARE = 0.25                    # at most this share of the money per vendor visit (BotEye.lua SUP_SHARE): training
#                                 costs ~27s at level 14, 39s at 16, 100s at 18 - stock must not eat it
RESERVE_PER_LEVEL = 50           # copper per level never spent on stock (BotEye.lua SUP_RESERVE, buy_bag's reserve)
# ... when the kind is all gone: resting on regen costs more than a later training trip (run 6, 2026-10-16: bot 2 at
# 333-425c, level 6-7, had 0 drinks for 50 min under the 300-350c reserve; BotEye.lua SUP_RESERVE_OUT / SUP_SHARE_OUT)
RESERVE_OUT, SHARE_OUT = 20, 0.5
WALK_MAX = 450.0                 # yd of walk a stock-only errand may take (Thelsamar's inn from the Loch's hunt spots)
WALK_MAX_OUT = 1000.0            # ... when a low kind is all gone: resting on regen costs more than the walk
#                                 (inst1 15:20: sellers 624 / 769 yd away, "later" every 5 min with 0 drinks)
DETOUR = 150.0                   # yd: a vendor that sells the low kind beats a nearer one by this much
KINDS = ("drink", "food", "arrow", "bullet")
# hunter ammo (BotEye fill_ammo / buy_ammo, AMMO_TARGET 1000): a stock errand under AMMO_LOW shots
AMMO_LOW = 300
AMMO_SUB = {2: "arrow", 3: "bullet"}     # item_template class 6 (projectile) subclass


def low_of(kind) -> int:
    return AMMO_LOW if kind in ("arrow", "bullet") else LOW


def known(s) -> bool:
    """The client's BotEye paints the stock blocks (build >= BUILD)."""
    rx = getattr(s, "rxp", None)
    return bool(rx and getattr(rx, "build", 0) >= BUILD)


def counts(s) -> dict:
    """{kind: (usable, current tier, wanted)}."""
    out = {"drink": (getattr(s, "drinks", 0), getattr(s, "drinks_cur", 0), getattr(s, "drink_want", False)),
           "food": (getattr(s, "foods", 0), getattr(s, "foods_cur", 0), getattr(s, "food_want", False))}
    if getattr(s, "ammo_want", False):
        n = getattr(s, "ammo", 0)
        out["bullet" if getattr(s, "ammo_bullets", False) else "arrow"] = (n, n, True)
    return out


def have(s, kind) -> bool | None:
    """Something of this kind to drink / eat now. Unknown (an older BotEye): None."""
    if not known(s):
        return None
    return counts(s)[kind][0] > 0


def low_kinds(s) -> list:
    """The kinds the character uses whose current-tier count is under LOW (nothing when the addon does not say)."""
    if not known(s):
        return []
    return [k for k, (_n, cur, want) in counts(s).items() if want and cur < low_of(k)]


def walk_max(s, kinds) -> float:
    """The walk a stock-only errand for `kinds` may take: WALK_MAX_OUT when one of them is all gone."""
    return WALK_MAX_OUT if any(have(s, k) is False for k in kinds) else WALK_MAX


def reserve(level) -> int:
    return RESERVE_PER_LEVEL * max(1, int(level or 1))


def tier_items(level, kind, table) -> dict:
    """{entry: unit price} of the current-tier items of this kind (required level SPAN..level), from
    {entry: (kind, required level, unit price)}."""
    lv = int(level or 1)
    lo = 0 if kind in ("arrow", "bullet") else lv - SPAN      # any ammo shoots (BotEye counts every tier)
    return {e: price for e, (k, req, price) in table.items() if k == kind and lo <= req <= lv}


def unit_price(level, kind, table) -> float | None:
    """What one current-tier item of this kind costs at the best tier (the highest required level) - the one BotEye
    buys; None when the table knows no such item."""
    it = {e: table[e] for e in tier_items(level, kind, table)}
    if not it:
        return None
    top = max(req for _k, req, _p in it.values())
    return min(p for _k, req, p in it.values() if req == top)


def lot_cost(level, kind, table, sold=None) -> float:
    """Copper for LOW items of the best tier a vendor sells, in whole vendor lots (a 1.12 vendor sells water / food
    5 per lot: BotEye buys whole lots only, so LOW * unit could pass here while the addon cannot buy a single lot).
    sold: the item entries some vendor sells (None: all priced ones) - the table also holds conjured water (price 0)
    and cooked food (Crispy Lizard Tail, level 12, 500c) that no vendor has: as the "best tier" they made a drink
    top-up free (always affordable) and a food one 20s at level 13."""
    it = {e: p for e, p in tier_items(level, kind, table).items() if p > 0 and (sold is None or e in sold)}
    if not it:
        return 0.0
    top = max(table[e][1] for e in it)
    e = min((e for e in it if table[e][1] == top), key=lambda e: it[e])
    lot = max(1, int(_lots.get(e, 1)))
    return it[e] * lot * -(-low_of(kind) // lot)


def affordable(s, kinds, table, sold=None) -> list:
    """The kinds of `kinds` whose LOW items are affordable over the reserve (drink first: the money goes to it first).
    sold: as lot_cost."""
    money = getattr(s, "money", 0) or 0
    lv = max(1, int(getattr(s, "level", 1) or 1))
    c = counts(s)
    out, spent = [], 0.0
    for k in kinds:
        gone = c.get(k, (1,))[0] == 0
        left = min(money - (RESERVE_OUT * lv if gone else reserve(lv)), int(money * (SHARE_OUT if gone else SHARE)))
        left -= spent
        cost = lot_cost(lv, k, table, sold)
        if left >= cost and left > 0:
            out.append(k)
            spent += cost
    return out


def sellers(level, kinds, table, vendor_items) -> dict:
    """{npc entry: set of kinds} of the vendors that sell a current-tier item of one of `kinds`, from vendor_items
    {npc entry: set of item entries}."""
    want = {k: set(tier_items(level, k, table)) for k in kinds}
    out = {}
    for npc, items in vendor_items.items():
        ks = {k for k, ids in want.items() if ids & items}
        if ks:
            out[npc] = ks
    return out


# ------------------------------------------------------------ world DB (cached for the session)
_table = None
_vendor_items = None
_lots: dict = {}                 # entry -> items per vendor lot (item_template buy_count), filled by item_table()


def item_table() -> dict:
    """{entry: (kind, required level, unit price in copper)} of every food / drink item (consumables.table(): the use
    spell is "Food" / "Drink"; mangos item_template buy_price is per buy_count lot)."""
    global _table
    if _table is None:
        import consumables
        import worlddb
        t = consumables.table()
        prices = {}
        if t:
            rows = worlddb._query("SELECT entry, MAX(buy_price), MAX(buy_count) FROM item_template WHERE entry IN (%s) "
                                  "GROUP BY entry" % ",".join(str(e) for e in t))
            prices = {int(r[0]): int(r[1]) / max(1, int(r[2])) for r in rows}
            _lots.update({int(r[0]): max(1, int(r[2])) for r in rows})
        _table = {e: (c.kind.lower(), int(c.req_level), prices.get(e, 0.0)) for e, c in t.items()}
        # arrows / bullets (projectiles), priced per buy_count lot like the food
        for r in worlddb._query("SELECT entry, MAX(subclass), MAX(required_level), MAX(buy_price), MAX(buy_count) "
                                "FROM item_template WHERE class=6 AND subclass IN (2,3) AND buy_price>0 GROUP BY entry"):
            e = int(r[0])
            _table[e] = (AMMO_SUB[int(r[1])], int(r[2]), int(r[3]) / max(1, int(r[4])))
            _lots[e] = max(1, int(r[4]))
    return _table


def vendor_items() -> dict:
    """{npc entry: set of food / drink item entries it sells} (tcf_world npc_vendor)."""
    global _vendor_items
    if _vendor_items is None:
        import worlddb
        t = item_table()
        out = {}
        if t:
            rows = worlddb._query_db("tcf_world", "SELECT entry, item FROM npc_vendor WHERE item IN (%s)"
                                     % ",".join(str(e) for e in t))
            for npc, item in rows:
                out.setdefault(int(npc), set()).add(int(item))
        _vendor_items = out
    return _vendor_items
