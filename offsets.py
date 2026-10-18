"""Memory layout of the WoW 1.12.1 (build 5875) client.

Two kinds of numbers live here:

* Descriptor field indices - taken from the server source
  (core/src/game/Objects/UpdateFields_1_12_1.h). These are exact: the server
  sends exactly these fields and the client stores them in this order.
* Static addresses / struct offsets inside WoW.exe - these are community-known
  values for build 5875 and are only trusted after probe.py confirms them
  against what is on screen. Each is marked with how it was verified.
"""

# --------------------------------------------------------------- statics
OBJECT_MANAGER = 0x00B41414      # ptr to the client connection's object manager
TARGET_GUID = 0x00B4E2D8         # u64, current target
MAP_ID = 0x00C4C498              # u32, current map (0 EK, 1 Kalimdor, 369 tram); found by a memory diff across a boat ride, 2026-09-20
PLAYER_NAME = 0x00C27D88         # char[], local player name (verified: memory scan)

# --------------------------------------------------------- object manager
OM_FIRST_OBJECT = 0xAC
OM_LOCAL_GUID = 0xC0
OBJ_NEXT = 0x3C
OBJ_TYPE = 0x14
OBJ_GUID = 0x30
OBJ_DESCRIPTORS = 0x08

# Unit movement info, stored on the object itself (not in descriptors).
UNIT_POS_X = 0x9B8
UNIT_POS_Y = 0x9BC
UNIT_POS_Z = 0x9C0
UNIT_FACING = 0x9C4

# Object types
TYPE_ITEM, TYPE_CONTAINER, TYPE_UNIT, TYPE_PLAYER = 1, 2, 3, 4
TYPE_GAMEOBJECT, TYPE_DYNAMICOBJECT, TYPE_CORPSE = 5, 6, 7

# ------------------------------------------------ descriptor fields (x4 bytes)
_OBJECT_END = 0x6
_UNIT_END = _OBJECT_END + 0xB6


def _f(index: int) -> int:
    return index * 4


OBJECT_FIELD_ENTRY = _f(0x3)
UNIT_FIELD_TARGET = _f(_OBJECT_END + 0xA)
UNIT_FIELD_SUMMONEDBY = _f(_OBJECT_END + 0x6)
UNIT_FIELD_CREATEDBY = _f(_OBJECT_END + 0x8)
UNIT_FIELD_HEALTH = _f(_OBJECT_END + 0x10)
UNIT_FIELD_POWER1 = _f(_OBJECT_END + 0x11)
UNIT_FIELD_MAXHEALTH = _f(_OBJECT_END + 0x16)
UNIT_FIELD_MAXPOWER1 = _f(_OBJECT_END + 0x17)
UNIT_FIELD_LEVEL = _f(_OBJECT_END + 0x1C)
UNIT_FIELD_FACTIONTEMPLATE = _f(_OBJECT_END + 0x1D)
UNIT_FIELD_FLAGS = _f(_OBJECT_END + 0x28)
UNIT_FIELD_AURA = _f(_OBJECT_END + 0x29)          # 48 x spell id
UNIT_FIELD_BYTES_1 = _f(_OBJECT_END + 0x84)       # byte 0 = stand state
UNIT_DYNAMIC_FLAGS = _f(_OBJECT_END + 0x89)
UNIT_CHANNEL_SPELL = _f(_OBJECT_END + 0x8A)
UNIT_NPC_FLAGS = _f(_OBJECT_END + 0x8D)
PLAYER_XP = _f(_UNIT_END + 0x210)
PLAYER_NEXT_LEVEL_XP = _f(_UNIT_END + 0x211)
PLAYER_FIELD_COINAGE = _f(_UNIT_END + 0x3DC)
PLAYER_FIELD_PACK_SLOT_1 = _f(_UNIT_END + 0x158)  # 16 backpack slots x guid
PLAYER_FIELD_BAG_SLOT_1 = _f(_UNIT_END + 0x150)   # 4 equipped bags x guid (end of the inventory slots)
_ITEM_END = _OBJECT_END + 0x2A
CONTAINER_FIELD_NUM_SLOTS = _f(_ITEM_END + 0x0)
CONTAINER_FIELD_SLOT_1 = _f(_ITEM_END + 0x2)      # 36 x guid

