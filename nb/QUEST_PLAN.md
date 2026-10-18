# Plan: make every route quest doable (nb, Dun Morogh / Loch Modan route)

The diagnoses hold up against the DB and the code, with one correction (218) and a few points that need a live check, listed under "Doubts" below. One finding matters for the user's premise. **About 20 route quests cannot be done on this server until world-DB rows are added**, whatever the bot does. Examples: Fire Tar for 94466 drops only in Kalimdor (confirmed); 86614, 86667 and 96629 have no `quest_template` row; Earthseer Farsen does not exist. Those rows are track **S** below. Each one needs the user's OK, because this task forbids server changes.

---

## Order (XP unlocked per effort)

| # | Package | Effort | Quests it unlocks (bot side) |
|---|---|---|---|
| 1 | WP1 Quest strip: 20 quests | S | Precondition for everything below (see WP1) |
| 2 | WP0 Block list / level-gate cleanup | XS | 5541, 413, 414, 432, 433, 319 (earlier), 419 |
| 3 | WP2 Objective model in quests.py | M | 97277 counters, 417, 384 (skill), 287/98319/96608 data, 311/403 starters |
| 4 | WP3a New actions: buy / sit / explore-visit / object starter | M | 384, 96608, 287, 291 (after 287), 417, 98319 (with S) |
| 5 | WP4a Ironforge walk areas and stuck budget | S-M | 291, 6391, 6388, 6392 (+ Brock path), 6387 |
| 6 | **S** Server data patches (user OK), in parallel | S per item | 97277, 98321, 98319â†’98323, 310/311/308/403, 95213/95214, Farsen chain (96408/96392/96390/96391/96393), Call of Fire 94449-94468, 96629 |
| 7 | WP4b Hard nav: 312 MacGrann climb + Icebeard sneak, 314 Vagash | M | 312, 314 |
| 8 | WP3b Gossip option, item-start quests, use-item-on-target | M | 96392, 95213, 96391, 6661 (with WP5) |
| 9 | WP5 Multi-map / Deeprun Tram | L | 1338, 6661, 6662, 353, 399 (399 also needs level 15) |
| â€” | No data to rebuild from | â€” | 86614 (no quest data anywhere), 86667 (needs a sniff or a hand-authored quest), 96393 (completion is a map-3065 dungeon boss: accept only) |

---

## WP1 Quest strip: report 20 quests (precondition)

**Problem:** BotEye `fill_quests` stops at 10 quests (BotEye.lua L419-451, `if k >= 10`), and eye.py L192-197 reads only 10. With the unblocks the log holds 11-12 quests (route steps around 162-188). Then `nquests > len(lqs)` in guide_plan.py L168 sends **every tick** to the planner, which turns the guide off. `bot.note_objectives` and the `quest_step` guard at L2254 react the same way.

**Design:**
- Blocks 145-159 are unused, and the strip already draws 3 rows of 64. Raise `NBLOCKS` from 160 to 192 in BotEye.lua L10 **and** eye.py L13. The image needs no new rows.
- Put quests 11-20 at `146 + 2*(k-10)` (2 blocks each, same encoding as now).
- In eye.py, loop `range(20)` with a base of 80 for k<10 and 146 for k>=10.

**Test:**
- Offline: decode a synthetic 192-block image.
- Live: `/reload` out of combat, check `len(s.quests) == s.nquests` with 12 or more quests in the log.

---

## WP0 Block list and level-gate cleanup (guide_plan.py L36/L44, quests.py L27)

| Change | Unlocks | Note |
|---|---|---|
| Take 413 out of `BLOCKED` (no reason is given for it) | 413, 414 | min 8, so the guide's own "QA 413 needs level 8: grind" applies |
| Take 432 and 433 out of `BLOCKED`; add `LEVEL_GATE[432]=8`, `[433]=9` | 432, 433 | quest_poi limiting is already in place (see overlap) |
| Take 5541 out of `SKIP` | 5541 | the data path is complete |
| Take 419 out of `SKIP` | 419 | object ender 2059 goes through `use_object`. Also: in `use_object` (bot.py L2033-2056), wait until all dialogs have been closed for 0.6 s before `close_windows()`, and never add the object to `skip_go` when a quest was *gained*. Otherwise the auto-offered 417 is closed before it is accepted |
| `LEVEL_GATE = {313: 8}` (drop 319) | 319 at step 45 instead of a second trip | no deaths near these mobs |
| Leave 384, 308, 314, 97277 for now | â€” | they come out in WP3a, WP4b and S |
| Add 86614 to `BLOCKED` with the comment "not on server" | â€” | documentation only |

