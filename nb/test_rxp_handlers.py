"""Offline tests for BotEye's RXP handler gating, the RXP-fed keys and the spirit healer accept:
python -m nb.test_rxp_handlers

Runs the real BotEye.lua in Lua 5.1 (lupa) on the stubs and the fake RestedXP of test_rxp_eye (Sten step), with spies
on the WoW calls the handlers make (AcceptQuest, SelectOption, UseContainerItem, ConfirmationInteraction, ...):
- QUEST_* / GOSSIP_SHOW / MERCHANT_SHOW / TRAINER_SHOW yield to RXP only while it automates (RXP_PLAN.md section 4)
- BotEyeFind / BotEyeRXPNpc / BotEyeQuestItem follow RXP's lists out of combat, PAGEUP & co. are bound
- CONFIRM_XP_LOSS / PLAYER_INTERACTION_MANAGER_FRAME_SHOW(18) accept the healer, the death ticker respects /be hold
Skipped without lupa or without the addon folder. Touches no game window, sends no keys.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nb import test_rxp_eye as T     # noqa: E402  (STUBS, FAKE_RXP, BOTEYE, _state)

SPIES = r"""
-- unknown globals are a callable stub (STUBS), except BotEye's own buttons: `_G.BotEyeFind or secure(...)` must create them
local STUB = _G.__no_such_global__
setmetatable(_G, { __index = function(_, k)
    if type(k) == "string" and k:find("^BotEye") then return nil end
    return STUB
end })
CALLS = {}
local function spy(name, ret)
    return function(...) CALLS[#CALLS + 1] = { name, ... }; return ret end
end
function NCALL(name) local n = 0; for _, c in ipairs(CALLS) do if c[1] == name then n = n + 1 end end; return n end
function LASTARG(name) for i = #CALLS, 1, -1 do if CALLS[i][1] == name then return CALLS[i][2] end end end
function RESET() CALLS = {} end
ALLFRAMES = {}
local CF = CreateFrame
CreateFrame = function(kind, name, parent, tmpl) local f = CF(kind, name, parent); ALLFRAMES[#ALLFRAMES + 1] = f; return f end
DELAYS, TICKERS = {}, {}
C_Timer.After = function(d, f) table.insert(TIMERS, f); table.insert(DELAYS, d) end
C_Timer.NewTicker = function(d, f) table.insert(TICKERS, f) end
function RUN_TIMERS() local t = TIMERS; TIMERS = {}; DELAYS = {}; for _, f in ipairs(t) do f() end end
BINDS = {}
SetOverrideBindingClick = function(o, p, key, name) BINDS[key] = name end
SetOverrideBinding = function(o, p, key, cmd) BINDS[key] = cmd end
ClearOverrideBindings = spy("ClearOverrideBindings")
COMBAT = false
InCombatLockdown = function() return COMBAT end
GHOST = false
UnitIsGhost = function() return GHOST end
UnitIsDead = function() return GHOST end
AcceptQuest = spy("AcceptQuest"); ConfirmAcceptQuest = spy("ConfirmAcceptQuest"); CompleteQuest = spy("CompleteQuest")
CloseQuest = spy("CloseQuest"); GetQuestReward = spy("GetQuestReward"); IsQuestCompletable = function() return true end
QuestGetAutoAccept = function() return false end
GetNumActiveQuests = function() return 1 end; GetActiveTitle = function() return "Q", true end
SelectActiveQuest = spy("SelectActiveQuest"); GetNumAvailableQuests = function() return 0 end
GetNumQuestChoices = function() return 1 end
QuestFrameRewardPanel = NEWFRAME("QuestFrameRewardPanel"); QuestFrameRewardPanel._shown = false
GossipFrame = NEWFRAME("GossipFrame"); GossipFrame._shown = true
MerchantFrame = NEWFRAME("MerchantFrame"); MerchantFrame._shown = true
GOSSIP_OPTS = { { gossipOptionID = 11, icon = 132060, name = "Let me browse your goods." },
                { gossipOptionID = 12, icon = 132058, name = "Teach me the ways of the spirits." } }
C_GossipInfo = { GetOptions = function() return GOSSIP_OPTS end, SelectOption = spy("SelectOption"),
    GetActiveQuests = function() return {} end, GetAvailableQuests = function() return { { questID = 179 } } end,
    SelectAvailableQuest = spy("SelectAvailableQuest"), SelectActiveQuest = spy("GossipSelectActiveQuest") }
BAG = { { quality = 0, hyperlink = "|Hitem:1|h", isLocked = false }, { quality = 0, hyperlink = "|Hitem:2|h", isLocked = true },
        { quality = 1, hyperlink = "|Hitem:3|h", isLocked = false } }
C_Container = { GetContainerNumSlots = function(bag) return bag == 0 and #BAG or 0 end,
    GetContainerItemInfo = function(bag, slot) return BAG[slot] end, UseContainerItem = spy("UseContainerItem"),
    GetContainerNumFreeSlots = function(bag) return bag == 0 and 12 or 0 end }
CanMerchantRepair = function() return true end; RepairAllItems = spy("RepairAllItems")
GetNumTrainerServices = spy("GetNumTrainerServices", 0)
C_PlayerInteractionManager = { ConfirmationInteraction = spy("ConfirmationInteraction"),
    ClearInteraction = spy("ClearInteraction") }
RetrieveCorpse = spy("RetrieveCorpse"); RepopMe = spy("RepopMe")
GetCorpseRecoveryDelay = function() return 0 end
StaticPopup1 = NEWFRAME("StaticPopup1"); StaticPopup1._shown = false; StaticPopup1.which = "XP_LOSS_NO_SICKNESS"
POPBTN = NEWFRAME("StaticPopup1Button1"); POPBTN.enabled = true
function POPBTN:IsEnabled() return self.enabled end
POPBTN.Click = spy("PopupClick")
function StaticPopup1:GetButton1() return POPBTN end
function EVENT(e, ...) local f = EVFRAMES[e]; f:GetScript("OnEvent")(f, e, ...) end
function MACRO_TICK()
    for _, f in ipairs(ALLFRAMES) do
        if rawget(f, "_name") == nil and f._scripts.OnUpdate then f._scripts.OnUpdate(f, 0.3) end   -- macro_frame
    end
end
"""

ALLOWED_GLOBALS = {"BotEyeDB", "BotEyeSetup", "BotEyePadClaim", "SLASH_BOTEYE1"}


def _lua(path=None):
    path = path or T.BOTEYE
    try:
        import lupa.lua51 as lua51
    except ImportError:
        return None
    if not os.path.exists(path):
        return None
    rt = lua51.LuaRuntime(unpack_returned_tuples=True)
    rt.execute(T.STUBS)
    rt.execute(SPIES)
    rt.execute(T.FAKE_RXP)
    rt.execute("BEFORE = {}; for k in pairs(_G) do BEFORE[k] = true end")
    load = rt.eval("function(s) local f, e = loadstring(s, 'BotEye.lua'); if not f then error(e) end; f('BotEye') end")
    load(open(path, encoding="utf-8").read())
    return rt


def _new_globals(rt):
    new = rt.eval("function() local t = {}; for k in pairs(_G) do if not BEFORE[k] then t[#t + 1] = k end end; return t end")()
    names = set(new.values())
    frames = set(rt.globals().FRAMES.keys())
    return {n for n in names if n not in ALLOWED_GLOBALS and n not in frames}


def _ev(rt, e, *args):
    rt.globals().EVENT(e, *args)


def _n(rt, name):
    return rt.globals().NCALL(name)


def _rxp_on(rt):
    T._slash(rt, "rxp on")
    T._state(rt)                 # fill_rxp: rx lists, step kinds
    rt.eval("RESET()")


def _skip(name):
    print(f"skip {name} (no lupa or no BotEye.lua)")


# ---------------------------------------------------------------- quest windows
def test_quest_handlers_yield_to_rxp():
    rt = _lua()
    if rt is None:
        return _skip("test_quest_handlers_yield_to_rxp")
    G = rt.globals()
    assert _new_globals(rt) == set(), _new_globals(rt)
    # flag off: BotEye's own handlers (the old guide driver)
    for e in ("QUEST_DETAIL", "QUEST_ACCEPT_CONFIRM", "QUEST_PROGRESS", "QUEST_COMPLETE", "QUEST_GREETING"):
        _ev(rt, e)
    assert (_n(rt, "AcceptQuest"), _n(rt, "ConfirmAcceptQuest"), _n(rt, "CompleteQuest"), _n(rt, "GetQuestReward"),
            _n(rt, "SelectActiveQuest")) == (1, 1, 1, 1, 1)
    # flag on, guide loaded: RXP owns them
    _rxp_on(rt)
    for e in ("QUEST_DETAIL", "QUEST_ACCEPT_CONFIRM", "QUEST_PROGRESS", "QUEST_GREETING"):
        _ev(rt, e)
    assert len(G.CALLS) == 0, [tuple(c.values()) for c in G.CALLS.values()]
    # RXP stalled (frame hidden = all its automation off): BotEye takes the window again
    G.R.isHidden = True
    _ev(rt, "QUEST_DETAIL")
    assert _n(rt, "AcceptQuest") == 1
    G.R.isHidden = False
    rt.eval("RESET()")
    G.R.settings.profile.enableQuestAutomation = False
    _ev(rt, "QUEST_PROGRESS")
    assert _n(rt, "CompleteQuest") == 1
    G.R.settings.profile.enableQuestAutomation = True
    assert G.BotEyeDB.lastErr is None, G.BotEyeDB.lastErr


def test_quest_complete_fallback():
    rt = _lua()
    if rt is None:
        return _skip("test_quest_complete_fallback")
    G = rt.globals()
    _rxp_on(rt)
    _ev(rt, "QUEST_COMPLETE")
    assert _n(rt, "GetQuestReward") == 0 and list(G.DELAYS.values()) == [1.0]
    rt.eval("RUN_TIMERS()")                          # RXP took the reward: the panel is gone
    assert _n(rt, "GetQuestReward") == 0
    _ev(rt, "QUEST_COMPLETE")
    G.QuestFrameRewardPanel._shown = True            # RXP left it open (not in the guide / no usable upgrade)
    rt.eval("RUN_TIMERS()")
    assert _n(rt, "GetQuestReward") == 1 and G.LASTARG("GetQuestReward") == 1


# ---------------------------------------------------------------- gossip
def test_gossip_rxp_mode():
    rt = _lua()
    if rt is None:
        return _skip("test_gossip_rxp_mode")
    G = rt.globals()
    # flag off: the old order (quest offer first, at once)
    _ev(rt, "GOSSIP_SHOW")
    assert _n(rt, "SelectAvailableQuest") == 1
    _rxp_on(rt)
    # Sten (accept + target): RXP takes the quest; BotEye picks nothing, not even later
    _ev(rt, "GOSSIP_SHOW")
    assert len(G.CALLS) == 0 and len(G.TIMERS) == 1 and list(G.DELAYS.values()) == [0.3]
    rt.eval("RUN_TIMERS()")
    assert len(G.CALLS) == 0
    # a .trainer step: the trainer option, 0.3 s later
    rt.execute("table.insert(STEN.elements, { tag = 'trainer', text = 'Train' })")
    _ev(rt, "GOSSIP_SHOW")
    assert _n(rt, "SelectOption") == 0
    rt.eval("RUN_TIMERS()")
    assert _n(rt, "SelectOption") == 1 and G.LASTARG("SelectOption") == 12
    # a second GOSSIP_SHOW (next page) cancels the pending pick of the first: one pick only
    rt.eval("RESET()")
    _ev(rt, "GOSSIP_SHOW")
    _ev(rt, "GOSSIP_SHOW")
    rt.eval("RUN_TIMERS()")
    assert _n(rt, "SelectOption") == 1
    # RXP's pick closed the gossip (quest window opened): nothing
    rt.eval("RESET()")
    _ev(rt, "GOSSIP_SHOW")
    G.GossipFrame._shown = False
    rt.eval("RUN_TIMERS()")
    assert _n(rt, "SelectOption") == 0
    G.GossipFrame._shown = True
    # .skipgossip in the step: RXP's own pick, BotEye stays out
    rt.execute("STEN.elements[#STEN.elements] = { tag = 'skipgossip' }")
    _ev(rt, "GOSSIP_SHOW")
    rt.eval("RUN_TIMERS()")
    assert _n(rt, "SelectOption") == 0
    # .vendor step: the vendor option; bags full with a quest step: the vendor option too
    rt.execute("STEN.elements[#STEN.elements] = { tag = 'vendor' }")
    _ev(rt, "GOSSIP_SHOW")
    rt.eval("RUN_TIMERS()")
    assert G.LASTARG("SelectOption") == 11
    rt.execute("STEN.elements[#STEN.elements] = nil; C_Container.GetContainerNumFreeSlots = function() return 0 end")
    rt.eval("RESET()")
    _ev(rt, "GOSSIP_SHOW")
    rt.eval("RUN_TIMERS()")
    assert _n(rt, "SelectOption") == 1 and G.LASTARG("SelectOption") == 11
    # a ghost at the spirit healer: option 1 at once, RXP mode or not
    rt.eval("RESET()")
    G.GHOST = True
    _ev(rt, "GOSSIP_SHOW")
    assert _n(rt, "SelectOption") == 1 and G.LASTARG("SelectOption") == 11 and len(G.TIMERS) == 0
    assert G.BotEyeDB.lastErr is None, G.BotEyeDB.lastErr


def test_rxp_accept_lost_quest():
    """/be rxp accept <qid>: a skipped accept the bot went back for (inst3 179 at Sten). RXP accepts only an active
    step's quests (RXPGuides.lua:286-300), so in RXP mode BotEye picks that one offer and accepts its QUEST_DETAIL."""
    rt = _lua()
    if rt is None:
        return _skip("test_rxp_accept_lost_quest")
    G = rt.globals()
    rt.execute("QID = 179; GetQuestID = function() return QID end")
    _rxp_on(rt)
    _ev(rt, "QUEST_DETAIL")                          # not armed: RXP's
    _ev(rt, "GOSSIP_SHOW")
    rt.eval("RUN_TIMERS()")
    assert _n(rt, "AcceptQuest") == 0 and _n(rt, "SelectAvailableQuest") == 0
    T._slash(rt, "rxp accept 179")
    _ev(rt, "GOSSIP_SHOW")
    rt.eval("RUN_TIMERS()")
    assert _n(rt, "SelectAvailableQuest") == 1 and G.LASTARG("SelectAvailableQuest") == 179
    G.QID = 180                                      # another quest's window: RXP's
    _ev(rt, "QUEST_DETAIL")
    assert _n(rt, "AcceptQuest") == 0
    G.QID = 179
    _ev(rt, "QUEST_DETAIL")
    assert _n(rt, "AcceptQuest") == 1 and "rxp accepted 179" in T._state(rt)[0].msg
    _ev(rt, "QUEST_DETAIL")                          # done: cleared
    assert _n(rt, "AcceptQuest") == 1
    # expired after 300 s
    T._slash(rt, "rxp accept 179")
    G.NOW = G.NOW + 301
    _ev(rt, "QUEST_DETAIL")
    assert _n(rt, "AcceptQuest") == 1
    assert G.BotEyeDB.lastErr is None, G.BotEyeDB.lastErr


# ---------------------------------------------------------------- merchant / trainer
def test_merchant_and_trainer():
    rt = _lua()
    if rt is None:
        return _skip("test_merchant_and_trainer")
    G = rt.globals()
    sup = [0.5] if (T.eye.disk_build(T.BOTEYE) or 0) >= 13 else []   # build 13: buy_supplies 0.5 s in (test_stock)
    _ev(rt, "MERCHANT_SHOW")                         # flag off: BotEye sells the unlocked gray item at once
    assert _n(rt, "UseContainerItem") == 1 and G.LASTARG("UseContainerItem") == 0 and _n(rt, "RepairAllItems") == 1
    _rxp_on(rt)
    _ev(rt, "MERCHANT_SHOW")                         # RXP sells too: BotEye still sells at once (build 10: the bot
    #                                                  closes the window ~1.5 s in), repairs, checks again 1.0 s later
    #                                                  and buys a bag 0.3 s later (build 9)
    assert _n(rt, "RepairAllItems") == 1 and sorted(G.DELAYS.values()) == sorted([0.3, 0.3, 1.0] + sup * 2), \
        list(G.DELAYS.values())
    n0 = _n(rt, "UseContainerItem")
    G.MerchantFrame._shown = False
    rt.eval("RUN_TIMERS()")
    assert _n(rt, "UseContainerItem") == n0          # window closed meanwhile: the delayed pass sells nothing
    G.MerchantFrame._shown = True
    _ev(rt, "MERCHANT_SHOW")
    rt.eval("RUN_TIMERS()")
    assert _n(rt, "UseContainerItem") >= n0          # open: the immediate and delayed passes sell what is unlocked
    rt.eval("RESET()")
    rt.execute("R.IsGamePadEnabled = function() return true end")   # RXP refuses with the pad on
    _ev(rt, "MERCHANT_SHOW")
    assert _n(rt, "UseContainerItem") == 1 and len(G.TIMERS) == 1 + len(sup)   # only buy_bag's (+ buy_supplies')
    rt.execute("R.IsGamePadEnabled = function() return false end; R.inventoryManager.IsFeatureEnabled = function() return false end")
    rt.eval("RESET()")
    _ev(rt, "MERCHANT_SHOW")
    assert _n(rt, "UseContainerItem") == 1
    # trainer: BotEye buys while RXP's trainer automation is off (rxp_config keeps it off)
    _ev(rt, "TRAINER_SHOW")
    assert _n(rt, "GetNumTrainerServices") == 1
    G.R.settings.profile.enableTrainerAutomation = True
    _ev(rt, "TRAINER_SHOW")
    assert _n(rt, "GetNumTrainerServices") == 1
    assert G.BotEyeDB.lastErr is None, G.BotEyeDB.lastErr


def test_vendor_collect_buy():
    """Adlin (file L119-129): .collect 159,10 + .target. RXP's .collect never buys (functions.lua:2976-3244);
    BotEye buys it via C_MerchantFrame (GetMerchantItemInfo is gone on this client), money- and stack-capped,
    once per landing, and reports the state in blocks 248-249."""
    rt = _lua()
    if rt is None:
        return _skip("test_vendor_collect_buy")
    G = rt.globals()
    rt.execute("""
        MONEY = 267; COUNT = 0; PRICE = 25
        GetMoney = function() return MONEY end
        C_Item = { GetItemCount = function(id) return id == 159 and COUNT or 0 end,
                   GetItemInfo = function() return nil end }
        GetMerchantItemMaxStack = function() return 20 end
        MITEMS = { 117, 159 }
        C_MerchantFrame = { GetNumItems = function() return #MITEMS end,
            GetItemLink = function(i) return "|Hitem:" .. MITEMS[i] .. "::|h[x]|h" end,
            GetItemInfo = function(i) return { name = "x", price = (MITEMS[i] == 159) and PRICE or 25, stackCount = 1,
                                              numAvailable = -1 } end }
        GetMerchantItemInfo = nil
        BuyMerchantItem = function(i, n) CALLS[#CALLS + 1] = { "BuyMerchantItem", i, n } end
        COLLECT = { tag = "collect", id = 159, ids = "159", qty = 10, numRequired = 10, count = 0 }
        STEN.elements = { COLLECT, { tag = "target", targets = { "Adlin Pridedrift" }, textOnly = true } }
    """)
    G.MerchantFrame._shown = False
    _rxp_on(rt)
    G.MerchantFrame._shown = True
    _ev(rt, "MERCHANT_SHOW")
    assert _n(rt, "BuyMerchantItem") == 1 and G.LASTARG("BuyMerchantItem") == 2, \
        [tuple(c.values()) for c in G.CALLS.values()]
    rt.eval("RUN_TIMERS()")                          # the 2nd pass while the purchase is in flight: no double buy
    assert _n(rt, "BuyMerchantItem") == 1
    v = T._state(rt)[1]
    assert v[248] == 159 + (1 << 20) + (1 << 22) and v[249] == 10 + 25 * 256, (hex(v[248]), v[249])
    # landed: nothing more to buy
    G.COUNT = 10
    _ev(rt, "MERCHANT_SHOW")
    rt.eval("RUN_TIMERS()")
    assert _n(rt, "BuyMerchantItem") == 1
    # short of money: buys what it can, flags short
    rt.eval("RESET()")
    G.COUNT, G.MONEY, G.PRICE = 0, 120, 25
    G.NOW = G.NOW + 6                                # (the guard of the first buy has run out)
    _ev(rt, "MERCHANT_SHOW")
    assert _n(rt, "BuyMerchantItem") == 1 and G.LASTARG("BuyMerchantItem") == 2
    v = T._state(rt)[1]
    assert v[248] >> 21 & 1 == 1, hex(v[248])
    # a vendor without the item: seen = 0, nothing bought
    rt.eval("RESET()")
    G.COUNT = 4                                      # 4 landed
    rt.execute("MITEMS = { 117 }")
    G.NOW = G.NOW + 10
    _ev(rt, "MERCHANT_SHOW")
    v = T._state(rt)[1]
    assert _n(rt, "BuyMerchantItem") == 0 and v[248] & 0xFFFFF == 159 and not v[248] >> 20 & 1 and v[249] & 255 == 6
    # flag off: no buying
    T._slash(rt, "rxp off")
    rt.execute("MITEMS = { 117, 159 }")
    _ev(rt, "MERCHANT_SHOW")
    assert _n(rt, "BuyMerchantItem") == 0
    assert G.BotEyeDB.lastErr is None, G.BotEyeDB.lastErr


ADLIN_LUA = """
    MONEY = 481; COUNT = 0
    GetMoney = function() return MONEY end
    C_Item = { GetItemCount = function(id) return id == 159 and COUNT or 0 end, GetItemInfo = function() return nil end }
    GetMerchantItemMaxStack = function() return 20 end
    MITEMS = { 117, 159 }
    C_MerchantFrame = { GetNumItems = function() return #MITEMS end,
        GetItemLink = function(i) return "|Hitem:" .. MITEMS[i] .. "::|h[x]|h" end,
        -- Adlin on this client (human, inst4 20:06:29-32): 23c per stack of 5, two buys -> 10 water
        GetItemInfo = function(i) return { name = "x", price = 23, stackCount = 5, numAvailable = -1 } end }
    GetMerchantItemInfo = nil
    BuyMerchantItem = function(i, n) CALLS[#CALLS + 1] = { "BuyMerchantItem", i, n } end
    STEN.elements = { { tag = "collect", id = 159, ids = "159", qty = 10, numRequired = 10, count = 0 },
                      { tag = "target", targets = { "Adlin Pridedrift" }, textOnly = true } }
"""


def test_build_stamp_and_stale_copy():
    """inst3 21:12: 4 visits to Adlin, money 481c, no 'rxp buy:' line. Client 3 had not been /reloaded since 20:24
    (WTF/Account/1#3 untouched after it) and ran the BotEye from before buy_collect (20:49): blocks 248-249 = 0,
    so buy_verdict was None and the TALK_TRIES rule skipped the step. The current file buys and stamps block 250."""
    from nb import eye, rxp_policy as pol
    rt = _lua()
    if rt is None:
        return _skip("test_build_stamp_and_stale_copy")
    G = rt.globals()
    rt.execute(ADLIN_LUA)
    G.MerchantFrame._shown = False
    _rxp_on(rt)
    v = T._state(rt)[1]
    disk = eye.disk_build(T.BOTEYE)
    assert disk is not None and disk >= pol.BUY_BUILD, disk
    assert v[250] == (eye.BUILD_TAG << 16) + disk, hex(v[250])
    r = eye.decode_rxp(v)
    assert r.build == disk and r.buy_item == 159 and r.buy_need == 10 and pol.stale_boteye(r, disk) is None, r
    G.MerchantFrame._shown = True
    _ev(rt, "MERCHANT_SHOW")
    assert _n(rt, "BuyMerchantItem") == 1 and G.LASTARG("BuyMerchantItem") == 2, \
        [tuple(c.values()) for c in G.CALLS.values()]
    assert [tuple(c.values()) for c in G.CALLS.values() if c[1] == "BuyMerchantItem"] == [("BuyMerchantItem", 2, 10)]
    assert G.BotEyeDB.lastErr is None, G.BotEyeDB.lastErr
    # the pre-fix copy (nb/BotEye.lua.bak_buy, kept by the buy fix): what client 3 ran
    old = os.path.join(os.path.dirname(os.path.abspath(__file__)), "BotEye.lua.bak_buy")
    rt = _lua(old)
    if rt is None:
        return
    G = rt.globals()
    rt.execute(ADLIN_LUA)
    _rxp_on(rt)
    _ev(rt, "MERCHANT_SHOW")
    v = T._state(rt)[1]
    r = eye.decode_rxp(v)
    assert _n(rt, "BuyMerchantItem") == 0 and r.buy_item == 0 and r.build == 0, (v[248], v[250])
    assert pol.buy_verdict(r) is None                                    # -> "4 talks changed nothing" (21:13:09)
    assert "/reload" in pol.stale_boteye(r, disk)


# ---------------------------------------------------------------- keys
def test_rxp_keys():
    rt = _lua()
    if rt is None:
        return _skip("test_rxp_keys")
    G = rt.globals()
    _ev(rt, "PLAYER_LOGIN")
    F = G.FRAMES
    assert (G.BINDS.PAGEUP, G.BINDS.DELETE, G.BINDS.PAGEDOWN) == ("BotEyeRXPNpc",) * 3
    assert G.BINDS.INSERT == "BotEyeQuestItem" and G.BINDS.NUMPADMULTIPLY == "BotEyeFind"
    assert F.BotEyeFriend.GetAttribute(F.BotEyeFriend, "macrotext").startswith("/cleartarget\n/targetexact Spirit Healer")
    attr = lambda b: F[b].GetAttribute(F[b], "macrotext")     # noqa: E731
    T._state(rt)
    G.MACRO_TICK()
    assert (attr("BotEyeRXPNpc"), attr("BotEyeFind"), attr("BotEyeQuestItem")) == ("", "", "/run local _"), "flag off"
    _rxp_on(rt)
    G.MACRO_TICK()
    assert attr("BotEyeRXPNpc") == "/targetexact Sten Stoutarm"
    assert attr("BotEyeFind") == "/targetexact Ragged Young Wolf\n/stopmacro [harm,nodead]"
    assert attr("BotEyeQuestItem") == "/run local _"
    # the step's active item; low-priority names after the others; nothing changes in combat
    rt.execute("STEN.activeItems = { [6948] = true }; R.targeting.GetCurrentTargets = function() "
               "return { 'Sten Stoutarm' }, { '*Ragged Young Wolf', 'Frostmane Troll Whelp' }, {} end")
    G.COMBAT = True
    T._state(rt)
    G.MACRO_TICK()
    assert attr("BotEyeQuestItem") == "/run local _"
    G.COMBAT = False
    G.MACRO_TICK()
    assert attr("BotEyeQuestItem") == "/use item:6948"
    assert attr("BotEyeFind") == ("/targetexact Frostmane Troll Whelp\n/stopmacro [harm,nodead]\n"
                                  "/targetexact Ragged Young Wolf\n/stopmacro [harm,nodead]")
    # a /be find after that stays until RXP's list changes
    T._slash(rt, "find Snow Leopard")
    G.MACRO_TICK()
    assert attr("BotEyeFind") == "/targetexact Snow Leopard\n/stopmacro [harm,nodead]"
    # the macro body limit: long lists are cut at whole names
    rt.execute("R.targeting.GetCurrentTargets = function() local t = {}; for i = 1, 9 do t[i] = string.rep('N', 30) .. i end; "
               "return {}, t, {} end")
    T._state(rt)
    G.MACRO_TICK()
    body = attr("BotEyeFind")
    assert len(body) <= 255 and body.count("/targetexact") == 3 and attr("BotEyeRXPNpc") == "/targetexact Sten Stoutarm"
    # flag off: the old driver's content back
    T._slash(rt, "rxp off")
    G.MACRO_TICK()
    assert (attr("BotEyeRXPNpc"), attr("BotEyeFind"), attr("BotEyeQuestItem")) == ("", "", "/run local _")
    # /be rxp keys: another key set, all bindings rebuilt
    T._slash(rt, "rxp keys numpad9")
    assert G.BINDS.NUMPAD9 == "BotEyeRXPNpc" and G.NCALL("ClearOverrideBindings") == 1
    assert list(G.BotEyeDB.rxpNpcKeys.values()) == ["NUMPAD9"]
    assert G.BotEyeDB.lastErr is None, G.BotEyeDB.lastErr


# ---------------------------------------------------------------- spirit healer and corpse
def test_spirit_healer_accept():
    rt = _lua()
    if rt is None:
        return _skip("test_spirit_healer_accept")
    G = rt.globals()
    _ev(rt, "CONFIRM_XP_LOSS")                       # alive: nothing
    rt.eval("RUN_TIMERS()")
    assert _n(rt, "ConfirmationInteraction") == 0
    G.GHOST = True
    _ev(rt, "PLAYER_INTERACTION_MANAGER_FRAME_SHOW", 5)    # a merchant interaction: not ours
    assert len(G.TIMERS) == 0
    _ev(rt, "PLAYER_INTERACTION_MANAGER_FRAME_SHOW", 18)
    assert list(G.DELAYS.values()) == [0]
    rt.eval("RUN_TIMERS()")
    assert _n(rt, "ConfirmationInteraction") == 1 and G.LASTARG("ConfirmationInteraction") == 18
    assert _n(rt, "ClearInteraction") == 1 and "healer accept" in T._state(rt)[0].msg
    # RXP mode with a .deathskip step active: RXP accepts first; BotEye waits and only accepts while still a ghost
    rt.eval("RESET()")
    _rxp_on(rt)
    rt.execute("table.insert(STEN.elements, { tag = 'deathskip', text = 'Die' })")
    _ev(rt, "CONFIRM_XP_LOSS")
    assert list(G.DELAYS.values()) == [0.6]
    G.GHOST = False                                  # RXP's accept revived us
    rt.eval("RUN_TIMERS()")
    assert _n(rt, "ConfirmationInteraction") == 0
    G.GHOST = True
    _ev(rt, "CONFIRM_XP_LOSS")
    rt.eval("RUN_TIMERS()")
    assert _n(rt, "ConfirmationInteraction") == 1


def test_death_ticker_popup_and_hold():
    rt = _lua()
    if rt is None:
        return _skip("test_death_ticker_popup_and_hold")
    G = rt.globals()
    tick = G.TICKERS[1]                              # the first ticker: release / popup / retrieve
    G.GHOST = True
    G.StaticPopup1._shown = True
    G.POPBTN.enabled = False                         # countdown: the button is disabled
    tick()
    assert _n(rt, "ConfirmationInteraction") == 1 and _n(rt, "PopupClick") == 0 and _n(rt, "RetrieveCorpse") == 0
    tick()                                           # within 3 s of that accept: no second one
    assert _n(rt, "ConfirmationInteraction") == 1
    G.NOW = G.NOW + 4
    G.POPBTN.enabled = True
    tick()
    assert _n(rt, "PopupClick") == 1
    G.StaticPopup1._shown = False
    # corpse: taken at once unless held; a hold ends with /be hold off or after 150 s
    rt.eval("RESET()")
    T._slash(rt, "hold on")
    tick()
    assert _n(rt, "RetrieveCorpse") == 0
    T._slash(rt, "hold off")
    tick()
    assert _n(rt, "RetrieveCorpse") == 1
    T._slash(rt, "hold on")
    G.NOW = G.NOW + 151
    tick()
    assert _n(rt, "RetrieveCorpse") == 2
    assert G.BotEyeDB.lastErr is None, G.BotEyeDB.lastErr


# ---------------------------------------------------------------- emote objectives (05-11 L49 "The Great Outdoors")
def test_emote_command_and_camp_aura_bits():
    """/be emote sit -> DoEmote("SIT") (the text emote the server's criteria count; no chat line); a bad token calls
    nothing. Block 24 bit 5 = the campfire rest 1289723, bit 6 = Boosted Rest 1229451 / Camp Benefits 1229741."""
    rt = _lua()
    if rt is None:
        return _skip("test_emote_command_and_camp_aura_bits")
    G = rt.globals()
    rt.execute('DoEmote = (function() return function(...) CALLS[#CALLS + 1] = { "DoEmote", ... } end end)()')
    T._slash(rt, "emote sit")
    assert _n(rt, "DoEmote") == 1 and G.LASTARG("DoEmote") == "SIT"
    T._slash(rt, "emote 1; /say hi")
    assert _n(rt, "DoEmote") == 1
    st = T._state(rt)[0]
    assert not st.campfire_rest and not st.camp_buff
    rt.execute("AURAS = { [1289723] = true }; C_UnitAuras = { GetBuffDataByIndex = function() return nil end, "
               "GetPlayerAuraBySpellID = function(id) if AURAS[id] then return { spellId = id } end end }")
    st = T._state(rt)[0]
    assert st.campfire_rest and not st.camp_buff, st.auras
    rt.execute("AURAS = { [1229741] = true }")
    st = T._state(rt)[0]
    assert st.camp_buff and not st.campfire_rest, st.auras
    assert G.BotEyeDB.lastErr is None, G.BotEyeDB.lastErr


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            n += 1
            print("ok", name)
    print(f"{n} tests passed")
