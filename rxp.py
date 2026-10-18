"""Read RestedXP guides into bot steps.

RestedXP is the guide format of the RXPGuides addon. The bot follows its
Survival Guide (`#hardcore`): the whole 1-60 route, written to avoid deaths.
The guide gives the ORDER and the PATH (19,000+ waypoints, human-chosen); the
world DB still supplies HOW to do each quest (see guide.quests).

The grammar mirrors the addon's own loader (RXPGuides/GuideLoader.lua) rather
than the guide text, because the text lies in places: a trailing ",0" on a
.goto is a flag and not a radius, .target and .mob split on ";" not ",", and
a "--" comments out the rest of any line, display text included.

    python rxp.py                  # parse every Survival guide, report problems
    python rxp.py <guide name>     # dump one guide's steps
"""

from __future__ import annotations

import glob
import os
import re
import sys
from dataclasses import dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))
GUIDE_DIR = os.environ.get("RXP_GUIDE_DIR") or os.path.join(HERE, "guides", "restedxp", "RXPGuides", "Guides", "SurvivalGuide")
# RestedXP v4.11.23's own route for the WoW Forever client (nb uses this one)
FOREVER_DIR = os.path.join(HERE, "guides", "rxp41123", "RXPGuides", "Guides", "forever")

# What this server is, for the directives that depend on it. VMaNGOS build
# 5875 with WowPatch = 10 in mangosd.conf is patch 1.12: the last Classic
# content phase, Era rules (not Season of Mastery), 1x xp, and a normal realm
# - so the quests Blizzard rewrote for official Hardcore realms are the
# ORIGINAL versions here.
PHASE = 6
SEASON = 0
XP_RATE = 1.0

# Commands that become bot actions, and the questing.do_action tag each maps
# to. The tags are the ones questing.py already executes.
ACTION = {
    "accept": "QA", "turnin": "QT", "complete": "QC", "collect": "CI",
    "xp": "XP", "train": "LE", "trainer": "LE", "vendor": "V", "fly": "F",
    "fp": "P", "hs": "H", "home": "S", "equip": "UI", "use": "USE",
    "destroy": "DESTROY", "cast": "CAST", "usespell": "CAST",
    "goto": "GO", "waypoint": "GO", "groundgoto": "GO", "flygoto": "GO",
    "xpto60": "XP", "xpto60hc": "XP", "xpto60alliance": "XP", "xpto60horde": "XP",
    # 129 uses chain-wide, every one of them the guide telling us a quest is
    # not going to be finished. Ignoring them is how the log fills up.
    "abandon": "AB",
}
# Step gates: the step only runs when these hold (checked when we reach it).
CONDITION = {"isOnQuest", "isNotOnQuest", "isQuestComplete", "isQuestTurnedIn",
             "isQuestAvailable", "isQuestNotComplete", "isQuestOffered"}
# The other half of the gates: conditions on the character's own state rather
# than on its quest log. These were in IGNORED as "addon UI", which was true
# of none of them - in Hardcore.lua alone, the 1-60 route carries 232
# .itemcount, 96 .skill, 81 .money, 64 .cooldown, 60 .timer and 18 .aura, and
# every one of those steps was executed unconditionally: re-collecting items
# already in the bag, walking to a vendor with no money, hearthing on a
# hearthstone that is on cooldown. None of it was exercised at 1-20, which is
# why nothing noticed. questing.step_gate evaluates them, and anything it
# cannot resolve runs the step (see there).
STEP_CONDITION = {"itemcount", "money", "skill", "reputation", "aura",
                  "cooldown", "maxlevel"}
