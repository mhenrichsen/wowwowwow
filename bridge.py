"""Bridge to the game's Lua without typing: a small add-on + key presses.

Typing /script lines into chat proved unreliable (characters occasionally get
mangled, and the chat line is capped at 255). Instead the bot installs an
add-on, WowBotBridge, into the client. It binds F1-F10 as the digits 1-9,0
plus F11 (clear) and F12 (execute), and the bot "dials" numeric commands with
key presses - the same channel that casts every spell reliably. (Not the
numpad: its keys share scan codes with the arrow keys and walk the character.)

    F11, digits of <2-digit command><argument>, F12      e.g. 10 783 = accept quest 783

Data the Lua side needs (quest titles, spell names) is generated from the
world DB into the add-on, so no strings ever travel through the keyboard.
New add-ons are only found when WoW.exe starts; after that, a reinstall is
picked up with the RELOAD_UI command (98).

    python bridge.py install     # (re)generate the add-on into the client
"""

from __future__ import annotations

import os
import sys
import time

import worlddb

ADDON_DIR = r"C:\Users\MadsG\Desktop\wow-forever-server\client\Interface\AddOns\WowBotBridge"
BUILD = 5875

# command codes
ACCEPT_QUEST, TURN_IN_QUEST, REWARD_CHOICE = 10, 11, 12
EQUIP_ITEM, SELL_GREYS, REPAIR, CLOSE_ALL, BUY_ITEM, SELL_ITEM, OPEN_MERCHANT = 20, 21, 22, 23, 24, 25, 26
TRAIN_ALL, GOSSIP_OPTION, BIND_HEARTH, USE_HEARTH, TAXI = 30, 31, 32, 33, 34
REPOP, RETRIEVE_CORPSE, SPIRIT_HEALER, ACCEPT_XP_LOSS = 40, 41, 42, 43
SIT, STAND, ATTACK, ATTACK_ON, ATTACK_OFF = 50, 51, 52, 53, 54
PLACE_SPELL, PLACE_WATER, PLACE_FOOD, EQUIP_BAGS = 60, 62, 63, 64
USE_SLOT, ABANDON_QUEST, PLACE_ITEM = 65, 66, 67
BAR_PAGE_1, UPGRADE_BAG = 68, 69
CAMERA_YAW, LOGOUT, USE_SLOT_SELF, TRAIN_SPELL, CAMERA_RESET = 70, 71, 72, 73, 74
# CAMERA_YAW: arg = degrees + 180 (0..360): turn the camera only
RELOAD_UI, PING = 98, 99

