"""What the bot does with a RestedXP step (nb/RXP_PLAN.md section 3): the policy tables and the pure decisions of
rxp_follow.py. No game access here, so the decisions are unit-tested with fake Rxp objects (nb/test_rxp_follow.py).

The tables are the old guide driver's data (guide_plan.py BLOCKED / LEVEL_GATE / STUCK_MIN), keyed by quest IDs, guide
names and step kinds. Not by line: block 195's `line` is RXP's count of non-empty, non-comment lines from the guide
start (step.stepId - guide.guideId), not the file line the plan quotes (BotEye-1+2 report: Sten is line 18, file line
25), and it shifts with every guide update.
"""

from __future__ import annotations

from nb.eye import (K_ACCEPT, K_TURNIN, K_COMPLETE, K_COLLECT, K_GOTO, K_WANDER, K_MOB, K_TARGET,   # noqa: F401
                    K_USE, K_TRAIN, K_TRAINER, K_VENDOR, K_HOME, K_FP, K_FLY, K_HS,
                    K_XP, K_DEATHSKIP, K_BUY, K_MANUAL, K_GOSSIP, K_UNITSCAN, K_OTHER,
                    Q_ACCEPT, Q_TURNIN, Q_COMPLETE, Q_COLLECT)

from nb import route   # noqa: E402  (this instance's race: which guide chain)
# (the names keep the dwarf route's: GUIDE_COLD / DUN / LOCH = the route's 1st / 2nd / 3rd guide, nb/route.py)
GUIDE_COLD, GUIDE_DUN, GUIDE_LOCH = route.GUIDE_1, route.GUIDE_2, route.GUIDE_3

# quests the route names but this server / the bot cannot do (guide_plan.BLOCKED): a step whose open quest element
# names one is skipped. 97277 Grund and Gozwin: its giver (2756) has npcflag 0 here; 86614: no quest_template row.
# (Writing them into R.disabledQuests would only stop the automation, not finish the step: RXP_PLAN.md section 3.)
# 97277 removed 2026-10-14: giver 2756 has npcflag 2 + starter/ender rows now (track S data), and the human run
# had it in the log (nb/human/20261014_1959_inst4.jsonl, accepted at (-6093.9,392.1))
SKIP_QUESTS = {86614, 384, 308, 314}
# step NPCs with no spawn on this server (the T: name of block 209-223): Eldrun Stormbreaker 258098, the guide's
# Ironforge shaman trainer (05-11 Dun Morogh, file L2297-2302; trainers.py: "has no spawn here")
SKIP_NPC_NAMES = {"Eldrun Stormbreaker"}
# (guide, kind bit) -> guide to load. The .fly Ironforge step at Thelsamar (05-11 Dun Morogh, file L2276-2282) starts
# the flight / Deeprun Tram / Stormwind / Elwynn leg; Loch Modan keeps the walkable route the old GUIDES list chose
# (guide_plan.py:33). Stays until taxi and tram handling exist.
JUMP = {(g, K_FLY): d for g, d in route.JUMP_FLY.items()}
# guides RXP may load by itself (05-11's #next lists 11-12 Elwynn first) that need the flight / tram: load Loch Modan
GUIDE_JUMP = dict(route.GUIDE_JUMP)
# at the end of a guide (no current step) when RXP has not loaded the next one itself
GUIDE_WEST = "13-15 Westfall"
# 12-14's #next is "13-15 Westfall;14-16 Darkshore" (RXP asks the player): Westfall, reached by air (nb/flight.py:
# Ironforge -> Stormwind, its first steps; the guide's own way there is the Deeprun Tram, map 369 has no mesh)
NEXT_GUIDE = {GUIDE_COLD: GUIDE_DUN, GUIDE_DUN: GUIDE_LOCH}
if GUIDE_LOCH != GUIDE_WEST:
    NEXT_GUIDE[GUIDE_LOCH] = GUIDE_WEST
# guides beyond the route (Darkshore needs the boat; Redridge / Ashenvale come after it): grind there
NO_ROUTE = {"14-16 Darkshore", "16-19 Darkshore", "19-20 Redridge", "19-21 Darkshore/Ashenvale",
            "20-21 Darkshore/Ashenvale"}