# Addon UI with nothing for a bot to do. Listed so an unknown command stands
# out: every other tag is counted and reported, never silently eaten.
#
# Two of these DO change the addon's control flow and are still ignored, on
# purpose: `.zoneskip` (567 uses) needs "which zone am I standing in", and the
# WorldMapArea rectangles the bot has overlap their neighbours, so a wrong
# answer would silently skip real content; `.itemStat` needs item stats the
# gear module reads but the guide compares in the client's display units.
# Both fail open today (the step runs), which is the safe direction.
IGNORED = {
    "target", "mob", "unitscan", "dungeon", "group", "solo", "line", "link",
    "disablecheckbox", "bronzetube", "blastedLands", "itemStat",
    "zone", "subzone",
    "zoneskip", "subzoneskip", "bindlocation", "skipgossip",
    "gossipoption", "bankdeposit", "bankwithdraw", "stable", "addquestitem",
    "deathskip", "daily", "dailyturnin", "pin", "wpradius", "loop",
    "questgoto", "questwaypoint", "clicknext", "next", "noop", "label", "tame",
}
# Arg splitting overrides (functions.lua:4745-4750, 4875-4884).
SEMICOLON = {"target", "mob", "unitscan"}
UNSPLIT = {"link", "clicknext"}

CLASSES = {"WARRIOR", "PALADIN", "HUNTER", "ROGUE", "PRIEST", "SHAMAN", "MAGE",
           "WARLOCK", "DRUID"}
RACE_BY_ID = {1: "Human", 2: "Orc", 3: "Dwarf", 4: "NightElf", 5: "Scourge",
              6: "Tauren", 7: "Gnome", 8: "Troll"}


@dataclass
class Who:
    """Who a guide is being read for: the atoms a `<<` filter can name."""
    cls: str            # "Warrior"
    race: str           # addon spelling: "Dwarf", "NightElf", "Scourge"
    faction: str        # "Alliance" / "Horde"

    @classmethod
    def of(cls, class_name: str, race_id: int) -> "Who":
        race = RACE_BY_ID.get(race_id, "")
        horde = race in ("Orc", "Scourge", "Tauren", "Troll")
        return cls(class_name.capitalize(), race, "Horde" if horde else "Alliance")


@dataclass
class Step:
    line_no: int
    text: str                                                      # display text, markup stripped
    actions: list[tuple[str, str]] = field(default_factory=list)   # (tag, args)
    npcs: list[int] = field(default_factory=list)                  # resolved .target entries
    goto: tuple[float, float, str] | None = None                   # first waypoint (x%, y%, zone)
    gotos: list[tuple[str, float, float, float | None]] = field(default_factory=list)
    targets: list[str] = field(default_factory=list)               # .target names
    mobs: list[str] = field(default_factory=list)                  # .mob names
    requires: list[tuple[str, list[int] | dict]] = field(default_factory=list)
    min_level: int | None = None                                   # #level
    label: str | None = None
    optional: bool = False
    completewith: str | None = None                                # #completewith: optional en-route work
    timer: float | None = None                                     # .timer: seconds of RP

    def applies_to(self, cls: str, race: str) -> bool:
        return True       # filtering happens in parse(), where the grammar is


@dataclass
class Guide:
    name: str
    path: str
    steps: list[Step]
    next_guide: str | None = None
    default_for: set[str] = field(default_factory=set)
    group: str = ""
    dropped: dict[str, int] = field(default_factory=dict)          # reason -> steps
    unknown: dict[str, int] = field(default_factory=dict)          # command -> uses
    problems: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- filters
TOKEN = re.compile(r"!?[A-Za-z0-9]+")


def _atom(atom: str, who: Who) -> bool:
    """One filter word (GuideLoader.lua:78-80). "skip" matches nothing, which
    is how the guide authors comment a step out."""
    a = {"Undead": "Scourge"}.get(atom, atom)
    if a.isdigit():
        return True                     # a level gate; none in the Survival guides
    return (a.upper() == who.cls.upper() or a == who.race or a == who.faction
            or a.upper() == "CLASSIC")


