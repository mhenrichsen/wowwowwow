"""Offline checks of the water / food stock (nb/stock.py), the rest's drink / eat / stand-up rules, the stock vendor
errand, and BotEye build 13's side (blocks 166-169, the eat / drink macros, buy_supplies) run under lupa.

Run: python -m nb.test_stock   (from the wowbot folder). Fake states, no game, no DB; the Lua part runs the repo copy
addon/BotEye/BotEye.lua (skipped without lupa). Sends no keys.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nb import bot as botmod            # noqa: E402
from nb.bot import Bot                  # noqa: E402
from nb import eye, hazards, stock, threat   # noqa: E402

LOGS: list[str] = []
botmod.log = lambda m: LOGS.append(m)
REPO_BOTEYE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "addon", "BotEye",
                           "BotEye.lua")

# {entry: (kind, required level, unit price)} - the 1.12 tiers (mangos item_template, 5 per lot)
TABLE = {159: ("drink", 1, 5.0), 1179: ("drink", 5, 25.0), 1205: ("drink", 15, 100.0),
         117: ("food", 1, 5.0), 4540: ("food", 1, 5.0), 4541: ("food", 5, 25.0), 4542: ("food", 15, 100.0)}
VENDORS = {1682: {159, 1179}, 6734: {159, 1179, 1205, 4540, 4541, 4542}, 3291: {4540, 4541, 4542}, 1691: {159}}


def rxp(build=13):
    return SimpleNamespace(build=build)


def st(level=13, hp=100.0, mana=100.0, combat=False, build=13, drinks=0, foods=0, drinks_cur=None, foods_cur=None,
       drink_id=0, food_id=0, drinking=False, eating=False, money=5000, plates=(), **kw):
    d = dict(level=level, hp_pct=hp, mana_pct=mana, mana=5 * mana, manamax=500, combat=combat, plates=list(plates),
             x=0.0, y=0.0, z=0.0, dead=False, ghost=False, has_target=False, t_dead=False, flags=0, actions=[],
             rxp=rxp(build) if build else None, drinks=drinks, foods=foods,
             drinks_cur=drinks if drinks_cur is None else drinks_cur,
             foods_cur=foods if foods_cur is None else foods_cur, drink_id=drink_id, food_id=food_id,
             drink_want=True, food_want=True, drinking=drinking, eating=eating, money=money, bags_free=10, dur=100,
             msg="", ui_open=lambda n: True)
    d.update(kw)
    return SimpleNamespace(**d)


def make_bot(states):
    b = Bot.__new__(Bot)
    seq = list(states)
    b.s = lambda: seq.pop(0) if len(seq) > 1 else seq[0]
    b.wanted, b.grind_ok, b.blacklist, b.avoid, b.visited = set(), True, {}, [], {}
    b.walk_rest, b.pull_anyway_t, b.last_multi, b.fight_maxatt = 0.0, 0.0, False, 0
    b.fight_costs, b._line_log = botmod.deque(maxlen=botmod.FIGHT_COSTS_N), None
    b.rot = SimpleNamespace(heal_next=0.0, bar=lambda s: {})
    b.pressed = []
    b.keys = SimpleNamespace(press=lambda k, hold=0.04: b.pressed.append(k))
    b.cast = lambda name, wait_done=True: False
    b.has_item = lambda e, s: False
    b.buffs = lambda: False
    b.W = 1920
    b.tm = threat.Model(threat.Spawns(rows=[], hostile=lambda f: True))
    b.hazards = hazards.Hazards(persist=False)
    return b, seq


# ------------------------------------------------------------ eye.py: blocks 166-169
def test_eye_decodes_supplies():
    v = [0] * eye.NBLOCKS
    v[0], v[24] = eye.MARKER, 8 | 4
    v[166], v[167] = 1179, 4541
    v[168] = 23 + 17 * 2048 + (1 << 22)
    v[169] = 41 + 0 * 2048 + (1 << 22)
    s = eye.decode(v, [0.5] * eye.NBARS)
    assert (s.drink_id, s.food_id, s.drinks, s.drinks_cur, s.foods, s.foods_cur) == (1179, 4541, 23, 17, 41, 0)
    assert s.drink_want and s.food_want and s.drinking and s.eating
    old = eye.decode([eye.MARKER] + [0] * (eye.NBLOCKS - 1), [0.5] * eye.NBARS)   # an older BotEye paints 0
    assert (old.drink_id, old.drinks, old.drink_want, old.drinking) == (0, 0, False, False)


# ------------------------------------------------------------ nb/stock.py
def test_tiers_prices_sellers():
    assert set(stock.tier_items(13, "drink", TABLE)) == {1179}          # 159 is level 1: eaten, not stocked
    assert set(stock.tier_items(15, "drink", TABLE)) == {1179, 1205}
    assert stock.unit_price(13, "drink", TABLE) == 25.0 and stock.unit_price(15, "food", TABLE) == 100.0
    assert stock.unit_price(3, "food", TABLE) == 5.0
    assert stock.sellers(13, ["drink"], TABLE, VENDORS) == {1682: {"drink"}, 6734: {"drink"}}
    assert stock.sellers(13, ["drink", "food"], TABLE, VENDORS)[3291] == {"food"}
    assert 1691 not in stock.sellers(13, ["drink", "food"], TABLE, VENDORS)    # water of level 1 only


def test_low_and_affordable():
    assert stock.low_kinds(st(build=12)) == [] and stock.have(st(build=12), "drink") is None   # older BotEye
    assert stock.low_kinds(st(drinks=41, drinks_cur=2, foods=10)) == ["drink"]
    s = st(drinks=0, foods=0)
    assert stock.low_kinds(s) == ["drink", "food"]
    assert stock.affordable(s, ["drink", "food"], TABLE) == ["drink", "food"]
    # level 13: 650c reserve; 4 x 25c drink = 100c, then 100c food
    assert stock.affordable(st(money=760, drinks=2, foods=2), ["drink", "food"], TABLE) == ["drink"]
    assert stock.affordable(st(money=600, drinks=2, foods=2), ["drink", "food"], TABLE) == []
    # all gone: 20c a level reserve, half the money (bot 2 run 6: 0 drinks for 50 min at 333-425c, level 6-7)
    assert stock.affordable(st(money=600), ["drink", "food"], TABLE) == ["drink", "food"]
    assert stock.affordable(st(money=360, drinks=0, foods=2), ["drink", "food"], TABLE) == ["drink"]
    nw = st(drinks=0, foods=20, drink_want=False)
    assert stock.low_kinds(nw) == []                                    # no mana: water is no stock
    # all gone: a longer errand walk is worth it
    assert stock.walk_max(st(drinks=0, foods=10), ["drink"]) == stock.WALK_MAX_OUT
    assert stock.walk_max(st(drinks=2, drinks_cur=2, foods=10), ["drink"]) == stock.WALK_MAX
    # vendor lots of 5 (1.12 buy_count): the addon buys whole lots, so 4 items cost a 5-lot (125c), not 100c
    stock._lots.update({1179: 5, 4541: 5})
    try:
        assert stock.lot_cost(13, "drink", TABLE) == 125.0
        assert stock.affordable(st(money=760, drinks=2, foods=2), ["drink", "food"], TABLE) == []
        assert stock.affordable(st(money=800, drinks=2, foods=2), ["drink", "food"], TABLE) == ["drink"]
    finally:
        stock._lots.pop(1179, None); stock._lots.pop(4541, None)
    # conjured water (price 0) and cooked food no vendor sells are no "best tier" to price a top-up by
    t2 = {**TABLE, 2288: ("drink", 5, 0.0), 5479: ("food", 12, 500.0)}
    sold = set().union(*VENDORS.values())
    assert stock.lot_cost(13, "drink", t2, sold) == 100.0 and stock.lot_cost(13, "food", t2, sold) == 100.0
    assert stock.affordable(st(money=760, drinks=2, foods=2), ["drink", "food"], t2, sold) == ["drink"]


# ------------------------------------------------------------ rest(): drink / eat / stand up / top-ups
def test_rest_drinks_at_once_and_stands_at_the_pull_line():
    LOGS.clear()
    s0 = st(level=7, mana=12, hp=95, drinks=5, drink_id=1179)
    b, seq = make_bot([s0, s0, st(level=7, mana=14, hp=95, drinks=4, drinking=True), st(level=7, mana=81, hp=95)])
    b.rest()
    assert b.pressed.count("f12") == 1 and b.pressed[-1] == "w", b.pressed
    assert any(m.startswith("rest: drinking item 1179 (5 in the bags)") for m in LOGS), LOGS
    assert any("mana under the drink line, until hp 85 mana 80" in m for m in LOGS), LOGS     # the pull line
    # after a 2+ mob fight the rest goes on to the rest target (pull line + margin)
    ln = b.res.lines(st(level=7))
    assert b.rest_exit(ln) == (ln.pull_hp, ln.pull_mana) and b.rest_exit(ln, multi=True) == (ln.exit_hp, ln.exit_mana)


def test_rest_eats_when_no_heal_and_old_addon_falls_back():
    LOGS.clear()
    s0 = st(level=7, mana=10, hp=40, drinks=3, foods=6, drink_id=159, food_id=117)
    kw = dict(level=7, drinks=3, foods=6, drink_id=159, food_id=117)
    drinking = st(mana=10, hp=40, drinking=True, **kw)
    b, _ = make_bot([s0, s0, drinking, drinking, st(mana=10, hp=40, drinking=True, eating=True, **kw),
                     st(level=7, mana=90, hp=90)])
    b.rest()
    assert "f12" in b.pressed and "num." in b.pressed, b.pressed
    assert any(m.startswith("rest: eating item 117") for m in LOGS), LOGS
    # no stock blocks (BotEye < 13): the old action-bar check decides; nothing on the bar -> no press
    LOGS.clear()
    s1 = st(level=7, mana=10, hp=95, build=12)
    b2, _ = make_bot([s1, s1, st(level=7, mana=90, hp=95, build=12)])
    b2.rest()
    assert "f12" not in b2.pressed and not any("drinking" in m for m in LOGS), (b2.pressed, LOGS)
    b3, _ = make_bot([s1, s1, st(level=7, mana=90, hp=95, build=12)])
    b3.has_item = lambda e, s: e == 159
    b3.rest()
    assert "f12" in b3.pressed


def test_no_drink_for_a_small_deficit():
    b, _ = make_bot([st()])
    assert not b.drink_due(st(mana=70), 80, 20) and b.drink_due(st(mana=55), 80, 20) and b.drink_due(st(mana=15), 30, 20)
    assert not b.eat_due(st(hp=80), 85) and b.eat_due(st(hp=60, mana=10), 85)
    assert not b.eat_due(st(hp=60, mana=90), 85)                      # heal, don't eat: the rest heals


def test_small_topup_walks_even_beside_a_pack():
    """A small top-up after a 2+ mob fight is walked off; beside a pack it still stands (walking on there pulls)."""
    LOGS.clear()
    b, _ = make_bot([st(level=7, mana=74, hp=100)])
    b.packed = lambda *a, **k: 99                       # mob country by the spawn table
    b.rest()
    assert not b.walk_rest and any("beside a pack" in m for m in LOGS), LOGS
    LOGS.clear()
    b1, _ = make_bot([st(level=7, mana=74, hp=100)])
    b1.packed = lambda *a, **k: 0
    b1.last_multi = True
    b1.rest()
    assert b1.walk_rest > 0 and any("a small top-up (after a 2+ mob fight)" in m for m in LOGS), LOGS
    # a real deficit beside the pack still stands
    LOGS.clear()
    b2, _ = make_bot([st(level=7, mana=50, hp=100), st(level=7, mana=50, hp=100), st(level=7, mana=85, hp=100)])
    b2.packed = lambda *a, **k: 99
    b2.rest()
    assert b2.walk_rest == 0.0 and any(m.startswith("rest: hp") and "beside a pack" in m for m in LOGS), LOGS


def test_rest_casts_the_buff_first():
    LOGS.clear()
    s0 = st(level=7, mana=15, hp=95)
    b, _ = make_bot([s0, s0, st(level=7, mana=90, hp=95)])
    calls = []
    b.buffs = lambda: calls.append(1) or True
    b.rest()
    assert calls == [1], calls


def test_wait_ready_drinks_on_a_long_wait():
    LOGS.clear()
    s0 = st(level=7, mana=40, hp=100, drinks=8, drink_id=1179)
    b, _ = make_bot([s0, s0, s0, st(level=7, mana=42, hp=100, drinking=True), st(level=7, mana=85, hp=100)])
    b.walk_rest = 1.0
    b.mv = SimpleNamespace(stop=lambda: None)
    assert b.wait_ready("test")
    assert b.pressed.count("f12") == 1 and b.pressed[-1] == "w", b.pressed
    assert any("-> ready" in m for m in LOGS), LOGS


# ------------------------------------------------------------ vendor_step: the stock errand
def errand_bot(states, walk):
    b, seq = make_bot(states)
    cand = {"name": "Innkeeper Hearthstove", "npc": 6734, "pos": (10.0, 0.0, 0.0)}
    went = []

    def order(s, need_repair=False, n_path=5, supplies=(), only_supplies=False):
        b.vendor_walks = {6734: walk}
        b.order_args = (need_repair, tuple(supplies), only_supplies)
        return [cand]
    b.vendor_order = order
    b.travel_to = lambda x, y, z, **kw: went.append((x, y)) or "ok"
    b.target_npc = lambda npc: True
    b.close_windows = lambda: None
    return b, went


def test_stock_errand():
    old = stock.item_table, stock.vendor_items
    stock.item_table, stock.vendor_items = (lambda: TABLE), (lambda: VENDORS)
    try:
        LOGS.clear()
        low = st(drinks=1, foods=12, drink_id=159)
        b, went = errand_bot([low], walk=900.0)
        assert b.vendor_step() is False and not went
        assert any(m.startswith("stock: drink 1 (best 159), food 12 low; nearest seller Innkeeper Hearthstove 900 yd")
                   for m in LOGS), LOGS
        assert b.retry.blocked("stock") and b.order_args == (False, ("drink",), True)
        LOGS.clear()
        b2, went2 = errand_bot([low, low, low, st(drinks=21, drinks_cur=20, foods=12, money=4500,
                                                     msg="I:stock 1179 x20 @25c")], walk=200.0)
        assert b2.vendor_step() is True and went2 == [(10.0, 0.0)]
        assert any(m.startswith("stock: drink 1 (best 159), food 12 low: errand to Innkeeper Hearthstove (200 yd")
                   for m in LOGS), LOGS
        assert any(m == "stock: drink 1 -> 20, food 12 -> 12 at Innkeeper Hearthstove (money 5000 -> 4500c)" for m in LOGS), LOGS
        assert not b2.retry.blocked("stock")
        # stock fine, bags fine: no trip; poor: no errand either
        b3, went3 = errand_bot([st(drinks=20, foods=10)], walk=10.0)
        assert b3.vendor_step() is False and not went3
        b4, went4 = errand_bot([st(drinks=0, foods=0, money=300)], walk=10.0)   # (all gone: 260c reserve)
        assert b4.vendor_step() is False and not went4
        # the near seller cannot be targeted: a far (unranked) one is no stock errand
        b5, went5 = errand_bot([low], walk=200.0)
        far = {"name": "Far Vendor", "npc": 1682, "pos": (900.0, 0.0, 0.0)}
        near = {"name": "Innkeeper Hearthstove", "npc": 6734, "pos": (10.0, 0.0, 0.0)}

        def order5(s, need_repair=False, n_path=5, supplies=(), only_supplies=False):
            b5.vendor_walks = {6734: 200.0}
            return [near, far]
        b5.vendor_order = order5
        b5.target_npc = lambda npc: False
        assert b5.vendor_step() is True and went5 == [(10.0, 0.0)], went5
        assert b5.retry.blocked("stock")
    finally:
        stock.item_table, stock.vendor_items = old


# ------------------------------------------------------------ BotEye.lua build 13 (lupa)
SUPPLY_STUBS = r"""
UnitLevel = function() return 13 end
UnitPowerType = function() return 0 end
MONEY = 1112
GetMoney = function() return MONEY end
-- id -> name, required level, spell
ITEMS = { [117] = { "Tough Jerky", 1, "Food" }, [1179] = { "Ice Cold Milk", 5, "Drink" },
          [2287] = { "Haunch of Meat", 5, "Food" }, [159] = { "Refreshing Spring Water", 1, "Drink" },
          [4541] = { "Freshly Baked Bread", 5, "Food" }, [1205] = { "Melon Juice", 15, "Drink" },
          [2589] = { "Linen Cloth", 1, nil } }
