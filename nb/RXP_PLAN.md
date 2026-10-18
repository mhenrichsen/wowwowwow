# Integration design: the bot follows RestedXP (RXP) instead of its own guide driver

Everything below comes from reading the code. Nothing was edited and nothing was tested in game. Paths:
- RXP = `C:\Program Files (x86)\World of Warcraft\_classic_beta_\Interface\AddOns\RXPGuides`
- BotEye = `...\AddOns\BotEye\BotEye.lua`
- nb = `C:\Users\MadsG\Desktop\wowbot\nb`

## 0. Three corrections to the reader reports, checked in the code

1. **No configurator window opens on Forever.** `UI/V2/GuideConfigurator.lua:2-5` returns early when `WOW_PROJECT_ID == WOW_PROJECT_CAMELOT`, so the call at `RXPGuides.lua:1809` does nothing. `addon.LoadDefaultGuide` is not defined in the forever DB, so `RXPGuides.lua:1792` does nothing either. A fresh character ends up with no guide and no popup. The engine reader was wrong here; the forever reader was right.
2. **`addon.defaultGuide` can point at the wrong starter route.** `GuideLoader.lua:202-203` sets it to the first guide registered with `#defaultfor`. Skyborne_03 loads first, so that is the Skyborne "1-6 Coldridge Valley", which has no shaman content. `GuideWindow.lua:2580` overwrites it later, but only when the right-click menu is built. Never use `defaultGuide`. Always load `"01-05 Coldridge Valley"` by name.
3. **RXP's trainer automation will most likely buy nothing on this client.** `OnTrainer` (`RXPGuides.lua:644-650`) reads the rank from the 5th return of `GetTrainerServiceInfo`. This client returns (name, state, icon), so the rank is nil, and `ProcessSpells` needs `GetSpellSubtext(id) == rank[id]` (`:600-604`). Keep BotEye's own buyer (`BotEye.lua:774-810`) until a live test proves otherwise.

## 1. First login: load the guide and set RXP up

### BotEye additions
Add `## OptionalDeps: RXPGuides` to `BotEye.toc` so RXP loads first.

**RXP accessor and on/off flag.** `BotEyeDB.rxp` is the feature flag, set with `/be rxp on|off`. The default is off.
```lua
local function RXPA()
    local A = LibStub and LibStub("AceAddon-3.0", true)
    return A and A:GetAddon("RXPGuides", true)
end
local function rxp_on() return BotEyeDB.rxp and RXPA() ~= nil end
```