def applies(filt: str, who: Who) -> bool:
    """`<<` filter: "/" is OR, whitespace is AND, "!" is NOT (GuideLoader.lua:36-103).

    Commas are not an operator - the tokenizer drops them - and "<<<" is the
    same as "<<" because the stray "<" is not a word character.
    """
    groups = [TOKEN.findall(g) for g in filt.split("/")]
    if not any(groups):
        return True
    return any(toks and all(_atom(t.lstrip("!"), who) != t.startswith("!") for t in toks)
               for toks in groups)


def _range_ok(spec: str, value: float) -> bool:
    """#phase "4-6" / "4" (open-ended) and #xprate "<2.0" / ">1.5" / "1-3"."""
    spec = spec.strip()
    try:
        if spec.startswith("<"):
            return value < float(spec[1:])
        if spec.startswith(">"):
            return value > float(spec[1:])
        lo, _, hi = spec.partition("-")
        return float(lo) <= value <= (float(hi) if hi else float("inf"))
    except ValueError:
        return True


def step_dropped(d: dict) -> str | None:
    """Why this server and this bot never see a step, from its directives."""
    if "softcore" in d:
        return "softcore"             # we play the hardcore route
    if "hardcoreserver" in d:
        return "hc-realm"             # quests Blizzard rewrote for HC realms; original here
    if "ah" in d:
        return "auction house"        # the bot never uses one
    if "som" in d:
        return "season of mastery"    # "#era/som" is a different tag and is kept
    if "phase" in d and not _range_ok(d["phase"], PHASE):
        return "phase"
    if "season" in d and str(SEASON) not in re.split(r"[,;\s]+", d["season"]):
        return "season"
    if "xprate" in d and not _range_ok(d["xprate"], XP_RATE):
        return "xp rate"
    return None


# ---------------------------------------------------------------- markup
def clean(text: str) -> str:
    """Strip textures, colour tokens and escaped newlines from display text.

    Colours are a flat token stream, not a tree: the guides nest them
    ("|cRXP_WARN_Be careful as |cRXP_ENEMY_Mob|r casts|r") but WoW's |r only
    closes one, so balancing them would be wrong.
    """
    text = re.sub(r"\|T[^|]*\|t", "", text)
    text = re.sub(r"\|c[A-Za-z0-9_]+", "", text)
    text = text.replace("|r", "").replace("\\n", " ")
    return re.sub(r"\s+", " ", text).strip()


def _split(tag: str, args: str) -> list[str]:
    if tag in UNSPLIT:
        return [args]
    sep = ";" if tag in SEMICOLON else ","
    return [a.strip() for a in args.split(sep) if a.strip()]


def _name(n: str) -> str:
    """A .target/.mob name: "+" marks a child line, "::1234" an explicit id."""
    return n.lstrip("+").strip()