**Test:** an offline `GuideDriver._decide` sweep (rq5/rq7 harness) at levels 5-10, with fake logs. Each listed QA/QC/QT must return an `act`.

---

## WP2 Objective model (quests.py)

1. **Counter index per objective (fixes 97277).** `_add_server_quests` L188 already sorts by StorageIndex. Change that to the server's order, `ORDER BY QuestID, \`Order\`, StorageIndex` (ObjectMgr.cpp L4897). Store `q['idx'] = {('kill'|'item'|'special', obj): i}`, where i is the 0-based position in that order (the client's `GetQuestObjectives` order, which BotEye packs into `counts`).
   - Mangos quests keep a kill-first default.
   - Use `q['idx']` in `_targets` L342-346, in plan()'s chest loop L430, and in `bot.gather_object` (replacing `len(kill)+k`).
   - On the route, only 97277 (item, then kill), 6661 (kill, then provided item: fine) and 96608 have mixed or special objectives.
2. **Special objective types.** Load types 14 (criteria tree) and 19 (areatrigger enter) into `q['special'] = [(type, ObjectID, idx)]`. A quest with `special` set is **not** talk-only (plan() L401). Map them to plan kinds:
   - 96608 â†’ `sit`
   - 98319 â†’ `visit`, at the guide spot (-5506.3,-312.5,352.5)
3. **Explore triggers (287).** `self.explore[q] = [(id, pos, r)]` from `tcf_world.areatrigger_involvedrelation` joined to the trigger positions (mangos `areatrigger_template` / AreaTrigger.db2). For 287, keep **only 97**: trigger 169 sits on the elite Arctikus.
   - plan() step 1: when all kill/item counters are 255, the quest is not complete and it has triggers, return `('explore', qid, pos, r, why)`.
   - Confirmed in MiscHandler.cpp L571: `QUEST_FLAGS_COMPLETION_AREA_TRIGGER` (0x4) â†’ `AreaExploredOrEventHappens`.
4. **Object starters.** Load `self.go_starters` from tcf `gameobject_queststarter` (269â†’403, 270â†’311, 2059â†’417, all confirmed) plus the mangos `gameobject_questrelation`.
   - Add `OR q.ID IN (SELECT quest FROM gameobject_queststarter/questender)` to the `_add_server_quests` WHERE at L174.
   - In `GuideDriver._action` QA (L282-292): when there is no creature starter, return `('use', go, pos, ...)`.