# flight nodes the guides use: name (as TaxiNodeName shows it) -> (code, landing spot)
TAXI_NODES = {   # short name -> (TaxiNodes.dbc id, landing (x, y, z)); all Alliance nodes, map in the comment
    "Aerie Peak": (43, (283.7, -2002.8, 194.7)),   # map 0: Aerie Peak, The Hinterlands
    "Booty Bay": (19, (-14473.0, 464.1, 36.4)),   # map 0: Booty Bay, Stranglethorn
    "Chillwind Camp": (66, (931.3, -1430.1, 64.7)),   # map 0: Chillwind Camp, Western Plaguelands
    "Crown Guard Tower": (87, (1857.6, -3658.5, 143.7)),   # map 0: Crown Guard Tower, Eastern Plaguelands
    "Darkshire": (12, (-10515.5, -1261.7, 41.3)),   # map 0: Darkshire, Duskwood
    "Eastwall Tower": (86, (2499.2, -4742.9, 93.5)),   # map 0: Eastwall Tower, Eastern Plaguelands
    "Ironforge": (6, (-4821.8, -1155.4, 502.2)),   # map 0: Ironforge, Dun Morogh
    "Lakeshire": (5, (-9429.1, -2231.4, 68.7)),   # map 0: Lakeshire, Redridge
    "Light's Hope Chapel": (67, (2271.1, -5340.8, 87.1)),   # map 0: Light's Hope Chapel, Eastern Plaguelands
    "Menethil Harbor": (7, (-3792.3, -783.3, 9.1)),   # map 0: Menethil Harbor, Wetlands
    "Morgan's Vigil": (71, (-8364.6, -2738.4, 185.5)),   # map 0: Morgan's Vigil, Burning Steppes
    "Nethergarde Keep": (45, (-11112.2, -3435.7, 79.1)),   # map 0: Nethergarde Keep, Blasted Lands
    "Northpass Tower": (85, (3109.3, -4285.1, 109.4)),   # map 0: Northpass Tower, Eastern Plaguelands
    "Plaguewood Tower": (84, (2998.7, -3050.1, 117.2)),   # map 0: Plaguewood Tower, Eastern Plaguelands
    "Refuge Pointe": (16, (-1240.5, -2515.1, 22.2)),   # map 0: Refuge Pointe, Arathi
    "Sentinel Hill": (4, (-10628.9, 1036.7, 34.1)),   # map 0: Sentinel Hill, Westfall
    "Southshore": (14, (-711.5, -515.5, 26.1)),   # map 0: Southshore, Hillsbrad
    "Stormwind": (2, (-8840.6, 489.7, 109.6)),   # map 0: Stormwind, Elwynn
    "Thelsamar": (8, (-5421.9, -2930.0, 347.2)),   # map 0: Thelsamar, Loch Modan
    "Thorium Point": (74, (-6552.6, -1168.3, 309.3)),   # map 0: Thorium Point, Searing Gorge
    "Astranaar": (28, (2827.3, -289.2, 107.2)),   # map 1: Astranaar, Ashenvale
    "Auberdine": (26, (6341.4, 557.7, 16.3)),   # map 1: Auberdine, Darkshore
    "Cenarion Hold": (73, (-6761.8, 772.0, 88.9)),   # map 1: Cenarion Hold, Silithus
    "Everlook": (52, (6799.2, -4742.4, 701.5)),   # map 1: Everlook, Winterspring
    "Feathermoon": (41, (-4373.8, 3338.6, 12.3)),   # map 1: Feathermoon, Feralas
    "Fishing Village": (57, (8701.5, 991.4, 14.2)),   # map 1: Fishing Village, Teldrassil
    "Gadgetzan": (39, (-7224.0, -3734.6, 8.4)),   # map 1: Gadgetzan, Tanaris
    "Marshal's Refuge": (79, (-6113.8, -1142.7, -187.6)),   # map 1: Marshal's Refuge, Un'Goro Crater
    "Moonglade": (49, (7458.5, -2487.2, 462.3)),   # map 1: Moonglade
    "Nighthaven": (62, (7793.6, -2403.5, 489.3)),   # map 1: Nighthaven, Moonglade
    "Nijel's Point": (37, (139.2, 1325.8, 193.5)),   # map 1: Nijel's Point, Desolace
    "Ratchet": (80, (-894.6, -3773.0, 11.5)),   # map 1: Ratchet, The Barrens
    "Rut'theran Village": (27, (8643.6, 841.0, 23.3)),   # map 1: Rut'theran Village, Teldrassil
    "Stonetalon Peak": (33, (2681.1, 1461.7, 232.9)),   # map 1: Stonetalon Peak, Stonetalon Mountains
    "Talonbranch Glade": (65, (6205.9, -1949.6, 571.3)),   # map 1: Talonbranch Glade, Felwood
    "Talrendis Point": (64, (2722.0, -3880.6, 100.9)),   # map 1: Talrendis Point, Azshara
    "Thalanaar": (31, (-4491.9, -775.9, -39.5)),   # map 1: Thalanaar, Feralas
    "Theramore": (32, (-3825.4, -4516.6, 10.4)),   # map 1: Theramore, Dustwallow Marsh
}