UNIT_FLAG_IN_COMBAT = 0x00080000
UNIT_FLAG_SKINNABLE = 0x04000000
UNIT_DYNFLAG_LOOTABLE = 0x0001
UNIT_DYNFLAG_TAPPED = 0x0004            # someone has tagged it ...
UNIT_DYNFLAG_TAPPED_BY_PLAYER = 0x0008  # ... and that someone is us
UNIT_DYNFLAG_DEAD = 0x0020

# Movement flags of the unit (MOVEFLAG_FORWARD = 0x1, ...). 0 = standing still.
# Verified: flips to 0x1 while click-to-move walks, 0 on arrival.
UNIT_MOVE_FLAGS = 0x9E8

PLAYER_FLAGS = _f(_UNIT_END + 0x2)
PLAYER_FLAGS_GHOST = 0x10
CORPSE_FIELD_OWNER = _f(_OBJECT_END + 0x0)
CORPSE_FIELD_POS_X = _f(_OBJECT_END + 0x3)   # x, y, z consecutive floats
UNIT_FLAG_LOOTING = 0x00000400

# Local player object (not descriptors). Verified by disassembly + live test:
#   0x5DF460 CGPlayer_C::SetAutoLoot(bool) writes this byte; when a loot
#   window opens with it set, the client's own auto-loot loop (0x4C1FA0)
#   picks up every slot and the coins, then clears it again.
# Note: Lua LootSlot() in 1.12 only CONFIRMS a pending bind-on-pickup slot;
# the real pickup is the loot button's native click handler (0x4C1820).
PLAYER_LOOT_GUID = 0x1D28
PLAYER_AUTOLOOT = 0x1D30

# Verified by memory scan against the known character state:
SPELLBOOK = 0x00B700F0      # u32 spell ids, zero-terminated
ACTION_BAR = 0x00BC6980     # 120 x u32; 0x80000000 | itemId for items

ITEM_FIELD_STACK_COUNT = _f(_OBJECT_END + 0x8)
ITEM_FIELD_DURABILITY = _f(_OBJECT_END + 0x28)
ITEM_FIELD_MAX_DURABILITY = _f(_OBJECT_END + 0x29)
UNIT_NPC_FLAG_VENDOR = 0x4
CORPSE_FIELD_FLAGS = _f(0x23)
CORPSE_FLAG_BONES = 0x01                     # left behind after resurrecting

UNIT_FIELD_BYTES_0 = _f(_OBJECT_END + 0x1E)   # race, class, gender, power type
UNIT_FIELD_POWER2 = _f(_OBJECT_END + 0x12)    # rage (x10)
UNIT_FIELD_POWER4 = _f(_OBJECT_END + 0x14)    # energy
UNIT_FIELD_MAXPOWER2 = _f(_OBJECT_END + 0x18)
UNIT_FIELD_MAXPOWER4 = _f(_OBJECT_END + 0x1A)

# Quest log: 20 entries of 3 u32 each - quest id, then state/counters.
PLAYER_QUEST_LOG_1_1 = _f(_UNIT_END + 0xA)
QUEST_LOG_ENTRIES = 20
QUEST_LOG_STRIDE = 3 * 4

GAMEOBJECT_POS_X = _f(_OBJECT_END + 0x9)       # x, y, z consecutive
GAMEOBJECT_DYN_FLAGS = _f(_OBJECT_END + 0xD)   # bit 0: usable/activatable for us
GAMEOBJECT_TYPE_ID = _f(_OBJECT_END + 0xF)

UNIT_CASTING_SPELL = 0xC8C      # CGUnit_C: spell id being cast (0 when idle), verified 2026-09-20