def condition(tag: str, args: list[str]) -> tuple[str, dict | list[int]] | None:
    """A STEP_CONDITION line as (kind, payload), or None if it is not one.

    The operators follow the addon's own parsers (functions.lua), including
    the two that read backwards:

      .money <90      is "skip unless we have 90 gold" - the guide states the
                      condition that makes the step POINTLESS, not the one
                      that makes it run. `.money <0.3022` sits on "buy a
                      Quarter Staff (3s 2c)".
      .aura -9991     is "only while we carry aura 9991" (Touch of Zanzil):
                      a negative id reverses the test, so the cure steps run
                      only when the debuff is up.
    """
    a = [x.strip() for x in args]
    if tag == "itemcount":
        ids = [int(x) for x in re.findall(r"\d+", a[0])] if a else []
        m = re.match(r"([<>]?)(=?)\s*(\d+)", a[1]) if len(a) > 1 else None
        if not ids or not m:
            return None
        return (tag, {"ids": ids, "n": int(m.group(3)), "eq": m.group(2) == "=",
                      "op": -1 if m.group(1) == "<" else 1 if m.group(1) == ">" else 0})
    if tag == "money":
        m = re.match(r"([<>])\s*(\d+\.?\d*)", a[0]) if a else None
        if not m:
            return None
        # the guide writes gold with silver and copper after the point
        return (tag, {"op": m.group(1), "copper": int(round(float(m.group(2)) * 10000))})
    if tag == "skill":
        m = re.match(r"(<?)\s*(\d+)", a[1]) if len(a) > 1 else None
        gate = len(a) > 2 and a[2].lstrip("-").isdigit() and int(a[2]) != 0
        if not m or not gate:
            # without the skipstep flag this is a progress marker on the
            # step ("level First Aid to 80"), not a gate on running it
            return None
        return (tag, {"skill": a[0].lower(), "level": int(m.group(2)),
                      "under": m.group(1) == "<"})
    if tag == "reputation":
        if not a:
            return None
        return (tag, {"faction": int(a[0]) if a[0].isdigit() else 0,
                      "standing": (a[1].lower() if len(a) > 1 else ""),
                      "under": any(x.startswith("<") for x in a[2:])})
    if tag == "aura":
        m = re.match(r"(-?)(\d+)", a[0]) if a else None
        if not m:
            return None
        return (tag, {"id": int(m.group(2)), "absent": m.group(1) == "-"})
    if tag == "cooldown":
        if len(a) < 3:
            return None
        m = re.match(r"([<>]?)\s*(\d+\.?\d*)\s*([ms]?)", a[2])
        if not m or not a[1].isdigit():
            return None
        secs = float(m.group(2)) * (60 if m.group(3) == "m" else 1)
        return (tag, {"what": a[0].lower(), "id": int(a[1]),
                      "op": ">" if m.group(1) != "<" else "<",
                      "secs": max(2.0, secs)})
    if tag == "maxlevel":
        m = re.match(r"(\d+)", a[0]) if a else None
        # the same shape questing.step_gate already evaluates for .xp gates
        return ("maxLevel", [int(m.group(1))]) if m else None
    return None


# ---------------------------------------------------------------- parsing
REGISTER = re.compile(r"RegisterGuide\(\s*(?:[\"'][^\"']*[\"']\s*,\s*)?\[\[(.*?)\]\]", re.S)


def blocks(path: str):
    """(first line number, guide text) for every RegisterGuide in a file."""
    raw = open(path, encoding="utf-8", errors="replace").read()
    for m in REGISTER.finditer(raw):
        yield raw.count("\n", 0, m.start(1)) + 1, m.group(1)