C_Item = { GetItemInfo = function(id) local t = ITEMS[id]; if t then return t[1], "l", 1, 5, t[2], "", "", 20 end end,
           GetItemSpell = function(id) local t = ITEMS[id]; if t and t[3] then return t[3], 1 end end,
           GetItemCount = function() return 0 end }
GetItemInfoInstant = function(id) return id, "", "", "", 0, ITEMS[id] and ITEMS[id][3] and 0 or 7 end
SLOTS = { { itemID = 117, stackCount = 41 }, { itemID = 1179, stackCount = 2 }, { itemID = 2287, stackCount = 2 },
          { itemID = 2589, stackCount = 5 }, { itemID = 1205, stackCount = 3 } }
C_Container = { GetContainerNumSlots = function(bag) return bag == 0 and #SLOTS or 0 end,
    GetContainerItemInfo = function(bag, slot) return SLOTS[slot] end, UseContainerItem = function() end,
    GetContainerNumFreeSlots = function(bag) return bag == 0 and 8 or 0 end }
MITEMS = { 159, 1179, 1205, 4541, 2589 }
C_MerchantFrame = { GetNumItems = function() return #MITEMS end,
    GetItemLink = function(i) return "|Hitem:" .. MITEMS[i] .. "::|h[x]|h" end,
    GetItemInfo = function(i) local id = MITEMS[i]
        local p = (id == 159 and 25) or (id == 1205 and 500) or 125
        return { name = "x", price = p, stackCount = 5, numAvailable = -1 } end }