# quests whose mobs out-fight the character below this level (guide_plan.LEVEL_GATE: died twice to the Grizzled Den
# wendigos at level 7). The bot grinds instead (a guard in the bot, not in RXP), and comes back on the next level.
# 2026-10-14: emptied - the user's rule is that doing every earlier guide quest gives the right level; an under-levelled
# step is a missed-quest bug to fix, not a gate (memory nb-complete-guide-not-gates). The mechanism stays for tests.
LEVEL_GATE: dict[int, int] = {}
# single non-elite quest targets worth a trip underground: Grik'nir in the Frostmane cave (bot.py imports this one copy)
INDOOR_QUESTS = {218, 313}

# stall budget per action, minutes on one step (guide_plan.STUCK_MIN: talk 8, hunt 25, use 10). Counted only while
# rxp_step runs (gaps over STALL_GAP s, i.e. a death / corpse run, count STALL_GAP), and restarted by any progress
STALL_MIN = {"talk": 8, "use": 10, "hs": 2, "kill": 25, "goto": 6, "xp": 90}   # (a sit / visit step is a "kill")
STALL_GAP = 30.0
MANUAL_WAIT = 20.0       # s a "+text" / unknown-tag step waits (its sticky companions may clear it), then skip
MANUAL_TRAVEL_YD = 800.0   # yd: a manual step (nothing for the bot to do) farther than this is not travelled to
HUNT_WAIT = 300          # s at the step's points with no target seen -> skip (bot.py's quest_step shelves on it too)
TALK_TRIES = 4           # interacts at the step NPC that changed nothing -> skip (a quest the server does not offer)
# talks at the step NPC that opened NO window (interact did not land: inst3 Sten 20:28-20:29, 16 presses, then 179 and
# its chain lost). Not a refusal, so a separate count, re-walked to NOWIN_TOL yd; more of them on accept / turn-in steps
NOWIN_TRIES = 4
NOWIN_TRIES_QUEST = 8
NOWIN_TOL = 2.0
# visits to the quest DB giver of a quest whose accept was skipped (or that is missing at a later step of it) before
# that later step is left to its old "not in the log" skip
LOST_TRIES = 3
LOST_ARM = 240.0         # s: /be rxp accept <qid> is re-sent this often (BotEye keeps it 300 s)
USE_MAX = 3              # item presses on one step before the item is left to the step's other elements
DEATH_SKIP = 3           # deaths on one kill step -> skip it
FLY_STEP_FAR = 400.0     # yd from a .fly step's flight master: skip the step (rxp_follow._rxp_talk)
DEATH_SKIP_SHARED = 2    # ... when another bot already skipped the step for deaths (rxp_follow death_skips.json)
# escapes (1 skip + nexts) on one step before the bot grinds ESC_PAUSE s between further tries: SetStep(n + 1) does not
# move past the last step while a sticky step is open (GuideWindow.lua:764-783), so "next" can repeat forever
ESC_MAX = 4
ESC_PAUSE = 120.0
HIDDEN_GRIND = 90.0      # s the RXP frame may stay hidden (/be rxp on every 30 s) before the bot grinds meanwhile

TALK = K_ACCEPT | K_TURNIN | K_VENDOR | K_TRAINER | K_HOME | K_FP | K_FLY | K_BUY | K_GOSSIP
KILL = K_COMPLETE | K_COLLECT | K_MOB | K_UNITSCAN
KIND_NAMES = ("accept", "turnin", "complete", "collect", "goto", "wander", "mob", "target", "use", "train",
              "trainer", "vendor", "home", "fp", "fly", "hs", "xp", "deathskip", "buy", "manual", "gossip",
              "unitscan", "other")


def kind_names(mask: int) -> str:
    return "|".join(n for b, n in enumerate(KIND_NAMES) if mask >> b & 1) or "-"


ROUTE_GUIDES = {GUIDE_COLD, GUIDE_DUN, GUIDE_LOCH, GUIDE_WEST}