# The zone each node is in, for guides that fly to a ZONE (RestedXP's
# ".fly Westfall"). Map geometry cannot tell: zone map rectangles overlap,
# and Nijel's Point (Desolace) lies inside Stonetalon's.
TAXI_ZONES = {
    'Aerie Peak': 'The Hinterlands',
    'Booty Bay': 'Stranglethorn',
    'Chillwind Camp': 'Western Plaguelands',
    'Crown Guard Tower': 'Eastern Plaguelands',
    'Darkshire': 'Duskwood',
    'Eastwall Tower': 'Eastern Plaguelands',
    'Ironforge': 'Dun Morogh',
    'Lakeshire': 'Redridge',
    "Light's Hope Chapel": 'Eastern Plaguelands',
    'Menethil Harbor': 'Wetlands',
    "Morgan's Vigil": 'Burning Steppes',
    'Nethergarde Keep': 'Blasted Lands',
    'Northpass Tower': 'Eastern Plaguelands',
    'Plaguewood Tower': 'Eastern Plaguelands',
    'Refuge Pointe': 'Arathi',
    'Sentinel Hill': 'Westfall',
    'Southshore': 'Hillsbrad',
    'Stormwind': 'Elwynn',
    'Thelsamar': 'Loch Modan',
    'Thorium Point': 'Searing Gorge',
    'Astranaar': 'Ashenvale',
    'Auberdine': 'Darkshore',
    'Cenarion Hold': 'Silithus',
    'Everlook': 'Winterspring',
    'Feathermoon': 'Feralas',
    'Fishing Village': 'Teldrassil',
    'Gadgetzan': 'Tanaris',
    "Marshal's Refuge": "Un'Goro Crater",
    'Moonglade': 'Moonglade',
    'Nighthaven': 'Moonglade',
    "Nijel's Point": 'Desolace',
    'Ratchet': 'The Barrens',
    "Rut'theran Village": 'Teldrassil',
    'Stonetalon Peak': 'Stonetalon Mountains',
    'Talonbranch Glade': 'Felwood',
    'Talrendis Point': 'Azshara',
    'Thalanaar': 'Feralas',
    'Theramore': 'Dustwallow Marsh',
}