GetMerchantItemMaxStack = function() return 20 end
BOUGHT = {}
BuyMerchantItem = function(i, n) BOUGHT[#BOUGHT + 1] = { MITEMS[i], n } end
"""


def _lua():
    import importlib.util
    if importlib.util.find_spec("lupa") is None:
        return None
    if not os.path.exists(REPO_BOTEYE):
        return None
    from nb import test_rxp_handlers as H
    rt = H._lua(REPO_BOTEYE)
    rt.execute(SUPPLY_STUBS)
    return rt


def test_lua_stock_blocks_macros_and_buy():
    rt = _lua()
    if rt is None:
        print("skip test_lua_stock_blocks_macros_and_buy (no lupa or no repo BotEye.lua)")
        return
    from nb import test_rxp_eye as T
    G = rt.globals()
    assert eye.disk_build(REPO_BOTEYE) >= stock.BUILD
    G.EVENT("PLAYER_LOGIN")
    s, v = T._state(rt)
    # level 13: Ice Cold Milk / Haunch of Meat (level 5) are the best usable; Melon Juice (15) is not usable yet;
    # Tough Jerky (1) is usable but no current-tier stock
    assert (s.drink_id, s.food_id) == (1179, 2287), (s.drink_id, s.food_id)
    assert (s.drinks, s.drinks_cur, s.foods, s.foods_cur) == (2, 2, 43, 2), (s.drinks, s.drinks_cur, s.foods, s.foods_cur)
    assert s.drink_want and s.food_want and s.rxp.build == eye.disk_build(REPO_BOTEYE)
    G.MACRO_TICK()
    F = G.FRAMES
    attr = lambda b: F[b].GetAttribute(F[b], "macrotext")     # noqa: E731
    assert (attr("BotEyeDrink"), attr("BotEyeEat")) == ("/use item:1179", "/use item:2287")
    assert G.BINDS.F12 == "BotEyeDrink" and G.BINDS.NUMPADDECIMAL == "BotEyeEat"
    # merchant: milk to 40 current drinks, bread to 20. Budget: min(1112c - 650c
    # reserve, 25% of 1112c = 278c) = 278c: 2 lots of milk (250c), then 28c left: no bread lot (SHARE: training money)
    G.MerchantFrame._shown = True
    G.EVENT("MERCHANT_SHOW")
    rt.eval("RUN_TIMERS()")
    got = [tuple(x.values()) for x in G.BOUGHT.values()]
    assert got == [(1179, 10)], got
    assert "I:stock 1179 x10 @25c, 4541 short (money)" in T._state(rt)[0].msg
    # rich: both, once per window
    rt.execute("BOUGHT = {}; MONEY = 100000")
    G.EVENT("MERCHANT_SHOW")
    rt.eval("RUN_TIMERS()")
    got = [tuple(x.values()) for x in G.BOUGHT.values()]
    assert got == [(1179, 20), (1179, 20), (4541, 20)], got    # 40 drinks in two stacks of 20, 20 food
    rt.execute("BOUGHT = {}")
    rt.eval("RUN_TIMERS()")
    assert len(G.BOUGHT) == 0
    # no mana (a warrior): no water bought, drink not wanted
    rt.execute("UnitPowerType = function() return 1 end; BOUGHT = {}")
    s, _ = T._state(rt)
    assert not s.drink_want and s.food_want
    G.EVENT("MERCHANT_SHOW")
    rt.eval("RUN_TIMERS()")
    assert [tuple(x.values()) for x in G.BOUGHT.values()] == [(4541, 20)]
    # an empty bag: the keys go back to the old fixed items
    rt.execute("SLOTS = {}")
    T._state(rt)
    G.MACRO_TICK()
    assert attr("BotEyeDrink") == "/use Refreshing Spring Water" and attr("BotEyeEat").startswith("/use Tough Hunk")
    assert G.BotEyeDB.lastErr is None, G.BotEyeDB.lastErr


def test_lua_bag_upgrade_and_spare_bags():
    """BotEye bag_upgrade (build 16): a loose 10-slot bag replaces the smallest equipped 6-slot pouch; a loose pouch
    that is no upgrade is sold at the vendor (inst4 17:11: four pouches on, six Blue Leather Bags loose, 0 free)."""
    rt = _lua()
    if rt is None:
        print("skip test_lua_bag_upgrade_and_spare_bags (no lupa)")
        return
    G = rt.globals()
    from nb import test_rxp_eye as T
    rt.execute("""
        BAGS = { [1] = 6, [2] = 6, [3] = 10, [4] = 6 }
        SLOTS = { { itemID = 856, stackCount = 1, quality = 1, hyperlink = "|Hitem:856::|h[Blue Leather Bag]|h" },
                  { itemID = 828, stackCount = 1, quality = 1, hyperlink = "|Hitem:828::|h[Small Blue Pouch]|h" } }
        C_Container.GetContainerNumSlots = function(bag) if bag == 0 then return #SLOTS end return BAGS[bag] or 0 end
        C_Container.GetContainerItemInfo = function(bag, slot) if bag == 0 then return SLOTS[slot] end end
        C_Container.ContainerIDToInventoryID = function(b) return 30 + b - 1 end
        CURSOR, PUT, USED = nil, nil, {}
        C_Container.PickupContainerItem = function(bag, slot) CURSOR = SLOTS[slot] end
        C_Container.UseContainerItem = function(bag, slot) USED[#USED + 1] = SLOTS[slot].itemID end
        PutItemInBag = function(inv) PUT = inv; CURSOR = nil end
        CursorHasItem = function() return rawget(_G, "CURSOR") ~= nil end
        ClearCursor = function() CURSOR = nil end
        UnitAffectingCombat = function() return false end
        local gi = GetItemInfoInstant
        C_Item.GetItemInfoInstant = function(id)
            local n = tonumber(tostring(id):match("item:(%d+)") or id)
            if n == 856 or n == 828 then return n, "", "", "", 0, 1, 0 end
            return gi(id)
        end
        C_Item.GetItemNumSlots = function(link) local n = tonumber(tostring(link):match("item:(%d+)")); return n == 856 and 10 or 6 end
    """)
    G.EVENT("QUEST_TURNED_IN")         # same branch as BAG_UPDATE_DELAYED (another frame holds that one here)
    rt.eval("RUN_TIMERS()")
    assert rt.eval("rawget(_G, 'PUT')") == 30, (rt.eval("rawget(_G, 'PUT')"), T._state(rt)[0].msg)   # bag 1 (6 slots, the first smallest) gets the 10-slot bag
    assert "bag swap Blue Leather Bag 10 for bag 1 (6)" in T._state(rt)[0].msg
    rt.execute("PUT = nil")
    G.EVENT("QUEST_TURNED_IN")         # same branch as BAG_UPDATE_DELAYED (another frame holds that one here)
    rt.eval("RUN_TIMERS()")
    assert rt.eval("rawget(_G, 'PUT') == nil")                    # 20 s throttle
    # at a vendor: the pouch is no upgrade over any equipped bag -> sold; the 10-slot bag is kept
    rt.eval("function() SELL_JUNK_TEST = true end")()
    G.MerchantFrame._shown = True
    G.EVENT("MERCHANT_SHOW")
    rt.eval("RUN_TIMERS()")
    used = [rt.eval("USED")[i + 1] for i in range(len(rt.eval("USED")))]
    assert 828 in used and 856 not in used, used


def test_lua_quest_items_not_sold():
    """BotEye build 17: white trade goods are sold, except the ones a quest in the log asks for (Thelsamar Blood
    Sausages' Boar Intestines went to the vendor on every trip)."""
    rt = _lua()
    if rt is None:
        print("skip test_lua_quest_items_not_sold (no lupa)")
        return
    G = rt.globals()
    rt.execute("""
        SLOTS = { { itemID = 3172, stackCount = 2, quality = 1, hyperlink = "|Hitem:3172::|h[Boar Intestines]|h" },
                  { itemID = 2589, stackCount = 5, quality = 1, hyperlink = "|Hitem:2589::|h[Linen Cloth]|h" } }
        C_Container.GetContainerNumSlots = function(bag) if bag == 0 then return #SLOTS end return 0 end
        C_Container.GetContainerItemInfo = function(bag, slot) if bag == 0 then return SLOTS[slot] end end
        USED = {}
        C_Container.UseContainerItem = function(bag, slot) USED[#USED + 1] = SLOTS[slot].itemID end
        UnitAffectingCombat = function() return false end
        C_Item.GetItemInfoInstant = function(id) return 1, "", "", "", 0, 7, 0 end
        C_QuestLog.GetNumQuestLogEntries = function() return 1 end
        C_QuestLog.GetInfo = function(i) return { questID = 418, isHeader = false } end
        C_QuestLog.GetQuestObjectives = function(q)
            return { { type = "item", text = "Boar Intestines: 2/3" }, { type = "item", text = "Bear Meat: 0/3" } }
        end
    """)
    G.MerchantFrame._shown = True
    G.EVENT("MERCHANT_SHOW")
    rt.eval("RUN_TIMERS()")
    used = [rt.eval("USED")[i + 1] for i in range(len(rt.eval("USED")))]
    assert 2589 in used and 3172 not in used, used


def test_lua_sells_green_gear_recipes_scrolls_potions():
    """BotEye build 18: green gear in the bags (not worn: no upgrade), recipes, gems, misc, scrolls and potions are
    sold; quest items and items without a sell price stay (bags at 39 of 40 after every trip, 2026-10-15)."""
    rt = _lua()
    if rt is None:
        print("skip test_lua_sells_green_gear_recipes_scrolls_potions (no lupa)")
        return
    G = rt.globals()
    rt.execute("""
        SLOTS = { { itemID = 2972, stackCount = 1, quality = 2, hyperlink = "|Hitem:2972::|h[Beaded Britches]|h" },
                  { itemID = 2409, stackCount = 1, quality = 2, hyperlink = "|Hitem:2409::|h[Pattern: Dark Leather Tunic]|h" },
                  { itemID = 1210, stackCount = 1, quality = 2, hyperlink = "|Hitem:1210::|h[Shadowgem]|h" },
                  { itemID = 1181, stackCount = 1, quality = 1, hyperlink = "|Hitem:1181::|h[Scroll of Spirit]|h" },
                  { itemID = 118, stackCount = 2, quality = 1, hyperlink = "|Hitem:118::|h[Minor Healing Potion]|h" },
                  { itemID = 6948, stackCount = 1, quality = 1, hasNoValue = true, hyperlink = "|Hitem:6948::|h[Hearthstone]|h" },
                  { itemID = 7109, stackCount = 1, quality = 1, hyperlink = "|Hitem:7109::|h[Pamela's Doll]|h" } }
        CLASS = { [2972] = {4, 1}, [2409] = {9, 1}, [1210] = {7, 7}, [1181] = {0, 4}, [118] = {0, 1},
                  [6948] = {15, 0}, [7109] = {12, 0} }
        C_Container.GetContainerNumSlots = function(bag) if bag == 0 then return #SLOTS end return 0 end
        C_Container.GetContainerItemInfo = function(bag, slot) if bag == 0 then return SLOTS[slot] end end
        USED = {}
        C_Container.UseContainerItem = function(bag, slot) USED[#USED + 1] = SLOTS[slot].itemID end
        UnitAffectingCombat = function() return false end
        C_Item.GetItemInfoInstant = function(link)
            local id = tonumber(tostring(link):match("item:(%d+)") or link)
            local c = CLASS[id] or {15, 0}
            return id, "", "", "", 0, c[1], c[2]
        end
        C_QuestLog.GetNumQuestLogEntries = function() return 0 end
        C_Item.GetItemInfo = function(link) return "loaded" end   -- item info loaded (unloaded gear is kept), no slot
    """)
    G.MerchantFrame._shown = True
    G.EVENT("MERCHANT_SHOW")
    rt.eval("RUN_TIMERS()")
    used = sorted(rt.eval("USED")[i + 1] for i in range(len(rt.eval("USED"))))
    assert used == sorted([2972, 2409, 1210, 1181, 118]), used     # the potion too: the bot never drinks one


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(f"{n} tests passed")