def pick_guide(level: int) -> str:
    """The guide for a character level (BotEye.lua rxp_pick_name)."""
    if level < route.LEVELS[0]:
        return GUIDE_COLD
    if level < route.LEVELS[1]:
        return GUIDE_DUN
    return GUIDE_LOCH


def no_step(r) -> bool:
    """fill_rxp found no current step (no guide, or the index is past the last step): block 195 holds only the flag
    bit and 196.. are cleared."""
    return r.line == 0 and r.nactive == 0 and r.kinds == 0


def target_name(r) -> str:
    """First .target name of the step from the log text "T:<name>|M:<mob>" (BotEye fill_rxp, cut at 45 chars)."""
    t = r.text or ""
    if not t.startswith("T:"):
        return ""
    return t[2:].split("|M:", 1)[0].split("::", 1)[0].strip()


def guide_action(r, level: int):
    """Guide-level decision before any step: ('load', guide, why) | ('grind', None, why) | None."""
    if r.guide in GUIDE_JUMP:
        return "load", GUIDE_JUMP[r.guide], f"{r.guide} needs the flight / tram"
    if r.guide == "other" or (r.guide and r.guide not in ROUTE_GUIDES and r.guide not in NO_ROUTE):
        # (another race's route: the hunter's first login ran an older BotEye, which loaded 01-05 Coldridge Valley)
        return "load", pick_guide(level), f"{r.guide or 'a guide'} is outside our route"
    if r.guide in NO_ROUTE:
        return "grind", None, f"{r.guide} is beyond the walkable route"
    if no_step(r):
        nxt = NEXT_GUIDE.get(r.guide)
        if nxt:
            return "load", nxt, f"{r.guide} finished"
        return "grind", None, f"no current step in {r.guide}"
    return None


def step_policy(r, level: int):
    """Policy for the current step: ('skip', why, others) | ('load', guide, why) | ('gate', qid, level) | None.
    others = the step also has open quest elements that are not blocked: talk to the NPC once first (it may give the
    other quests), then skip (RXP's skip marks every open element of the step)."""
    for (g, bit), dest in JUMP.items():
        if r.guide == g and r.kinds & bit:
            return "load", dest, f"{g} {kind_names(bit)} step (no taxi / tram handling)"
    name = target_name(r)
    if name in SKIP_NPC_NAMES:
        return "skip", f"{name} has no spawn on this server", False
    open_q = [q for q in r.quests if not q[2]]
    bad = [q[0] for q in open_q if q[0] in SKIP_QUESTS]
    if bad:
        return "skip", f"quest {bad[0]} cannot be done on this server", len(bad) < len(open_q)
    for qid, tag, done in r.quests:
        if not done and tag in (Q_COMPLETE, Q_COLLECT) and level < LEVEL_GATE.get(qid, 0):
            return "gate", qid, LEVEL_GATE[qid]
    return None


def vendor_collect(r) -> bool:
    """A .collect with the step's .target as its only actor (no mobs, no quest elements): an item bought there."""
    return bool(r.kinds & K_COLLECT and r.n_targets and not r.n_mobs and not r.quests)


BUY_TALKS = 8            # merchant visits at a vendor .collect that is affordable but not bought yet -> skip anyway
BUY_BUILD = 2            # first BotEye build (block 250) with buy_collect and blocks 248-249


def stale_boteye(r, disk: int | None = None) -> str | None:
    """Why this client's BotEye is older than needed, or None. Build 0 = no block-250 stamp (a copy from before
    buy_collect); `disk` = BOTEYE_BUILD of the file on disk (eye.disk_build): a lower build in the strip means the
    client was not /reloaded after the last addon change (inst3 21:12: Adlin skipped, blocks 248-249 read 0)."""
    b = getattr(r, "build", 0) or 0
    if disk is not None and b < disk:
        return f"BotEye build {b} in the strip, {disk} on disk: /reload this client (out of combat)"
    if b < BUY_BUILD:
        return f"BotEye build {b} < {BUY_BUILD} (no buy_collect): /reload this client (out of combat)"
    return None