LUA = r'''-- WowBotBridge: generated by wowbot/bridge.py - do not edit by hand.
WowBot = { buf = "", cmds = {} }
local C = WowBot.cmds

local function say(m) DEFAULT_CHAT_FRAME:AddMessage("|cff66ccffWowBot:|r " .. m) end

function WowBot_Clear() WowBot.buf = "" end
function WowBot_Digit(d) WowBot.buf = WowBot.buf .. d end
function WowBot_Exec()
  local b = WowBot.buf
  WowBot.buf = ""
  if strlen(b) < 2 then return end
  local f = C[tonumber(strsub(b, 1, 2))]
  if f then f(tonumber(strsub(b, 3)) or 0) end
end

local function pick(list, t, sel)
  for i = 1, table.getn(list), 2 do
    if list[i] == t then sel((i + 1) / 2) return true end
  end
  return false
end

local function bestChoice()
  local best, bq = 1, -1
  for i = 1, GetNumQuestChoices() do
    local _, _, _, q, usable = GetQuestItemInfo("choice", i)
    if usable and (q or 0) > bq then best, bq = i, q or 0 end
  end
  return best
end

-- 10: accept quest <id>
C[10] = function(id)
  local t = WowBot_Q[id]
  if not t then say("unknown quest " .. id) return end
  if QuestFrameDetailPanel:IsVisible() then
    if GetTitleText() == t then AcceptQuest() else DeclineQuest() end
    return
  end
  if GossipFrame:IsVisible() and pick({GetGossipAvailableQuests()}, t, SelectGossipAvailableQuest) then return end
  if QuestFrameGreetingPanel:IsVisible() then
    for i = 1, GetNumAvailableQuests() do
      if GetAvailableTitle(i) == t then SelectAvailableQuest(i) return end
    end
  end
  -- Nothing matched. A silent miss here is how quest chains die: the step
  -- fails three times, gets skipped, and every quest that needs it is
  -- skipped too (guide completion fell to 15% this way). Say what the NPC
  -- IS offering so the next failure is diagnosable instead of invisible.
  local seen = {}
  if GossipFrame:IsVisible() then
    local g = {GetGossipAvailableQuests()}
    for i = 1, table.getn(g), 2 do table.insert(seen, tostring(g[i])) end
  end
  if QuestFrameGreetingPanel:IsVisible() then
    for i = 1, GetNumAvailableQuests() do table.insert(seen, GetAvailableTitle(i)) end
  end
  say("no '" .. tostring(t) .. "' here; offered: " .. table.concat(seen, " | "))
end

-- 11: hand in quest <id> (select -> complete -> reward; call repeatedly)
C[11] = function(id)
  local t = WowBot_Q[id]
  if not t then say("unknown quest " .. id) return end
  if QuestFrameRewardPanel:IsVisible() then
    if GetNumQuestChoices() > 0 then
      local c = WowBot.choice
      if not c or c > GetNumQuestChoices() then c = bestChoice() end
      GetQuestReward(c)
    else GetQuestReward() end
    WowBot.choice = nil
    return
  end
  if QuestFrameProgressPanel:IsVisible() then CompleteQuest() return end
  if GossipFrame:IsVisible() and pick({GetGossipActiveQuests()}, t, SelectGossipActiveQuest) then return end
  if QuestFrameGreetingPanel:IsVisible() then
    for i = 1, GetNumActiveQuests() do
      if GetActiveTitle(i) == t then SelectActiveQuest(i) return end
    end
  end
end

-- 12: which reward to take on the next hand-in (1-based; picked by gear.py)
C[12] = function(n) WowBot.choice = n end

local function findBag(test)
  for b = 0, 4 do
    for s = 1, GetContainerNumSlots(b) do
      local l = GetContainerItemLink(b, s)
      if l and test(l) then return b, s end
    end
  end
end

-- 20: equip item <id>
C[20] = function(id)
  local b, s = findBag(function(l) return strfind(l, "item:" .. id .. ":") end)
  if b then
    UseContainerItem(b, s)
    -- bind-on-equip items ask for confirmation first
    for i = 1, 4 do
      local f = getglobal("StaticPopup" .. i)
      if f and f:IsVisible() and (f.which == "EQUIP_BIND" or f.which == "AUTOEQUIP_BIND") then
        StaticPopup_OnClick(f, 1)
      end
    end
  end
end
-- 21: sell up to two grey items (merchant window open). Two DIFFERENT
-- slots, picked before selling: looking the second one up after the first
-- UseContainerItem found the same, still-pending slot again, so every
-- command sold one item and a visit spent 10-12 s on greys.
C[21] = function()
  local got = {}
  for b = 0, 4 do
    for s = 1, GetContainerNumSlots(b) do
      local l = GetContainerItemLink(b, s)
      if l and strfind(l, "ff9d9d9d") and table.getn(got) < 2 then
        table.insert(got, {b, s})
      end
    end
  end
  for _, bs in ipairs(got) do UseContainerItem(bs[1], bs[2]) end
end
C[22] = function() if CanMerchantRepair() then RepairAllItems() end end
-- 25: sell one stack of item <id> to the open merchant
C[25] = function(id)
  local b, s = findBag(function(l) return strfind(l, "item:" .. id .. ":") end)
  if b then UseContainerItem(b, s) end
end
-- 24: buy <count> of item <id> from the open merchant: arg = id * 100 + count
C[24] = function(a)
  local id, n = floor(a / 100), mod(a, 100)
  for i = 1, GetMerchantNumItems() do
    local l = GetMerchantItemLink(i)
    if l and strfind(l, "item:" .. id .. ":") then
      for k = 1, n do BuyMerchantItem(i, 1) end
      return
    end
  end
  say("merchant does not sell item " .. id)
end
-- 26: a vendor with a gossip menu (Farmer Saldean): pick "what do you have to sell"
C[26] = function()
  if MerchantFrame:IsVisible() then return end
  if GossipFrame:IsVisible() then
    local o = {GetGossipOptions()}
    for i = 1, table.getn(o), 2 do
      if o[i + 1] == "vendor" then SelectGossipOption((i + 1) / 2) return end
    end
  end
end
C[23] = function()
  CloseGossip() CloseQuest() CloseMerchant() CloseTrainer() CloseLoot()
  HideUIPanel(GameMenuFrame)
  if ScriptErrors then ScriptErrors:Hide() end
end

-- 30: open the trainer from gossip if needed, then buy everything available
C[30] = function()
  if GossipFrame:IsVisible() then
    local o = {GetGossipOptions()}
    for i = 1, table.getn(o), 2 do
      if o[i + 1] == "trainer" then SelectGossipOption((i + 1) / 2) return end
    end
  end
  for i = 1, GetNumTrainerServices() do
    local _, _, c = GetTrainerServiceInfo(i)
    if c == "available" then BuyTrainerService(i) end
  end
end
-- 73: buy one trainer service by spell (WowBot_S id), opening the trainer
-- page from gossip first like 30. 30 buys in list order - alphabetical - so
-- with money for one spell a level-4 paladin learned Blessing of Might and
-- not Judgement; the bot now buys its rotation first, by name.
C[73] = function(id)
  if GossipFrame:IsVisible() then
    local o = {GetGossipOptions()}
    for i = 1, table.getn(o), 2 do
      if o[i + 1] == "trainer" then SelectGossipOption((i + 1) / 2) return end
    end
  end
  local want = WowBot_S[id]
  if not want then return end
  for i = 1, GetNumTrainerServices() do
    local n, _, c = GetTrainerServiceInfo(i)
    if n == want and c == "available" then BuyTrainerService(i) return end
  end
end
-- 74: back to the standard third-person view (saved view 2). At 12:25 the
-- camera sat inside the character looking at the sky and every NPC and
-- corpse click projected off screen.
C[74] = function() SetView(2) CameraZoomOut(8) end
C[31] = function(n) SelectGossipOption(n) end
-- 32: make this inn our home (binder gossip option, then the confirm popup)
C[32] = function()
  if GossipFrame:IsVisible() then
    local o = {GetGossipOptions()}
    for i = 1, table.getn(o), 2 do
      if o[i + 1] == "binder" then SelectGossipOption((i + 1) / 2) return end
    end
  end
  for i = 1, 4 do
    local f = getglobal("StaticPopup" .. i)
    if f and f:IsVisible() and f.which == "CONFIRM_BINDER" then StaticPopup_OnClick(f, 1) return end
  end
end
-- 33: use the hearthstone
C[33] = function()
  local b, s = findBag(function(l) return strfind(l, "item:6948:") end)
  if b then UseContainerItem(b, s) end
end

-- 34: fly to taxi node <k> (WowBot_T): opens the taxi map from gossip if needed
C[34] = function(k)
  local name = WowBot_T[k]
  if not name then say("unknown taxi node " .. k) return end
  if GossipFrame:IsVisible() then
    local o = {GetGossipOptions()}
    for i = 1, table.getn(o), 2 do
      if o[i + 1] == "taxi" then SelectGossipOption((i + 1) / 2) return end
    end
  end
  if not TaxiFrame:IsVisible() then return end
  for i = 1, NumTaxiNodes() do
    if strfind(TaxiNodeName(i), name) and TaxiNodeGetType(i) ~= "CURRENT" then
      TakeTaxiNode(i) return
    end
  end
  say("no known flight to " .. name)
end

C[40] = function() RepopMe() end
C[41] = function() RetrieveCorpse() end
C[42] = function() if GossipFrame:IsVisible() then SelectGossipOption(1) else AcceptXPLoss() end end
C[43] = function() AcceptXPLoss() end

-- sit for regen when there is nothing to eat: a warrior with no food
-- stood from 75% to 92% on natural regen at 12:39
C[50] = function() DoEmote("SIT") end
C[51] = function() DoEmote("STAND") end
-- AttackTarget() TOGGLES in 1.12: a second "attack" from a re-entered fight
-- switched auto-attack off, and the warrior stood at 2 yd for minutes with
-- rage piling up and no swings. Keep the Attack spell on action slot 72 (a
-- bar the UI does not show) and ask the client whether it is running:
-- IsCurrentAction is the client's own state, which an event flag was not
-- (PLAYER_LEAVE_COMBAT does not always come, and then nothing attacked).
local ATTACK_SLOT = 72
C[52] = function()
  if not IsAttackAction(ATTACK_SLOT) then
    for i = 1, 400 do
      local n = GetSpellName(i, "spell")
      if not n then break end
      if n == "Attack" then PickupSpell(i, "spell") PlaceAction(ATTACK_SLOT) ClearCursor() break end
    end
  end
  if IsAttackAction(ATTACK_SLOT) then
    -- restart it, never just "leave it on": the bot selects targets by
    -- writing the client's target global, which does not re-aim a running
    -- auto-attack - it kept swinging at the previous (dead) mob while the
    -- new one clawed us to 1 hp. Off and on again sends a fresh swing at
    -- the current target.
    if IsCurrentAction(ATTACK_SLOT) then UseAction(ATTACK_SLOT) end
    UseAction(ATTACK_SLOT)
  else
    AttackTarget()
  end
end
-- 53/54: auto-attack ON / OFF, never a toggle. The off-and-on of 52 runs in
-- one frame, where IsCurrentAction still reads "on" after the first
-- UseAction - so the second switched it off again, and at 16:07 the warrior
-- stood at a wolf's side with auto-attack off after every "attack". The bot
-- reads what we swing at from memory (UNIT_FIELD_TARGET) and sends 54 then
-- 53 in separate frames when it has to re-aim.
local function WowBot_AttackSlot()
  if not IsAttackAction(ATTACK_SLOT) then
    for i = 1, 400 do
      local n = GetSpellName(i, "spell")
      if not n then break end
      if n == "Attack" then PickupSpell(i, "spell") PlaceAction(ATTACK_SLOT) ClearCursor() break end
    end
  end
  return IsAttackAction(ATTACK_SLOT)
end
C[53] = function()
  if WowBot_AttackSlot() then
    if not IsCurrentAction(ATTACK_SLOT) then UseAction(ATTACK_SLOT) end
  else
    AttackTarget()
  end
end
C[54] = function()
  if WowBot_AttackSlot() and IsCurrentAction(ATTACK_SLOT) then UseAction(ATTACK_SLOT) end
end

-- 60: place spell on an action slot: arg = spellId * 100 + slot (highest rank)
C[60] = function(a)
  local id, slot = floor(a / 100), mod(a, 100)
  local name = WowBot_S[id]
  if not name then say("unknown spell " .. id) return end
  local k
  for i = 1, 400 do
    local n = GetSpellName(i, "spell")
    if not n then break end
    if n == name then k = i end
  end
  if k then PickupSpell(k, "spell") PlaceAction(slot) ClearCursor() end
end
-- 62: put conjured water on an action slot
C[62] = function(slot)
  local b, s = findBag(function(l) return strfind(l, "Conjured") and strfind(l, "Water") end)
  if b then PickupContainerItem(b, s) PlaceAction(slot) ClearCursor() end
end
-- 63: put conjured food (muffin, bread, ...) on an action slot
C[63] = function(slot)
  local b, s = findBag(function(l) return strfind(l, "Conjured") and not strfind(l, "Water") end)
  if b then PickupContainerItem(b, s) PlaceAction(slot) ClearCursor() end
end
-- 64: equip bags lying in the bags into free bag slots (inventory ids 20-23).
-- Every container, not just the backpack: a looted bag lands wherever there
-- is room, and one in bag 2 was never equipped.
-- (UseContainerItem while a merchant window is open SELLS the item instead.)
C[64] = function()
  for c = 0, 4 do
  for s = 1, GetContainerNumSlots(c) do
    local l = GetContainerItemLink(c, s)
    if l then
      -- 1.12 GetItemInfo wants "item:<id>" (not the coloured link):
      -- name, link, rarity, level, type, subtype, ...
      local _, _, id = strfind(l, "item:(%d+)")
      local _, _, _, _, itype, isub = GetItemInfo("item:" .. (id or 0))
      local known = id and strfind(",4496,4497,4498,4499,828,805,5571,5572,5573,", "," .. id .. ",")
      if known or itype == "Container" or itype == "Bag" or isub == "Bag" then
        for k = 20, 23 do
          if not GetInventoryItemLink("player", k) then
            PickupContainerItem(c, s) PutItemInBag(k) ClearCursor()
            return                       -- one per call: the move needs a frame
          end
        end
      end
    end
  end
  end
end
-- 69: one step of replacing the equipped bag in inventory slot 20+k with a
-- bigger bag (item <entry>) lying in the bags: arg = entry * 10 + k.
-- ONE action per call, because every item move needs a frame to settle:
--   the old bag on the cursor from the last swap -> put it in a free slot;
--   the old bag still holds items -> move one of them to another bag;
--   the old bag is empty -> swap the new one in (the old one lands on the
--   cursor and the next call puts it away).
-- Nothing here ever deletes an item.
C[69] = function(a)
  local id, k = floor(a / 10), mod(a, 10)
  local old = k + 1                      -- container index of inventory slot 20+k
  local function free(skip)
    for b = 0, 4 do
      if b ~= skip then
        for t = 1, GetContainerNumSlots(b) do
          if not GetContainerItemLink(b, t) then return b, t end
        end
      end
    end
  end
  if CursorHasItem() then
    local b, t = free(-1)
    if b then PickupContainerItem(b, t) else say("no room for the old bag") end
    return
  end
  if k > 3 then return end               -- k = 9: only put the cursor item away
  for s = 1, GetContainerNumSlots(old) do
    if GetContainerItemLink(old, s) then
      local b, t = free(old)
      if b then PickupContainerItem(old, s) PickupContainerItem(b, t) ClearCursor()
      else say("no room to empty bag " .. k) end
      return
    end
  end
  for b = 0, 4 do
    if b ~= old then
      for s = 1, GetContainerNumSlots(b) do
        local l = GetContainerItemLink(b, s)
        if l and strfind(l, "item:" .. id .. ":") then
          PickupContainerItem(b, s) PutItemInBag(20 + k)
          return
        end
      end
    end
  end
  say("no bag " .. id .. " to swap in")
end
-- 65: press an action slot from Lua. The keyboard route cannot reach
-- slots 11/12: this PC has a Danish layout, so the "=" key the client
-- binds to ACTIONBUTTON12 is not the key Windows sends for "=" - the
-- character drank for hours and never once ate.
C[65] = function(slot) UseAction(slot) end
-- 72: the same slot, cast on ourselves (UseAction's onSelf flag). A self
-- spell pressed through 65 needed our own guid written as the target first,
-- and the client takes ~1 s to adopt a written target: every Seal, Holy
-- Light and Blessing in a fight cost that second, then another to put the
-- mob back.
C[72] = function(slot) UseAction(slot, 0, 1) end
-- 66: abandon a quest by id. The log caps at 20 in 1.12 and a full log
-- makes every accept fail silently, so stale quests from zones we have
-- left have to go.
C[66] = function(id)
  local t = WowBot_Q[id]
  if not t then say("unknown quest " .. id) return end
  for i = 1, GetNumQuestLogEntries() do
    local title, _, _, _, header = GetQuestLogTitle(i)
    if title and not header and title == t then
      SelectQuestLogEntry(i)
      SetAbandonQuest()
      AbandonQuest()
      return
    end
  end
  say("not in the log: " .. t)
end
-- 67: put a bag item on an action slot by entry: arg = entry * 100 + slot.
-- 62/63 only ever find CONJURED food and water, so a warrior (or any
-- class that buys its food from a vendor) had no way to get it onto
-- the bar at all.
C[67] = function(a)
  local id, slot = floor(a / 100), mod(a, 100)
  local b, s = findBag(function(l) return strfind(l, "item:" .. id .. ":") end)
  if b then PickupContainerItem(b, s) PlaceAction(slot) ClearCursor()
  else say("no item " .. id .. " in the bags") end
end
-- 68: show action bar page 1
-- The bot drives raw action ids with UseAction, which always means page 1.
-- The keys do not: pressing "3" uses (page-1)*12 + 3. With the bar left on
-- another page the character casts the rotation correctly while the user
-- sees the dwarf racials and gets Stoneform from the 3 key. Put the bar
-- back where the bot is.
C[68] = function()
  local was = CURRENT_ACTIONBAR_PAGE
  CURRENT_ACTIONBAR_PAGE = 1
  -- 1.12's ActionButton_Update takes no argument: it reads the global
  -- "this". Passing the button gave "attempt to index global 'this'".
  for i = 1, 12 do
    local btn = getglobal("ActionButton" .. i)
    if btn then
      local prev = this
      this = btn
      ActionButton_Update()
      this = prev
    end
  end
  say("action bar page " .. (was or "?") .. " -> " .. CURRENT_ACTIONBAR_PAGE)
end
-- Swing the camera (not the character) by a - 180 degrees. Standing still,
-- a facing written to memory does not drag the camera round, and an NPC
-- beside us stayed off screen for the click.
C[70] = function(a) if a and FlipCameraYaw then FlipCameraYaw(a - 180) end end
C[71] = function() Logout() end         -- to the character screen
C[98] = function() ReloadUI() end            -- pick up a reinstalled add-on
C[99] = function() say("bridge ok") end

-- Key bindings are loaded AFTER VARIABLES_LOADED and would overwrite ours,
-- so bind on entering the world (and whenever something rebinds the keys),
-- then save them so they stick.
local binding = false
local function bind()
  if binding or (GetBindingAction("F12") == "WOWBOT_EXEC" and GetBindingAction("F10") == "WOWBOT_D0") then
    return false
  end
  binding = true                      -- SetBinding re-fires UPDATE_BINDINGS
  for d = 0, 9 do SetBinding("NUMPAD" .. d) end          -- undo an earlier version
  for d = 1, 9 do SetBinding("F" .. d, "WOWBOT_D" .. d) end
  SetBinding("F10", "WOWBOT_D0")
  SetBinding("F11", "WOWBOT_CLEAR")
  SetBinding("F12", "WOWBOT_EXEC")
  SaveBindings(1)
  binding = false
  return true
end

local f = CreateFrame("Frame")
f:RegisterEvent("VARIABLES_LOADED")
f:RegisterEvent("PLAYER_ENTERING_WORLD")
f:RegisterEvent("UPDATE_BINDINGS")
f:SetScript("OnEvent", function()
  if event == "VARIABLES_LOADED" then
    -- errors go to chat instead of a modal popup that would swallow input
    seterrorhandler(function(m) say("|cffff5555lua error:|r " .. tostring(m)) end)
  elseif bind() then
    say("loaded, keys bound")
  end
end)
'''