5. **Skill check.** Load `RequiredSkillPoints` (tcf addon) / `RequiredSkillValue` (mangos). `eligible()` L293 then rejects only when the skill value is above 0. 384 is 185/0 (confirmed), so it passes.
6. **Provided items.** Drop item objectives equal to `quest_template.StartItem` / `ProvidedItemCount` (94465, 94467, 6661's flute, deliveries), so no drop source is looked for.
7. **Vendor sources.** `self.vendor_src[item] = [(npc, pos)]` from `npc_vendor`, for items with no loot and no object source. 2894 Rhapsody Malt and 2686 Thunder Ale are sold by Belm 1247.
8. **Autocomplete quests.** `q['auto'] = QuestType == 0` (308 and 403 confirmed). These never enter the log.
9. **Rare/elite filter.** Load `creature_template.Classification`. `_usable` drops Classification in (1, 2, 4) when `mob_hi > level`. This keeps Edan 1137 (L9 rare) out of 313 at level 8.
10. **Log the silent level skip.** When `_usable` empties every objective, log `plan: <q> all objective mobs above level+1` once per level. This is diagnostic only; the behaviour is already a deferral.

**Test:**
- Offline: build the QuestDB and assert `idx[97277] == {('item',277976):0, ('kill',269075):1}`; `special[96608]` has 2 entries; `explore[287] == [97]`; `go_starters[417]`; `eligible(384, 6)`.
- Re-run rq5 to get the new verdict totals.

---

## WP3a New actions (bot.py quest_step L2276-2290, guide_plan `_action`/`_complete`, BotEye)

| Kind | Quests | Design |
|---|---|---|
| `buy` (npc, pos, item, n) | 384 malt, 308 ale | `Bot.buy_item`: `travel_to`, `target_npc`, `/be want vendor`, interact. BotEye MERCHANT_SHOW (L766) gets a `BotEyeDB.buy={[item]=n}` honour loop (`GetMerchantItemID(i)==id` â†’ `BuyMerchantItem(i,n)`). Success = the item is in the bags (needs a bag item bit, or watch `s.msg`). Emitted by plan()/`_complete` from `vendor_src` when the item counter is not 255. Remove 384 from `BLOCKED`. |
| `sit` (pos, secs, qid) | 96608 | `travel_to` within 4 yd of fire 529161 (-5645.0,-496.7), stop, `keys.chat('/sit')`, wait 2 s, `/sit` again (the text emote with aura 1289723 up), then hold still with no eat/rest/turn for up to 75 s or until `counts` for both special indexes are 255. Abort on combat. Add `STUCK_MIN['sit']=5`. |
| `explore` / `visit` (pos, r, qid, idx) | 287, 98319 | `travel_to(tol=min(6, r-3))` with indoor walking allowed. Wait up to 3-5 s for `lq.complete` or `counts[idx]==255`, then step to the exact centre and retry once. Add 287 to `INDOOR_QUESTS` (bot.py L62). While `qdb.focus==287`, the `run()` danger rule skips non-elite plates up to `level+2` (Arctikus 1260 still triggers it). 98319 also needs S3. |
| `use` as a *starter* | 417, 311 | Reuse `use_object`. Success = `after - before` is not empty (a quest was gained) **or** a quest was handed in. |
| `autoquest` | 403 â†’ 308 â†’ 310 â†’ 311 | Sequenced errand, only after S5: (1) `use_object(269)`, success = the "completed" system message in `s.msg`, add 403 to done; (2) `buy` 2686; (3) `talk(1373)`, success = the ale leaves the bags, stamp `qdb.jarven_t`; (4) within 8-49 s, `use_object(270)`, success = 310 is gone or 311 is gained. Take 308/310/311/403 out of `SKIP`/`BLOCKED` and drop `skip_go={270}`. |

**Test:** offline plan sweeps return the new kinds. Live: one bot per action (96608 and 384 are both in Kharanos).

---

## WP4a Ironforge walk areas and stuck budget

- quests.py: add `ROUTE_AREAS = [(-4800,-1100,350)]` (Ironforge only, for now). `npc_pos(npc, near, route=False)` accepts those circles only when `route=True`. guide_plan QA L286 and the QT `plan(only=...)` talk path pass `route=True`. The free planner and the hunt spawns stay home. Unlocks 291 (after 287), 6391, 6388, 6392.
- Vagash: widen `AREAS` or add patrol nodes (WP4b).
- Stuck budget (guide_plan L234-240): add `round(path_dist_to_goal/50)` to the fingerprint, so a 3.6 km walk (Thelsamar â†’ IF) counts as progress.
- Brock 1681 (6387, 6392):
  - Hand-checked entry path in move.py, used when the goal is within 6 yd of (-5366.9,-3014.9). Points (from the 6392 diagnosis): (-5350.1,-3018.7,326.7) â†’ (-5352.9,-3019.4,326.3) â†’ â€¦ â†’ (-5366.7,-3015.4,319.6). Plus a gate for the way out.
  - `move._path` goal-floor fallback (`floors_at`) for exact z values that have no poly.
- Safety: put the Searing Gorge door (-6034.97,-2488.73) r=8 in the navmesh holes, so no map-0 Stormwind goal plans the 12 km route through level 40-55 zones.

**Test:** offline `find_path` from Thelsamar to Golnir/Gryth/Barin, plus a `_decide` run with 287 done. Live: one IF trip (budget about 10 min).

---

## WP4b Hard navigation

- **312 MacGrann:**
  - Add `CLIMB_OK = {(((13,19),2647),((13,19),2654))}` in `navmesh._find_path`, which skips the `MAX_CLIMB` and `blocked_climbs` tests for that pair (rise 1.02 against a 1.0 cap).
  - Add a `GUARDS={272:(1271,...)}` sneak in `gather_object`: stage at (-5660,-22,416); go when Icebeard's x > -5590 (read through `/be find` plus `t_x`); about 12 s each way.
  - `gather_object`'s avoid test uses death zones only.
  - Remove 312 from `SKIP`.
  - Walk the climb once live.
- **314 Vagash:**
  - Load `creature_addon.PathId` â†’ `waypoint_path_node` points as hunt points. Without this the single spawn is out of `AREAS` and indoors in vmap; 5 of the 17 nodes are outdoors and inside `AREAS`.
  - Replace `BOSS_MIN_LEVEL` with a per-boss table `BOSS_MIN={808:6, 1388:10}`; add 1388 to `BOSS_NPCS`.
  - Add the Mountaineer (-5669.7,-1289.8) to `GUARDS`.
  - `LEVEL_GATE[314]=10`; remove 314 from `BLOCKED`.
  - Note: RXP says "51% damage for loot", so the guard kite must not let the guard take the kill.
- **Call of Fire trails** (after S): move.TRAILS entries for the Kindleborn ridge (-5482.7,-2603.6) â†’ (-5440.0,-2603.6,501.9) and the Ashmantle climb (-5629.1,-2917.7,413.8) â†’ (-5652.0,-2897.0,430.2). Steps are about 47-48 degrees, so run a terrain slope check and one GM walk first.

---

## WP3b Gossip, item-start quests, use-on-target

- **Gossip option (96392):** take `gossipoption` out of rxp IGNORED and parse it as a `GOSSIP` action. guide_plan returns `talk` with a `want` string, and BotEye `gossip()` already matches `st.want` against option names (L713). Then ESC to drop aura 1293681.
- **Item-start quests (95213 keg 268548, 96391 map 274268):**
  - QA fallback: when the quest has no starters and its item (ItemSparse `StartQuestID`) is in the bags, press `ITEM_KEYS[item]`; otherwise hunt the droppers.
  - The `/be use` macro is defined but **unbound** (BotEye L835), so each item needs a new `keymacros` entry with a free key (BotEye L839-846). Or bind BotEyeUse to one key and set `/be use <name>` before each press. That is better: one key covers every item.
  - Also have `_use_item` (L309) take the quest from the next step's QC when `requires` is empty (needed for 94467's Fire Sapta).
