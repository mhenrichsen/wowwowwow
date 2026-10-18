# wowbot

A levelling bot for a **local VMaNGOS 1.12.1 server** (client build 5875).
It reads the client's memory for world state and drives the character with
click-to-move plus a small in-game addon; it does not modify the client.

## Running it

```bash
python control.py start      # or stop | restart | kill | status
```

Arguments live in `launch_args.txt`, never in a script. A restart that
hardcoded the human Elwynn guide once put a dwarf on it, which is why.

There is also a web page:

```bash
python web.py --port 8767 --control
```

`--control` adds Start/Stop/Restart buttons and is **off by default**: the
read-only page is safe to expose through a tunnel, one that can spawn
processes is not. The access key is generated into `web_key.txt`, which is
git-ignored and should stay that way.

`bot-ui.bat` starts that page and opens it.

## Layout

| | |
|---|---|
| `bot.py` | main loop, movement, looting, resting, town trips |
| `combat.py` | fight loop, rotation, add avoidance |
| `questing.py` | quest log, objectives, hand-ins, guide steps |
| `rxp.py` | parses the RestedXP Survival guides (1-60, `#next` chain, no dungeons) |
| `events.py` | structured event log (`data/events.jsonl`): state, step, action, kill, level, death, stuck |
| `dbg.py` | debugging CLI: `status`, `watch`, `report`, `steps`, `tail`, `quest`, `mob`, `guide` (run with no args for help) |
| `navdbg.py` | navigation debugging in text: `here`, `path`, `scene`, `navs` (reachability, detours, where it walked / stood still / got stuck) |
| `triage.py` | issue tracker over all logs: `scan`, `show ID`, `fixed ID note`, `wontfix`, `list`, `json` - ranks deaths (with cause), stuck spots, failed walks, skipped steps, errors; flags REGRESSED fixes. `.claude/workflows/bot-triage.js` runs it end to end (triage -> diagnose -> challenge -> fix -> redeploy) |
| `guide.py` | the quest DB side of a guide: givers, enders, objectives, drop sources |
| `navmesh.py` | Recast/Detour mmap reader, A* and funnel smoothing |
| `game.py`, `wowmem.py`, `offsets.py` | memory reading |
| `bridge.py` | command channel to the in-game addon (F-key protocol) |
| `consumables.py` | what we can eat and drink, from the world DB |
| `classes/` | per-class profiles: rotation, opener, buffs as data |
| `routes/`, `guides/` | levelling routes and guide files |
| `control.py`, `web.py` | start/stop and the status page |

## Things that will bite you

- **PostMessage keypresses are unreliable on this machine** for digits and
  OEM keys. Only F-keys work, which is why the addon bridge uses them and
  why abilities go through `UseAction`, not keystrokes.
- **The addon caches a spell-name table at install time.** Add an ability
  to a class profile and it will not place until you run
  `reload_addon.sh` (bot stopped) — before that the addon answers
  `unknown spell <id>`.
- **`item_template` and `spell_template` have one row per content patch.**
  Join without taking the newest and you get several rows per item with
  different numbers in them.
- **The quest log packs objective counters at six bits each**, not one
  byte each, with the complete flag at `0x01000000`.
- **Truncating `bot.log` while the bot runs** leaves NUL padding; the
  process keeps writing at its old offset. Restart to archive it instead.