def parse_block(path: str, first_line: int, body: str, who: Who | None) -> Guide:
    g = Guide(name="", path=path, steps=[])
    header, enabled = {}, True
    cur: Step | None = None
    cur_dirs: dict = {}
    cur_texts: list[str] = []
    cur_drop: str | None = None
    zone_last: str | None = None

    def finish():
        nonlocal cur
        if cur is None:
            return
        why = cur_drop or step_dropped(cur_dirs)
        # A step with only arrows and no job is a travel leg: follow the
        # author's route. Anywhere else the arrows only mark where the job
        # is, and the job's own verb already knows how to get there - so
        # they become a hint (nearest_spawn and the kill-area anchor).
        # (Two steps in the chain change side because of .abandon: "walk to
        # Blasted Lands 65.5,24.3 / abandon Mazen's Behest" used to be a
        # walk with no job, and abandoning a quest needs no position at all.)
        if cur.gotos:
            z, x, y, _ = cur.gotos[0]
            cur.goto = (x, y, z)
            if not cur.actions:
                cur.actions = [("GO", f"{z},{x},{y},{r if r else ''}".rstrip(","))
                               for z, x, y, r in cur.gotos]
        cur.completewith = cur_dirs.get("completewith")
        cur.text = " / ".join(t for t in cur_texts if t)[:300]
        if why:
            g.dropped[why] = g.dropped.get(why, 0) + 1
        elif cur.actions or cur.requires:
            g.steps.append(cur)
        cur = None

    for n, raw in enumerate(body.splitlines(), first_line):
        line = re.sub(r"--.*", "", raw).strip()
        if not line:
            continue
        filt = None
        m = re.search(r"<<+\s*(.*)$", line)
        if m:
            filt, line = m.group(1).strip(), line[:m.start()].strip()

        if raw.strip().startswith("step"):
            finish()
            cur, cur_dirs, cur_texts, cur_drop = Step(n, ""), {}, [], None
            if who is not None and filt is not None and not applies(filt, who):
                cur_drop = "filtered"
            continue

        if cur is None:                              # guide header
            if line == "" and filt is not None:
                enabled = who is None or applies(filt, who)
                continue
            if who is not None and filt is not None and not applies(filt, who):
                continue
            hm = re.match(r"^#(\S+)\s*=?\s*(.*)", line)
            if hm:
                header.setdefault(hm.group(1), hm.group(2).strip())
            continue

        if who is not None and filt is not None and not applies(filt, who):
            continue                                 # this one line is not for us
        if cur_drop:
            continue

        dm = re.match(r"^#(\S+)\s*=?\s*(.*)", line)
        if dm:
            cur_dirs.setdefault(dm.group(1), dm.group(2).strip())   # first write wins
            if dm.group(1) == "label":
                cur.label = dm.group(2).strip()
            elif dm.group(1) == "optional":
                cur.optional = True
            elif dm.group(1) == "level" and dm.group(2).strip().isdigit():
                cur.min_level = int(dm.group(2))
            continue

        payload, _, text = line.partition(">>")
        payload, text = payload.strip(), clean(text)
        cm = re.match(r"^\.(\S+)\s*(.*)", payload)
        if not cm:
            if payload.startswith(("+", "*")):
                text = clean(payload[1:]) or text
            cur_texts.append(text)
            continue
        tag, args = cm.group(1), _split(cm.group(1), cm.group(2))
        cur_texts.append(text)

        if tag == "dungeon":
            if args and not args[0].startswith("!"):
                cur_drop = "dungeon"                 # "!TAG" is the overland fallback: keep
        elif tag == "group":
            cur_drop = "group"                       # solo: the addon hides these unless enabled
        elif tag in ("goto", "waypoint", "groundgoto", "flygoto"):
            if len(args) > 5:
                # The addon reads zone,x,y,radius,optional and ignores the
                # rest, so do the same: nearly all of these are a harmless
                # extra ",0". The exception is a decimal point typed as a
                # comma, "Un'Goro Crater,46,0,56.6" - read literally that is
                # y = 0, the map's top edge, and it lands on valid ground so
                # no navmesh or distance check notices. A y of exactly "0"
                # is never a real destination, so rejoin it: 46.0, 56.6.
                # (Once in the whole corpus: Hardcore.lua:20793.)
                g.problems.append(f"{os.path.basename(path)}:{n} .goto has {len(args)} fields")
                if args[2] == "0" and "." not in args[1]:
                    args = [args[0], f"{args[1]}.0"] + args[3:]
                args = args[:5]
            if len(args) < 3:
                continue
            zone = args[0] or zone_last
            zone_last = zone
            try:
                x, y = float(args[1]), float(args[2])
                r = float(args[3]) if len(args) > 3 and args[3] else None
            except ValueError:
                g.problems.append(f"{os.path.basename(path)}:{n} bad .goto {args}")
                continue
            if r is not None and r <= 0:
                r = None                             # 0 = pin only, <0 = dynamic
            cur.gotos.append((zone, x, y, r))
        elif tag in ("target", "unitscan"):
            cur.targets += [_name(a) for a in args]
        elif tag == "mob":
            cur.mobs += [_name(a) for a in args]
        elif tag in CONDITION:
            ids = [int(a) for a in args if a.lstrip("-").isdigit()]
            cur.requires.append((tag, [abs(i) for i in ids]))
        elif tag in STEP_CONDITION:
            c = condition(tag, args)
            if c:
                cur.requires.append(c)
            else:
                g.problems.append(f"{os.path.basename(path)}:{n} bad .{tag} {args}")
        elif tag == "timer":
            # An RP sequence the guide knows the length of: "turn in, then
            # 28 s of Velarok's speech before the next quest can be taken".
            # The addon shows it as a countdown; for the bot it is patience,
            # not a deadline - questing's own budget would otherwise cut the
            # step while the NPC is still talking. Read as a deadline (the
            # obvious reading of "hard budget") an 8 s timer would abandon
            # every step it sits on the moment it started.
            try:
                cur.timer = max(cur.timer or 0.0, float(args[0]))
            except (IndexError, ValueError):
                g.problems.append(f"{os.path.basename(path)}:{n} bad .timer {args}")
        elif tag in ACTION:
            act = ACTION[tag]
            if act in ("QA", "QT", "QC") and args:
                # a negative id is "only if we have it", which is what
                # turn_in/complete already do for a quest not in the log
                args = [args[0].lstrip("-")] + args[1:]
            elif act == "XP" and tag.startswith("xpto60"):
                args = ["60"]
            elif act == "XP":
                # With a second "skipstep" argument .xp is a level GATE, not
                # a grind target (functions.lua, addon.functions.xp):
                #   ".xp <10,1"  runs only at level 10 and up   (647 uses)
                #   ".xp >58,1"  runs only below level 58       (225 uses)
                # The addon has no ">" operator - its unanchored match skips
                # the character, so ">58,1" is "58,1": skip once we reach 58.
                m = re.match(r"\s*(<?)\s*>?\s*(\d+)([+.\-]?\d*)", args[0] if args else "")
                if not m:
                    g.problems.append(f"{os.path.basename(path)}:{n} bad .xp {args}")
                    continue
                below, lvl, extra = m.group(1) == "<", int(m.group(2)), m.group(3)
                flag = args[1] if len(args) > 1 else ""
                if flag.lstrip("-").isdigit() and int(flag) != 0:
                    if int(flag) < 0:
                        below = not below
                    cur.requires.append(("minLevel", [lvl]) if below else ("maxLevel", [lvl - 1]))
                    continue
                args = [f"{lvl}{extra}"]           # grind_to's forms: 9, 9.5, 9+1000, 9-1000
            elif act == "UI":
                args = args[-1:]                    # ".equip <slot>,<item>": the item
            elif act == "CI":
                if not args:
                    continue
                qty = args[1] if len(args) > 1 and args[1].lstrip("-").isdigit() else "1"
                args = [args[0], str(abs(int(qty)) or 1)]
            elif act == "H":
                # ".hs >> Hearth to Thelsamar" names the place only in its
                # display text, and use_hearth needs it: with no destination
                # it skips the check that the stone is bound near there.
                args = [text] if text else []
            elif act == "F" and not args:
                continue
            cur.actions.append((act, ",".join(args)))
        elif tag not in IGNORED:
            g.unknown[tag] = g.unknown.get(tag, 0) + 1
    finish()

    g.name = header.get("name", "")
    g.group = header.get("group", "")
    nx = header.get("next")
    if nx:
        g.next_guide = [a.split("\\")[-1].strip() for a in nx.split(";") if a.strip()][0]
    g.default_for = set(header.get("defaultfor", "").replace(",", "/").split("/")) - {""}
    if not enabled:
        g.steps = []
        g.dropped["guide filter"] = 1
    return g