- **Use item on target (6661):** a new objective type from smart_scripts (SPELLHIT spell S on creature E, linked to KILLEDMONSTER C) plus the item whose spell is S: 17117 â†’ 21050 â†’ 13016 â†’ credit 13017. Kind `useon`: target with `/targetexact Deeprun Rat`, close to 4 yd or less, use, wait 1.3 s. Needs WP5.

---

## WP5 Multi-map / tram (1338, 6661, 6662, 353, 399)

- `QuestDB.spawns_map` for map 369.
- A `NavMesh` per map, chosen by `s.inst` (already in the strip): r3 has 369 tiles. The 399 diagnosis says data_r4 has them and the 1338 diagnosis says it does not. Check before building.
- Tram leg ported from wowbot/bot.py `TRAM` / `ride_tram` (L2787-2790, L3492-3522): walk the track bed rather than boarding the cars.
- Force the tram for any trip with x below -7500 vs above -6500.
- Stormwind `ROUTE_AREAS` circle (-8400,600,250), used only when on that side.
- 399 also needs `LEVEL_GATE[399]=15` and Westfall; it is effectively outside the 3-14 route.

---

## Track S: server data patches (each needs the user's OK; no server changes were made in this pass)

Order by value:

1. **Call of Fire 94449-94468** (the shaman Fire Totem quest chain).
   - Create 258098/257808 and spawn 257597; npcflag fixes on those NPCs.
   - Starter/ender rows; `AllowableClasses 64`.
   - **Fire Tar 5026 on Tunnel Rat Geomancer 1174.** Confirmed: tcf has 5026 only on Razormane 3267/3268/3269/3271/3458 (Kalimdor). Also add 94466 to `INDOOR_QUESTS`.
   - A 5893 manifestation spawn and a brazier GO (gameobject_questender 94467 / queststarter 94468) at the Loch Modan shrine.
2. **98321:** `gameobject_loot_template (142903, 280413, 100, QuestRequired 1)`. Confirmed 0 rows today. Bot side: in plan() step 2b, wait the spawn's own respawn (120 s + 10) instead of the fixed 600 s.
3. **Farsen chain** (96408, 96392, 96390, 96391, 96393): create the Farsen NPC at (-5797.83,-1394.24,438.2), plus starter/ender rows, the gossip/SAI credit for 96392, and the 274268 drop on 6123.
4. **95213/95214:** 268548 and 267415 loot on 1116, plus Thesten 1256 starter/ender rows.
5. **310 Jarven SAI:** event 20 (quest 308) â†’ action 70 ENABLE_TEMP_GOBJ on 270, and action 41 despawn on 269. See Doubts for the targeting choice.
6. **98319** areatrigger 44117 template, create properties and spawn (r 8 at the corpse); **98323** starter 271546 and ender 1252.
7. **97277:** 2756 â†’ npcflag 2 and a client-known display (1774). Name it 'Grund Drokda' or keep it.
8. **96629:** clone 96626 (Eric 265813 â†’ Gremlock 1699). It needs a `train` action if it gets a "learn" objective.
9. **86667:** needs a sniff or a hand-authored quest. **86614:** nothing on disk to rebuild it from; leave it blocked.