**Settings, applied once per login out of combat.** RXP writes them to `RXPSettings` at logout. Each account (1#1 to 1#4) has its own copy, so each must be set once.
```lua
local function rxp_config()
    local R = RXPA(); if not R or not R.settings then return end
    local p = R.settings.profile
    p.enableQuestAutomation = true          -- accept / turn-in, gated on the active step (RXPGuides.lua:1076, :286)
    p.enableQuestRewardAutomation = true    -- use .turnin id,N rewards (:302-313)
    p.enableQuestChoiceAutomation = true    -- otherwise best upgrade or highest sell price (:975-1006)
    p.enableTips = true; p.enableItemUpgrades = true
    p.autoSellJunk = true                   -- InventoryManager.lua:792-856
    p.autoDiscardItems = false              -- deletes items, from input hooks only
    p.enableTrainerAutomation = false       -- rank match is broken (section 0); flip only after a live test
    p.enableGossipAutomation = true; p.enableFPAutomation = true; p.enableBindAutomation = true
    p.showEnabled = true; p.hideGuideWindow = false   -- isHidden switches off all automation (:1581, :2342)
    if (p.numMapPins or 0) == 0 then p.numMapPins = 7 end   -- arrow and goto completion need pins (map.lua:916)
    p.soloSelfFound = true                  -- hide #ah auction-house steps, as our parser did (AHCheck :2548)
    p.notifyOnTargetUpdates = false         -- stop chat spam that overwrites the strip text area
    if RXPFrame and not RXPFrame:IsShown() then RXPFrame:Show() end
    R.isHidden = false
end
```

**Guide selection.** Run at `PLAYER_ENTERING_WORLD` and retry every 2 s for up to 60 s. Embedded guides load during an OnUpdate (`RXPGuides.lua:1336-1397`), so they may not exist yet.
```lua
local RXP_GROUP = "RestedXP Forever Guide (A)"
local function rxp_pick_name(lv)
    if lv < 5 then return "01-05 Coldridge Valley" end
    if lv < 11 then return "05-11 Dun Morogh" end
    return "12-14 Loch Modan (Dwarf/Gnome)"
end
local function rxp_autoload(tries)
    local R = RXPA(); if not (BotEyeDB.rxp and R) then return end
    rxp_config()
    local g = R.currentGuide
    if g and not g.empty and g.group == RXP_GROUP then return end   -- already on our route (resumes from RXPCData.guideProgress)
    local name = rxp_pick_name(UnitLevel("player") or 1)
    if R.guides and R.guides[RXP_GROUP .. "||" .. name] then
        R:LoadGuideTable(RXP_GROUP, name)               -- GuideWindow.lua:1808
        note("I:rxp load " .. name)
    elseif (tries or 0) < 30 then
        C_Timer.After(2, function() rxp_autoload((tries or 0) + 1) end)
    end
end
-- in ev:SetScript OnEvent, PLAYER_ENTERING_WORLD branch (BotEye.lua:728-735):
--   C_Timer.After(5, function() pcall(rxp_autoload, 0) end)
```

**Slash subcommands.** These replace long `/be eval` calls (ev.py has a ~240-character limit and is slow). Put them in `SlashCmdList.BOTEYE`, `BotEye.lua:897-932`:
- `/be rxp on|off`: sets `BotEyeDB.rxp`, then runs `rxp_autoload(0)`.
- `/be rxp load <name>`: `R:LoadGuideTable(RXP_GROUP, name)`.
- `/be rxp next`: `R.SetStep(R.GetGuideProgress() + 1)` (`GuideWindow.lua:755`). This is the stall escape.
- `/be rxp skip`: on the current step, set `e.skip = true` for every element that is not completed and not textOnly, then `R.updateSteps = true; R.UpdateMap()`. Copied from `GuideWindow.lua:242-246`.
- `/be rxp accept <qid>` (0 clears): for 300 s, BotEye picks that quest's gossip offer and accepts its QUEST_DETAIL even while RXP owns the windows. RXP accepts only an active step's quests (`QuestAutoAccept`, `RXPGuides.lua:286-300`), so an escaped accept step's quest would otherwise never be taken. The bot sends it when a later step needs a quest that is not in the log (`rxp_follow._rxp_lost_accept`; inst3 179 at Sten, 2026-10-14).
- `/be rxp dump`: writes the current step's line, tags and quest IDs into `BotEyeDB.rxpDump` and the note area, for triage.

**Enabling RXP per character.** `RXPGuides: enabled` is only in the AddOns.txt of Shamae-Stone (1#1) and Shamag-Stone (1#3). For the others, either:
- edit `WTF\Account\<acct>\70\<char>\AddOns.txt` while the client is closed, or
- run `/be eval C_AddOns.EnableAddOn("RXPGuides",UnitName("player"))` in game, then reload.

## 2. What BotEye exports into the strip

### Free blocks
`eye.py` decodes blocks 0-5, 7, 9-14, 16-31, 32-79 (plates), 80-99 and 146-185 (quests), 100-111 (actions), 112-144 (text and counter), and 145 (tag). Blocks 6, 8 and 15 are not decoded (BotEye only clears 15 in `fill_target`), and 186-191 are free. That is 9 blocks, too few.

So add a 4th row:
- `NBLOCKS = 256`, at `BotEye.lua:10` and `eye.py:13`. Both must change together. `eye.py` computes `rows` and `bar_y0` from NBLOCKS (`eye.py:122-124`), and `layout()` does the same, so the bars move down 3 px in both.
- Block 192 is a tag. Without the tag, `eye.py` sets `state.rxp = None`. This is the same pattern as QUEST20_TAG (`eye.py:16-18`, `:202`).

### Row-4 layout
Painted by a new `fill_rxp`. Put it in the slow group (the 0.25 s tick) of `groups` at `BotEye.lua:482`, so it runs under `gi > 4`, and wrap it in pcall like the other groups.

| block | content |
|---|---|
| 192 | `RXP_TAG = 0x5A0F16` |
| 193 | Status bits: b0 RXP present, b1 guide loaded, b2 `R.isHidden`, b3 `enableQuestAutomation`, b4 `RXPG_ARROW:IsShown()`, b5 current step completed, b6 `R.loadNextStep`, b7 guide has no `next`, b8 `R.IsGamePadEnabled()`, b9 `inventoryManager:IsFeatureEnabled()`, b10 `UnitOnTaxi("player")`, b11 `enableTrainerAutomation`. Bits 12-23: step-change counter (mod 4096), bumped whenever (guide key, step index) changes. |
| 194 | guide ordinal (8 bits; index in a BotEye list `{"01-05 Coldridge Valley","05-11 Dun Morogh","11-12 Elwynn (Dwarf/Gnome)","12-14 Loch Modan (Dwarf/Gnome)","13-15 Westfall",...}`, 255 = other) + step index Ã— 256 (16 bits) |
| 195 | source line = `step.stepId - R.currentGuide.guideId` (16 bits; a line in the guide file, stable across race and class) + `#R.RXPFrame.activeSteps` Ã— 65536 (4 bits) + 2^20 if the step is sticky |
| 196 | Kind mask of the current step (not sticky; the step whose `index == R.GetGuideProgress()`), only incomplete, non-skipped elements: b0 accept, b1 turnin, b2 complete, b3 collect, b4 goto with radius, b5 wander goto (optional), b6 mob, b7 target, b8 use/usespell, b9 train, b10 trainer, b11 vendor, b12 home, b13 fp, b14 fly, b15 hs, b16 xp, b17 deathskip, b18 buy, b19 manual `+text` (no tag), b20 skipgossip/gossipoption, b21 unitscan, b22 any tag not in this list |
| 197 | Same mask, unioned over the sticky `#completewith` steps |
| 198, 199 | Bot waypoint X and Y in `coord()` encoding (`BotEye.lua:84`). X = `e.wy`, Y = `e.wx`, because HBD swaps them (`functions.lua:1910-1930`, HereBeDragons-2.0.lua:663). Choice: (1) the first incomplete goto with radius in the current step, else (2) the first wander goto that is not skipped, else (3) `RXPG_ARROW.element` if it has `wx`. |
| 200 | radius (8 bits, 255 = none) + `e.instance` Ã— 256 + flags Ã— 65536 (b0 from the current step, b1 from a sticky step, b2 from the arrow, b3 lowPrio, b4 dynamic) |
| 201-204 | Up to 4 quest elements of the current step: `questId` (20 bits) + tag Ã— 2^20 (1 accept, 2 turnin, 3 complete, 4 collect) + 2^23 if completed |
| 205 | active item: first itemId from `step.activeItems` of the current step, else the first shown `RXPItemFrameButtonN.itemId` (20 bits) + N Ã— 2^20 + 2^23 if `C_Container.GetItemCooldown` is ready |
| 206 | `train` element `.id` (20 bits) + 2^23 if `IsPlayerSpell(id)` |
| 207 | `xp` element level (8 bits) + #mobs Ã— 256 (4) + #targets Ã— 4096 (4) + #unitscan Ã— 65536 (4) |
| 208 | target-list counter: bumped on the AceEvent `RXP_TARGET_LIST_UPDATE` (`GuideWindow.lua:949`) and on any change of the polled `GetCurrentTargets()` |
| 209-223 | 45 characters of text, same 3-chars-per-block packing as `fill_text` (`BotEye.lua:473-480`): `"T:<first .target name>|M:<first mob>"`, for logs only |
| 224-247 | Up to 8 goto points of the current step in file order, 3 blocks each: X, Y, radius (255 = none) + flags Ã— 256 (b0 optional/wander, b1 completed or skipped, b2 hidePin) |
| 248 | Vendor `.collect` buy state (BotEye `buy_collect`; RXP's `.collect` never buys, functions.lua:2976-3244): item id (20 bits) + b20 a merchant this step sells it, b21 money short for all of it, b22 BotEye bought some, b23 bags full |
| 249 | Items of that `.collect` still needed (live, 8 bits) + unit price in copper × 256 |
| 250-255 | spare |

### Plate and target name bits
NPC IDs are lost in RXP (`CheckNpcIds`, `functions.lua:4915-4931`, keeps only names), so BotEye matches names:
- In `fill_plates` (`BotEye.lua:376-416`), the plate's first block is just `1` today, and `eye.py:197` overwrites sx/sy from the markers anyway, so its other bits are free. Paint `1 + 2*inMobList + 4*inTargetList + 8*inUnitscan`, compared against `UnitName(u)` with an `issecretvalue` guard.
- Names may be secret values in combat, so cache the result by NPC ID (`rxpIds[npcid] = bits`, reset when the step counter changes). This keeps the bits working in combat.
- Mob lists come from `R.targeting.GetCurrentTargets()` (`Targeting.lua:160`), with the `'*'` low-priority prefix stripped. `inTargetList` uses the current step's `target` elements (`e.targets`).
- Do the same for the current target: `t_flags` bit 21 = in mob list, bit 22 = in target list (`fill_target`, `BotEye.lua:241-259`). These fit; the highest bit used today is 20.

### eye.py
Add a `@dataclass Rxp` with fields:
- `present`, `loaded`, `hidden`, `qauto`, `arrow`, `done`, `taxi`, `gamepad`, `stepctr`
- `guide`, `index`, `line`, `kinds`, `skinds`
- `wp` = (x, y, radius, inst, flags), `quests` = [(qid, tag, done)]
- `item`, `item_btn`, `item_ready`, `train_id`, `train_known`, `xp_level`
- `tgtctr`, `text`, `pts` = [(x, y, r, fl)]

Decode it only when `v[192] == 0x5A0F16`. Add `rxp_mob` / `rxp_npc` / `rxp_scan` to `Plate` from `v[b] >> 1 & 7` (`eye.py:197`). Add properties `t_rxp_mob` = `t_flags >> 21 & 1` and `t_rxp_npc` = `>> 22`.

**Z coordinate.** RXP has none. Use `self.mv.nm.ground_z(x, y, hint=s.z)` (`navmesh.py:469`), or pass `z=None`, which `Mover.goto` already handles (`move.py:962-966`).

### Rare data via ev (`nb/ev.py`)
- `/be rxp dump`
- `tostring(RXPCData.currentGuideName)`
- `select(3,GetMacroInfo("RXPTargeting"))`

## 3. The new bot loop: "follow RXP"

### Switching
- New flag file `nb/RXP_MODE` containing "on" (same pattern as GUIDE_MODE / `read_mode`, `guide_plan.py:29-30`).
- When it is on, `Bot.__init__` (`bot.py:144-153`) skips `GuideDriver`.
- Main loop (`bot.py:2515`): call `if not (self.rxp_step() if self.rxp_on() else self.quest_step()): self.roam()`.
- `quest_step`, `GuideDriver` and the `QuestDB` planning stay in place, untouched, as the fallback when the flag is off.
- On start, `rxp_on()` sends `/be rxp on` once. On stop, nothing changes.

### `Bot.rxp_step()`
New file `nb/rxp_follow.py`, mixed into Bot. One decision per tick:
```text
r = s.rxp
if r is None or not r.present: log once, return False          # old addon or no RXP: grind
if not r.loaded: keys.chat("/be rxp on"); wait 5 s; return True
if r.hidden: keys.chat("/be rxp on"); return True               # rxp_config re-shows RXPFrame
if r.taxi: sleep 1; return True                                  # .fly in progress
stall check: same (r.guide, r.line) for longer than BUDGET[kind] (as guide_plan.STUCK_MIN, :55)
    -> log "rxp stall <guide> L<line> kinds=..." then /be rxp skip, then /be rxp next on a 2nd stall
policy check: (guide, line) in rxp_policy.SKIP or JUMP -> /be rxp next, or /be rxp load <name>
K = r.kinds
1. TALK if K & (accept|turnin|vendor|trainer|train(not known)|home|fp|fly|buy|skipgossip):
     travel_to(wp.x, wp.y, ground_z, tol=4, abort=combat)
     target: press PAGEUP (BotEyeRXPNpc: /targetexact <npc>), verify s.t_rxp_npc; fallback is the
             F-key of a plate with rxp_npc set (target_npc logic, bot.py:199-211)
     approach until t_within10 (bot.py:1951-1956), press M (INTERACTTARGET)
     wait until every window is closed (the talk() wait loop, bot.py:1958-1976): RXP picks quests and
             rewards, BotEye picks the trainer/vendor option from the step kind (section 4), then close_windows()
             (the .vendor / .trainer elements complete on MERCHANT_CLOSED / TRAINER_CLOSED)
     success = r.stepctr changed, or a quest element flipped to done
2. USE if K & use and r.item: travel to wp; if the step also has a target, PAGEUP first; press INSERT
     (BotEyeQuestItem, now fed from RXP); wait for the cast to end.
     The .hs step lands here too: 6948 is in activeItems (functions.lua:2577-2586).
3. KILL/COLLECT if K & (complete|collect|mob|xp|unitscan):
     self.wanted = {p.npc for p in plates if p.rxp_mob} | self.rxp_ids (learned, reset on stepctr change)
     pts = [(x, y, ground_z) for x, y, r, fl in r.pts if not fl & 2] or [wp]
     self.roam(pts)   # the existing roam, pick_target, fight and loot do the work (bot.py:1612, 604, 705, 1193)
     name_target (bot.py:1572) keeps working: BotEyeFind is now filled from mobList, so no "/be find" chat
     complete or collect with no mobs (an object to loot): ask self.qdb for that quest's object data and call
             gather_object / use_object (bot.py:2068, 2147). quests.py stays as an object knowledge base, not a planner.
     HUNT_WAIT / shelve logic (bot.py:2388-2406): on timeout, /be rxp skip instead of qdb.shelve
4. GOTO if K & goto: travel_to(wp, tol=max(1, 0.6*radius)); RXP completes it within the radius (map.lua:1322-1331)
5. deathskip (sticky softcore steps, DwarfGnome.lua L692, L1322): ignore. They are #completewith next,
     so the next step finishes them. If one is the current step, call /be rxp skip.
6. manual "+text" only, or b22 unknown: wait 20 s (the sticky companions may clear it), then /be rxp skip and log it
return True
```

### Modules that keep running unchanged
- fight, rotation, imbue, buffs: `bot.py:705-930`, `rotation.py`, `1746-1786`
- rest: `1240`
- loot: `1086-1239`
- death: `on_death`, `1391`, plus the BotEye RepopMe/RetrieveCorpse ticker at `BotEye.lua:937-954`
- danger avoidance: `2490-2496`
- movement: `move.py`, `navmesh`
- pad claim
- `equip_upgrades` and the bind-confirm handlers: `BotEye.lua:757-763`
- `RepairAllItems`: `BotEye.lua:773`. RXP has no repair.
- `vendor_step` (`bot.py:1788`): keep as the bags-full / repair fallback. RXP sells while the window is open.

### Training
- `train_step` (`bot.py:1834`): in RXP mode, run it only as a catch-up, i.e. when `trainers.is_due` and the level is at least 2 past `trainers.due_level`. The guide has its own trainer steps (DwarfGnome.lua:776-780, 1378-1382), and they go through TALK (item 1 above).
- `/be want train` is no longer needed. BotEye derives the wanted option from the step (section 4).

### Retired, but kept on disk until the RXP mode is verified
- `guide_plan.py`: `GuideDriver._decide` (`:212-251`), `GUIDES` (`:33`), `ITEM_KEYS` (`:53`), the coordinate conversion (`:344-357`)
- the `quests.py` planning (`QuestDB.plan`)
- `track_handins` writing to `qdb.done`
- in RXP mode, BotEye's quest handlers (section 4)

Keep `BLOCKED` and `LEVEL_GATE` (`guide_plan.py:36-50`) as data. Move them into a new `nb/rxp_policy.py`:
- `SKIP_QUESTS = {97277, 86614, 384, 308, 314}`. For these the bot calls `/be rxp skip` whenever a quest element of the current step names them. (Writing them into `R.disabledQuests` would only stop the automation, not finish the step.)
- `SKIP_NPC_STEPS`: the Eldrun Stormbreaker step (05-11 Dun Morogh, L2297-2302; the NPC has no spawn here).
- `JUMP`: at "05-11 Dun Morogh" around L2280 (the flight to Ironforge, then the Deeprun Tram L2489-2597, then Stormwind, then Elwynn) run `/be rxp load 12-14 Loch Modan (Dwarf/Gnome)`. This stays until taxi and tram handling exist. It keeps the walkable route the old `GUIDES` list chose.
- `LEVEL_GATE`: when a quest element's qid is gated and the character is under that level, the bot grinds at the step's points instead (this is a guard in the bot, not in RXP).

## 4. BotEye changes and key bindings

### Handlers gated on `rxp_on()`
In `ev:SetScript`, `BotEye.lua:750-813`:
- **QUEST_DETAIL, QUEST_ACCEPT_CONFIRM, QUEST_PROGRESS, QUEST_GREETING (751-755, 764-769):** return when `rxp_on()`. RXP owns them (`RXPGuides.lua:1164-1219`).
- **QUEST_COMPLETE (756):** when RXP is on, schedule `C_Timer.After(1.0, ...)`. If `QuestFrameRewardPanel:IsShown()` at that point, call `GetQuestReward(best_reward())`. This covers the cases RXP leaves open (no upgrade, quest not in the step).
- **gossip() (657-725):** when RXP is on, skip the quest-picking blocks (664-703) but keep the spirit-healer block (659-663). Defer the option picks by 0.3 s and only act if `GossipFrame:IsShown()` is still true, so RXP's GOSSIP_SHOW quest choice wins. Derive `want` from the current step's kinds:
  - train/trainer â†’ trainer option (icon 132058)
  - vendor/buy â†’ vendor option (132060)
  - home, fp, fly â†’ nothing; RXP picks those itself
  - otherwise keep the bags/durability "browse" rule (712-714)
  - With RXP on, `train_due()` (649-655) is not used for the gossip pick.
- **MERCHANT_SHOW (771-773):** keep `RepairAllItems`. Call `sell_junk()` only if `R.IsGamePadEnabled()` is true or `not R.inventoryManager:IsFeatureEnabled()`. RXP refuses to sell when the gamepad is on (`InventoryManager.lua:795`), and `BotEyePadClaim` sets `GamePadEnable 1` (`BotEye.lua:1061`), so this fallback probably matters.
- **TRAINER_SHOW `buy_all` (774-810):** keep as long as `enableTrainerAutomation` is false. When the bot's trainer test passes, set it true and make BotEye skip `buy_all`.

### Keys
Create bindings once in `BotEyeSetup` (`BotEye.lua:828-875`), which runs out of combat. Only attributes change afterwards.
- **NUMPADMULTIPLY â†’ BotEyeFind (`:838-839`):** OnUpdate rebuilds its macrotext from `mobList` as `/targetexact <name>` + `/stopmacro [harm,nodead]` lines, at most 6 (same format as `/be find`, `:909-921`). Do it in the existing `macro_frame` tick (`:881-894`) under `if InCombatLockdown() then return end`, and only when the list changes. This retires `bot.py:1574-1581` and `1932-1935`.
- **PAGEUP (new) â†’ `secure("BotEyeRXPNpc", {type="macro", macrotext=""})`:** macrotext = `/targetexact <first current-step .target name>`. Test this key first: HOME and END overrides do not fire on this account (`BotEye.lua:934-936`). Fallbacks in order: DELETE, PAGEDOWN, then reusing NUMPAD9 (nameplate 20, BotEye only paints 12 plates).
- **INSERT â†’ BotEyeQuestItem (`:851`):** set `macrotext = "/use item:"..id` out of combat from block 205's item. This replaces the hard-coded `/use Earth Sapta`. Do not bind the `RXPItemFrameButtonN` buttons directly: their order follows `GetActiveItemList` (`ActiveItemFrame.lua:45-158`) and hidden buttons keep stale attributes (`:497`).
- **M â†’ INTERACTTARGET:** unchanged (`:857`).
- **Never bind** `CLICK RXPInventory_DeleteJunk`. It destroys an item (`InventoryManager.lua:85-86`).
- The `RXPTargeting` macro is not needed. Its stale body when lists are empty (`Targeting.lua:178`) and the missing `[harm,nodead]` stop make BotEyeFind the better choice.

### Combat lockdown
- `SetAttribute`, `SetOverrideBinding*` and `EditMacro` fail in combat. So BotEyeFind, BotEyeRXPNpc and BotEyeQuestItem keep their pre-combat content until `PLAYER_REGEN_ENABLED`. The same holds for RXP's own frames (`Targeting.lua:180`, `:1128`; `ActiveItemFrame.lua:378`).
- The bot must not trust `s.rxp` name-derived keys for about 0.5 s after combat ends. It checks `t_rxp_mob` / `t_rxp_npc` after every key press.
- Pressing a bound key in combat still works, with the old content.

### Modifiers
- A held Ctrl disables all quest automation (`RXPGuides.lua:1076`).
- Shift disables bind, flight and gossip steps (`functions.lua:2664, 2848, 5684`).
- So never type chat (`/be ...`; capitals use Shift) while an NPC window is open, and never post Ctrl.

## 5. Risks and implementation order

### Risks
1. `isHidden` (frame hidden, or the loading screen at `RXPGuides.lua:1816`) silently stops everything. Bit b2 is exported so the bot reacts.
2. Junk selling is off when the gamepad is enabled; BotEye's fallback covers it.
3. Trainer rank mismatch (section 0); BotEye buys.
4. Server gaps (97277, 86614, Eldrun 258098) and quests missing on the TC server leave elements stuck. The policy table and the stall budget cover them.
5. The RXP route after 05-11 needs a flight, the tram and Elwynn; `JUMP` keeps the walkable route.
6. `RXPCData` is saved only at logout or `/reload`. After a client crash, RXP resumes at an old step; accept and turnin elements auto-complete from the quest log, so it catches up.
7. The arrow can point at a sticky step's waypoint (map.lua:1023-1146). Block 198 prefers the current step's own gotos.
8. Step indices differ per character; log the source line (block 195) instead.
9. Event order between BotEye and RXP on the same event is not measured. The gating in section 4 removes the race.
10. The strip grows by 3 px; BotEye and eye.py must be deployed together. The tag protects a mismatch.
11. Name matching uses `issecretvalue` guards and an NPC-ID cache.

### Implementation order and tests
1. **Live, read-only probe** (one character with RXP on, ev.py):
   - `C_AddOns.IsAddOnLoaded("RXPGuides")`, `RXP.game`, `RXP.isHidden`
   - `GetCVar("InputDeviceInterfaceStyle")`, `C_GamePad.IsEnabled()`
   - `GetSpellSubtext(8017)`
   - `select("#",GetTrainerServiceInfo(1))`, with the trainer open
   - `RXPFrame:GetPoint()`, to check it does not cover the play area the bot needs
2. **BotEye part 1:** accessor, `/be rxp` commands, `rxp_config`, `rxp_autoload`, all behind `BotEyeDB.rxp`.
   - Live test: `/be rxp on` on a fresh level-1 shaman. Then `RXP.currentGuide.name` must be "01-05 Coldridge Valley", and RXP must show step 1 (Sten, `.accept 179`).
3. **BotEye part 2 plus eye.py:** row 4, plate and target bits.
   - Offline: `nb/test_rxp_eye.py` decodes synthetic `v` lists (with tag, without tag, edge coordinates, plate bits).
   - Live: `python -m nb.eye` prints `rxp`. At Sten, check `wp` â‰ˆ (-6214.85, 328.18), `quests` = [(179, accept)], `T:Sten Stoutarm`.
4. **BotEye part 3:** handler gating and the dynamic keys.
   - Live, by hand, bot stopped: press PAGEUP then M at Sten. RXP must accept 179 and the step must advance (stepctr changes). Open Grundel's vendor and check RXP sells or the BotEye fallback does. Check the reward choice on a multi-choice quest.
5. **bot.py:** `rxp_step`, `rxp_follow.py`, `rxp_policy.py`, the `RXP_MODE` flag.
   - Offline: unit-test the decision order with fake `State.rxp` objects (style of `test_search.py`).
   - Live: Coldridge from level 1 to 5 on one character, with `idle_probe.py` on (dead-time memory). Watch the log for `rxp stall` lines and compare the time per level with the old runs (old_run4).
6. **Rest of 05-11 Dun Morogh:** extend the policy table from the triage of the stall logs. Keep training on the BotEye buyer.
7. **Retire the old path** after 2 clean runs: stop importing `guide_plan` and the `QuestDB` planning when the flag is on. Keep the files and `GUIDE_MODE` as a rollback. Then roll out per account, which needs RXP enabled in every character's AddOns.txt and `rxp_config` run once per account.