def buy_verdict(r) -> str | None:
    """After a merchant visit at a vendor .collect step that did not finish it (BotEye blocks 248-249):
    'retry' (the vendor sells it, the money covers it, the bags have room: the purchase is in flight or the window
    closed first) | a reason to skip (cannot buy) | None (an old BotEye without the buy blocks: the TALK_TRIES rule)."""
    if not vendor_collect(r) or not r.buy_item:
        return None
    if not r.buy_seen:
        return f"the vendor does not sell item {r.buy_item}"
    if r.buy_full:
        return f"bags full, cannot buy item {r.buy_item}"
    if r.buy_short:
        return (f"short of money for item {r.buy_item}: {r.buy_need} more at {r.buy_price}c each"
                f"{' (bought what it could)' if r.buy_done else ''}")
    return "retry"


def classify(r, uses: int = 0) -> str:
    """The action for the current step, in the plan's order (TALK, USE, KILL, GOTO, deathskip, manual):
    talk | use | hs | kill | xp | goto | deathskip | manual.
    `.use` elements are textOnly in RXP (functions.lua:6035) and never complete: the bit only says the step has an
    item. So the item is used when RXP shows its button (item_btn: it is in the bags) or when nothing else in the step
    is actionable (Earth Sapta: .cast 8202 + .use 6635, file L585-587), at most USE_MAX times per step."""
    K = r.kinds
    talk = bool(K & TALK) or bool(K & K_TRAIN and not r.train_known)
    kill = bool(K & KILL)
    if K & K_HS:
        return "hs"
    if K & K_USE and r.item and uses < USE_MAX and (r.item_btn or not (talk or kill)):
        return "use"
    if kill and not talk and r.item and r.item_btn and getattr(r, "n_mobs", 0) and uses < USE_MAX:
        # a .complete + .use + .mob step whose .use / .mob RXP already marked done (textOnly): its item button is
        # still shown, so the item goes on the step's mob (Taming the Beast 94792, Hunt 12:25: the Taming Rod's
        # Rockhide Boars were killed instead - the server credits the rod's 20 s channel, not a kill)
        return "use"
    if talk and (r.n_targets or not kill):    # no step NPC but mobs to kill: the item drop starts the quest (L1984)
        return "talk"
    if vendor_collect(r):
        # a .collect bought from the step's .target vendor ("buy 10 Refreshing Spring Water" from Adlin, file L119-129).
        # RXP's .collect only counts (functions.lua:2976-3244; only .buy buys, :5583-5666): BotEye's buy_collect buys
        # it when the merchant window opens. Hunting it found nothing (20:29, bots 1+2)
        return "talk"
    if kill:
        return "kill"
    if K & K_XP:
        return "xp"
    if K & K_GOTO:
        return "goto"
    if K & K_DEATHSKIP:
        return "deathskip"
    if r.item and uses < USE_MAX:
        # the step names an item and nothing else: use it. The Forever guide's Earth Sapta step (L568, .cast 8202 +
        # .use 6635) arrives as kinds=other without the USE bit; called "manual" it was skipped, the Minor
        # Manifestation of Earth never appeared, and the shaman quest chain (94374 -> 94375) was lost (bots 2+3, 22:54)
        return "use"
    return "manual"


# ---- .complete steps whose objective is not a kill (2026-10-14 bot 1, 05-11 L49 "The Great Outdoors" 96608: '"/sit" in
# chat and wait for one minute around the campfire' was hunted for 5 min, "no target seen", skipped; the turn-in then
# failed 4 talks). The objective type comes from the server (tcf_world quest_objectives.Type, TrinityCore
# QuestObjectiveType): 0 monster, 1 item, 2 gameobject, 3 talk-to are a hunt's; 10 / 19 / 20 are area triggers (walk
# into it: 98319 "Mountaineer Cornelius found"); anything else (14 criteria tree: 96608's sit emote + Boosted Rest aura)
# is done by being there, with the emote the step text or the objective names. Scan of the 01-05 / 05-11 / 12-14
# guides (all 41 objectives of their .complete quests): 96608 (two type 14) and 98319 (type 19) are the only ones;
# 96629's type 14 "Learn cooking" sits on a talk step (.train 2550 + .turnin at Gremlock Pilsnor), not here.
OBJ_HUNT = {0, 1, 2, 3}
OBJ_TRIGGER = {10, 19, 20}
# emote tokens (DoEmote) a step may ask for as "/token" in its text, and the objective verbs that name one
EMOTES = {"sit", "dance", "kneel", "sleep", "cheer", "salute", "bow", "wave", "roar", "flex", "applaud", "clap",
          "pray", "laugh", "hug", "kiss", "thank", "greet", "hello", "chicken", "beg", "cry", "point", "rude"}