def load(path: str, who: Who | None = None) -> list[Guide]:
    return [parse_block(path, ln, body, who) for ln, body in blocks(path)]


# ---------------------------------------------------------------- the chain
def catalog(who: Who) -> dict[str, Guide]:
    """Every guide that applies to this character, by #name."""
    out: dict[str, Guide] = {}
    for path in sorted(glob.glob(os.path.join(GUIDE_DIR, "*.lua"))):
        for g in load(path, who):
            if g.name and g.steps and g.name not in out:
                out[g.name] = g
    return out


def _levels(name: str) -> tuple[int, int]:
    m = re.match(r"\s*(\d+)-(\d+)", name)
    return (int(m.group(1)), int(m.group(2))) if m else (99, 99)


def entry_guide(who: Who, cat: dict[str, Guide] | None = None) -> str | None:
    """The first guide of this race's chain: its lowest #defaultfor guide."""
    cat = cat if cat is not None else catalog(who)
    mine = [g for g in cat.values() if who.race in g.default_for]
    return min(mine, key=lambda g: _levels(g.name)).name if mine else None


def chain(who: Who, start: str | None = None) -> list[str]:
    """Guide names from `start` (default: this race's first) along #next."""
    cat = catalog(who)
    name, out = start or entry_guide(who, cat), []
    while name and name in cat and name not in out:
        out.append(name)
        name = cat[name].next_guide
    return out


