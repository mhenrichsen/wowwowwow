-- BotEye: paints game state into a strip of coloured blocks in the top-left
-- corner. Every block is one 24-bit number (r,g,b). The local bot reads the
-- strip from a window capture. Layout must match botnew/eye.py.

local ADDON = ...
BotEyeDB = BotEyeDB or {}
-- no global GetItemInfoInstant on this client (C_Item only; RXP does the same, RXPGuides/.../:13). Without it
-- sell_junk never sold white gear or trade goods and buy_bag never ran: inst3 2026-10-15 06:49 bags 0 free after a sale
local function GetItemInfoInstant(...)              -- looked up per call (a stub / a late C_Item is seen too)
    local f = C_Item and C_Item.GetItemInfoInstant or _G.GetItemInfoInstant
    if f then return f(...) end
end

local BLOCK_PX = 3          -- physical pixels per block edge
local PER_ROW = 64
local NBLOCKS = 256         -- 4 full rows; 145 = layout tag, 146-165 = quests 11-20 (was 160: 10 quests only);
                            -- row 4 (192-255) = RestedXP state, 192 = RXP_TAG (was 192: 3 rows, nb/RXP_PLAN.md section 2)

local frame = CreateFrame("Frame", "BotEyeFrame", nil)
frame:SetFrameStrata("TOOLTIP")
frame:SetFrameLevel(10000)
local tex = {}
local vals = {}
for i = 0, NBLOCKS - 1 do
    local t = frame:CreateTexture(nil, "OVERLAY")
    if t.SetSnapToPixelGrid then t:SetSnapToPixelGrid(false); t:SetTexelSnappingBias(0) end
    tex[i] = t
    vals[i] = 0
end

-- secret values (current health/power) can only be shown, not read: paint them
-- as status bars (BAR_PX wide, 3 px tall, two columns) below the block rows.
-- bar 0 player hp, 1 player power, 2 target hp, 3..14 nameplates 0..11
local BAR_PX, NBARS = 128, 16   -- 0 hp, 1 power, 2 target hp, 3-14 plates, 15 pet hp
local bars = {}
for i = 0, NBARS - 1 do
    local b = CreateFrame("StatusBar", nil, frame)
    b:SetStatusBarTexture("Interface/Buttons/WHITE8X8")
    b:SetStatusBarColor(1, 1, 1, 1)
    local bg = b:CreateTexture(nil, "BACKGROUND")
    bg:SetAllPoints(); bg:SetColorTexture(0, 0, 0, 1)
    b:SetMinMaxValues(0, 1); b:SetValue(0)
    bars[i] = b
end
local function setbar(i, v, mx)
    local b = bars[i]
    if v == nil or mx == nil or (not issecretvalue(mx) and mx == 0) then
        b:SetMinMaxValues(0, 1); b:SetValue(0); return
    end
    b:SetMinMaxValues(0, mx); b:SetValue(v)
end

local function layout()
    local _, ph = GetPhysicalScreenSize()
    local u = 768 / ph                  -- UI units per physical pixel at scale 1
    frame:SetScale(1)
    frame:ClearAllPoints()
    frame:SetPoint("TOPLEFT", UIParent, "TOPLEFT", 0, 0)
    local rows = math.ceil(NBLOCKS / PER_ROW)
    frame:SetSize(PER_ROW * BLOCK_PX * u, rows * BLOCK_PX * u)
    for i = 0, NBLOCKS - 1 do
        local c, r = i % PER_ROW, math.floor(i / PER_ROW)
        local t = tex[i]
        t:ClearAllPoints()
        t:SetPoint("TOPLEFT", frame, "TOPLEFT", c * BLOCK_PX * u, -r * BLOCK_PX * u)
        t:SetSize(BLOCK_PX * u, BLOCK_PX * u)
    end
    for i = 0, NBARS - 1 do
        local c, r = i % 2, math.floor(i / 2)
        local b = bars[i]
        b:ClearAllPoints()
        b:SetPoint("TOPLEFT", frame, "TOPLEFT", c * BAR_PX * u, -(rows * BLOCK_PX + r * 3) * u)
        b:SetSize(BAR_PX * u, 3 * u)
    end
end

local function put(i, v)
    if issecretvalue and issecretvalue(v) then vals[i] = 16777214; return end
    v = math.floor((v or 0) + 0.5)
    if v < 0 then v = 0 elseif v > 16777215 then v = 16777215 end
    vals[i] = v
end

local function paint()
    for i = 0, NBLOCKS - 1 do
        local v = vals[i]
        tex[i]:SetColorTexture(math.floor(v / 65536) / 255, (math.floor(v / 256) % 256) / 255, (v % 256) / 255, 1)
    end
end

local function coord(v) return ((v or 0) + 20000) * 256 end
local function bit(b, cond) return cond and 2 ^ b or 0 end

local function hash(s, mod)
    if not s then return 0 end
    local h = 7
    for k = 1, #s do h = (h * 31 + s:byte(k)) % 16777213 end
    return h % (mod or 16777216)
end

local function npcid(guid)
    if not guid then return 0 end
    local kind, _, _, _, _, id = strsplit("-", guid)
    if kind == "Creature" or kind == "Vehicle" or kind == "Pet" or kind == "GameObject" then
        return tonumber(id) or 0
    end
    return 0
end

-- ---------------------------------------------------------------- state
-- ---------------------------------------------------------------- class config
-- Everything class-specific BotEye needs, one entry per class file token (UnitClass 2nd return); the bot's side is
-- nb/classes.py. ranged: the spell whose range is target bit 11 (nil: a melee class, bit 12 "no range" then);
-- buffs: self-buff names (a "^" pattern matches a prefix) whose aura sets aura bit 0; armor: the heaviest armor
-- subclass worn before 40 and at 40+ (1 cloth, 2 leather, 3 mail, 4 plate); shield: may wear one; weapons: usable
-- weapon subclasses (item class 2); w: gear stat weights; pet: the pet's spells (known: the spell id that says the
-- class has its pet yet; call / revive / mend / feed: names for the pet keys).
local CLASS_CFG = {
    SHAMAN  = { ranged = "Lightning Bolt", buffs = { "Lightning Shield" }, armor = { 2, 3 }, shield = true,
                weapons = { 0, 1, 4, 5, 10, 13, 15 },
                w = { STRENGTH = 0.8, AGILITY = 0.7, INTELLECT = 0.8, SPIRIT = 0.5, STAMINA = 0.9, spell = 0.5, mp5 = 0.3 } },
    HUNTER  = { ranged = "Auto Shot", buffs = { "^Aspect of the" }, armor = { 2, 3 },
                pet = { known = 883, call = "Call Pet", revive = "Revive Pet", mend = "Mend Pet", feed = "Feed Pet",
                        tame = "Tame Beast" },
                weapons = { 0, 1, 2, 3, 6, 7, 8, 10, 13, 15, 16, 18 },
                w = { STRENGTH = 0.3, AGILITY = 1.0, INTELLECT = 0.4, SPIRIT = 0.3, STAMINA = 0.9, spell = 0, mp5 = 0.1 } },
    WARRIOR = { buffs = { "Battle Shout" }, armor = { 3, 4 }, shield = true,
                weapons = { 0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 13, 15, 16, 18 },
                w = { STRENGTH = 1.0, AGILITY = 0.7, INTELLECT = 0, SPIRIT = 0.1, STAMINA = 0.9, spell = 0, mp5 = 0 } },
    PALADIN = { buffs = { "Devotion Aura", "^Seal of" }, armor = { 3, 4 }, shield = true,
                weapons = { 0, 1, 4, 5, 6, 7, 8 },
                w = { STRENGTH = 1.0, AGILITY = 0.4, INTELLECT = 0.5, SPIRIT = 0.3, STAMINA = 0.9, spell = 0.3, mp5 = 0.2 } },
    ROGUE   = { buffs = {}, armor = { 2, 2 }, weapons = { 2, 3, 4, 7, 13, 15, 16, 18 },
                w = { STRENGTH = 0.5, AGILITY = 1.0, INTELLECT = 0, SPIRIT = 0.1, STAMINA = 0.8, spell = 0, mp5 = 0 } },
    DRUID   = { ranged = "Wrath", buffs = { "Mark of the Wild", "Thorns" }, armor = { 2, 2 },
                weapons = { 4, 5, 10, 13, 15 },
                w = { STRENGTH = 0.6, AGILITY = 0.5, INTELLECT = 0.8, SPIRIT = 0.6, STAMINA = 0.8, spell = 0.5, mp5 = 0.3 } },
    MAGE    = { ranged = "Fireball", buffs = { "Frost Armor", "Ice Armor", "Mage Armor" }, armor = { 1, 1 },
                weapons = { 7, 10, 15, 19 },
                w = { STRENGTH = 0, AGILITY = 0, INTELLECT = 1.0, SPIRIT = 0.6, STAMINA = 0.8, spell = 0.8, mp5 = 0.3 } },
    PRIEST  = { ranged = "Smite", buffs = { "Power Word: Fortitude", "Inner Fire" }, armor = { 1, 1 },
                weapons = { 4, 10, 15, 19 },
                w = { STRENGTH = 0, AGILITY = 0, INTELLECT = 1.0, SPIRIT = 0.8, STAMINA = 0.8, spell = 0.8, mp5 = 0.4 } },
    WARLOCK = { ranged = "Shadow Bolt", buffs = { "Demon Skin", "Demon Armor" }, armor = { 1, 1 },
                pet = { known = 688, call = "Summon Imp", revive = "Summon Imp", mend = "Health Funnel" },
                weapons = { 7, 10, 15, 19 },
                w = { STRENGTH = 0, AGILITY = 0, INTELLECT = 0.9, SPIRIT = 0.5, STAMINA = 1.0, spell = 0.8, mp5 = 0.2 } },
}
local CFG_NONE = { buffs = {}, armor = { 1, 1 }, weapons = {}, w = { STAMINA = 1.0 } }
local function ccfg()
    local ok, _, tok = pcall(UnitClass, "player")
    return ok and tok and CLASS_CFG[tok] or CFG_NONE
end
local function class_buff(name)
    if type(name) ~= "string" then return false end
    for _, b in ipairs(ccfg().buffs) do
        if (b:sub(1, 1) == "^" and name:find(b)) or name == b then return true end
    end
    return false
end

-- block 171, the pet: bit 0 exists, 1 dead, 2-8 hp %, 9-10 happiness (1 unhappy .. 3 happy, 0 unknown), 11 in
-- combat, 12 its target is ours, 13 the class's pet spell is known
local function pet_bits()
    local p = ccfg().pet
    if not p then return 0 end
    local function q(fn) local ok, r = pcall(fn); if ok and not (issecretvalue and issecretvalue(r)) then return r end end
    local f = IsPlayerSpell or IsSpellKnown
    local known = f and q(function() return f(p.known) end) and 1 or 0
    local ex = q(function() return UnitExists("pet") end) and 1 or 0
    if ex == 0 then return known * 8192 end
    local dead = q(function() return UnitIsDead("pet") end) and 1 or 0
    local hp = q(function()
        local m = UnitHealthMax("pet") or 0
        return m > 0 and math.floor(100 * (UnitHealth("pet") or 0) / m + 0.5) or 0
    end) or 0
    local hap = (GetPetHappiness and q(function() return GetPetHappiness() end)) or 0
    local comb = q(function() return UnitAffectingCombat("pet") end) and 1 or 0
    local same = q(function() return UnitExists("pettarget") and UnitIsUnit("pettarget", "target") end) and 1 or 0
    return ex + dead * 2 + math.min(127, math.max(0, hp)) * 4 + math.min(3, hap) * 512 + comb * 2048 + same * 4096
        + known * 8192
end

local st = { counter = 0, autoattack = false, err = 0, errn = 0, msg = "", msgn = 0, errs = 0 }

local ERRS = {
    { "ERR_BADATTACKFACING", 1 }, { "ERR_BADATTACKPOS", 2 }, { "SPELL_FAILED_LINE_OF_SIGHT", 3 },
    { "SPELL_FAILED_UNIT_NOT_INFRONT", 4 }, { "ERR_OUT_OF_RANGE", 5 }, { "SPELL_FAILED_OUT_OF_RANGE", 5 },
    { "ERR_INV_FULL", 6 }, { "OUT_OF_MANA", 7 }, { "ERR_OUT_OF_MANA", 7 }, { "SPELL_FAILED_NO_POWER", 7 },
    { "SPELL_FAILED_NOT_READY", 8 }, { "ERR_SPELL_COOLDOWN", 8 }, { "ERR_ABILITY_COOLDOWN", 8 },
    { "ERR_GENERIC_NO_TARGET", 9 }, { "SPELL_FAILED_BAD_TARGETS", 10 }, { "ERR_INVALID_ATTACK_TARGET", 10 },
    { "SPELL_FAILED_MOVING", 11 }, { "ERR_USE_TOO_FAR", 12 }, { "ERR_TOO_FAR_TO_INTERACT", 12 },
    { "SPELL_FAILED_TOO_CLOSE", 13 }, { "ERR_NO_LOOT", 14 }, { "ERR_LOOT_GONE", 14 },
    { "SPELL_FAILED_INTERRUPTED", 15 }, { "ERR_OBJECT_IS_BUSY", 16 }, { "SPELL_FAILED_NOT_STANDING", 17 },
    { "ERR_CANT_INTERACT_SHAPESHIFTED", 18 }, { "SPELL_FAILED_TARGETS_DEAD", 19 }, { "ERR_TARGET_DEAD", 19 },
    { "ERR_QUEST_LOG_FULL", 20 }, { "ERR_ITEM_MAX_COUNT", 21 }, { "ERR_NOT_WHILE_MOVING", 11 },
    { "ERR_NOT_IN_COMBAT", 22 }, { "ERR_AFFECTING_COMBAT", 22 }, { "ERR_TAXINOTENOUGHMONEY", 23 },
    { "ERR_NOT_ENOUGH_MONEY", 23 },
}
local function errcode(msg)
    for _, e in ipairs(ERRS) do
        local g = _G[e[1]]
        if g and msg == g then return e[2] end
    end
    return 99
end

local function note(msg)
    st.msg = tostring(msg or ""):gsub("|c%x%x%x%x%x%x%x%x", ""):gsub("|r", ""):sub(1, 96)
    st.msgn = (st.msgn + 1) % 65536
end

-- ---------------------------------------------------------------- RestedXP (RXP) bridge
-- Design: nb/RXP_PLAN.md sections 1-2. Everything that CHANGES RXP (settings, guide loads, step skips) is behind
-- BotEyeDB.rxp (/be rxp on|off, default off). Strip row 4 (fill_rxp) and the plate/target name bits only READ RXP,
-- so they are painted whenever RXP is loaded: the bot sees the flag and RXP's state either way.
local RXP_TAG = 0x5A0F16
-- painted from the first frame: fill_rxp runs only every 0.25 s, and eye.py places the bars by this tag (y=12 with it,
-- y=9 without), so a frame without it read hp / mana from row 4 right after a /reload (review 2026-10-14)
vals[192] = RXP_TAG
-- build stamp, block 250 = BUILD_TAG * 65536 + BOTEYE_BUILD (eye.py Rxp.build). Bump BOTEYE_BUILD whenever the strip or
-- a handler the bot relies on changes: the bot compares it with this file on disk and says when a client still runs
-- an older copy (inst3 21:12: client 3 never got the /reload after buy_collect, blocks 248-249 read 0, Adlin skipped).
-- 2 = buy_collect + blocks 248-249; 3 = .home binder pick + confirm (bind_pick / bind_confirm)
-- 4 = /be emote <token> (DoEmote) + block 24 bits 5-6 (campfire rest 1289723 / Boosted Rest 1229451, Camp Benefits)
-- 13 = water / food: blocks 166-169, the eat / drink macros use the best owned item, buy_supplies on MERCHANT_SHOW
-- 14 = stock to 40 drinks / 20 food, buy_supplies retries up to 4 times while merchant item data loads
-- 15 = /be fly <node>: TakeTaxiNode on TAXIMAP_OPENED (nb/flight.py), the flight gossip option in RXP mode
-- 16 = bag_upgrade: a loose bigger bag replaces the smallest equipped one; spare bags are sold
local BUILD_TAG, BOTEYE_BUILD = 0xBE, 32
vals[250] = BUILD_TAG * 65536 + BOTEYE_BUILD
-- group and names checked in Guides/forever/Alliance-1-14_DwarfGnome.lua:10-13 and Alliance-11-20.lua:9-11.
-- The files say "#name 1-5 Coldridge Valley"; GuideLoader.lua:1172 (addon.affix, :108-111) pads a 1-digit start,
-- so the registered names are "01-05 ..." / "05-11 ..."; "11-12" and later stay as written.
local RXP_GROUP = "RestedXP Forever Guide (A)"
-- ordinal list for block 194 (1-based; 0 = no guide, 255 = another guide or group). Must match eye.py RXP_GUIDES.
local RXP_GUIDES = { "01-05 Coldridge Valley", "05-11 Dun Morogh", "11-12 Elwynn (Dwarf/Gnome)",
    "12-14 Loch Modan (Dwarf/Gnome)", "13-15 Westfall", "14-16 Darkshore", "16-19 Darkshore", "19-20 Redridge",
    "19-21 Darkshore/Ashenvale", "20-21 Darkshore/Ashenvale", "11-13 Loch Modan (Hunter)",
    "01-06 Northshire", "06-11 Elwynn Forest", "11-13 Loch Modan" }
local RXP_ORD = {}
for i, n in ipairs(RXP_GUIDES) do RXP_ORD[n] = i end

-- RXP is an AceAddon named after its folder (Locale.lua:5 NewAddon(addon, addonName, ...))
local function RXPA()
    local A = LibStub and LibStub("AceAddon-3.0", true)
    return A and A:GetAddon("RXPGuides", true)
end
local function rxp_on() return BotEyeDB.rxp and RXPA() ~= nil end

-- settings, applied out of combat on every autoload (RXP saves them to RXPSettings at logout, per account)
local function rxp_config()
    local R = RXPA(); if not R or not R.settings or not R.settings.profile then return end
    local p = R.settings.profile
    p.enableQuestAutomation = true          -- accept / turn-in, gated on the active step (RXPGuides.lua:1076, :286)
    p.enableQuestRewardAutomation = true    -- use .turnin id,N rewards (:302-313)
    p.enableQuestChoiceAutomation = true    -- otherwise best upgrade or highest sell price (:975-1006)
    p.enableTips = true; p.enableItemUpgrades = true
    p.autoSellJunk = true                   -- InventoryManager.lua:792-856
    p.autoDiscardItems = false              -- deletes items, from input hooks only
    p.enableTrainerAutomation = false       -- rank match is broken on this client (RXP_PLAN.md section 0); BotEye buys
    p.enableGossipAutomation = true; p.enableFPAutomation = true; p.enableBindAutomation = true
    p.showEnabled = true; p.hideGuideWindow = false   -- isHidden switches off all automation (:1581, :2342)
    if (p.numMapPins or 0) == 0 then p.numMapPins = 7 end   -- arrow and goto completion need pins (map.lua:916)
    p.soloSelfFound = true                  -- hide #ah auction-house steps (AHCheck RXPGuides.lua:2549-2550)
    p.enableTargetMacro = true              -- RXPTargeting: BotEyeFind runs its body (rxp_find_text)
    p.notifyOnTargetUpdates = false         -- no chat spam over the strip text area (Targeting.lua:166-170)
    if R.RXPFrame and not R.RXPFrame:IsShown() then R.RXPFrame:Show() end
    R.isHidden = false
end