**Test:** apply as one custom SQL file, restart the worldserver out of combat (per memory). Re-run rq3/rq5 to confirm each quest loads with starter and ender.

---

## Overlap with the concurrent agent's edits (re-check these; do not redo them)

- **gameobject_questitem join:** `quests._add_questitem_objects` L242-254 is already present. WP2 #1 changes the `gather_object` / plan L430 index logic on top of it. Also add the per-spawn respawn wait for 98321.
- **creature_loot_template chances:** `_tcf_loot` L226-240 and the level+1 filter in `_usable` L368. WP2 #9 (Classification) and #10 (log line) edit the same function.
- **quest_poi spawn limit:** `_load_poi` L256 and `_spawns_for` L358 (`POI_MARGIN`). WP0's 432/433 and WP4b's Vagash patrol points rely on it. Patrol points must go through the same poi filter.
- **2160 unblocked:** the `SKIP` comment at L27 already reflects it. WP2 #6 (provided-item filter) generalises the 2160 fix: check it does not double-filter.
- **218 unblocked:** already out of `BLOCKED` (guide_plan L37-40 comment), with `BOSS_MIN_LEVEL=6`. WP4b's per-boss `BOSS_MIN` replaces this constant, so keep `808:6` unless the user wants 5.
- `_add_server_quests` L165-224 is shared ground: re-read it before WP2 #1/#2/#4.

---

## Doubts and corrections after spot checks

- **218 does not idle for 25 minutes.** When `can_hunt` is False, `_complete` returns None (guide_plan L370), so the route moves on and comes back at level 6. The real cost is a backtrack to Coldridge, not idle time.
- **96608 counter positions:** the objectives have `Order` 0/1 but `StorageIndex` 2/3. BotEye packs `GetQuestObjectives(qid)[1..3]`. Check live with `/be eval` whether criteria-tree objectives appear in that array, and at which positions, before relying on `idx`. The double-`/sit` text emote is also unproven: `/sit` while already seated may toggle stand state or send no emote.
- **291 vs 287 contradiction:** the 291 diagnosis says unskipping 287 is enough, but 287 has flag 0x4 COMPLETION_AREA_TRIGGER, so it **also** needs the explore at trigger 97 (WP2 #3 / WP3a).
- **Brock 1681:** the 6391 diagnosis says the floor may be at z 329.9; the 6392 diagnosis says a sunken room at z 319.6 behind a 2.9 yd doorway. Check live before writing the entry path.
- **310 SAI fix:** target 14 uses `FindGameObjectNear(Jarven, guid)`, so the unspawned barrel (spawntimesecs -60) must exist in the grid. This needs a live test. This source also has `SMART_TARGET_CLOSEST_UNSPAWNED_GAMEOBJECT`, which is the safer target for action 70.
- **Map-369 mesh:** the 399 diagnosis says data_r4 has tram tiles; the 1338 diagnosis says r4 has 0 files and r3 has 13. Check before WP5.
- **No other ordering bug:** among the route quests, only 97277 has an item objective before a kill objective (checked with a DB scan of mixed/special objectives). No route quest has more than 3 objectives, so 3 counters are enough.
- **The user's premise does not hold as the DB stands:** 94466 (Fire Tar), 86614, 86667, 96629, the Farsen chain, 95213/95214, 98321, 98319/98323, 97277 and 310/311 all need track S. 399 needs level 15 and Westfall, and 96393 is a dungeon quest.

---

## Test approach (every package)

1. **Offline:** a scratch harness (rq5/rq7 style) imports quests/guide_plan, builds QuestDB against live tcf_world, and runs `GuideDriver._decide` for levels 3-14 with synthetic logs (`eye.Quest` with counts). Assert the expected action kind for each step of each target quest. Then `navmesh.find_path` for every new goal or trail, and a diff of the verdict totals against the baseline (OK 30 / BLOCKED 17 / BAD_DATA 16 / UNREACHABLE 8 / NO_SOURCE 2 / UNSUPPORTED_OBJECTIVE 2).
2. **Live, later, with the user's go-ahead:** one bot per package (Shamaa, Shamab or Shamac). Restart only out of combat, run `idle_probe.py`, and grep nb.log for `guide: step .* skipping` and `could not target`. A package is done when its quests show up in `quests_done.json`.