def guide_for_level(who: Who, level: int) -> str | None:
    """Where a character of this level belongs on its chain."""
    names = chain(who)
    fits = [n for n in names if _levels(n)[0] <= level <= _levels(n)[1]]
    return fits[0] if fits else (names[-1] if names else None)


def get(name: str, who: Who) -> Guide:
    name = name.replace("_", " ")
    cat = catalog(who)
    if name not in cat:
        raise KeyError(f"no guide named {name!r} for a {who.race} {who.cls}")
    return cat[name]


def resolve_npcs(steps: list[Step]) -> None:
    """.target names -> creature entries, one query for the whole guide.

    Names are not unique (a quest giver can have several entries across
    patches), so a name keeps every entry; pick_npc prefers whichever of
    them is actually a giver or ender of the quest at hand.
    """
    import worlddb
    names = {n.split("::")[0] for s in steps for n in s.targets}
    if not names:
        return
    quoted = ",".join("'" + n.replace("\\", "\\\\").replace("'", "\\'") + "'" for n in names)
    by_name: dict[str, list[int]] = {}
    for e, n in worlddb._query(f"SELECT entry, name FROM creature_template WHERE name IN ({quoted})"):
        by_name.setdefault(n, []).append(int(e))
    for s in steps:
        ids = []
        for n in s.targets:
            base, _, explicit = n.partition("::")
            ids += [int(explicit)] if explicit.isdigit() else by_name.get(base, [])
        s.npcs = list(dict.fromkeys(ids))


# ---------------------------------------------------------------- CLI
def _report():
    who = Who("Warrior", "Dwarf", "Alliance")
    total = dropped = 0
    why: dict[str, int] = {}
    unknown: dict[str, int] = {}
    problems: list[str] = []
    for path in sorted(glob.glob(os.path.join(GUIDE_DIR, "*.lua"))):
        gs = load(path, who)
        n = sum(len(g.steps) for g in gs)
        d = sum(sum(g.dropped.values()) for g in gs)
        total, dropped = total + n, dropped + d
        for g in gs:
            for k, v in g.dropped.items():
                why[k] = why.get(k, 0) + v
            for k, v in g.unknown.items():
                unknown[k] = unknown.get(k, 0) + v
            problems += g.problems
        print(f"  {os.path.basename(path):45} {len(gs):3} guides {n:5} steps  ({d} dropped)")
    print(f"{total} steps kept, {dropped} dropped:",
          ", ".join(f"{k} {v}" for k, v in sorted(why.items(), key=lambda x: -x[1])))
    print("unknown commands:", unknown or "none")
    print(f"{len(problems)} problems")
    for p in problems[:40]:
        print("  " + p)
    print("\nchain for a Dwarf Warrior:")
    for name in chain(who):
        print("  " + name)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        who = Who("Warrior", "Dwarf", "Alliance")
        g = get(" ".join(sys.argv[1:]), who)
        print(f"{g.name}: {len(g.steps)} steps, next {g.next_guide!r}, dropped {g.dropped}")
        for i, s in enumerate(g.steps, 1):
            print(f"{i:3} L{s.line_no:<5} {s.actions} {s.requires or ''} {s.text[:70]}")
    else:
        _report()