local function rxp_pick_name(lv)
    if select(2, UnitRace("player")) == "Human" then           -- (nb/route.py: the tram leg to Loch Modan is skipped)
        if lv < 6 then return "01-06 Northshire" end
        if lv < 11 then return "06-11 Elwynn Forest" end
        return "13-15 Westfall"
    end
    if lv < 5 then return "01-05 Coldridge Valley" end
    if lv < 11 then return "05-11 Dun Morogh" end
    return "12-14 Loch Modan (Dwarf/Gnome)"     -- skips the Elwynn detour (flight + tram), RXP_PLAN.md section 3 JUMP
end

-- load our route by name (never addon.defaultGuide: RXP_PLAN.md section 0.2). Embedded guides register during an
-- OnUpdate after login (RXPGuides.lua:1336-1397), so retry every 2 s for up to 60 s.
local function rxp_autoload(tries)
    tries = tries or 0
    local R = RXPA(); if not (BotEyeDB.rxp and R) then return end
    if InCombatLockdown() then                    -- the plan applies the settings out of combat: wait
        if tries < 30 then C_Timer.After(2, function() pcall(rxp_autoload, tries + 1) end) end
        return
    end
    rxp_config()
    local g = R.currentGuide
    if g and not g.empty and g.group == RXP_GROUP then return end   -- on our route (resumes from RXPCData.guideProgress)
    local name = rxp_pick_name(UnitLevel("player") or 1)
    if R.guides and R.guides[RXP_GROUP .. "||" .. name] then     -- the index GuideLoader.lua:159 registers
        R:LoadGuideTable(RXP_GROUP, name)                            -- GuideWindow.lua:1808
        note("I:rxp load " .. name)
    elseif tries < 30 then
        C_Timer.After(2, function() pcall(rxp_autoload, tries + 1) end)
    else
        note("E:rxp no guide " .. name)
    end
end

-- step element -> bit of the kind mask (block 196/197). goto / waypoint are handled by rxp_goto_kind.
local RXP_KIND = { accept = 0, turnin = 1, complete = 2, collect = 3, collectmultiple = 3, mob = 6, target = 7,
    use = 8, usespell = 8, train = 9, trainer = 10, vendor = 11, home = 12, fp = 13, fly = 14, hs = 15, xp = 16,
    deathskip = 17, buy = 18, skipgossip = 20, gossipoption = 20, unitscan = 21 }
local RXP_QTAG = { accept = 1, turnin = 2, complete = 3, collect = 4 }
local RXP_GOTO = { ["goto"] = true, groundgoto = true, flygoto = true }
-- 4 = goto RXP completes inside its radius (functions.lua:1964-1972 clears textOnly only for those; map.lua:1322-1331),
-- 5 = "wander" point: a goto without a completion radius (NPC spot, optional ",0" pin) or a .waypoint (:2095)
local function rxp_goto_kind(e)
    if RXP_GOTO[e.tag] then return (e.radius and not e.textOnly) and 4 or 5 end
    if e.tag == "waypoint" then return 5 end
end
local function setb(m, b) if b and math.floor(m / 2 ^ b) % 2 == 0 then return m + 2 ^ b end; return m end
-- incomplete, non-skipped elements only. Untagged elements: "+text" (GuideLoader.lua:933-935, no textOnly) is a
-- manual step (b19); ">>" / "*" text (textOnly) is ignored. An unknown tag counts (b22) only when it is not textOnly:
-- conditions like .isOnQuest / .isQuestComplete / .disablecheckbox are textOnly (functions.lua:4320-4340, 7697).
local function rxp_kinds(step, m)
    m = m or 0
    for _, e in ipairs(step and step.elements or {}) do
        if not e.completed and not e.skip then
            local b = rxp_goto_kind(e) or RXP_KIND[e.tag]
            if not b then
                if not e.tag then
                    if not e.textOnly and e.text then b = 19 end
                elseif not e.textOnly then b = 22 end
            end
            m = setb(m, b)
        end
    end
    return m
end

-- name lists for the plate / target bits. NPC IDs are lost in RXP (CheckNpcIds, functions.lua:4915-4931), so match
-- names; names are secret in combat, so cache the bits by NPC ID (reset when the lists or the step change)
local rx = { ctr = 0, key = nil, tctr = 0, sig = "", any = false, mob = {}, tgt = {}, scan = {}, ids = {},
    first_t = nil, first_m = nil, nmob = 0, ntgt = 0, nscan = 0,
    ordm = {}, mlow = {}, item = 0 }       -- mob names in list order, the '*' low-priority ones, block 205's item (keys)
local function rxp_unit_bits_raw(u)
    local id = npcid(UnitGUID(u))
    local name = UnitName(u)
    if name and not (issecretvalue and issecretvalue(name)) then
        local b = (rx.mob[name] and 1 or 0) + (rx.tgt[name] and 2 or 0) + (rx.scan[name] and 4 or 0)
        if id ~= 0 then rx.ids[id] = b end
        return b
    end
    return id ~= 0 and rx.ids[id] or 0
end
-- bits 1 mob list, 2 current step's .target list, 4 unitscan list (runs per plate per frame: no closure)
local function rxp_unit_bits(u)
    if not rx.any then return 0 end
    local ok, r = pcall(rxp_unit_bits_raw, u)
    return ok and type(r) == "number" and r or 0
end

local function rxp_cur_step(R)
    local g = R.currentGuide
    if not g or g.empty or not g.steps then return nil, g end
    local idx = tonumber((R.GetGuideProgress())) or 0
    return g.steps[idx], g, idx
end

-- the 3-chars-per-block text packing of fill_text
local function put_text(base, n, s)
    for i = 0, n - 1 do
        local c1, c2, c3 = s:byte(i * 3 + 1) or 0, s:byte(i * 3 + 2) or 0, s:byte(i * 3 + 3) or 0
        put(base + i, c1 * 65536 + c2 * 256 + c3)
    end
end

local function rxp_clear(from)
    for i = from, 255 do put(i, 0) end
end

-- ---- .collect bought from a vendor (Adlin: "Buy 10 Refreshing Spring Water", Alliance-1-14_DwarfGnome.lua:119-129).
-- RXP's .collect only COUNTS the item (functions.lua:2976-3244 has no merchant code); only .buy / .buyAll /
-- .buyUntilBroke buy (functions.lua:5583-5666). So BotEye buys the open .collect elements of the active steps that this
-- merchant sells, the way RXP's .buy does: capped by numAvailable and by money, in max-stack chunks. On this client
-- GetMerchantItemInfo is gone (C_MerchantFrame.GetItemInfo returns a table, as functions.lua:24-32 handles);
-- GetMerchantNumItems / GetMerchantItemLink / BuyMerchantItem / GetMerchantItemMaxStack exist (WowB.exe strings).
-- The state goes to blocks 248-249 for the bot (eye.py decode_rxp): it skips the step only when it cannot buy.
local function new_buy() return { item = 0, seen = false, short = false, done = false, full = false, price = 0,
    expect = nil, shown = false } end
local buy = new_buy()
local function gii_any(x) return ((C_Item and C_Item.GetItemInfo) or GetItemInfo)(x) end
local function item_count(id)
    local ok, n = pcall(function() return (C_Item and C_Item.GetItemCount or GetItemCount)(id) end)
    return ok and tonumber(n) or 0
end
-- open .collect elements of the current + active steps -> { { ids = {id = true}, first = id, need = n } }
local function collect_wants(R)
    local out, seen = {}, {}
    local steps = { (rxp_cur_step(R)) }
    for _, s in ipairs(R.RXPFrame and R.RXPFrame.activeSteps or {}) do steps[#steps + 1] = s end
    for _, s in ipairs(steps) do
        for _, e in ipairs(type(s) == "table" and s.elements or {}) do
            if e.tag == "collect" and not e.completed and not e.skip and not seen[e] and tonumber(e.id) then
                seen[e] = true
                local ids, have = {}, 0
                for d in tostring(e.ids or e.id):gmatch("%d+") do
                    local id = tonumber(d)
                    if not ids[id] then ids[id] = true; have = have + item_count(id) end
                end
                -- numRequired is RXP's live target (0 at parse, set by the element's handler: e.count marks a run)
                local req = (e.count ~= nil and tonumber(e.numRequired)) or tonumber(e.qty) or 0
                if req - have > 0 then out[#out + 1] = { ids = ids, first = tonumber(e.id), need = req - have } end
            end
        end
    end
    return out
end
local function bag_free()
    local n = 0
    for bag = 0, 4 do
        local ok, f = pcall(C_Container and C_Container.GetContainerNumFreeSlots or GetContainerNumFreeSlots, bag)
        n = n + (ok and tonumber(f) or 0)
    end
    return n
end
local function merchant_price(i)
    local m = C_MerchantFrame
    if m and m.GetItemInfo then
        local t = m.GetItemInfo(i)
        if type(t) == "table" then return t.price, t.stackCount, t.numAvailable end
    end
    if GetMerchantItemInfo then
        local _, _, p, q, n = GetMerchantItemInfo(i)
        return p, q, n
    end
end
local function buy_collect()
    if not rxp_on() or not (MerchantFrame and MerchantFrame:IsShown()) then return end
    local R = RXPA()
    local ex = buy.expect                     -- the last purchase is still in flight: no double buy
    if ex and item_count(ex.id) < ex.count and GetTime() - ex.t < 5 then return end
    buy.expect = nil
    local wants = collect_wants(R)
    if #wants == 0 then return end
    local m = C_MerchantFrame
    local n = tonumber(((m and m.GetNumItems) or GetMerchantNumItems)()) or 0
    local link_of = (m and m.GetItemLink) or GetMerchantItemLink
    local any = false
    for i = 1, n do
        local id = tonumber((link_of(i) or ""):match("item:(%d+)"))
        for _, w in ipairs(wants) do
            if id and w.need > 0 and w.ids[id] then
                any = true
                local price, qty, avail = merchant_price(i)
                price, qty = tonumber(price) or 0, math.max(1, tonumber(qty) or 1)
                local unit = price / qty
                local want = w.need
                if tonumber(avail) and avail >= 0 then want = math.min(want, avail) end
                local can = unit > 0 and math.floor((GetMoney() or 0) / unit) or want
                local k = math.min(want, can)
                local stack = tonumber(GetMerchantItemMaxStack and GetMerchantItemMaxStack(i))
                    or tonumber((select(8, gii_any(id)))) or 20
                stack = math.max(1, stack)
                local have = item_count(id)
                buy.item, buy.seen, buy.price = id, true, math.ceil(unit)
                buy.short = can < w.need
                buy.full = bag_free() == 0 and have % stack == 0
                if k > 0 and not buy.full then
                    local left = k
                    while left > 0 do
                        local p = math.min(stack, left)
                        left = left - p
                        BuyMerchantItem(i, p)
                    end
                    buy.done = true
                    buy.expect = { id = id, count = have + k, t = GetTime() }
                    note("I:buy " .. id .. " x" .. k .. " of " .. w.need .. " @" .. math.ceil(unit) .. "c")
                elseif k <= 0 then
                    note("I:buy " .. id .. " short: need " .. w.need .. " @" .. math.ceil(unit) .. "c have "
                        .. tostring(GetMoney()) .. "c")
                end
                w.need = 0
            end
        end
    end
    if not any then
        buy.item, buy.seen = wants[1].first or 0, false
    end
end
-- a failed pass counts in the strip's luaerrs (block 144) and BotEyeDB.lastErr, as a fill error does
local function buy_pass()
    local ok, e = pcall(buy_collect)
    if not ok then
        st.errs = (st.errs + 1) % 256
        if BotEyeDB.lastErr ~= e then BotEyeDB.lastErr = e; note("LUAERR buy " .. tostring(e)) end
    end
end
-- MERCHANT_SHOW: one pass at once, one after the junk sale has paid (RXP sells on the same event, BotEye 1.5 s later)
local function buy_on_show()
    buy.shown = true
    if not rxp_on() then return end
    local ok, w = pcall(collect_wants, RXPA())
    if not ok or #w == 0 then return end
    buy_pass()
    C_Timer.After(2.0, buy_pass)
end

-- slow group: a merchant window seen without MERCHANT_SHOW still gets one pass
local function buy_tick()
    local shown = MerchantFrame and MerchantFrame:IsShown() and true or false
    if shown and not buy.shown then buy.shown = true; buy_pass() end
    if not shown then buy.shown = false end
end

-- ---- water and food (build 13). 2026-10-15 rest review: 20-35% of the wall time resting on natural regen, never a
-- drink - the eat / drink keys named level-1 items the bags did not hold (Tough Jerky, Ice Cold Milk went unused) and
-- no vendor visit bought any. An item whose use spell is "Drink" / "Food" (GetItemSpell) is water / food; its required
-- level ranks it (each vendor tier restores more). "Current" = required level within SUP_SPAN of ours: what is worth
-- stocking. Blocks 166-169 tell the bot (eye.py), the eat / drink macros use the best owned item (macro_frame), and a
-- merchant that sells a current tier tops the bags up to SUP_TARGET, keeping SUP_RESERVE c per level for training.
local SUP_SPAN, SUP_RESERVE = 10, 50                 -- (nb/stock.py SPAN / RESERVE_PER_LEVEL)
-- a kind that is all gone may spend more (nb/stock.py RESERVE_OUT / SHARE_OUT): regen without water costs more time
local SUP_RESERVE_OUT, SUP_SHARE_OUT = 20, 0.5
local SUP_SHARE = 0.25         -- at most this share of our money per visit: training costs 27s+ from level 14 (nb/stock.py SHARE)
local SUP_TARGET = { drink = 40, food = 20 }         -- (nb/stock.py DRINK_TARGET / FOOD_TARGET)
local DRINK_DEFAULT = "/use Refreshing Spring Water"
local EAT_DEFAULT = "/use Tough Hunk of Bread\n/use Darnassian Bleu\n/use Shiny Red Apple"
local sup = { drink = 0, food = 0, n = { drink = 0, food = 0 }, cur = { drink = 0, food = 0 }, done = {} }
local sup_kinds = {}
-- -> "drink" / "food" / false, nil while the item's data is not loaded yet
local function sup_kind(id)
    local k = sup_kinds[id]
    if k ~= nil then return k end
    local okn, name = pcall(gii_any, id)
    if not okn or not name then return nil end
    local oks, sp = pcall((C_Item and C_Item.GetItemSpell) or GetItemSpell, id)
    k = (oks and sp == "Drink" and "drink") or (oks and sp == "Food" and "food") or false
    if not k and oks and sp == nil and GetItemInfoInstant and select(6, GetItemInfoInstant(id)) == 0 then
        return nil                                    -- a consumable whose spell is not loaded yet: ask again
    end
    sup_kinds[id] = k
    return k
end
local function sup_req(id)
    local ok, _, _, _, _, req = pcall(gii_any, id)
    return ok and tonumber(req) or nil
end
local function sup_mana() return (UnitPowerType and UnitPowerType("player")) == 0 end
-- bags -> sup.drink / sup.food (best usable: highest required level), sup.n (usable) / sup.cur (current tier) counts.
-- An item an open RestedXP .collect wants is the guide's, not ours (Adlin's 10 Refreshing Spring Water)
local function sup_scan()
    local lvl = UnitLevel("player") or 1
    local keep = {}
    if rxp_on() then
        local ok, w = pcall(collect_wants, RXPA())
        for _, x in ipairs(ok and w or {}) do for id in pairs(x.ids) do keep[id] = true end end
    end
    local best, bl = {}, {}
    local n, cur = { drink = 0, food = 0 }, { drink = 0, food = 0 }
    local C = C_Container
    for bag = 0, 4 do
        local ns = (C and C.GetContainerNumSlots or GetContainerNumSlots)(bag) or 0
        for slot = 1, ns do
            local id, cnt
            if C and C.GetContainerItemInfo then
                local info = C.GetContainerItemInfo(bag, slot)
                if info then id, cnt = info.itemID, info.stackCount end
            else
                local _, c, _, _, _, _, _, _, _, i = GetContainerItemInfo(bag, slot)
                id, cnt = i, c
            end
            local k = id and not keep[id] and sup_kind(id)
            local req = k and sup_req(id)
            if req and req <= lvl then
                cnt = tonumber(cnt) or 1
                n[k] = n[k] + cnt
                if req >= lvl - SUP_SPAN then cur[k] = cur[k] + cnt end
                if not best[k] or req > bl[k] or (req == bl[k] and id < best[k]) then best[k], bl[k] = id, req end
            end
        end
    end
    sup.n, sup.cur, sup.drink, sup.food = n, cur, best.drink or 0, best.food or 0
end
-- 166 best drink id, 167 best food id, 168 / 169 drink / food: usable count + current-tier count * 2048 + b22 wanted
-- (drink: a mana user; food: always)
local function fill_supplies()
    sup_scan()
    put(166, sup.drink)
    put(167, sup.food)
    put(168, math.min(sup.n.drink, 2047) + math.min(sup.cur.drink, 2047) * 2048 + (sup_mana() and 2 ^ 22 or 0))
    put(169, math.min(sup.n.food, 2047) + math.min(sup.cur.food, 2047) * 2048 + 2 ^ 22)
