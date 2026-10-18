# What nb took from polestar

Source: https://github.com/fitzforge/polestar (GPL-3.0). The data tables live in `polestar_data/`, fetched 2026-10-17; `SOURCE_COMMIT.txt` records the commit. They are read as data by `polestar_data.py` and never executed. If nb is ever shared, GPL applies to these tables and to the ideas ported below.

## Ported (code)
| polestar idea | nb |
|---|---|
| Steep-face links held back by cost, not cut (`pack.js` Faces) | `navmesh._face`, `FACE_*` |
| Region and pocket snap refusal (`path.js` snap) | `navmesh.in_pocket`; used by `rxp_follow._rxp_point_calc`, `move._ps_start_floor` and `move.seed_floor` |
| Floor reseed with no z (`Walk.lua` floor tracking) | `move.seed_floor` (pockets last, lowest building floor when indoors, otherwise the floor on the terrain) |
| No swim over deep ocean (`mh2o.js` deep bits) | `navmesh._deep`, `terrain.liquid`, `OCEAN_ENTRY`/`DEEP_*` |
| Follower lookahead and stray replan (`Walk.lua`) | `move.resync`, `STRAY_*` |
| Zone danger: x3 at 4+ levels over us, x20 past 10 (`Danger.lua`) | `zones.py` (area grid from the .map files plus AreaTable parents) and the zone factor in `navmesh._find_path` |
| Hostile town disks (`hubs.js`) | `dangers._load_hubs`: hostile service NPCs in towns, 25 yd each |
| Lift timetables (`TravelData` Transit) | `polestar_data.lift_stops`, `navloop.lift_between`: a no-path leg across a lift is ridden as a teleport |
| Objective camps (`Guides.lua` spots) | `polestar_data.quest_spots` (`quest_spots.json`) and `rxp_follow._polestar_pts`, used when neither the quest DB nor RXP gives a point |
| Unavailable quests: the window opened without the quest (`ARCHITECTURE` "Smart logic") | BotEye `note_offers` (`I:offers`), `Bot.s` records `self.offers`, `rxp_follow._rxp_note_unoffered` / `_rx_quest_dead` (until a level up or `UNAVAIL_S`) |
| Forever quest corrections (`corrections.json`) | `QuestDB._apply_corrections` (prerequisites, race masks) |
| Recorder step report | `python -m nb.step_report nb/rxp*.out` |
| Nav eval corpus (`routes.json`) | `python -m nb.nav_eval [--rxp N] [--level L]` |
| Class trainer list | `trainers.SHAMAN_TRAINERS` gains Eldrun Stormbreaker (Ironforge) |

Already in nb, so nothing was taken: the resurrection-sickness choice (`Bot.revive_choice`, same formula) and spirit healers (the server DB).

Not taken:
- **Boat timetables and riding lifts for real.** The pixel bot cannot see the car or the ship, so these stay teleports.
- **Fall links and wall-edge links.** The body-point grid covers them.
- **Chokepoint "building" mouths.** These are landmark-size (10-22 yd) areas, not shop doors.

## Forever addon-API facts (for BotEye work)
- `UnitPosition` z reads 0 on this client, hence `seed_floor`/`floor_z`.
- Some unit APIs can return "secret" values; test them with `issecretvalue` before use (Compat.lua:38-50).
- `COMBAT_LOG_EVENT_UNFILTERED` is secure-only: take kills from `UNIT_DIED` / `PARTY_KILL`.
- Rares: `C_VignetteInfo.GetVignettes`, and nameplate `UnitClassification`.
- Zone-to-world: `C_Map.GetWorldPosFromMapPos`. Era and live map frames differ on Mulgore, Eastern Plaguelands, Redridge and Stormwind. `worldmap.py` is the Era (1.12) frame, so `quest_spots` keeps only `f="e"` goals.
- The quest log holds 40 quests. BotEye's strip shows only part of that, so `full_log` logic is needed.

## Benchmarks (polestar docs/SIMULATION.md)
- Its consensus solo route reaches level 10 in about 1.9-2.4 h of /played. nb's 1->10 target is 3.5 h.
- Quest XP by level difference: 100% up to +5, then 80/60/40/20/10%.