BINDINGS = ('<Bindings>\n'
            + "".join(f'  <Binding name="WOWBOT_D{d}" header="WOWBOT">WowBot_Digit({d})</Binding>\n'
                      for d in range(10))
            + '  <Binding name="WOWBOT_CLEAR">WowBot_Clear()</Binding>\n'
            + '  <Binding name="WOWBOT_EXEC">WowBot_Exec()</Binding>\n'
            + '</Bindings>\n')

TOC = """## Interface: 11200
## Title: WowBot Bridge
## Notes: Lets the wowbot drive quest, vendor and trainer windows with key presses.
WowBotBridge_Data.lua
WowBotBridge.lua
"""


def _lua_string(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def install() -> str:
    """Write the add-on (code + data tables) into the client."""
    import classes
    quests = worlddb._query(
        "SELECT q.entry, q.Title FROM quest_template q JOIN (SELECT entry, MAX(patch) p "
        "FROM quest_template GROUP BY entry) m ON q.entry = m.entry AND q.patch = m.p")
    # every class profile in the package, not a hardcoded three: a spell that
    # is missing from WowBot_S makes the add-on answer "unknown spell <id>"
    # and silently leave the action bar empty, which is only visible in the
    # game's chat frame and never in bot.log
    names = set()
    for prof in classes.all_profiles():
        names |= set(prof.bar())
    spell_rows = worlddb._query(
        "SELECT MIN(entry), name FROM spell_template WHERE build<=%d AND name IN (%s) GROUP BY name"
        % (BUILD, ",".join(_lua_string(n).replace('"', "'") for n in names)))
    data = ["-- generated by wowbot/bridge.py from the world DB", "WowBot_Q = {"]
    data += [f"[{e}]={_lua_string(t)}," for e, t in quests]
    data += ["}", "WowBot_S = {"]
    data += [f"[{e}]={_lua_string(n)}," for e, n in spell_rows]
    data += ["}", "WowBot_T = {"]
    data += [f"[{k}]={_lua_string(n)}," for n, (k, _) in TAXI_NODES.items()]
    data += ["}"]
    os.makedirs(ADDON_DIR, exist_ok=True)
    open(os.path.join(ADDON_DIR, "WowBotBridge.toc"), "w", newline="\r\n").write(TOC)
    open(os.path.join(ADDON_DIR, "WowBotBridge.lua"), "w", newline="\r\n").write(LUA)
    open(os.path.join(ADDON_DIR, "Bindings.xml"), "w", newline="\r\n").write(BINDINGS)
    open(os.path.join(ADDON_DIR, "WowBotBridge_Data.lua"), "w", encoding="utf-8",
         newline="\r\n").write("\n".join(data) + "\n")
    return f"{len(quests)} quest titles, {len(spell_rows)} spell names"


def spell_id(name: str) -> int | None:
    rows = worlddb._query(
        f"SELECT MIN(entry) FROM spell_template WHERE build<={BUILD} AND name={_lua_string(name).replace(chr(34), chr(39))}")
    return int(rows[0][0]) if rows and rows[0][0] != "NULL" else None


DIGIT_KEY = {str(d): f"f{d}" for d in range(1, 10)} | {"0": "f10"}


class Bridge:
    """Send numeric commands to the WowBotBridge add-on with key presses."""

    def __init__(self, kb):
        self.kb = kb

    def send(self, cmd: int, arg: int = 0, wait: float = 0.25):
        # F12 runs the command in the client's own key handler, so the wait
        # is only for the game to apply it before the caller reads memory;
        # callers whose command opens a window or talks to the server pass
        # their own, longer wait
        self.kb.press("f11")
        for ch in f"{cmd:02d}{arg if arg else ''}":
            self.kb.press(DIGIT_KEY[ch])
        self.kb.press("f12")
        time.sleep(wait)


if __name__ == "__main__":
    if sys.argv[1:2] == ["install"]:
        print("installed WowBotBridge:", install())
    else:
        print(__doc__)