end
-- the merchant's best current-tier water and food, bought up to SUP_TARGET current items (scaled down to the money over
-- the reserve; drink first). One purchase per kind per window: the bags count a purchase a moment later
local function buy_supplies(retry)
    if not (MerchantFrame and MerchantFrame:IsShown()) then return end
    local lvl = UnitLevel("player") or 1
    local m = C_MerchantFrame
    local n = tonumber(((m and m.GetNumItems) or GetMerchantNumItems)()) or 0
    local link_of = (m and m.GetItemLink) or GetMerchantItemLink
    local pick, unknown = {}, false
    for i = 1, n do
        local id = tonumber((link_of(i) or ""):match("item:(%d+)"))
        local k = id and sup_kind(id)
        if id and k == nil then
            unknown = true
            if C_Item and C_Item.RequestLoadItemDataByID then pcall(C_Item.RequestLoadItemDataByID, id) end
        end
        local req = k and sup_req(id)
        if req and req <= lvl and req >= lvl - SUP_SPAN and (k ~= "drink" or sup_mana()) then
            local price, qty, avail = merchant_price(i)
            price, qty = tonumber(price) or 0, math.max(1, tonumber(qty) or 1)
            avail = tonumber(avail)
            local p = pick[k]
            if price > 0 and (not avail or avail ~= 0)
                    and (not p or req > p.req or (req == p.req and price / qty < p.unit)) then
                pick[k] = { i = i, id = id, req = req, unit = price / qty, lot = qty, avail = avail }
            end
        end
    end
    sup_scan()
    local spent, out = 0, {}
    for _, k in ipairs({ "drink", "food" }) do
        local p = pick[k]
        local want = p and not sup.done[k] and (SUP_TARGET[k] - sup.cur[k]) or 0
        if want > 0 then
            if p.avail and p.avail > 0 then want = math.min(want, p.avail) end
            local m = GetMoney() or 0
            local gone = (sup.n[k] or 0) == 0
            local budget = math.min(m - (gone and SUP_RESERVE_OUT or SUP_RESERVE) * lvl,
                math.floor(m * (gone and SUP_SHARE_OUT or SUP_SHARE))) - spent
            local lots = math.min(math.ceil(want / p.lot), math.floor(budget / (p.unit * p.lot)))
            local k2 = math.max(0, lots) * p.lot
            sup.done[k] = true
            if k2 > 0 and bag_free() > 0 then
                local stack = math.max(1, tonumber(GetMerchantItemMaxStack and GetMerchantItemMaxStack(p.i))
                    or tonumber((select(8, gii_any(p.id)))) or 20)
                local left = k2
                while left > 0 do
                    local q = math.min(stack, left)
                    left = left - q
                    BuyMerchantItem(p.i, q)
                end
                spent = spent + k2 * p.unit
                out[#out + 1] = p.id .. " x" .. k2 .. " @" .. math.ceil(p.unit) .. "c"
            else
                out[#out + 1] = p.id .. " short (" .. (bag_free() > 0 and "money" or "bags") .. ")"
            end
        end
    end
    if #out > 0 then note("I:stock " .. table.concat(out, ", ")) end
    -- items whose data is not loaded yet are skipped: try again (Khara Deepwater's fish, inst2 15:20: one retry
    -- was too few, the visit bought nothing)
    retry = tonumber(retry) or (retry and 4) or 0
    if unknown and retry > 0 then C_Timer.After(1.0, function() pcall(buy_supplies, retry - 1) end) end
end
-- ---- ammo (hunter). The ranged weapon in slot 18 says which kind (bow / crossbow: arrows, gun: bullets); the bags
-- and the ammo slot (0) hold it. Block 170: count (capped 65535) + b21 bullets + b22 wanted (a ranged weapon that
-- shoots ammo). Every merchant that sells the kind tops it up to AMMO_TARGET (within the stock budget), and the
-- best owned ammo goes into the ammo slot out of combat (nb/stock.py "ammo")
local AMMO_TARGET = 1000
local ammo = { kind = nil, n = 0, best = nil, bestreq = -1, eq = nil, t_eq = 0 }
local function gii_inst(x) return ((C_Item and C_Item.GetItemInfoInstant) or GetItemInfoInstant)(x) end
local function ammo_scan()
    ammo.kind, ammo.n, ammo.best, ammo.bestreq = nil, 0, nil, -1
    local w = GetInventoryItemID and GetInventoryItemID("player", 18)
    if not w then return end
    local _, _, _, _, _, cls, sub = gii_inst(w)
    if cls ~= 2 then return end
    if sub == 2 or sub == 18 then ammo.kind = 2 elseif sub == 3 then ammo.kind = 3 else return end
    local lvl = UnitLevel("player") or 1
    ammo.eq = GetInventoryItemID("player", 0)
    local C = C_Container
    for bag = 0, 4 do
        local ns = (C and C.GetContainerNumSlots or GetContainerNumSlots)(bag) or 0
        for slot = 1, ns do
            local id, cnt
            if C and C.GetContainerItemInfo then
                local info = C.GetContainerItemInfo(bag, slot)
                if info then id, cnt = info.itemID, info.stackCount end
            else
                local _, c, _, _, _, _, _, _, _, i = GetContainerItemInfo(bag, slot)
                id, cnt = i, c
            end
            if id then
                local _, _, _, _, _, c2, s2 = gii_inst(id)
                local req = c2 == 6 and s2 == ammo.kind and sup_req(id) or nil
                if req and req <= lvl then
                    ammo.n = ammo.n + (tonumber(cnt) or 1)
                    if req > ammo.bestreq then ammo.best, ammo.bestreq = id, req end
                end
            end
        end
    end
end
local function fill_ammo()
    ammo_scan()
    put(170, math.min(ammo.n, 65535) + (ammo.kind == 3 and 2 ^ 21 or 0) + (ammo.kind and 2 ^ 22 or 0))
    -- the best ammo into the ammo slot (a new tier is not used until it is equipped; out of combat, every 10 s at most)
    if ammo.best and ammo.best ~= ammo.eq and not InCombatLockdown() and not (CursorHasItem and CursorHasItem())
            and GetTime() - ammo.t_eq > 10 then
        ammo.t_eq = GetTime()
        if C_Item and C_Item.EquipItemByName then pcall(C_Item.EquipItemByName, ammo.best)
        elseif EquipItemByName then pcall(EquipItemByName, ammo.best) end
    end
end
local function buy_ammo()
    if not (MerchantFrame and MerchantFrame:IsShown()) then return end
    ammo_scan()
    if not ammo.kind or ammo.n >= AMMO_TARGET then return end
    local lvl = UnitLevel("player") or 1
    local m = C_MerchantFrame
    local n = tonumber(((m and m.GetNumItems) or GetMerchantNumItems)()) or 0
    local link_of = (m and m.GetItemLink) or GetMerchantItemLink
    local pick
    for i = 1, n do
        local id = tonumber((link_of(i) or ""):match("item:(%d+)"))
        if id then
            local _, _, _, _, _, c2, s2 = gii_inst(id)
            local req = c2 == 6 and s2 == ammo.kind and sup_req(id) or nil
            if req and req <= lvl then
                local price, qty = merchant_price(i)
                price, qty = tonumber(price) or 0, math.max(1, tonumber(qty) or 1)
                if price > 0 and (not pick or req > pick.req) then
                    pick = { i = i, id = id, req = req, unit = price / qty, lot = qty }
                end
            end
        end
    end
    if not pick then return end
    local money = GetMoney() or 0
    local gone = ammo.n == 0
    local budget = math.min(money - (gone and SUP_RESERVE_OUT or SUP_RESERVE) * lvl,
        math.floor(money * (gone and SUP_SHARE_OUT or SUP_SHARE)))
    local lots = math.min(math.ceil((AMMO_TARGET - ammo.n) / pick.lot), math.floor(budget / (pick.unit * pick.lot)))
    local k = math.max(0, lots) * pick.lot
    if k <= 0 or bag_free() == 0 then note("I:ammo " .. pick.id .. " short"); return end
    local stack = math.max(1, tonumber(GetMerchantItemMaxStack and GetMerchantItemMaxStack(pick.i)) or 200)
    local left = k
    while left > 0 do
        local q = math.min(stack, left)
        left = left - q
        BuyMerchantItem(pick.i, q)
    end
    note("I:ammo " .. pick.id .. " x" .. k .. " @" .. math.ceil(pick.unit * 100) / 100 .. "c")
end
-- strip row 4, layout in nb/RXP_PLAN.md section 2 (decoded by eye.py decode_rxp)
local function fill_rxp()
    put(192, RXP_TAG)
    local R = RXPA()
    rx.item = 0
    if not R then
        rx.any = false; rx.first_t, rx.first_m, rx.ordm, rx.mlow = nil, nil, {}, {}
        rxp_clear(193); put(250, BUILD_TAG * 65536 + BOTEYE_BUILD); return
    end
    local step, g, idx = rxp_cur_step(R)
    local p = R.settings and R.settings.profile or {}
    local function pb(b, fn)
        local ok, r = pcall(fn)
        if ok and r then return 2 ^ b end
        return 0
    end
    -- step-change counter: bumped whenever (guide key, step index) changes
    local key = step and ((g.key or g.name or "?") .. "#" .. tostring(idx)) or ""
    if key ~= rx.key then rx.key = key; rx.ctr = (rx.ctr + 1) % 4096; rx.ids = {}
        local b = new_buy(); b.expect, b.shown = buy.expect, buy.shown; buy = b end
    local stat = 1 + pb(1, function() return g and not g.empty end)
        + pb(2, function() return R.isHidden end)
        + pb(3, function() return p.enableQuestAutomation end)
        + pb(4, function() return R.arrowFrame and R.arrowFrame:IsShown() end)      -- RXPG_ARROW (map.lua:41)
        + pb(5, function() return step and step.completed end)
        + pb(6, function() return R.loadNextStep end)
        + pb(7, function() return g and not g.empty and not g.next end)
        + pb(8, function() return R.IsGamePadEnabled() end)                         -- RXPGuides.lua:8-16
        + pb(9, function() return R.inventoryManager:IsFeatureEnabled() end)
        + pb(10, function() return UnitOnTaxi("player") end)
        + pb(11, function() return p.enableTrainerAutomation end)
    put(193, stat + rx.ctr * 4096)

    -- name lists (Targeting.lua:160 GetCurrentTargets = targetList, mobList, unitscanList; '*' = low priority)
    local tl, ml, ul
    if R.targeting and R.targeting.GetCurrentTargets then
        local ok, a, b, c = pcall(R.targeting.GetCurrentTargets)
        if ok then tl, ml, ul = a, b, c end
    end
    local mob, tgt, scan, ordm, ordt, ords = {}, {}, {}, {}, {}, {}
    local function add(set, ord, n)
        if type(n) ~= "string" then return end
        n = n:gsub("^%*", "")
        if n ~= "" and not set[n] then set[n] = true; ord[#ord + 1] = n end
    end
    local mlow = {}
    for _, n in ipairs(type(ml) == "table" and ml or {}) do
        if type(n) == "string" and n:sub(1, 1) == "*" then mlow[n:sub(2)] = true end
        add(mob, ordm, n)
    end
    for _, n in ipairs(type(ul) == "table" and ul or {}) do add(scan, ords, n) end
    for _, e in ipairs(step and step.elements or {}) do       -- inTargetList: the CURRENT step's .target names
        if e.tag == "target" and type(e.targets) == "table" then
            for _, n in ipairs(e.targets) do add(tgt, ordt, n) end
        end
    end
    local sig = table.concat(ordm, ";") .. "|" .. table.concat(ordt, ";") .. "|" .. table.concat(ords, ";")
    if sig ~= rx.sig then rx.sig = sig; rx.tctr = (rx.tctr + 1) % 16777216; rx.ids = {} end
    rx.mob, rx.tgt, rx.scan = mob, tgt, scan
    rx.any = #ordm + #ordt + #ords > 0
    rx.first_t, rx.first_m = ordt[1], ordm[1]
    rx.ordm, rx.mlow = ordm, mlow

    -- 194 guide ordinal + step index; 195 source line + #activeSteps + sticky (+ b21 = BotEyeDB.rxp)
    local ord = 0
    if g and not g.empty then ord = (g.group == RXP_GROUP and RXP_ORD[g.name]) or 255 end
    if not step then       -- no guide, or the index is past the last step
        rxp_clear(194)
        put(194, ord + math.max(0, math.min(tonumber(idx) or 0, 65535)) * 256)
        put(195, BotEyeDB.rxp and 2 ^ 21 or 0)
        put(208, rx.tctr)
        put(250, BUILD_TAG * 65536 + BOTEYE_BUILD)
        return
    end
    put(194, ord + math.min(idx, 65535) * 256)
    local active = R.RXPFrame and R.RXPFrame.activeSteps or {}
    local line = (step.stepId and g.guideId) and (step.stepId - g.guideId) or 0   -- GuideLoader.lua:1075, :1090
    if line < 0 or line > 65535 then line = 0 end
    put(195, line + math.min(#active, 15) * 65536 + (step.sticky and 2 ^ 20 or 0) + (BotEyeDB.rxp and 2 ^ 21 or 0))

    -- 196 kind mask of the current step, 197 the same over the active sticky (#completewith) steps
    put(196, rxp_kinds(step))
    local sk = 0
    for _, s in ipairs(active) do
        if s ~= step and s.sticky and s.elements then sk = rxp_kinds(s, sk) end
    end
    put(197, sk)

    -- 198-200 waypoint: (1) first incomplete radius goto of the step, (2) first non-skipped wander point,
    -- (3) the arrow's element. X = e.wy, Y = e.wx: HBD swaps them (functions.lua:1910-1930)
    local wp, wfl
    for _, e in ipairs(step.elements or {}) do
        if e.wx and e.wy and not e.skip and not e.completed and rxp_goto_kind(e) == 4 then wp, wfl = e, 1; break end
    end
    if not wp then
        for _, e in ipairs(step.elements or {}) do
            if e.wx and e.wy and not e.skip and rxp_goto_kind(e) == 5 then wp, wfl = e, 1; break end
        end
    end
    if not wp then
        local ae = R.arrowFrame and R.arrowFrame.element
        if type(ae) == "table" and ae.wx and ae.wy then
            wp, wfl = ae, 4 + ((type(ae.step) == "table" and ae.step.sticky) and 2 or 0)
        end
    end
    if wp then
        put(198, coord(wp.wy)); put(199, coord(wp.wx))
        local r = tonumber(wp.radius)
        r = r and math.max(0, math.min(254, math.floor(r + 0.5))) or 255
        local inst = math.max(0, math.min(255, tonumber(wp.instance) or 0))
        wfl = wfl + (wp.lowPrio and 8 or 0) + (wp.dynamic and 16 or 0)
        put(200, r + inst * 256 + wfl * 65536)
    else
        put(198, 0); put(199, 0); put(200, 0)
    end

    -- 201-204 quest elements; 206 train; 207 xp level + list sizes; 224-247 goto points
    local nq, trn, xpl, np = 0, 0, 0, 0
    for _, e in ipairs(step.elements or {}) do
        local qt = RXP_QTAG[e.tag]
        if qt and nq < 4 and tonumber(e.questId) then
            put(201 + nq, tonumber(e.questId) % 1048576 + qt * 1048576 + ((e.completed or e.skip) and 2 ^ 23 or 0))
            nq = nq + 1
        end
        if e.tag == "train" and trn == 0 and tonumber(e.id) and not e.completed and not e.skip then
            local okk, known = pcall(IsPlayerSpell, e.id)
            trn = tonumber(e.id) % 1048576 + ((okk and known == true) and 2 ^ 23 or 0)
        end
        if e.tag == "xp" and xpl == 0 and tonumber(e.level) and not e.completed and not e.skip then
            xpl = math.max(0, math.min(255, tonumber(e.level)))
        end
        if np < 8 and e.wx and e.wy and rxp_goto_kind(e) then
            local r = tonumber(e.radius)
            r = r and math.max(0, math.min(254, math.floor(r + 0.5))) or 255
            local fl = (rxp_goto_kind(e) == 5 and 1 or 0) + ((e.completed or e.skip) and 2 or 0) + (e.hidePin and 4 or 0)
            put(224 + np * 3, coord(e.wy)); put(225 + np * 3, coord(e.wx)); put(226 + np * 3, r + fl * 256)
            np = np + 1
        end
    end
    for k = nq, 3 do put(201 + k, 0) end
    for k = np, 7 do put(224 + k * 3, 0); put(225 + k * 3, 0); put(226 + k * 3, 0) end
    put(206, trn)
    put(207, xpl + math.min(#ordm, 15) * 256 + math.min(#ordt, 15) * 4096 + math.min(#ords, 15) * 65536)

    -- 205 active item: the step's activeItems (prefer one in the bags, lowest id) else the first shown item button
    local item, btn = 0, 0
    if type(step.activeItems) == "table" then
        local best, bestbag
        for id, v in pairs(step.activeItems) do
            if v and type(id) == "number" then
                local okc, cnt = pcall(function() return (C_Item and C_Item.GetItemCount or GetItemCount)(id) end)
                local inbag = okc and type(cnt) == "number" and cnt > 0
                if not best or (inbag and not bestbag) or (inbag == bestbag and id < best) then best, bestbag = id, inbag end
            end
        end
        item = best or 0
    end
    local bl = R.activeItemFrame and R.activeItemFrame.buttonList or {}   -- ActiveItemFrame.lua:413, :476
    for i, b in ipairs(bl) do
        if b:IsShown() and not b.spell and tonumber(b.itemId) then
            if item == 0 then item = b.itemId; btn = i; break end
            if b.itemId == item then btn = i; break end
        end
    end
    local ready = false
    if item ~= 0 then
        local okr, s, d = pcall(function() return (C_Container and C_Container.GetItemCooldown or GetItemCooldown)(item) end)
        if okr and type(s) == "number" and type(d) == "number" then ready = s == 0 or s + d <= GetTime() end
    end
    put(205, item % 1048576 + math.min(btn, 7) * 1048576 + (ready and 2 ^ 23 or 0))
    rx.item = item

    put(208, rx.tctr)
    put_text(209, 15, ("T:" .. (rx.first_t or "") .. "|M:" .. (rx.first_m or "")):sub(1, 45))
    -- 248 buy state of the step's vendor .collect: item + b20 this merchant sells it, b21 short of money for all of
    -- it, b22 BotEye bought some, b23 bags full; 249 items still needed (live) + unit price in copper * 256
    local okw, wants = pcall(collect_wants, R)
    local w = okw and wants[1] or nil
    local bi = buy.item ~= 0 and buy.item or (w and w.first) or 0
    local need = 0
    for _, x in ipairs(okw and wants or {}) do if x.ids[bi] then need = x.need; break end end
    put(248, bi % 1048576 + (buy.seen and 2 ^ 20 or 0) + (buy.short and 2 ^ 21 or 0) + (buy.done and 2 ^ 22 or 0)
        + (buy.full and 2 ^ 23 or 0))
    put(249, math.min(need, 255) + math.min(buy.price or 0, 65535) * 256)
    put(250, BUILD_TAG * 65536 + BOTEYE_BUILD)
    for i = 251, 255 do put(i, 0) end
end

-- ---- handler gating (RXP_PLAN.md section 4). BotEye's quest / gossip / merchant handlers yield to RXP only while
-- RXP really automates: flag on, our guide loaded, frame not hidden (isHidden switches all of RXP's automation off,
-- RXPGuides.lua:1076) and quest automation on. Otherwise (flag off = the old guide driver, or RXP stalled) BotEye's own
-- handlers run as before, so a quest window is never left without an owner.
local function rxp_owns()
    if not BotEyeDB.rxp then return false end
    local R = RXPA(); if not R then return false end
    local g = R.currentGuide
    local p = R.settings and R.settings.profile
    return (g and not g.empty and not R.isHidden and p and p.enableQuestAutomation) and true or false
end

local function kbit(m, b) return math.floor((m or 0) / 2 ^ b) % 2 == 1 end

-- kind mask (block 196 bits) of the current step; 0 without a step
local function rxp_step_kinds()
    local R = RXPA(); if not R then return 0 end
    local ok, step = pcall(rxp_cur_step, R)
    if not ok or not step then return 0 end
    local ok2, m = pcall(rxp_kinds, step)
    return ok2 and m or 0
end

-- an active step with a .deathskip element: RXP accepts the spirit healer itself (functions.lua:2912-2922)
local function rxp_deathskip_active()
    local R = RXPA(); if not R or not R.RXPFrame then return false end
    for _, s in ipairs(R.RXPFrame.activeSteps or {}) do
        for _, e in ipairs(s.elements or {}) do
            if e.tag == "deathskip" and not e.completed then return true end
        end
    end
    return false
end

-- BotEye sells junk itself unless RXP will: ProcessJunk returns at once when the feature is off or the gamepad is on
-- (InventoryManager.lua:792-795) and BotEyePadClaim sets GamePadEnable 1; autoSellJunk gates the sale (:797-798)
local function rxp_sells_junk()
    if not rxp_on() then return false end
    local R = RXPA()
    local ok, r = pcall(function()
        local p = R.settings.profile
        return p.autoSellJunk and not R.IsGamePadEnabled() and R.inventoryManager:IsFeatureEnabled()
    end)
    return ok and r and true or false
end

-- RXP's trainer automation buys nothing on this client (rank from GetTrainerServiceInfo's 5th return, RXP_PLAN.md
-- section 0.3); rxp_config keeps it off. Only when someone turns it on (after a passing live test) BotEye stops buying.
local function rxp_trains()
    if not rxp_on() then return false end
    local R = RXPA()
    return (R.settings and R.settings.profile and R.settings.profile.enableTrainerAutomation) and true or false
end

-- ---- dynamic keys, fed from RXP (RXP_PLAN.md section 4 "Keys"), set out of combat by the macro_frame tick:
-- NUMPADMULTIPLY BotEyeFind = the step's mob list as /targetexact + /stopmacro [harm,nodead] (as /be find),
-- PAGEUP (+ fallbacks) BotEyeRXPNpc = /targetexact <first .target name of the current step>,
-- INSERT BotEyeQuestItem = /use item:<block 205 item> (the RXP step's item; a no-op until a step names one).
local QUEST_ITEM_DEFAULT = "/run local _"
local MACRO_MAX = 255                                    -- the client's macro body limit; longer bodies may be cut
local function rxp_find_text()
    -- RXP's own targeting macro (Targeting.lua: the step's unitscan, .mob and .target names, low-priority ones
    -- first, /targetexact each - the last live match wins - then /targetlasttarget [dead]): the user found it the
    -- best at finding the step's next target. Our 6-name mob list below only when RXP has not built it.
    local okm, _, _, body = pcall(GetMacroInfo, "RXPTargeting")
    if okm and type(body) == "string" and body:find("/targetexact", 1, true) then return body end
    local t, len = {}, 0
    local low = rx.mlow or {}
    for pass = 1, 2 do                                   -- normal names first, '*' low-priority names after
        for _, name in ipairs(rx.ordm or {}) do
            local is_low = low[name] and true or false
            if is_low == (pass == 2) and #t < 6 then     -- at most 6 names, like /be find
                local l = "/targetexact " .. name .. "\n/stopmacro [harm,nodead]"
                local add = #l + (len > 0 and 1 or 0)    -- + the joining newline
                if len + add > MACRO_MAX then break end
                t[#t + 1] = l; len = len + add
            end
        end
    end
    return table.concat(t, "\n")
end
local function setmacro(b, text)
    if b and b:GetAttribute("macrotext") ~= text then b:SetAttribute("macrotext", text); return true end
    return false
end
local function rxp_keys()
    if InCombatLockdown() then return end               -- SetAttribute is blocked in combat (RXP_PLAN.md section 4)
    local find = _G.BotEyeFind
    if not rxp_on() then
        if rx.keys_on then                               -- switched off: put the old driver's content back once
            rx.keys_on = false
            if find and rx.find_text and find:GetAttribute("macrotext") == rx.find_text then setmacro(find, "") end
            rx.find_text = nil
            setmacro(_G.BotEyeRXPNpc, "")
            setmacro(_G.BotEyeQuestItem, QUEST_ITEM_DEFAULT)
        end
        return
    end
    rx.keys_on = true
    -- BotEyeFind: only on a list change, and an empty list clears only our own text (a later /be find stays)
    local text = rxp_find_text()
    if text ~= rx.find_text then
        if text ~= "" or (find and find:GetAttribute("macrotext") == rx.find_text) then setmacro(find, text) end
        rx.find_text = text
    end
    setmacro(_G.BotEyeRXPNpc, rx.first_t and ("/targetexact " .. rx.first_t) or "")
    setmacro(_G.BotEyeQuestItem, (tonumber(rx.item) or 0) > 0 and ("/use item:" .. rx.item) or QUEST_ITEM_DEFAULT)
end

-- /be rxp ... (RXP_PLAN.md section 1: replaces long /be eval calls)
local function rxp_cmd(arg)
    local sub, rest = (arg or ""):match("^(%S*)%s*(.-)$")
    sub = (sub or ""):lower()
    if sub == "on" or sub == "off" then
        BotEyeDB.rxp = sub == "on"
        note("I:rxp " .. sub)
        if BotEyeDB.rxp then pcall(rxp_autoload, 0) end
        return
    end
    local R = RXPA()
    if not R then note("E:rxp missing"); return end
    if sub == "load" then
        if rest == "" then note("E:rxp load <name>"); return end
        if not (R.guides and R.guides[RXP_GROUP .. "||" .. rest]) then note("E:rxp no guide " .. rest); return end
        R:LoadGuideTable(RXP_GROUP, rest); note("I:rxp load " .. rest)
    elseif sub == "next" then                                      -- stall escape (GuideWindow.lua:755)
        local n = tonumber((R.GetGuideProgress())) or 0
        R.SetStep(n + 1); note("I:rxp next " .. (n + 1))
    elseif sub == "skip" then                                      -- the checkbox click, GuideWindow.lua:239-249
        local step = rxp_cur_step(R)
        if not step then note("E:rxp no step"); return end
        local n = 0
        for _, e in ipairs(step.elements or {}) do
            if not e.completed and not e.textOnly and not e.optional and not e.skip then
                if e.OnComplete then pcall(e.OnComplete, e) end
                e.skip = true; n = n + 1
            end
        end
        R.updateSteps = true; pcall(R.UpdateMap)
        note("I:rxp skip " .. n)
    elseif sub == "accept" then
        -- a skipped accept the bot goes back for (inst3 20:29:21: Sten's 179 skipped, its chain lost): RXP accepts
        -- only an active step's quests (QuestAutoAccept, RXPGuides.lua:286-300), so BotEye takes this one id, 300 s
        local id = tonumber(rest) or 0
        st.rxp_acc = id > 0 and { id = id, t = GetTime() } or nil
        note("I:rxp accept " .. id)
    elseif sub == "dump" then
        local step, g, idx = rxp_cur_step(R)
        if not step then note("I:rxp dump none " .. tostring(g and g.name)); BotEyeDB.rxpDump = nil; return end
        local t = {}
        for _, e in ipairs(step.elements or {}) do
            local s = tostring(e.tag or (e.textOnly and "txt" or "+txt"))
            if e.questId then s = s .. ":" .. tostring(e.questId) end
            if e.completed then s = s .. "!" elseif e.skip then s = s .. "~" end
            t[#t + 1] = s
        end
        local line = (step.stepId and g.guideId) and (step.stepId - g.guideId) or 0
        local d = tostring(g.name) .. " #" .. tostring(idx) .. " L" .. line .. (step.sticky and " S" or "") .. " " .. table.concat(t, ",")
        BotEyeDB.rxpDump = d
        note("I:rxp " .. d)
    else
        note("I:rxp " .. tostring(BotEyeDB.rxp and "on" or "off") .. " " .. tostring(R.currentGuide and R.currentGuide.name))
    end
end

-- ---------------------------------------------------------------- blocks
-- Casting state from the cast EVENTS: UnitCastingInfo returns secret values in combat (the flag stuck at 1).
-- START / CHANNEL_START set it; STOP / FAILED / INTERRUPTED / CHANNEL_STOP clear it. DELAYED (pushback)
-- keeps it set until the real STOP. Instant spells fire no START. A 6 s watchdog covers a missed event.
local cast_end, chan_end = 0, 0
local function casting_now()
    local now = GetTime()
    return now < cast_end or now < chan_end
end
-- a channel (castTime 0): its own end from UnitChannelInfo (ms; secret in combat -> 30 s, CHANNEL_STOP ends it).
-- SUCCEEDED fires as a channel starts and must not end it (Hunt 12:33: the Taming Rod's 20 s channel read as over
-- after 2.8 s, the bot fought the boar and killed it)
local function chan_end_time(now)
    local ok, e = pcall(function() return select(5, UnitChannelInfo("player")) end)
    if ok and type(e) == "number" and not (issecretvalue and issecretvalue(e)) and e / 1000 > now then
        return e / 1000 + 0.3
    end
    return now + 30
end
local castf = CreateFrame("Frame")
for _, e in ipairs({ "UNIT_SPELLCAST_START", "UNIT_SPELLCAST_CHANNEL_START", "UNIT_SPELLCAST_STOP",
    "UNIT_SPELLCAST_FAILED", "UNIT_SPELLCAST_INTERRUPTED", "UNIT_SPELLCAST_CHANNEL_STOP",
    "UNIT_SPELLCAST_FAILED_QUIET", "UNIT_SPELLCAST_SUCCEEDED", "UNIT_SPELLCAST_DELAYED",
    "UNIT_SPELLCAST_CHANNEL_UPDATE" }) do
    pcall(castf.RegisterUnitEvent, castf, e, "player")
end
-- START: expected end = now + the spell's cast time (not secret) + a little; DELAYED (pushback) pushes the end out
-- by 0.5 s; STOP / SUCCEEDED / FAILED / INTERRUPTED end it at once. So a missed stop event costs at most the cast time.
castf:SetScript("OnEvent", function(_, e, unit, guid, spellid)
    local now = GetTime()
    if e == "UNIT_SPELLCAST_CHANNEL_START" or e == "UNIT_SPELLCAST_CHANNEL_UPDATE" then
        chan_end = chan_end_time(now)
    elseif e == "UNIT_SPELLCAST_CHANNEL_STOP" or e == "UNIT_SPELLCAST_INTERRUPTED" then
        chan_end, cast_end = 0, 0
    elseif e == "UNIT_SPELLCAST_SUCCEEDED" and now < chan_end then
        -- (a channel's own SUCCEEDED, at its start)
    elseif e == "UNIT_SPELLCAST_START" then
        local ms = 2500
        local ok, info = pcall(C_Spell.GetSpellInfo, spellid)
        if ok and type(info) == "table" and type(info.castTime) == "number" and info.castTime > 0 then ms = info.castTime end
        cast_end = now + ms / 1000 + 0.3
    elseif e == "UNIT_SPELLCAST_DELAYED" then
        if cast_end > now then cast_end = cast_end + 0.5 end
    else
        cast_end = 0
    end
end)

local function fill_player()
    local a, b, z, inst = UnitPosition("player")
    put(2, coord(a)); put(3, coord(b)); put(4, coord(z))
    put(5, (GetPlayerFacing() or 0) * 2097152)
    put(7, UnitHealthMax("player")); put(9, UnitPowerMax("player"))
    setbar(0, UnitHealth("player"), UnitHealthMax("player"))
    setbar(1, UnitPower("player"), UnitPowerMax("player"))
    put(10, UnitLevel("player")); put(11, inst or 0)
    put(12, UnitXP("player")); put(13, UnitXPMax("player"))
    -- every term is guarded: in combat some of these return secret values and a comparison would throw,
    -- which aborted the whole function and left the flags (casting, combat ...) frozen on their last value
    local function sb(b, fn)
        local ok, r = pcall(fn)
        if ok and r then return 2 ^ b end
        return 0
    end
    local ok0, speed = pcall(function() return (GetUnitSpeed("player") or 0) + 0 end)
    if not ok0 or type(speed) ~= "number" then speed = 0 end
    local fl = sb(0, function() return UnitAffectingCombat("player") end)
        + sb(1, function() return UnitIsDead("player") and not UnitIsGhost("player") end)
        + sb(2, function() return UnitIsGhost("player") end)
        + sb(3, casting_now)
        + sb(4, function() return speed > 0 end)
        + sb(5, function() return IsSwimming() end)
        + sb(6, function() return IsFalling() end)
        + sb(7, function() return IsResting() end)
        + sb(8, function() return st.autoattack end)
        + sb(9, function() return UnitExists("target") end)
        + sb(10, function() return UnitExists("pet") end)
        + sb(11, function() return IsIndoors() end)
        + sb(12, function() return IsMounted() end)
        -- an edit box holding the keyboard (IsShown is always true with the classic chat style: inst3 23:30-23:37
        -- a ghost typed "w w aw dsw" into an open Say line and never moved)
        + sb(14, function() return GetCurrentKeyBoardFocus and GetCurrentKeyBoardFocus() ~= nil end)
        + sb(15, function() return LootFrame and LootFrame:IsShown() end)
        + sb(16, function() return UnitIsAFK and UnitIsAFK("player") end)
        + sb(17, function() local s = UnitThreatSituation("player"); return s and s >= 1 end)   -- some mob has us on its threat list
        + sb(18, function() return HasFullControl and not HasFullControl() end)                -- stunned / feared / charmed
        + sb(20, function() local rg = ccfg().ranged                                              -- auto-repeat on
            return st.autorepeat or (rg and IsAutoRepeatSpell and IsAutoRepeatSpell(rg)) end)
        + sb(21, function() return HasNewMail and HasNewMail() end)                              -- unread mail
        + sb(19, function()                                                                    -- global cooldown running
            -- spell 61304 is the global cooldown itself (any class); else the class's ranged spell, which has no
            -- cooldown of its own (a running one is the global cooldown)
            local d, st
            local rg = ccfg().ranged
            if C_Spell and C_Spell.GetSpellCooldown then
                local cd = C_Spell.GetSpellCooldown(61304)
                if (type(cd) ~= "table" or not cd.duration or cd.duration == 0) and rg then cd = C_Spell.GetSpellCooldown(rg) end
                if type(cd) == "table" then d, st = cd.duration, cd.startTime end
            end
            if d == nil and GetSpellCooldown and rg then st, d = GetSpellCooldown(rg) end
            return type(d) == "number" and d > 0 and d <= 2.5 and (st + d) > GetTime()
        end)
    put(14, fl)
    local okp, pb = pcall(pet_bits)
    put(171, okp and pb or 0)
    -- the pet's health is a secret value like the player's: a bar (15), not the bits (Hunt 12:57: "mending (0% hp)"
    -- on a full-health bear, every 15 s)
    if okp and pb % 2 == 1 then pcall(setbar, 15, UnitHealth("pet"), UnitHealthMax("pet")) else pcall(setbar, 15) end
    put(30, speed * 1000)
    local okc, cast = pcall(function() return select(9, UnitCastingInfo("player")) or select(8, UnitChannelInfo("player")) or 0 end)
    if not okc or type(cast) ~= "number" then cast = 0 end
    put(26, cast)
end

local function fill_target()
    if not UnitExists("target") then
        for i = 15, 20 do put(i, 0) end
        put(28, 0); put(29, 0); setbar(2)
        return
    end
    put(16, UnitHealthMax("target"))
    setbar(2, UnitHealth("target"), UnitHealthMax("target"))
    local cls = UnitClassification("target")
    local cl = (cls == "elite" and 1) or (cls == "rareelite" and 2) or (cls == "rare" and 3)
        or (cls == "worldboss" and 4) or 0
    local lvl = UnitLevel("target") or 0
    if lvl < 0 then lvl = 255 end
    put(17, lvl + cl * 256 + (UnitReaction("target", "player") or 0) * 4096)
    put(18, npcid(UnitGUID("target")))
    local function cid(n)
        local ok, r = pcall(CheckInteractDistance, "target", n)
        return ok and r
    end
    local rg = ccfg().ranged                               -- the class's ranged attack (nil: melee - "no range")
    local lb = rg and IsSpellInRange and IsSpellInRange(rg, "target")
    local function tb(b, fn)
        local ok, r = pcall(fn)
        if ok and r then return 2 ^ b end
        return 0
    end
    local fl = tb(0, function() return UnitIsDead("target") end)
        + tb(1, function() return UnitCanAttack("player", "target") end)
        + tb(2, function() return UnitIsPlayer("target") end)
        + tb(3, function() return UnitIsTapDenied and UnitIsTapDenied("target") end)
        + tb(4, function() return UnitIsUnit("targettarget", "player") end)
        + tb(5, function() return UnitIsFriend("player", "target") end)
        + tb(6, function() return UnitAffectingCombat("target") end)
        + tb(7, function() return cid(3) end) + tb(8, function() return cid(2) end)
        + tb(9, function() return cid(4) end) + tb(10, function() return cid(1) end)
        + tb(11, function() return lb == 1 or lb == true end) + tb(12, function() return lb == nil end)
        + tb(13, function() return UnitIsUnit("target", "mouseover") end)
        + tb(14, function() return UnitIsTrivial and UnitIsTrivial("target") end)               -- gray to us
        + tb(15, function() local s = UnitThreatSituation("player", "target"); return s and s >= 1 end) -- it has us on its threat list
        + tb(16, function() return UnitCastingInfo("target") ~= nil or UnitChannelInfo("target") ~= nil end)  -- it is casting
        + tb(17, function() return UnitIsVisible("target") end)
        + tb(18, function() return UnitIsPVP and UnitIsPVP("target") end)
        + tb(19, function() return CanLootUnit and CanLootUnit(UnitGUID("target")) end)        -- a dead target with loot for us
        + tb(20, function() return CanLootUnit ~= nil end)                                      -- bit 19 is meaningful
    local rb = rxp_unit_bits("target")                     -- RXP names: 21 in the mob list, 22 in the step's .target list
    fl = fl + (rb % 2) * 2 ^ 21 + (math.floor(rb / 2) % 2) * 2 ^ 22
    put(19, fl)
    put(20, hash(UnitGUID("target")))
    local a, b = UnitPosition("target")
    put(28, a and coord(a) or 0); put(29, b and coord(b) or 0)
end

local function freebags()
    local n = 0
    for bag = 0, 4 do
        local f = (C_Container and C_Container.GetContainerNumFreeSlots or GetContainerNumFreeSlots)(bag)
        n = n + (f or 0)
    end
    return n
end

local function worn_pct()
    -- lowest durability % over the worn items (100 when nothing wears)
    local low = 100
    for slot = 1, 18 do
        local cur, mx = GetInventoryItemDurability(slot)
        if cur and mx and mx > 0 then low = math.min(low, math.floor(cur * 100 / mx)) end
    end
    return low
end

local function fill_misc()
    local nq = 0
    if C_QuestLog and C_QuestLog.GetNumQuestLogEntries then
        nq = select(2, C_QuestLog.GetNumQuestLogEntries()) or 0
    elseif GetNumQuestLogEntries then
        nq = select(2, GetNumQuestLogEntries()) or 0
    end
    put(21, freebags() + math.min(nq, 255) * 256 + (worn_pct() + 1) * 65536)      -- 0 = addon too old to report
    put(22, GetMoney())
    -- action slots 1..12: bit 2k = ready (usable, off cooldown), 2k+1 = in range or no range
    local v = 0
    for s = 1, 12 do
        if HasAction(s) then
            local usable, nomana = IsUsableAction(s)
            local start, dur = GetActionCooldown(s)
            local cd = false
            if start and dur and not (issecretvalue and (issecretvalue(start) or issecretvalue(dur))) then
                cd = start > 0 and dur > 1.6
            end
            local r = IsActionInRange(s)
            if usable and not nomana and not cd then v = v + 2 ^ (2 * (s - 1)) end
            if r ~= false then v = v + 2 ^ (2 * (s - 1) + 1) end
        end
    end
    put(23, v)
    -- auras
    local a = 0
    local aura_ok = true
    for i = 1, 40 do
        local name
        if C_UnitAuras and C_UnitAuras.GetBuffDataByIndex then
            local ok, d = pcall(C_UnitAuras.GetBuffDataByIndex, "player", i)
            if not ok then aura_ok = false; break end
            name = d and d.name
        else
            name = UnitBuff("player", i)
        end
        if not name or (issecretvalue and issecretvalue(name)) then break end
        if class_buff(name) then a = a + 1 end                                   -- the class self-buff (CLASS_CFG.buffs)
        if name == "Food" then a = a + 4 end
        if name == "Drink" then a = a + 8 end
        if name == "Resurrection Sickness" then a = a + 16 end
    end
    -- main-hand weapon enchant (an imbue / oil / poison): bit 1, seconds left in bits 8..23
    local wsecs, w_ok = 0, false
    if GetWeaponEnchantInfo then
        local ok, has, exp = pcall(GetWeaponEnchantInfo)
        if ok and not (issecretvalue and (issecretvalue(has) or issecretvalue(exp))) then
            w_ok = true
            if has then a = a + 2; wsecs = math.min(65535, math.floor((tonumber(exp) or 0) / 1000)) end
        end
    end
    if aura_ok and w_ok then st.auras_last = a + wsecs * 256 else a, wsecs = (st.auras_last or 0) % 256, math.floor((st.auras_last or 0) / 256) end
    -- camping (the Forever server's classic_spell_campfire_rest): bit 5 = the 60 s campfire rest 1289723 (sitting within
    -- 15 yd of a campfire starts it), bit 6 = Boosted Rest 1229451 / Camp Benefits 1229741 (what the rest gives).
    -- By spell id (names are localised; hidden auras may not be listed), outside the name scan's fallback
    if C_UnitAuras and C_UnitAuras.GetPlayerAuraBySpellID then
        local function has(id)
            local ok, d = pcall(C_UnitAuras.GetPlayerAuraBySpellID, id)
            return ok and d ~= nil
        end
        if a % 64 < 32 and has(1289723) then a = a + 32 end
        if a % 128 < 64 and (has(1229451) or has(1229741)) then a = a + 64 end
        -- Resurrection Sickness by id too: the name scan never set bit 4 after inst3's level-11 spirit healer revives
        -- (2026-10-15 07:32-07:58, 6 deaths, nb never sat it out)
        if a % 32 < 16 and has(15007) then a = a + 16 end
    end
    put(24, a + wsecs * 256)
    put(25, st.err * 65536 + st.errn % 65536)
    local ui = bit(0, GossipFrame and GossipFrame:IsShown()) + bit(1, QuestFrame and QuestFrame:IsShown())
        + bit(2, MerchantFrame and MerchantFrame:IsShown()) + bit(3, ClassTrainerFrame and ClassTrainerFrame:IsShown())
        + bit(4, LootFrame and LootFrame:IsShown()) + bit(5, StaticPopup1 and StaticPopup1:IsShown())
        + bit(6, TaxiFrame and TaxiFrame:IsShown()) + bit(7, MailFrame and MailFrame:IsShown())
        + bit(9, CinematicFrame and CinematicFrame:IsShown())
        + bit(10, StaticPopup_Visible and StaticPopup_Visible("DEATH"))
        + bit(11, StaticPopup_Visible and StaticPopup_Visible("RECOVER_CORPSE"))
        + bit(12, StaticPopup_Visible and StaticPopup_Visible("RESURRECT_NO_SICKNESS"))
        + bit(13, true)
    put(27, ui)
    put(31, GetTime() * 10 % 16777216)
end

-- nameplates: 12 x 4 blocks from 32. Plate positions cannot be measured
-- (restricted regions), so each plate k gets a marker anchored to it: a 6x3 px
-- pair of squares, (254,1,k) left and (1,254,k) right, found in the capture.
local markers = {}
for k = 0, 11 do
    local m = CreateFrame("Frame", nil, frame)
    m:SetFrameStrata("TOOLTIP")
    local l = m:CreateTexture(nil, "OVERLAY"); l:SetColorTexture(254 / 255, 1 / 255, (k + 1) / 255, 1)
    local r = m:CreateTexture(nil, "OVERLAY"); r:SetColorTexture(1 / 255, 254 / 255, (k + 1) / 255, 1)
    m.l, m.r = l, r
    m:Hide()
    markers[k] = m
end
local function size_markers()
    local _, ph = GetPhysicalScreenSize()
    local u = 768 / ph
    for k = 0, 11 do
        local m = markers[k]
        m:SetSize(6 * u, 3 * u)
        m.l:ClearAllPoints(); m.l:SetPoint("TOPLEFT", m, "TOPLEFT"); m.l:SetSize(3 * u, 3 * u)
        m.r:ClearAllPoints(); m.r:SetPoint("TOPRIGHT", m, "TOPRIGHT"); m.r:SetSize(3 * u, 3 * u)
    end
end

local function fill_plates()
    local all = C_NamePlate and C_NamePlate.GetNamePlates() or {}
    -- only 12 are painted: the client's order is arbitrary and the abbey's friendly NPCs took the slots, so the mob
    -- just targeted often had none (2026-10-17). Our target first, then RXP's step mobs, attackable ones, the rest
    local plates, rank = {}, {}
    for i, p in ipairs(all) do
        local u = p.namePlateUnitToken or (p.UnitFrame and p.UnitFrame.unit)
        if u and UnitExists(u) then
            local okr, r = pcall(function()
                if UnitIsUnit(u, "target") then return 0 end
                if rxp_unit_bits(u) % 2 == 1 then return 1 end
                if UnitCanAttack("player", u) and not UnitIsDead(u) then return 2 end
                return 3
            end)
            plates[#plates + 1] = p
            rank[p] = (okr and r or 3) * 1000 + i
        end
    end
    table.sort(plates, function(a, b) return rank[a] < rank[b] end)
    local k = 0
    for _, p in ipairs(plates) do
        if k >= 12 then break end
        local u = p.namePlateUnitToken or (p.UnitFrame and p.UnitFrame.unit)
        if u and UnitExists(u) then
            local m = markers[k]
            m:ClearAllPoints()
            m:SetPoint("CENTER", p, "CENTER", 0, 0)
            m:Show()
            local base = 32 + k * 4
            -- 1 = plate present; +2 in RXP's mob list, +4 in the current step's .target list, +8 in its unitscan list
            -- (eye.py overwrites sx/sy from the markers, so these bits were free: RXP_PLAN.md section 2)
            put(base, 1 + 2 * rxp_unit_bits(u))
            put(base + 1, npcid(UnitGUID(u)))
            local hp = 0
            setbar(3 + k, UnitHealth(u), UnitHealthMax(u))
            local lvl = UnitLevel(u) or 0
            if lvl < 0 then lvl = 127 end
            local cls = UnitClassification(u)
            local fl = bit(0, UnitCanAttack("player", u)) + bit(1, UnitIsDead(u))
                + bit(2, UnitIsUnit(u .. "target", "player")) + bit(3, UnitIsUnit(u, "target"))
                + bit(4, UnitIsTapDenied and UnitIsTapDenied(u)) + bit(5, UnitAffectingCombat(u))
                + bit(6, UnitIsPlayer(u)) + bit(7, cls == "elite" or cls == "rareelite" or cls == "worldboss")
                + bit(8, UnitIsFriend("player", u))
            -- game flags: trivial = gray to us (UnitIsTrivial); threat = this mob has us on its threat list
            local okt, triv = pcall(function() return UnitIsTrivial and UnitIsTrivial(u) and 1 or 0 end)
            if okt and triv == 1 then hp = 1 end
            local okh, thr = pcall(function() local s = UnitThreatSituation("player", u); return s and s >= 1 and 1 or 0 end)
            if okh and thr == 1 then fl = fl + 512 end
            put(base + 2, math.min(127, lvl) + hp * 128 + fl * 16384)
            local tok = tonumber(u:match("(%d+)$")) or 0
            -- bit 7 of the token byte: it fights our pet (tokens stay under 128). A mob on the pet is no "on me"
            -- plate, so the bot never took it (Hunt 13:08: "combat 24s, nothing pickable" while the bear fought)
            local okp, onpet = pcall(function() return UnitExists("pet") and UnitIsUnit(u .. "target", "pet") end)
            put(base + 3, math.min(tok, 127) + ((okp and onpet) and 128 or 0) + hash(UnitGUID(u), 65536) * 256)
            k = k + 1
        end
    end
    for j = k, 11 do
        for q = 0, 3 do put(32 + j * 4 + q, 0) end
        setbar(3 + j)
        markers[j]:Hide()
    end
end

-- quests: 20 x 2 blocks, 1-10 from 80, 11-20 from 146 (the log held 11-12 quests around route steps 162-188
-- and the bot saw nquests > quests read on every tick). Block 145 = QUEST20_TAG: eye.py reads 146+ only then
-- (an older addon leaves 145 black and 160+ unpainted).
local QUEST20_TAG = 0x5A0F14
local expand_t = 0
local function qbase(k) return k < 10 and 80 + k * 2 or 146 + (k - 10) * 2 end
local function fill_quests()
    local k = 0
    local CQ = C_QuestLog
    local n = GetNumQuestLogEntries and GetNumQuestLogEntries()
        or (CQ and CQ.GetNumQuestLogEntries and (CQ.GetNumQuestLogEntries())) or 0
    put(145, QUEST20_TAG)
    for i = 1, n do
        if k >= 20 then break end
        local title, isHeader, isComplete, qid
        if GetQuestLogTitle then
            local _
            title, _, _, isHeader, _, isComplete, _, qid = GetQuestLogTitle(i)
        elseif CQ and CQ.GetInfo then
            local info = CQ.GetInfo(i)
            if info then
                title, isHeader, qid = info.title, info.isHeader, info.questID
                if qid and CQ.IsComplete and CQ.IsComplete(qid) then isComplete = 1
                elseif qid and CQ.IsFailed and CQ.IsFailed(qid) then isComplete = -1 end
            end
        end
        if title and not isHeader and qid then
            local base = qbase(k)
            put(base, qid + (isComplete == 1 and 2 ^ 23 or 0) + (isComplete == -1 and 2 ^ 22 or 0))
            local objs = C_QuestLog and C_QuestLog.GetQuestObjectives and C_QuestLog.GetQuestObjectives(qid) or {}
            local v = 0
            for o = 1, 3 do
                local ob = objs[o]
                local c = ob and (ob.finished and 255 or math.min(254, ob.numFulfilled or 0)) or 0
                v = v + c * 256 ^ (o - 1)
            end
            put(base + 1, v)
            k = k + 1
        end
    end
    for j = k, 19 do local b = qbase(j); put(b, 0); put(b + 1, 0) end
    -- a collapsed zone header hides its quests from the loop above (inst2 01:38: 287 in the server's log, the strip
    -- without it -> "turn-in not in the quest log", skipped): fewer read than the log holds -> expand all (5 s apart)
    local nq = GetNumQuestLogEntries and select(2, GetNumQuestLogEntries()) or 0
    if k < 20 and nq > k and ExpandQuestHeader and GetTime() - expand_t > 5 then
        expand_t = GetTime()
        ExpandQuestHeader(0)
    end
end

-- action bar spell ids: 12 blocks from 100
local function fill_actions()
    for s = 1, 12 do
        local kind, id = GetActionInfo(s)
        local v = 0
        if kind == "spell" then v = id
        elseif kind == "item" then v = 8388608 + (id % 8388608)
        elseif kind == "macro" then v = 4194304 end
        put(99 + s, v)
    end
end

-- text: 96 chars from 112 (32 blocks), counter at 144
local function fill_text()
    local s = st.msg
    for i = 0, 31 do
        local c1, c2, c3 = s:byte(i * 3 + 1) or 0, s:byte(i * 3 + 2) or 0, s:byte(i * 3 + 3) or 0
        put(112 + i, c1 * 65536 + c2 * 256 + c3)
    end
    put(144, st.msgn + st.errs * 65536)
end

-- fill_rxp is in the slow group (gi > 4: every 0.25 s)
local groups = { fill_player, fill_target, fill_misc, fill_plates, fill_quests, fill_actions, fill_text, fill_rxp,
    buy_tick, fill_supplies, fill_ammo }
local slow = 0
frame:SetScript("OnUpdate", function(self, el)
    st.counter = (st.counter + 1) % 16777216
    put(0, 0xA55A0F)
    put(1, st.counter)
    slow = slow + el
    for gi, f in ipairs(groups) do
        if gi <= 4 or slow > 0.25 then
            local ok, e = pcall(f)
            if not ok then
                st.errs = (st.errs + 1) % 256
                if BotEyeDB.lastErr ~= e then BotEyeDB.lastErr = e; note("LUAERR " .. tostring(e)) end
            end
        end
    end
    if slow > 0.25 then slow = 0 end
    paint()
end)

-- ---------------------------------------------------------------- automation
local auto = true

-- ---- gear scoring (ported from the old bot's gear.py: stat weights per class, slot-aware, usable only) ----
-- the class's stat weights (CLASS_CFG.w); armor and weapon dps weigh the same for every class
local function gear_w()
    local w = ccfg().w
    return { STRENGTH = w.STRENGTH or 0, AGILITY = w.AGILITY or 0, INTELLECT = w.INTELLECT or 0,
             SPIRIT = w.SPIRIT or 0, STAMINA = w.STAMINA or 0, armor = 0.10, dps = 2.0, spell = w.spell or 0,
             mp5 = w.mp5 or 0 }
end
local SLOT_OF = { INVTYPE_HEAD = 1, INVTYPE_NECK = 2, INVTYPE_SHOULDER = 3, INVTYPE_CHEST = 5, INVTYPE_ROBE = 5,
    INVTYPE_WAIST = 6, INVTYPE_LEGS = 7, INVTYPE_FEET = 8, INVTYPE_WRIST = 9, INVTYPE_HAND = 10,
    INVTYPE_FINGER = 11, INVTYPE_TRINKET = 13, INVTYPE_CLOAK = 15, INVTYPE_WEAPON = 16, INVTYPE_WEAPONMAINHAND = 16,
    INVTYPE_2HWEAPON = 16, INVTYPE_SHIELD = 17, INVTYPE_HOLDABLE = 17,
    INVTYPE_RANGED = 18, INVTYPE_RANGEDRIGHT = 18, INVTYPE_THROWN = 18 }
-- weapon subclass -> the weapon skill spell that lets the character use it
local WEAPON_SKILL = { [0] = 196, [1] = 197, [2] = 264, [3] = 266, [4] = 198, [5] = 199, [6] = 200, [7] = 201,
    [8] = 202, [10] = 227, [13] = 15590, [15] = 1180, [16] = 2567, [18] = 5011, [19] = 5009 }
local function gii(link)
    local f = (C_Item and C_Item.GetItemInfo) or GetItemInfo
    return f(link)
end
local function item_stats(link)
    local f = (C_Item and C_Item.GetItemStats) or GetItemStats
    local ok, r = pcall(f, link)
    return ok and type(r) == "table" and r or {}
end
local function gear_score(link)
    if not link then return 0 end
    local s, n = item_stats(link), 0
    local GEAR_W = gear_w()
    local function g(k) local v = s[k]; return type(v) == "number" and v or 0 end
    for k, w in pairs({ STRENGTH = GEAR_W.STRENGTH, AGILITY = GEAR_W.AGILITY, INTELLECT = GEAR_W.INTELLECT,
                        SPIRIT = GEAR_W.SPIRIT, STAMINA = GEAR_W.STAMINA }) do n = n + w * g("ITEM_MOD_" .. k .. "_SHORT") end
    n = n + GEAR_W.armor * g("RESISTANCE0_NAME") + GEAR_W.dps * g("ITEM_MOD_DAMAGE_PER_SECOND_SHORT")
    n = n + GEAR_W.spell * (g("ITEM_MOD_SPELL_POWER_SHORT") + g("ITEM_MOD_HEALING_POWER_SHORT"))
    n = n + GEAR_W.mp5 * g("ITEM_MOD_MANA_REGENERATION_SHORT")
    return n + 0.01
end
-- (slot, usable) for a link; slot is the first slot it would take
local function gear_slot(link)
    if not link then return nil end
    local ok, name, _, _, _, minlvl, _, _, _, loc, _, _, cid, scid = pcall(gii, link)
    if not ok or not name or not loc or loc == "" then return nil end
    local slot = SLOT_OF[loc]
    if not slot then return nil end
    if (minlvl or 0) > (UnitLevel("player") or 1) then return nil end
    local cf = ccfg()
    if cid == 4 and scid == 6 and not cf.shield then return nil end                -- a shield
    if cid == 4 and scid >= 1 and scid <= 4 and scid > cf.armor[(UnitLevel("player") or 1) >= 40 and 2 or 1] then
        return nil                                                                 -- heavier armor than the class wears
    end
    if cid == 2 then                                                               -- a weapon we cannot use
        -- the character's own weapon skills (a human hunter starts with daggers and bows only: 2026-10-18 a mailed
        -- two-handed axe passed the class list and was refused by the client); the class list without the API
        local skill = WEAPON_SKILL[scid]
        local f = IsPlayerSpell or IsSpellKnown
        local ok2
        if skill and f then
            local okk, r = pcall(f, skill)
            ok2 = okk and r and not (issecretvalue and issecretvalue(r))
        else
            ok2 = false
            for _, w in ipairs(cf.weapons) do if w == scid then ok2 = true end end
        end
        if not ok2 then return nil end
    end
    -- (IsUsableItem is about use-effects, not proficiency: it says false for a staff that equips fine)
    return slot, loc
end
local function worn_link(slot) return GetInventoryItemLink("player", slot) end
-- score an item would add over what it replaces
local function gear_gain(link)
    local slot, loc = gear_slot(link)
    if not slot then return nil end
    local new = gear_score(link)
    local old
    if loc == "INVTYPE_2HWEAPON" then
        old = gear_score(worn_link(16)) + gear_score(worn_link(17))
    elseif slot == 17 then
        local mh = worn_link(16)
        if mh then
            local mhloc = select(9, gii(mh))
            if mhloc == "INVTYPE_2HWEAPON" then return nil end      -- would unequip a two-hander
        end
        old = gear_score(worn_link(17))
    elseif slot == 11 or slot == 13 then
        old = math.min(gear_score(worn_link(slot)), gear_score(worn_link(slot + 1)))
    else
        old = gear_score(worn_link(slot))
    end
    return new - old, new, old, slot
end

local function best_reward()
    local n = GetNumQuestChoices and GetNumQuestChoices() or 0
    if n <= 1 then return 1 end
    local best, bg = nil, 0.05
    local sell, bsell = 1, -1
    for i = 1, n do
        local link = GetQuestItemLink and GetQuestItemLink("choice", i)
        local okg, gain = pcall(gear_gain, link)
        if okg and gain and gain > bg then best, bg = i, gain end
        local ok, price = false, 0
        if link then ok, price = pcall(function() return select(11, gii(link)) end) end
        if not ok or type(price) ~= "number" then price = 0 end
        if price > bsell then sell, bsell = i, price end
    end
    note("I:reward " .. (best and ("gear " .. best .. " +" .. string.format("%.1f", bg)) or ("sell " .. sell)) .. " of " .. n)
    return best or sell                       -- nothing wearable beats what we have: take the money
end

-- equip any bag item that beats what is worn (a quest reward, a drop); out of combat only
local gear_next = 0
local gear_tried = {}                      -- link -> do not try again before this time (equip refused)
local function equip_upgrades()
    if InCombatLockdown() or GetTime() < gear_next then return end
    gear_next = GetTime() + 4
    local get = C_Container and C_Container.GetContainerItemInfo
    local use = (C_Container and C_Container.UseContainerItem) or UseContainerItem
    local nslots = (C_Container and C_Container.GetContainerNumSlots) or GetContainerNumSlots
    local best, bg, bb, bs = nil, 0.3, nil, nil
    for bag = 0, 4 do
        for slot = 1, (nslots(bag) or 0) do
            local link
            if get then local info = get(bag, slot); link = info and info.hyperlink
            else link = select(7, GetContainerItemInfo(bag, slot)) end
            if link then
                local ok, gain, new, old = pcall(gear_gain, link)
                if ok and gain and gain > bg and (gear_tried[link] or 0) < GetTime() then best, bg, bb, bs = link, gain, bag, slot end
            end
        end
    end
    if best then
        note("I:equip " .. tostring(select(1, gii(best))):sub(1, 40) .. " +" .. string.format("%.1f", bg))
        gear_tried[best] = GetTime() + 600
        pcall(use, bb, bs)
        gear_next = GetTime() + 1.5            -- look again soon: more than one slot may improve
    end
end

-- bags: a spare container in the bags replaces the smallest equipped bag that has fewer slots (the client moves its
-- items over); spares that are no upgrade are sold (bag_spare). inst4 2026-10-15 17:11: four 6-slot pouches equipped,
-- six 10-slot Blue Leather Bags and five pouches loose in them, 0 slots free, "sell 0 kept 40"
local function bag_slots_of(link)
    local n = C_Item and C_Item.GetItemNumSlots and C_Item.GetItemNumSlots(link)
    return tonumber(n) or 0
end
local function is_bag(link)
    if not (link and GetItemInfoInstant) then return false end
    local ok, _, _, _, _, _, classID, subID = pcall(GetItemInfoInstant, link)
    return ok and classID == 1 and subID == 0
end
local function equipped_bags()
    local C = C_Container
    local out = {}
    for b = 1, 4 do out[b] = ((C and C.GetContainerNumSlots or GetContainerNumSlots)(b) or 0) end
    return out
end
local function bag_upgrade()
    if InCombatLockdown() or UnitAffectingCombat("player") or CursorHasItem() then return end
    if GetTime() - (st.bag_try_t or -1e9) < 20 then return end
    local C = C_Container
    local get = C and C.GetContainerItemInfo
    if not (get and C.PickupContainerItem and PutItemInBag and C.ContainerIDToInventoryID) then return end
    local eq = equipped_bags()
    local small, sn = nil, 999
    for b = 1, 4 do if eq[b] < sn then small, sn = b, eq[b] end end
    if not small then return end
    local best, bb, bs, bn = nil, nil, nil, sn
    for bag = 0, 4 do
        for slot = 1, (C.GetContainerNumSlots(bag) or 0) do
            local info = get(bag, slot)
            local link = info and info.hyperlink
            if link and not info.isLocked and is_bag(link) and bag ~= small then
                local n = bag_slots_of(link)
                if n > bn then best, bb, bs, bn = link, bag, slot, n end
            end
        end
    end
    if not best then return end
    st.bag_try_t = GetTime()
    C.PickupContainerItem(bb, bs)
    if CursorHasItem() then
        PutItemInBag(C.ContainerIDToInventoryID(small))
    end
    if CursorHasItem() then ClearCursor() end
    note("I:bag swap " .. (best:match("%[(.-)%]") or "?") .. " " .. bn .. " for bag " .. small .. " (" .. sn .. ")")
end
-- a loose bag that is no upgrade over every equipped bag (sold at the vendor)
local function bag_spare(link)
    if not is_bag(link) then return false end
    local n = bag_slots_of(link)
    for _, m in pairs(equipped_bags()) do if n > m then return false end end
    return true
end

-- item names an objective of a quest in the log asks for ("Boar Intestines: 1/3"): never sold. The white trade
-- goods rule sold Thelsamar Blood Sausages' Bear Meat / Boar Intestines / Spider Ichor on every vendor trip
local function quest_item_names()
    local out = {}
    local ql = C_QuestLog
    if not (ql and ql.GetNumQuestLogEntries and ql.GetInfo and ql.GetQuestObjectives) then return out end
    for i = 1, ql.GetNumQuestLogEntries() do
        local info = ql.GetInfo(i)
        if info and not info.isHeader and info.questID then
            for _, o in ipairs(ql.GetQuestObjectives(info.questID) or {}) do
                if o.type == "item" and o.text then
                    local name = o.text:match("^%s*%d+/%d+%s+(.-)%s*$") or o.text:match("^(.-):%s*%d+/%d+")
                    if name and name ~= "" then out[name] = true end
                end
            end
        end
    end
    return out
end

local function sell_junk()
    local get = C_Container and C_Container.GetContainerItemInfo
    local sold, kept = 0, 0
    local okw, wanted = pcall(quest_item_names)       -- a quest-log error must not stop the sale
    if not okw or type(wanted) ~= "table" then wanted = {} end
    pcall(sup_scan)                                    -- the best drink / food (kept) and the stocked tier counts
    if rxp_on() then
        -- what an open RestedXP .collect wants is the guide's too (Adlin's water, a quest's 10 Linen)
        local okc, w = pcall(collect_wants, RXPA())
        for _, x in ipairs(okc and w or {}) do
            for id in pairs(x.ids or {}) do
                local okn, nm = pcall(gii_any, id)
                if okn and nm then wanted[nm] = true end
            end
        end
    end
    for bag = 0, 4 do
        local ns = (C_Container and C_Container.GetContainerNumSlots or GetContainerNumSlots)(bag)
        for slot = 1, ns do
            local q, noValue, link, locked
            if get then
                local info = get(bag, slot)
                if info then q, noValue, link, locked = info.quality, info.hasNoValue, info.hyperlink, info.isLocked end
            else
                local _, _, lk, quality, _, _, l, _, nv = GetContainerItemInfo(bag, slot)
                q, noValue, link, locked = quality, nv, l, lk
            end
            -- a locked item is in flight (a sale RXP just made, RXP_PLAN.md section 4 MERCHANT_SHOW): leave it
            -- also white (q 1) weapons / armour in the bags: worn gear is not in the bags, and a white drop never
            -- replaces it (RXP / BotEye equip upgrades from rewards). inst1 2026-10-15 00:40-00:55: bags full of
            -- Frostmane drops, each vendor trip freed 1 slot (gray only)
            local gear = false
            if link and q and q >= 1 and q <= 2 and not noValue and not locked and GetItemInfoInstant then
                local okc, id, _, _, _, _, classID, subID = pcall(GetItemInfoInstant, link)
                -- white trade goods (class 7: cloth, meat, eggs) too: the bot crafts nothing. inst3 2026-10-15
                -- 06:38:58: a vendor trip at 0 free sold nothing, then loot and the 1338 accept failed err 6
                -- build 18, green too, and what else filled 40-slot bags at every trip (2026-10-15 21:5x, "sell 0
                -- kept 39"): gear in the bags (an upgrade is worn the moment it drops: what stays is not one, or a
                -- class we cannot wear), recipes and gems (no crafting), misc reagents / junk, scrolls, and food or
                -- drink other than the best one of each (the stocked tier; 8 kinds of looted food took 8 slots)
                if okc then
                    if classID == 2 or classID == 4 or classID == 7 or classID == 9 or classID == 15 then
                        gear = true
                    elseif classID == 0 and subID ~= 5 and subID ~= 8 then
                        gear = true                    -- potions, elixirs, scrolls, bandages: the bot uses none
                    elseif classID == 0 and subID == 5 and id and id ~= sup.drink and id ~= sup.food
                            and (sup.cur.drink or 0) + (sup.cur.food or 0) > 0 then
                        gear = true                    -- spare food / drink while a stocked tier is in the bags
                    end
                end
            end
            if link and not gear and not noValue and not locked and q and q <= 1 and bag_spare(link) then
                gear = true                            -- a spare bag that is no upgrade (bag_upgrade)
            end
            local want = link and wanted[link:match("%[(.-)%]") or ""]     -- a quest in the log needs it
            if gear and link and q and q >= 1 then
                -- never sell an upgrade the gear check has not put on yet, or gear whose item info is not loaded (a
                -- mailed set, 2026-10-18 09:29: the vendor came 5 s after the mailbox and sold all of it unworn)
                local okc2, id2, _, _, _, _, cls2 = pcall(GetItemInfoInstant, link)
                if okc2 and (cls2 == 2 or cls2 == 4) then
                    local okn = pcall(gii, link) and gii(link) ~= nil
                    local okg, gain = pcall(gear_gain, link)
                    if not okn or (okg and gain and gain > 0) then gear = false; want = true end
                end
            end
            if link and (q == 0 or gear) and not noValue and not locked and not want then
                (C_Container and C_Container.UseContainerItem or UseContainerItem)(bag, slot)
                sold = sold + 1
            elseif link then
                kept = kept + 1
            end
        end
    end
    note("I:sell " .. sold .. " kept " .. kept)
end

-- a bag for an empty bag slot (bags 1-4): the bots carried the 16-slot backpack only, full of quest items and food
-- (inst1 2026-10-15 00:40-00:59: 4 vendor trips, 1 slot free after each). The merchant's biggest affordable container
-- (class 1 subclass 0), keeping 2 s per level for training. EquipItemByName puts a bag into the first free bag slot
local function buy_bag()
    local C = C_Container
    local free_slot = false
    for b = 1, 4 do
        if ((C and C.GetContainerNumSlots or GetContainerNumSlots)(b) or 0) == 0 then free_slot = true end
    end
    if not free_slot or not GetMerchantNumItems then return end
    -- 50 c per level kept for training (2 s per level kept inst1, 18 s at level 9, from every bag: 01:21:49 at Kreg)
    local reserve = 50 * (UnitLevel("player") or 1)
    local best, bslots, bprice
    for i = 1, GetMerchantNumItems() do
        local link = GetMerchantItemLink(i)
        local _, _, price, _, avail = GetMerchantItemInfo(i)
        if link and price and (avail == -1 or (avail or 0) > 0) and GetMoney() - price >= reserve then
            local _, _, _, _, _, classID, subID = GetItemInfoInstant(link)
            if classID == 1 and subID == 0 then
                local slots = (C_Item and C_Item.GetItemNumSlots and C_Item.GetItemNumSlots(link))
                    or (GetItemInfoInstant and 0) or 0
                slots = tonumber(slots) or 0
                if not best or slots > bslots or (slots == bslots and price < bprice) then
                    best, bslots, bprice = i, slots, price
                end
            end
        end
    end
    if not best then return end
    local link = GetMerchantItemLink(best)
    BuyMerchantItem(best, 1)
    note("I:bag bought " .. tostring(link):match("%[(.-)%]") .. " " .. bprice .. "c")
    C_Timer.After(1.0, function()
        local id = GetItemInfoInstant(link)
        local eq = (C_Item and C_Item.EquipItemByName) or EquipItemByName
        if id and eq then pcall(eq, id) end
    end)
end

-- mail: one take per call (money first, then the items); a COD mail is left alone
local function take_mail()
    if not (GetInboxNumItems and GetInboxHeaderInfo) then return end
    if not (MailFrame and MailFrame:IsShown()) then return end
    for i = (GetInboxNumItems() or 0), 1, -1 do
        local _, _, sender, subject, money, cod, _, has_item, was_read = GetInboxHeaderInfo(i)
        -- reading a mail marks it read: an emptied mail left unread keeps HasNewMail on and the bot walking back
        -- (Hunt 09:51: 13 empty gear mails, unread 1 after every visit)
        if not was_read and GetInboxText then pcall(GetInboxText, i) end
        if (cod or 0) == 0 then
            if (money or 0) > 0 and TakeInboxMoney then
                TakeInboxMoney(i); note("I:mail money " .. money); return
            end
            if has_item and AutoLootMailItem then
                AutoLootMailItem(i); note("I:mail items " .. tostring(subject):sub(1, 30)); return
            end
        end
    end
end

local ev = CreateFrame("Frame")
for _, e in ipairs({ "PLAYER_LOGIN", "PLAYER_ENTERING_WORLD", "UI_ERROR_MESSAGE", "UI_INFO_MESSAGE",
    "CHAT_MSG_SYSTEM", "PLAYER_ENTER_COMBAT", "PLAYER_LEAVE_COMBAT", "QUEST_DETAIL", "QUEST_PROGRESS",
    "QUEST_COMPLETE", "GOSSIP_SHOW", "QUEST_GREETING", "MERCHANT_SHOW", "TRAINER_SHOW",
    "DISPLAY_SIZE_CHANGED", "UI_SCALE_CHANGED", "QUEST_ACCEPT_CONFIRM", "CONFIRM_BINDER",
    "CHAT_MSG_COMBAT_XP_GAIN", "PLAYER_LEVEL_UP", "QUEST_TURNED_IN", "BAG_UPDATE_DELAYED",
    "EQUIP_BIND_CONFIRM", "AUTOEQUIP_BIND_CONFIRM", "CONFIRM_XP_LOSS", "PLAYER_INTERACTION_MANAGER_FRAME_SHOW",
    "HEARTHSTONE_BOUND", "TAXIMAP_OPENED", "START_AUTOREPEAT_SPELL", "STOP_AUTOREPEAT_SPELL",
    "MAIL_SHOW", "MAIL_INBOX_UPDATE" }) do
    pcall(ev.RegisterEvent, ev, e)
end

-- Enum.PlayerInteractionType.SpiritHealer (RestedXP's deathskip, functions.lua:2915-2920, uses the same)
local SPIRIT_IT = 18
if type(Enum) == "table" and type(Enum.PlayerInteractionType) == "table"
    and type(Enum.PlayerInteractionType.SpiritHealer) == "number" then SPIRIT_IT = Enum.PlayerInteractionType.SpiritHealer end

-- .home (hearthstone). RXP's .home element (functions.lua:2648-2696) picks the binder gossip on GOSSIP_SHOW and confirms
-- on CONFIRM_BINDER, but its frame registers HEARTHSTONE_BOUND, CONFIRM_BINDER, GOSSIP_SHOW in that order with no
-- pcall (GuideWindow.lua:287-299): an event this 12.x-based client does not know stops the loop before GOSSIP_SHOW and
-- before the OnEvent script is set, so it never picks (2026-10-14 21:35 Innkeeper Belm: gossip open, 4 talks, no
-- popup). The human's bind (human/20261014_1959_inst4 t=1792002933.8-936.4): gossip, then StaticPopup1 0.9-1.4 s later
-- (the Binder interaction), then "Thunderbrew Distillery is now your home.". BotEye does both halves the way RXP does.
local BINDER_IT = 20                  -- Enum.PlayerInteractionType.Binder
if type(Enum) == "table" and type(Enum.PlayerInteractionType) == "table"
    and type(Enum.PlayerInteractionType.Binder) == "number" then BINDER_IT = Enum.PlayerInteractionType.Binder end
local HOME_UP = string.upper(type(HOME) == "string" and HOME or "home")

-- the binder option: RXP's SelectGossipType("binder") test (option.type, or the binder icon + HOME in the text), then
-- the text alone ("Make this inn my home.", tcf_world gossip_menu_option 1297/1, OptionNpc 5) in case the icon differs
local function binder_opt()
    local opts = C_GossipInfo.GetOptions() or {}
    for _, o in ipairs(opts) do
        local up = string.upper(o.name or "")
        if o.type == "binder" or ((o.icon == 132052 or o.icon == 136458) and up:find(HOME_UP, 1, true)) then return o end
    end
    for _, o in ipairs(opts) do
        if string.upper(o.name or ""):find(HOME_UP, 1, true) then return o end
    end
    -- this client words it "Bind your hearthstone to this inn." (no HOME): every bot skipped every .home step, homebinds
    -- all still in Coldridge (tcf_characters character_homebind, 2026-10-15 06:25)
    for _, o in ipairs(opts) do
        if string.upper(o.name or ""):find("HEARTHSTONE", 1, true) then return o end
    end
end

-- the current step still has an unfinished .home element
local function rxp_home_due()
    return rxp_on() and kbit(rxp_step_kinds(), 12)
end

-- the bind went through: RXP's HEARTHSTONE_BOUND branch (functions.lua:2678-2680) completes the step's .home elements
local function bind_done()
    st.bound_t, st.bind_t = GetTime(), nil
    note("I:hearth bound")
    local R = RXPA(); if not (R and R.SetElementComplete and rxp_on()) then return end
    local ok, step = pcall(rxp_cur_step, R)
    if not ok or not step then return end
    for _, e in ipairs(step.elements or {}) do
        if e.tag == "home" and not e.completed then e.confirm = false; pcall(R.SetElementComplete, e) end
    end
end

-- the Binder confirmation (StaticPopup CONFIRM_BINDER): RXP's own accept (functions.lua:2681-2692), 0.3 s later and
-- only when no bind landed since (RXP, where its handler runs, confirms on the next frame)
local function bind_confirm()
    local t0 = GetTime()
    C_Timer.After(0.3, function()
        if (st.bound_t or -1) >= t0 then return end
        local pim = C_PlayerInteractionManager
        if pim and pim.ConfirmationInteraction then
            pcall(pim.ConfirmationInteraction, BINDER_IT); pcall(pim.ClearInteraction, BINDER_IT)
        elseif ConfirmBinder then pcall(ConfirmBinder) end
        pcall(StaticPopup_Hide, "CONFIRM_BINDER")
        st.bind_t = GetTime()
        note("I:bind confirm")
    end)
end
-- class training is due at every even level (2, 4, 6, 8, 10) until bought once
local function train_due()
    local lv = UnitLevel("player") or 1
    if (BotEyeDB.trained or 0) > lv then BotEyeDB.trained = 0 end      -- a new, lower-level character
    local g = UnitGUID("player")
    if g and BotEyeDB.char ~= g then BotEyeDB.char = g; BotEyeDB.trained = 0 end   -- a different character
    return lv >= 2 and math.floor(lv / 2) * 2 > (BotEyeDB.trained or 0)
end

-- /be fly <name>: take the flight to the node whose name holds <name> when the flight map opens (nb/flight.py picks
-- the node; the bot walks to the flight master and interacts). Asked > 120 s ago: forgotten
local function fly_nodes()
    local out = {}
    if NumTaxiNodes and TaxiNodeName then
        for i = 1, NumTaxiNodes() do
            out[#out + 1] = { i = i, name = TaxiNodeName(i) or "", ok = (TaxiNodeGetType and TaxiNodeGetType(i)) == "REACHABLE" }
        end
    elseif C_TaxiMap and C_TaxiMap.GetAllTaxiNodes and GetTaxiMapID then
        for _, n in ipairs(C_TaxiMap.GetAllTaxiNodes(GetTaxiMapID()) or {}) do
            local reach = Enum and Enum.FlightPathState and n.state == Enum.FlightPathState.Reachable
            out[#out + 1] = { i = n.slotIndex, name = n.name or "", ok = reach or n.state == 1 }
        end
    end
    return out
end
local function take_fly()
    local f = st.fly
    if not f or GetTime() - f.t > 120 then return end
    local names = {}
    for _, n in ipairs(fly_nodes()) do
        if n.name:lower():find(f.name, 1, true) then
            if n.ok then
                st.fly = nil
                TakeTaxiNode(n.i)
                note("I:fly " .. n.name)
                return
            end
            names[#names + 1] = n.name .. " (not reachable)"
        else
            names[#names + 1] = n.name
        end
    end
    st.fly = nil
    note(("E:fly no node '" .. f.name .. "' in [" .. table.concat(names, "|") .. "]"):sub(1, 120))
end

local function gossip_opt(icon, word)
    for _, o in ipairs(C_GossipInfo.GetOptions() or {}) do
        if o.icon == icon or (o.name and o.name:lower():find(word)) then return o end
    end
end

-- RXP mode (RXP_PLAN.md section 4): RXP takes quest offers and hand-ins on GOSSIP_SHOW (RXPGuides.lua:1100-1161) and
-- runs its own gossip steps (.skipgossip / .gossipoption, .home, .fp, .fly). BotEye only opens the window the current
-- step needs (trainer, vendor) or the bags-full / worn vendor; 0.3 s later, so RXP's pick wins, and only while the SAME
-- gossip is still open (st.gsn: a new GOSSIP_SHOW page cancels the pending pick).
-- the quest id of a pending /be rxp accept, nil when none or older than 300 s (the walk to the giver comes first)
local function rxp_acc_id()
    local a = st.rxp_acc
    if a and GetTime() - a.t < 300 then return a.id end
end

local function gossip_rxp()
    st.gsn = (st.gsn or 0) + 1
    local n = st.gsn
    C_Timer.After(0.3, function()
        if n ~= st.gsn or not (GossipFrame and GossipFrame:IsShown()) or UnitIsGhost("player") then return end
        local ok, err = pcall(function()
            local id = rxp_acc_id()                                         -- /be rxp accept: that offer first
            if id and C_GossipInfo.GetAvailableQuests then
                for _, q in ipairs(C_GossipInfo.GetAvailableQuests() or {}) do
                    if q.questID == id then C_GossipInfo.SelectAvailableQuest(id); note("I:rxp accept pick " .. id); return end
                end
            end
            local K = rxp_step_kinds()
            local o, why
            -- .home before the .skipgossip return: RXP's pick never runs (BINDER_IT), and a .home step that also carries
            -- a gossip element left Innkeeper Belm's window untouched (inst3 2026-10-15 00:16, 4 talks, no bind note)
            if kbit(K, 12) and GetTime() - (st.bound_t or -1e9) > 60 then
                o, why = binder_opt(), "bind"
                if o then
                    st.bind_t = GetTime()
                    local okv, cb = pcall(function() return C_EventUtils.IsEventValid("CONFIRM_BINDER") end)
                    why = "bind (icon " .. tostring(o.icon) .. ", CONFIRM_BINDER " .. tostring(okv and cb) .. ")"
                else
                    local names = {}
                    for _, g in ipairs(C_GossipInfo.GetOptions() or {}) do names[#names + 1] = tostring(g.name) end
                    note("I:bind: no binder option in " .. #names .. " [" .. table.concat(names, "|"):sub(1, 60) .. "]")
                end
            end
            if not o and kbit(K, 20) then return end                        -- .skipgossip / .gossipoption: RXP's
            if o then                                                        -- the bind first, as in the step
            elseif kbit(K, 9) or kbit(K, 10) or st.want == "train" then      -- .train / .trainer (or the bot asked)
                o, why = gossip_opt(132058, "train"), "train"
            elseif kbit(K, 11) or kbit(K, 18) or st.want == "vendor" then    -- .vendor / .buy
                o, why = gossip_opt(132060, "browse"), "vendor"
            elseif st.want == "taxi" or st.fly then                          -- nb/flight.py: the flight map
                o, why = gossip_opt(132057, "fly"), "taxi"
            elseif kbit(K, 12) or kbit(K, 13) or kbit(K, 14) then return     -- .home / .fp / .fly: RXP picks
            elseif freebags() <= 3 or worn_pct() <= 40 then
                o, why = gossip_opt(132060, "browse"), "bags"
            end
            if o then C_GossipInfo.SelectOption(o.gossipOptionID); note("I:gossip " .. why) end
        end)
        if not ok then note("E:gossip " .. tostring(err)) end
    end)
end

-- the quests this NPC's window offers, for the bot (polestar's unavailable-quest rule: a giver whose window opens
-- without the step's quest cannot give it now - level, a missing prerequisite or a dead breadcrumb). The note comes
-- before any pick (gossip_rxp waits 0.3 s), so the bot's next state reads see it
local function note_offers()
    local ok = pcall(function()
        local ids = {}
        if C_GossipInfo and C_GossipInfo.GetAvailableQuests and GossipFrame and GossipFrame:IsShown() then
            for _, q in ipairs(C_GossipInfo.GetAvailableQuests() or {}) do ids[#ids + 1] = tostring(q.questID) end
        elseif GetNumAvailableQuests and GetAvailableQuestInfo then
            for i = 1, GetNumAvailableQuests() do
                local id = select(5, GetAvailableQuestInfo(i))
                if id then ids[#ids + 1] = tostring(id) end
            end
        end
        note("I:offers " .. table.concat(ids, ","))
    end)
    if not ok then note("I:offers ?") end
end

local function gossip()
    note_offers()
    if not auto then return end
    if UnitIsGhost("player") then            -- Spirit Healer: "Return me to life"
        local o = C_GossipInfo and C_GossipInfo.GetOptions and C_GossipInfo.GetOptions() or {}
        if o[1] then C_GossipInfo.SelectOption(o[1].gossipOptionID) end
        return
    end
    if rxp_owns() and C_GossipInfo and C_GossipInfo.GetOptions then gossip_rxp(); return end
    if C_GossipInfo and C_GossipInfo.GetActiveQuests then
        -- finished quests first. A quest we picked here before and that is STILL in the log when the gossip
        -- comes back (e.g. its unique reward is already in the bags) is left alone for 10 min, so it cannot
        -- swallow the trainer option every time. (Training first made Teo skip Archaic Rune at level 4.)
        st.badq = st.badq or {}
        local now = GetTime()
        for _, q in ipairs(C_GossipInfo.GetActiveQuests() or {}) do
            if q.isComplete and (st.badq[q.questID] or 0) < now then
                if st.lastq == q.questID and now - (st.lastq_t or 0) < 20 then
                    st.badq[q.questID] = now + 600
                else
                    st.lastq, st.lastq_t = q.questID, now
                    C_GossipInfo.SelectActiveQuest(q.questID); return
                end
            end
        end
        local function train_opt()
            for _, o in ipairs(C_GossipInfo.GetOptions() or {}) do
                if o.icon == 132058 or (o.name and o.name:lower():find("train")) then return o end
            end
        end
        -- the bot asked to train (/be want train): the trainer window first
        if st.want == "train" and train_due() then
            local o = train_opt()
            if o then C_GossipInfo.SelectOption(o.gossipOptionID); return end
        end
        -- quest offers before training: training first hid Teo's Call of Earth from an untrained character (the
        -- gossip went to the trainer window every visit, 2026-10-14 inst4 15:52:39 "Teo still owes a quest").
        -- An offer picked here <20 s ago that is still offered (cannot be taken) is left alone for 10 min
        st.badav = st.badav or {}
        for _, q in ipairs(C_GossipInfo.GetAvailableQuests() or {}) do
            if (st.badav[q.questID] or 0) < now then
                if st.lastav == q.questID and now - (st.lastav_t or 0) < 20 then
                    st.badav[q.questID] = now + 600
                else
                    st.lastav, st.lastav_t = q.questID, now
                    C_GossipInfo.SelectAvailableQuest(q.questID); return
                end
            end
        end
        -- no quest to take: train when due (the bot's train_step presses interact again after a quest accept)
        if train_due() then
            local o = train_opt()
            if o then C_GossipInfo.SelectOption(o.gossipOptionID); return end
        end
        local opts = C_GossipInfo.GetOptions() or {}
        for _, o in ipairs(opts) do
            local ic = o.icon
            if (freebags() <= 3 or worn_pct() <= 40) and (ic == 132060 or (o.name and o.name:lower():find("browse"))) then
                C_GossipInfo.SelectOption(o.gossipOptionID); return
            end
            if train_due() and (ic == 132058 or (o.name and o.name:lower():find("train"))) then
                C_GossipInfo.SelectOption(o.gossipOptionID); return
            end
            if st.want and o.name and o.name:lower():find(st.want) then C_GossipInfo.SelectOption(o.gossipOptionID); return end
            if (st.want == "vendor" and ic == 132060) or (st.want == "train" and ic == 132058)
                or (st.want == "bind" and ic == 132052) or (st.want == "taxi" and ic == 132057) then
                C_GossipInfo.SelectOption(o.gossipOptionID); return
            end
        end
    end
end

ev:SetScript("OnEvent", function(self, e, ...)
    if e == "PLAYER_LOGIN" or e == "PLAYER_ENTERING_WORLD" or e == "DISPLAY_SIZE_CHANGED" or e == "UI_SCALE_CHANGED" then
        layout(); size_markers()
        if e == "PLAYER_LOGIN" then BotEyeSetup() end
        if e == "PLAYER_ENTERING_WORLD" and (...) then
            -- fresh login: read no pad until the nb bot claims its own device (else every bot's pad steers us and
            -- the clients fight over the mouse). 999 is refused by SetCVar; GamePadEnable 0 is the off switch.
            pcall(SetCVar, "GamePadEnable", 0)
        end
        -- RestedXP: settings + our route, only with /be rxp on (rxp_autoload returns at once when the flag is off)
        if e == "PLAYER_ENTERING_WORLD" then C_Timer.After(5, function() pcall(rxp_autoload, 0) end) end
    elseif e == "UI_ERROR_MESSAGE" then
        local a, b = ...
        local msg = type(a) == "string" and a or b
        st.err = errcode(msg); st.errn = st.errn + 1
        note("E:" .. tostring(msg))
    elseif e == "UI_INFO_MESSAGE" then
        local a, b = ...
        note("I:" .. tostring(type(a) == "string" and a or b))
    elseif e == "CHAT_MSG_SYSTEM" or e == "CHAT_MSG_COMBAT_XP_GAIN" then
        note("S:" .. tostring((...)))
        -- "Thunderbrew Distillery is now your home." (ERR_DEATHBIND_SUCCESS_S) after our bind pick / confirm
        if e == "CHAT_MSG_SYSTEM" and st.bind_t and GetTime() - st.bind_t < 15 and type((...)) == "string" then
            local fmt = type(ERR_DEATHBIND_SUCCESS_S) == "string" and ERR_DEATHBIND_SUCCESS_S or "%s is now your home."
            local tail = fmt:gsub("^%%s", "")
            if tail ~= "" and (...):find(tail, 1, true) then bind_done() end
        end
    elseif e == "HEARTHSTONE_BOUND" then
        if st.bind_t or rxp_home_due() then bind_done() end
    elseif e == "PLAYER_LEVEL_UP" then
        note("S:LEVEL " .. tostring((...)))
    elseif e == "START_AUTOREPEAT_SPELL" then st.autorepeat = true
    elseif e == "STOP_AUTOREPEAT_SPELL" then st.autorepeat = false
    elseif e == "PLAYER_ENTER_COMBAT" then st.autoattack = true
    elseif e == "PLAYER_LEAVE_COMBAT" then st.autoattack = false
    elseif not auto then return
    elseif e == "CONFIRM_BINDER" or (e == "PLAYER_INTERACTION_MANAGER_FRAME_SHOW" and (...) == BINDER_IT) then
        -- the Binder confirmation: ours when we picked the binder option, the step has a .home or the bot asked
        if st.want == "bind" or (st.bind_t and GetTime() - st.bind_t < 10) or rxp_home_due() then bind_confirm() end
    elseif e == "CONFIRM_XP_LOSS" or (e == "PLAYER_INTERACTION_MANAGER_FRAME_SHOW" and (...) == SPIRIT_IT) then
        -- "Return me to life": accept through the interaction manager like RestedXP's deathskip on this client
        -- (functions.lua:2915-2922); the popup button can be missing or delay-disabled (2026-10-14 18:03-18:08: 4 healer
        -- visits, no revive). An active RXP .deathskip step accepts this same event on its next frame: then wait 0.6 s
        -- and accept only if we are still a ghost (one accept = one durability loss).
        if UnitIsGhost("player") then
            st.healer_t = GetTime()
            local d = (rxp_on() and rxp_deathskip_active()) and 0.6 or 0
            C_Timer.After(d, function()
                if not UnitIsGhost("player") then return end
                local pim = C_PlayerInteractionManager
                if pim and pim.ConfirmationInteraction then
                    pcall(pim.ConfirmationInteraction, SPIRIT_IT); pcall(pim.ClearInteraction, SPIRIT_IT)
                elseif AcceptXPLoss then pcall(AcceptXPLoss) end
                st.healer_t = GetTime()
                note("I:healer accept")
            end)
        end
    -- quest windows: RXP owns them while it automates (RXPGuides.lua:1164-1219, rxp_owns). It accepts / completes only
    -- the current step's quests and leaves the others open: the bot closes those (RXP_PLAN.md section 3 TALK).
    elseif e == "QUEST_DETAIL" then
        if rxp_owns() then
            local id = rxp_acc_id()                      -- /be rxp accept: RXP leaves a skipped step's quest alone
            if id and GetQuestID and GetQuestID() == id then
                st.rxp_acc = nil
                if QuestGetAutoAccept and QuestGetAutoAccept() then AcknowledgeAutoAcceptQuest() else AcceptQuest() end
                note("I:rxp accepted " .. id)
            end
            return
        end
        if QuestGetAutoAccept and QuestGetAutoAccept() then AcknowledgeAutoAcceptQuest() else AcceptQuest() end
    elseif e == "QUEST_ACCEPT_CONFIRM" then
        if rxp_owns() then return end
        ConfirmAcceptQuest()
    elseif e == "QUEST_PROGRESS" then
        if rxp_owns() then return end
        if IsQuestCompletable() then CompleteQuest() else CloseQuest() end
    elseif e == "QUEST_COMPLETE" then
        if rxp_owns() then
            -- RXP takes 1-choice rewards, .turnin id,N and usable upgrades (RXPGuides.lua:947-1014); it leaves the
            -- panel open for a quest that is not in the guide or a choice with no usable upgrade: pick then
            C_Timer.After(1.0, function()
                if QuestFrameRewardPanel and QuestFrameRewardPanel:IsShown() then
                    local ok, err = pcall(function() GetQuestReward(best_reward()) end)
                    if not ok then note("E:reward " .. tostring(err)) end
                end
            end)
            return
        end
        GetQuestReward(best_reward())
    elseif e == "QUEST_TURNED_IN" or e == "BAG_UPDATE_DELAYED" then
        C_Timer.After(1.0, function() pcall(equip_upgrades) end)
        C_Timer.After(1.5, function() pcall(bag_upgrade) end)
    elseif e == "EQUIP_BIND_CONFIRM" or e == "AUTOEQUIP_BIND_CONFIRM" then
        local slot = ...
        pcall(EquipPendingItem, slot)
        pcall(StaticPopup_Hide, "EQUIP_BIND")
        pcall(StaticPopup_Hide, "AUTOEQUIP_BIND")
    elseif e == "QUEST_GREETING" then
        note_offers()
        if rxp_owns() then return end                    -- RXPGuides.lua:1197-1219
        for i = 1, GetNumActiveQuests() do
            local _, done = GetActiveTitle(i)
            if done then SelectActiveQuest(i); return end
        end
        if GetNumAvailableQuests() > 0 then SelectAvailableQuest(1) end
    elseif e == "GOSSIP_SHOW" then gossip()
    elseif e == "TAXIMAP_OPENED" then C_Timer.After(0.3, function() pcall(take_fly) end)
    elseif e == "MAIL_SHOW" or e == "MAIL_INBOX_UPDATE" then
        -- the bot opened a mailbox (nb bot.py mail_step): take the money and the items of every mail without a
        -- COD, one per event (each take fires MAIL_INBOX_UPDATE again)
        C_Timer.After(0.4, function() pcall(take_mail) end)
    elseif e == "MERCHANT_SHOW" then
        -- at once, whether or not RXP sells too: the bot closes the window ~1.5 s after it opens, so the old delayed
        -- pass lost the race and white gear (RXP never sells it) stayed (inst1 00:59: 2 Trogg Hand Axes kept).
        -- Locked items (a sale RXP has in flight) are skipped; the delayed pass takes what is left
        local oks, es = pcall(sell_junk)
        if not oks then note("E:sell " .. tostring(es):sub(-80)) end
        if rxp_sells_junk() then
            C_Timer.After(1.0, function()
                if MerchantFrame and MerchantFrame:IsShown() then pcall(sell_junk) end
            end)
        end
        if CanMerchantRepair and CanMerchantRepair() then RepairAllItems() end   -- RXP has no repair
        C_Timer.After(0.3, function() pcall(buy_bag) end)   -- after the sale and the repair have paid
        buy_on_show()                                    -- the step's vendor .collect: RXP never buys it (buy_collect)
        -- water / food after the junk sale has paid (one pass; a retry only for items whose data was not loaded)
        sup.done = {}
        C_Timer.After(0.5, function() pcall(buy_supplies, true) end)
        C_Timer.After(1.2, function() pcall(buy_ammo) end)
    elseif e == "TRAINER_SHOW" then
        if rxp_trains() then return end                  -- RXP's trainer automation switched on (rxp_trains)
        -- the service list is filled a moment after the event: wait for it
        local function buy_all(tries)
            local okn, n = pcall(GetNumTrainerServices)
            n = okn and tonumber(n) or 0
            if n == 0 then
                if tries > 0 then C_Timer.After(0.4, function() buy_all(tries - 1) end)
                else BotEyeDB.lastTrain = "empty: GetNumTrainerServices=" .. tostring(n) .. " ok=" .. tostring(okn) end
                return
            end
            local dump = {}
            for i = 1, math.min(n, 12) do
                local o1, nm, kd = pcall(GetTrainerServiceInfo, i)
                local o2, cs = pcall(GetTrainerServiceCost, i)
                dump[#dump + 1] = tostring(nm):sub(1, 14) .. ":" .. tostring(kd):sub(1, 5) .. ":" .. tostring(cs)
            end
            BotEyeDB.trainDump = table.concat(dump, ",")
            local bought, rest, poor, need = 0, {}, 0, 0
            for i = n, 1, -1 do
                local oki, name, kind = pcall(GetTrainerServiceInfo, i)   -- returns name, state, icon
                if oki and kind == "available" then
                    local okc, cost = pcall(GetTrainerServiceCost, i)
                    cost = okc and tonumber(cost) or 0
                    if cost > (GetMoney() or 0) then
                        poor = poor + 1
                        if need == 0 or cost < need then need = cost end
                    else
                        local bok = pcall(BuyTrainerService, i)
                        if bok then bought = bought + 1 end
                    end
                elseif oki then rest[#rest + 1] = tostring(name):sub(1, 12) .. ":" .. tostring(kind):sub(1, 5) end
            end
            note("I:train bought " .. bought .. " of " .. n .. " poor " .. poor .. " need " .. need .. " c" .. tostring(GetMoney()))
            BotEyeDB.lastTrain = "bought " .. bought .. " of " .. n .. " poor " .. poor .. " need " .. need .. " money " .. tostring(GetMoney()) .. " lvl " .. tostring(UnitLevel("player"))
            if poor == 0 then BotEyeDB.trained = math.floor((UnitLevel("player") or 1) / 2) * 2 end
        end
        buy_all(10)
    end                                                  -- CONFIRM_BINDER: bind_confirm, above
end)

-- ---------------------------------------------------------------- key hooks
-- Override bindings (not saved): F1-F10 target nameplate1-10, NUMPAD0-9 nameplate11-20,
-- F11 clears the target, NUMPADMULTIPLY uses the item set with /be use.
local owner = CreateFrame("Frame")
local function secure(name, attrs)
    local b = CreateFrame("Button", name, UIParent, "SecureActionButtonTemplate")
    b:RegisterForClicks("AnyUp", "AnyDown")
    for k, v in pairs(attrs) do b:SetAttribute(k, v) end
    b:Hide()
    return b
end

function BotEyeSetup()
    if InCombatLockdown() then return end
    for i = 1, 20 do
        local key = i <= 10 and ("F" .. i) or ("NUMPAD" .. (i - 11))
        local b = _G["BotEyeT" .. i] or secure("BotEyeT" .. i, { type = "macro", macrotext = "/target nameplate" .. i })
        SetOverrideBindingClick(owner, true, key, b:GetName())
    end
    local c = _G.BotEyeClear or secure("BotEyeClear", { type = "macro", macrotext = "/cleartarget" })
    SetOverrideBindingClick(owner, true, "F11", "BotEyeClear")
    -- NUMPADMULTIPLY runs /target by name for the quest mobs the bot is hunting (/be find A;B loads the names)
    local f = _G.BotEyeFind or secure("BotEyeFind", { type = "macro", macrotext = "" })
    SetOverrideBindingClick(owner, true, "NUMPADMULTIPLY", "BotEyeFind")
    if not _G.BotEyeUse then secure("BotEyeUse", { type = "macro", macrotext = "" }) end   -- /be use stays defined, now unbound
    local s = _G.BotEyeStart or secure("BotEyeStart", { type = "macro", macrotext = "/startattack" })
    SetOverrideBindingClick(owner, true, "NUMPADDIVIDE", "BotEyeStart")
    -- keys the bot presses instead of typing chat commands
    local keymacros = {
        -- the healer by exact name first; /targetfriend only when no healer is in reach (it moved the target off the
        -- healer whenever another friendly was in view: death-loop diagnosis 2026-10-14)
        { "NUMPADPLUS", "BotEyeFriend", "/cleartarget\n/targetexact Spirit Healer\n/stopmacro [exists]\n/targetfriend" },
        { "NUMPADMINUS", "BotEyeLast", "/targetlasttarget" },
        -- eat / drink: the best owned food / water once macro_frame has seen the bags (sup_scan), these until then
        { "NUMPADDECIMAL", "BotEyeEat", EAT_DEFAULT },
        { "F12", "BotEyeDrink", DRINK_DEFAULT },
        { "HOME", "BotEyeRelease", "/run RepopMe()" },
        { "END", "BotEyeRetrieve", "/run RetrieveCorpse()" },
        { "INSERT", "BotEyeQuestItem", QUEST_ITEM_DEFAULT },     -- guide .use steps (the RXP step's item)
        -- the way out of a death loop (nb Bot.hearth_out): bots 3 / 4 2026-10-16 03:43-06:11 revived at the Badlands
        -- spirit healer ~150 times each among level 35 mobs
        { "F9", "BotEyeHearth", "/use Hearthstone" },
        -- the pet (CLASS_CFG.pet; nb bot.py pet_step / pet_attack): J attack, K call, U revive, Y mend, H feed (the
        -- best food in the bags: macro_frame), G dismiss (a taming step needs no pet out)
        { "J", "BotEyePetAttack", "/petattack" },
        { "K", "BotEyePetCall", (ccfg().pet and ("/cast " .. ccfg().pet.call)) or "" },
        { "U", "BotEyePetRevive", (ccfg().pet and ("/cast " .. ccfg().pet.revive)) or "" },
        { "Y", "BotEyePetMend", (ccfg().pet and ("/cast " .. ccfg().pet.mend)) or "" },
        { "H", "BotEyePetFeed", "" },
        { "G", "BotEyePetDismiss", "/run PetDismiss()" },
        { "B", "BotEyePetAssist", "/assist pet" },               -- target what the pet fights
        { "N", "BotEyePetTame", (ccfg().pet and ccfg().pet.tame and ("/dismount" .. string.char(10) .. "/cast " .. ccfg().pet.tame)) or "" },
    }
    for _, k in ipairs(keymacros) do
        local kb = _G[k[2]] or secure(k[2], { type = "macro", macrotext = k[3] })
        SetOverrideBindingClick(owner, true, k[1], k[2])
    end
    -- RXP's step NPC (RXP_PLAN.md section 4): /targetexact <first .target name>, filled by rxp_keys. PAGEUP is the key
    -- to test first (HOME / END overrides do not fire on this account); DELETE and PAGEDOWN carry the same button as
    -- fallbacks. BotEyeDB.rxpNpcKeys (/be rxp keys K1 K2 ...) replaces the list, e.g. with NUMPAD9 (nameplate 20:
    -- BotEye paints 12 plates only).
    if not _G.BotEyeRXPNpc then secure("BotEyeRXPNpc", { type = "macro", macrotext = "" }) end
    for _, key in ipairs(BotEyeDB.rxpNpcKeys or { "PAGEUP", "DELETE", "PAGEDOWN" }) do
        SetOverrideBindingClick(owner, true, key, "BotEyeRXPNpc")
    end
    SetOverrideBinding(owner, true, "M", "INTERACTTARGET")    -- fresh accounts: no per-account binding needed
    -- the sit key (nb SIT_KEY): inst3 2026-10-15 00:07 the X press left the character standing by Eric's campfire, so
    -- the campfire rest (server: sit within 15 yd of a fire) never started
    SetOverrideBinding(owner, true, "X", "SITORSTAND")
    -- the bots fake-activate background windows for their pads: no gamepad cursor mode, or every client
    -- grabs and re-centres the ONE system mouse (2026-10-14: "the mouse is going crazy")
    for _, cv in ipairs({ "GamePadCursorAutoEnable", "GamePadCursorCentering", "GamePadCursorForTargeting",
                          "GamePadCursorOnLogin", "GamePadTouchCursorEnable", "GamePadCursorPushCamera",
                          "GamePadCursorCenteredEmulation" }) do
        pcall(SetCVar, cv, 0)
    end
    pcall(SetCVar, "nameplateShowFriendlyNPCs", 1)
    pcall(SetCVar, "nameplateShowEnemies", 1)
    pcall(SetCVar, "nameplateMaxDistance", 41)
    pcall(SetCVar, "autoLootDefault", 1)
    pcall(SetCVar, "scriptErrors", 0)       -- Blizzard UI errors from server data (quest portraits) must not cover the screen
    -- soft-target interact: the interact key / pad A button then uses the nearest object (barrels, chests)
    pcall(SetCVar, "SoftTargetInteract", 3)
    pcall(SetCVar, "SoftTargetInteractRange", 12)
    pcall(SetCVar, "SoftTargetIconInteract", 1)
    note("I:BotEye ready")
end

-- dynamic macros: each nameplate key targets "its" unit by NAME (the client
-- refuses /target nameplateN and TargetUnit from Lua). Refreshed out of combat.
-- the Game Menu (a stray Esc with nothing to close opens it) swallows the interact key and is not in the ui bits:
-- inst4 2026-10-15 00:28-00:35, 32 presses at Mountaineer Gretchen and 6 at the Spirit Healer opened nothing.
-- The bot never wants it: close it within half a second
local gm_t = 0
local gm_frame = CreateFrame("Frame")
gm_frame:SetScript("OnUpdate", function(self, el)
    gm_t = gm_t + el
    if gm_t < 0.5 then return end
    gm_t = 0
    if GameMenuFrame and GameMenuFrame:IsShown() then
        local ok = pcall(HideUIPanel, GameMenuFrame)
        if not ok or GameMenuFrame:IsShown() then pcall(GameMenuFrame.Hide, GameMenuFrame) end
        note("I:game menu closed")
    end
end)

local macro_t = 0
local macro_frame = CreateFrame("Frame")
macro_frame:SetScript("OnUpdate", function(self, el)
    macro_t = macro_t + el
    if macro_t < 0.25 or InCombatLockdown() then return end
    macro_t = 0
    for i = 1, 20 do
        local b = _G["BotEyeT" .. i]
        if b then
            local u = "nameplate" .. i
            local name = UnitExists(u) and UnitName(u)
            local text = (name and not issecretvalue(name)) and ("/target " .. name) or ""
            if b:GetAttribute("macrotext") ~= text then b:SetAttribute("macrotext", text) end
        end
    end
    local ok, err = pcall(rxp_keys)                      -- BotEyeFind / BotEyeRXPNpc / BotEyeQuestItem from RXP
    if not ok and BotEyeDB.lastErr ~= err then BotEyeDB.lastErr = err; note("LUAERR keys " .. tostring(err)) end
    -- the eat / drink keys use the best usable food / water in the bags (fill_supplies keeps sup current)
    setmacro(_G.BotEyeDrink, sup.drink > 0 and ("/use item:" .. sup.drink) or DRINK_DEFAULT)
    setmacro(_G.BotEyeEat, sup.food > 0 and ("/use item:" .. sup.food) or EAT_DEFAULT)
    local pf = ccfg().pet and ccfg().pet.feed
    setmacro(_G.BotEyePetFeed, (pf and sup.food > 0) and ("/cast " .. pf .. "\n/use item:" .. sup.food) or "")
end)

SLASH_BOTEYE1 = "/be"
SlashCmdList.BOTEYE = function(line)
    local cmd, arg = line:match("^(%S*)%s*(.-)$")
    cmd = (cmd or ""):lower()
    if cmd == "sell" then sell_junk()
    elseif cmd == "auto" then auto = arg ~= "off"; note("I:auto " .. tostring(auto))
    elseif cmd == "want" then st.want = arg ~= "" and arg:lower() or nil; note("I:want " .. tostring(st.want))
    elseif cmd == "bags" then
        st.bag_try_t = nil
        local ok, err = pcall(bag_upgrade); if not ok then note("E:bags " .. tostring(err)) end
    elseif cmd == "fly" then
        st.fly = { name = arg:lower(), t = GetTime() }; note("I:fly want " .. arg)
        if TaxiFrame and TaxiFrame:IsShown() or FlightMapFrame and FlightMapFrame:IsShown() then pcall(take_fly) end
    elseif cmd == "release" then RepopMe()
    elseif cmd == "retrieve" then RetrieveCorpse()
    elseif cmd == "corpse" then
        -- /be corpse: where the game says our corpse lies ("I:corpse A B": the world pos of the corpse map pin,
        -- "I:corpse none" when it does not say). The bot guessed it from its last fight position: a flee 200 yd
        -- into the water left bot 3 22:51-23:00 reclaiming at the wrong spot.
        local ok, a, b = pcall(function()
            local m = C_Map.GetBestMapForUnit("player")
            local p = m and C_DeathInfo and C_DeathInfo.GetCorpseMapPosition(m)
            if not p then return nil end
            local _, w = C_Map.GetWorldPosFromMapPos(m, p)
            if not w then return nil end
            return w.x, w.y
        end)
        if ok and a then note(string.format("I:corpse %.1f %.1f", a, b)) else note("I:corpse none") end
    elseif cmd == "hold" then
        -- /be hold on|off: the death ticker does not take the corpse while held, so the bot picks the spot to revive on
        -- (inst3 2026-10-14 18:05:16 / 18:07:22 revived on the corpse at 35% hp among 3-4 whelps). A hold expires
        -- after 150 s by itself, so a bot that dies or stops mid-run cannot leave the ghost stuck.
        st.hold_until = (arg ~= "off") and GetTime() + 150 or 0
        note("I:hold " .. tostring(arg ~= "off"))
    elseif cmd == "rxp" and arg:lower():match("^keys") then
        -- /be rxp keys K1 K2 ...: the keys of BotEyeRXPNpc (default PAGEUP DELETE PAGEDOWN); rebinds everything
        if InCombatLockdown() then note("E:combat"); return end
        local t = {}
        for k in arg:sub(5):gmatch("%S+") do t[#t + 1] = k:upper() end
        BotEyeDB.rxpNpcKeys = #t > 0 and t or nil
        ClearOverrideBindings(owner); BotEyeSetup()
        note("I:rxp keys " .. table.concat(BotEyeDB.rxpNpcKeys or { "PAGEUP", "DELETE", "PAGEDOWN" }, " "))
    elseif cmd == "use" then
        if InCombatLockdown() then note("E:combat"); return end
        BotEyeUse:SetAttribute("macrotext", "/use " .. arg)
        note("I:use " .. arg)
    elseif cmd == "find" then
        -- /be find Name A;Name B  ->  NUMPADMULTIPLY targets the first of those names that exists and is hostile+alive
        if InCombatLockdown() then note("E:combat"); return end
        if not _G.BotEyeFind then note("E:nofind"); return end
        local t = {}
        for raw in arg:gmatch("[^;]+") do
            local n = raw:match("^%s*(.-)%s*$")
            if n ~= "" and #t < 6 then
                t[#t + 1] = "/targetexact " .. n
                t[#t + 1] = "/stopmacro [harm,nodead]"
            end
        end
        BotEyeFind:SetAttribute("macrotext", table.concat(t, "\n")); note("I:find " .. arg)
    elseif cmd == "rxp" then
        -- /be rxp on|off|load <name>|next|skip|dump (RXP calls can throw: report, do not break the slash command)
        local ok, err = pcall(rxp_cmd, arg)
        if not ok then note("E:rxp " .. tostring(err)) end
    elseif cmd == "setup" then BotEyeSetup()
    elseif cmd == "gossip" then gossip()
    elseif cmd == "close" then
        if CloseGossip then CloseGossip() elseif C_GossipInfo then C_GossipInfo.CloseGossip() end
        CloseQuest(); CloseMerchant(); if CloseTrainer then CloseTrainer() end; CloseLoot()
    elseif cmd == "emote" then
        -- /be emote sit: an emote objective (RestedXP 05-11 "The Great Outdoors": "/sit" by the campfire). The server
        -- counts the text emote (CMSG_SEND_TEXT_EMOTE -> criteria DoEmote, ChatHandler.cpp HandleTextEmoteOpcode), not the
        -- stand state the sit key sends; DoEmote sends exactly what typing /sit does, with no chat line from the bot
        local tok = (arg or ""):upper():match("^(%a+)$")
        if not tok then note("E:emote " .. tostring(arg)); return end
        local ok, err = pcall(DoEmote, tok)
        note((ok and "I:emote " or "E:emote ") .. tok .. (ok and "" or (" " .. tostring(err))))
    elseif cmd == "say" then note("I:" .. arg)
    elseif cmd == "eval" then
        local f, e = loadstring("return " .. arg)
        if not f then note("E:" .. e) else local ok, r = pcall(f); note("R:" .. tostring(r)) end
    else note("I:unknown " .. cmd) end
end

-- Death handling without key bindings (the HOME/END override bindings do not fire on this
-- account): release the spirit a couple of seconds after dying, and retrieve the corpse as
-- soon as the ghost stands within reach of it.
C_Timer.NewTicker(2, function()
    if not auto then return end
    local dead, ghost = UnitIsDead("player"), UnitIsGhost("player")
    if dead and not ghost then
        pcall(RepopMe)
    elseif ghost then
        -- spirit healer on a character below level 10: "resurrect without penalty" popup
        for i = 1, 4 do
            local f = _G["StaticPopup" .. i]
            if f and f:IsShown() and tostring(f.which):find("XP_LOSS") then
                -- not within 3 s of the event accept (no second durability loss). This client's dialogs have
                -- GetButton1() and can disable button 1 during a countdown: then confirm through the manager
                if GetTime() - (st.healer_t or 0) > 3 then
                    local okb, b = pcall(function() return f.GetButton1 and f:GetButton1() end)
                    if not okb or type(b) ~= "table" then b = _G["StaticPopup" .. i .. "Button1"] end
                    if b and (not b.IsEnabled or b:IsEnabled()) then pcall(b.Click, b)
                    elseif C_PlayerInteractionManager and C_PlayerInteractionManager.ConfirmationInteraction then
                        pcall(C_PlayerInteractionManager.ConfirmationInteraction, SPIRIT_IT)
                    end
                    st.healer_t = GetTime()
                end
                return
            end
        end
        local delay = GetCorpseRecoveryDelay and GetCorpseRecoveryDelay() or 0
        -- /be hold: the bot walks to a safer spot first (bot.py on_death / revive_spot)
        if (tonumber(delay) or 0) <= 0 and GetTime() >= (st.hold_until or 0) then pcall(RetrieveCorpse) end
    end
end)

-- ---------------------------------------------------------------- action bar upkeep
-- The bot presses the keys 1..0 = action slots 1..10. Keep the best known rank of every spell the class's combat
-- rotation (nb/classes.py) uses on those slots (only the player's own class's spells are ever known): a lower rank that is already there is swapped for the best
-- known rank in the same slot (same key), a spell that is on no slot goes into the first empty one (else over
-- a spell nothing here uses, or the Hearthstone). Out of combat only, one change per tick, never while the
-- cursor holds something. Items (food, water) and macros are never touched.
local BAR_FAMILIES = {          -- ranks lowest first; the order is the priority when slots run short
    -- shaman
    { 403, 529, 548, 915 },     -- Lightning Bolt
    { 331, 332, 547, 913 },     -- Healing Wave (3/4 at 12/18)
    { 8017, 8018, 8019 },       -- Rockbiter Weapon (3 at 16)
    { 8042, 8044, 8045 },       -- Earth Shock (3 at 14)
    { 324, 325 },               -- Lightning Shield
    { 5730, 6390 },             -- Stoneclaw Totem
    { 2484 },                   -- Earthbind Totem (the class snare: dropped as the bot flees)
    { 8050, 8052 },             -- Flame Shock
    -- hunter
    { 75 },                     -- Auto Shot (the bot keeps it running)
    { 2973, 14260, 14261 },     -- Raptor Strike
    { 1978, 13549, 13550 },     -- Serpent Sting
    { 3044, 14281 },            -- Arcane Shot
    { 1130 },                   -- Hunter's Mark
    { 5116 },                   -- Concussive Shot
    { 13165 },                  -- Aspect of the Hawk
    { 13163 },                  -- Aspect of the Monkey
    { 2974 },                   -- Wing Clip
    -- warrior
    { 78, 284, 285, 1608 },     -- Heroic Strike
    { 6673, 5242 },             -- Battle Shout
    { 100, 6178 },              -- Charge
    { 772, 6546 },              -- Rend
    -- paladin
    { 635, 639, 647 },          -- Holy Light
    { 21084, 20287, 20288 },    -- Seal of Righteousness
    { 20271 },                  -- Judgement
    { 465, 10290 },             -- Devotion Aura
    -- rogue
    { 1752, 1757, 1758 },       -- Sinister Strike
    { 2098, 6760 },             -- Eviscerate
    -- druid
    { 5176, 5177, 5178 },       -- Wrath
    { 5185, 5186, 5187 },       -- Healing Touch
    { 1126, 5232 },             -- Mark of the Wild
    { 8921, 8924 },             -- Moonfire
    -- mage
    { 133, 143, 145 },          -- Fireball
    { 168, 7300 },              -- Frost Armor
    -- priest
    { 585, 591, 598 },          -- Smite
    { 2050, 2052, 2053 },       -- Lesser Heal
    { 1243, 1244 },             -- Power Word: Fortitude
    { 589, 594 },               -- Shadow Word: Pain
    -- warlock
    { 686, 695, 705 },          -- Shadow Bolt
    { 687, 696 },               -- Demon Skin
    { 348, 707 },               -- Immolate
}
local BAR_LAST = 10
local BAR_KEEP = { [6603] = true }      -- Attack: the bot presses it
local HEARTHSTONE = 6948

local function bar_family(id)
    if id then
        for fi, fam in ipairs(BAR_FAMILIES) do
            for r, sid in ipairs(fam) do
                if sid == id then return fi, r end
            end
        end
    end
end

local function bar_known(id)
    local f = IsPlayerSpell or IsSpellKnown or (C_SpellBook and C_SpellBook.IsSpellKnown)
    if not f then return false end
    local ok, r = pcall(f, id)
    if not ok or (issecretvalue and issecretvalue(r)) then return false end
    return r and true or false
end

local function bar_place(slot, id)
    pcall(ClearCursor)
    local pick = (C_Spell and C_Spell.PickupSpell) or PickupSpell
    if not pick then return false end
    pcall(pick, id)
    local placed = false
    if GetCursorInfo() then
        placed = pcall(PlaceAction, slot)
    end
    pcall(ClearCursor)
    return placed
end

local function bar_upkeep()
    if not auto or InCombatLockdown() or UnitAffectingCombat("player") or UnitIsDeadOrGhost("player") then return end
    if GetCursorInfo() then return end
    local info = {}
    for s = 1, BAR_LAST do
        local kind, id = GetActionInfo(s)
        if issecretvalue and (issecretvalue(kind) or issecretvalue(id)) then return end
        info[s] = { kind = kind, id = id }
    end
    for fi, fam in ipairs(BAR_FAMILIES) do
        local best, rbest
        for r = #fam, 1, -1 do
            if bar_known(fam[r]) then best, rbest = fam[r], r; break end
        end
        if best then
            local at, rcur
            for s = 1, BAR_LAST do
                if info[s].kind == "spell" then
                    local f2, r2 = bar_family(info[s].id)
                    if f2 == fi and (not rcur or r2 > rcur) then at, rcur = s, r2 end
                end
            end
            if at then
                if rcur < rbest and bar_place(at, best) then
                    note("I:bar " .. at .. " rank " .. rbest .. " id " .. best)
                    return
                end
            else
                local free, drop
                for s = 1, BAR_LAST do
                    local k, id = info[s].kind, info[s].id
                    if not k then
                        free = free or s
                    elseif (k == "spell" and not bar_family(id) and not BAR_KEEP[id]) or (k == "item" and id == HEARTHSTONE) then
                        drop = s
                    end
                end
                local slot = free or drop
                if slot and bar_place(slot, best) then
                    note("I:bar " .. slot .. " <- " .. best)
                    return
                end
            end
        end
    end
end

C_Timer.NewTicker(2, function() pcall(bar_upkeep) end)
-- the gear check on a timer too (its own 4 s throttle): a bag update fires it once, and an item whose info was not
-- loaded yet (mail, a fresh drop) was never looked at again
C_Timer.NewTicker(5, function() if not UnitAffectingCombat("player") then pcall(equip_upgrades) end end)

-- multibox: the nb bot holds |right stick Y| = sig on ITS virtual pad; claim that device for this client only
function BotEyePadClaim(sig)
    for _, id in ipairs(C_GamePad.GetAllDeviceIDs()) do
        local r = C_GamePad.GetDeviceRawState(id)
        local a = r and r.rawAxes and r.rawAxes[4]
        if a and math.abs(math.abs(a) - sig) < 0.012 then
            SetCVar("GamePadEnable", 1)
            SetCVar("GamePadSingleActiveID", id)
            return id
        end
    end
    return 0
end