EMOTE_VERBS = {"sit", "dance", "kneel", "sleep", "cheer", "salute", "bow", "wave", "pray", "applaud"}
SPECIAL_WAIT = 90.0      # s standing still at the spot (the campfire rest is 60 s) before that try counts as failed
SPECIAL_TRIES = 3        # tries (a fight in between ends one) before the step is skipped
VISIT_WAIT = 10.0        # s inside an area trigger without the objective -> back to the old hunt of the step's points


def emote_token(*texts) -> str:
    """The emote a step asks for: '/sit' (in its text) -> 'SIT'; else an objective name's leading verb ('Sit near the
    campfire' -> 'SIT'); '' when none."""
    import re
    for t in texts:
        for m in re.finditer(r"(?<![\w/])/([A-Za-z]+)\b", t or ""):
            if m.group(1).lower() in EMOTES:
                return m.group(1).upper()
    for t in texts:
        w = (t or "").strip().split(" ", 1)[0].lower()
        if w in EMOTE_VERBS:
            return w.upper()
    return ""


def special_step(r, objs: dict, logq: dict, text: str = "", qids=None):
    """A kill-classified step that is really 'be there' / 'emote there' / 'walk into a trigger':
    ('emote', TOKEN, ()) | ('wait', '', ()) | ('visit', '', trigger ids) | None (hunt it as before).
    objs: qid -> [(type, object id, description)] in the server's objective order (the log's counter order);
    logq: qid -> (complete, counters) of the quests in the log; text: the guide step's text (may be '').
    qids: the quests to judge (default: the step's open .complete elements; an unfinished hand-in passes its own)."""
    if r.n_mobs or r.kinds & (K_MOB | K_UNITSCAN | K_COLLECT):
        return None
    if qids is None:
        qids = [q[0] for q in r.quests if not q[2] and q[1] == Q_COMPLETE]
    live = [qid for qid in qids if logq.get(qid) is not None and not logq[qid][0]]
    if not live:
        return None                           # not in the log / all complete: the kill path's checks handle it
    types, trig, descs, known = set(), [], [], False
    for qid in dict.fromkeys(live):
        lq = logq[qid]
        rows = objs.get(qid) or []
        if not rows:
            continue
        known = True
        counts = tuple(lq[1] or ())
        for i, (typ, obj, desc) in enumerate(rows):
            if i < len(counts) and counts[i] == 255:
                continue                      # this objective is done
            if typ in OBJ_HUNT:
                return None                   # a real kill / loot / object part: hunt the step
            types.add(typ)
            descs.append(desc or "")
            if typ in OBJ_TRIGGER:
                trig.append(obj)
    tok = emote_token(text, *descs)
    if not known:                             # no server rows (DB down): only the step text's "/emote" decides
        return ("emote", tok, ()) if tok else None
    if not types:
        return None
    if trig and not tok:
        return "visit", "", tuple(trig)
    return ("emote", tok, ()) if tok else ("wait", "", ())


def progress_sig(r, s) -> tuple:
    """Anything that moves while a step is being worked: open kinds, quest elements done, the step's quest counters."""
    qids = {q[0] for q in r.quests}
    counts = tuple(sorted((q.qid, bool(q.complete), tuple(q.counts)) for q in getattr(s, "quests", ()) if q.qid in qids))
    return r.kinds, tuple(r.quests), counts, r.stepctr
