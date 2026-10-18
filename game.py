"""Object-manager view of the game world, built purely from memory reads."""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field

import offsets as O
from wowmem import Process


@dataclass
class Unit:
    base: int
    guid: int
    type: int
    entry: int = 0
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    facing: float = 0.0
    health: int = 0
    max_health: int = 0
    power: int = 0
    max_power: int = 0
    level: int = 0
    faction: int = 0
    flags: int = 0
    dyn_flags: int = 0
    npc_flags: int = 0
    target: int = 0
    summoned_by: int = 0
    move_flags: int = 0
    stand_state: int = 0          # 0 standing, 1 sitting, ...
    race: int = 0
    class_id: int = 0
    power_type: int = 0           # 0 mana, 1 rage, 3 energy
    auras: list[int] = field(default_factory=list)

    @property
    def dead(self) -> bool:
        return self.health == 0 or bool(self.dyn_flags & O.UNIT_DYNFLAG_DEAD)

    @property
    def in_combat(self) -> bool:
        return bool(self.flags & O.UNIT_FLAG_IN_COMBAT)

    @property
    def lootable(self) -> bool:
        return bool(self.dyn_flags & O.UNIT_DYNFLAG_LOOTABLE)

    @property
    def tapped_by_other(self) -> bool:
        return bool(self.dyn_flags & O.UNIT_DYNFLAG_TAPPED) and \
            not self.dyn_flags & O.UNIT_DYNFLAG_TAPPED_BY_PLAYER

    @property
    def hp_pct(self) -> float:
        return 100.0 * self.health / self.max_health if self.max_health else 0.0

    @property
    def mana_pct(self) -> float:
        return 100.0 * self.power / self.max_power if self.max_power else 0.0

    def dist(self, other: "Unit | tuple[float, float, float]") -> float:
        ox, oy, oz = (other.x, other.y, other.z) if isinstance(other, Unit) else other
        return math.dist((self.x, self.y, self.z), (ox, oy, oz))

    @property
    def sitting(self) -> bool:
        return self.stand_state != 0

    @property
    def moving(self) -> bool:
        return self.move_flags != 0

    def facing_error(self, other: "Unit") -> float:
        """Signed angle (radians) we would need to turn to face `other`."""
        return (self.angle_to(other) - self.facing + math.pi) % (2 * math.pi) - math.pi

    def angle_to(self, other: "Unit") -> float:
        return math.atan2(other.y - self.y, other.x - self.x) % (2 * math.pi)


@dataclass
class Player(Unit):
    xp: int = 0
    next_xp: int = 0
    money: int = 0
    player_flags: int = 0

    @property
    def ghost(self) -> bool:
        return bool(self.player_flags & O.PLAYER_FLAGS_GHOST)

    @property
    def dead(self) -> bool:
        return not self.ghost and self.health == 0

    @property
    def looting(self) -> bool:
        return bool(self.flags & O.UNIT_FLAG_LOOTING)


class Game:
    def __init__(self, proc: Process):
        self.p = proc

    # ---------------------------------------------------------- statics
    def object_manager(self) -> int:
        return self.p.u32(O.OBJECT_MANAGER)

    def map_id(self) -> int | None:
        """The map the client is on (0 Eastern Kingdoms, 1 Kalimdor, 369 tram)."""
        raw = self.p.try_read(O.MAP_ID, 4)
        return None if raw is None else struct.unpack("<I", raw)[0]

    def in_world(self) -> bool:
        om = self.p.try_read(O.OBJECT_MANAGER, 4)
        if not om:
            return False
        om = struct.unpack("<I", om)[0]
        return om != 0 and self.p.try_read(om + O.OM_LOCAL_GUID, 8) not in (None, b"\0" * 8)

    def local_guid(self) -> int:
        return self.p.u64(self.object_manager() + O.OM_LOCAL_GUID)

    def target_guid(self) -> int:
        return self.p.u64(O.TARGET_GUID)

    def player_name(self) -> str:
        return self.p.cstr(O.PLAYER_NAME)

    # --------------------------------------------------------- iteration
    def iter_objects(self):
        """Yield (base, guid, type) for every object the client knows about."""
        cur = self.p.u32(self.object_manager() + O.OM_FIRST_OBJECT)
        seen = 0
        # A valid object pointer is even; the list terminator has bit 0 set.
        while cur and not cur & 1 and seen < 5000:
            hdr = self.p.try_read(cur, 0x40)
            if hdr is None:
                break
            typ = struct.unpack_from("<I", hdr, O.OBJ_TYPE)[0]
            guid = struct.unpack_from("<Q", hdr, O.OBJ_GUID)[0]
            yield cur, guid, typ
            cur = struct.unpack_from("<I", hdr, O.OBJ_NEXT)[0]
            seen += 1

    def _fill_unit(self, u: Unit) -> Unit:
        p = self.p
        pos = p.read(u.base + O.UNIT_POS_X, 16)
        u.x, u.y, u.z, u.facing = struct.unpack("<4f", pos)
        u.move_flags = p.u32(u.base + O.UNIT_MOVE_FLAGS)
        d = p.u32(u.base + O.OBJ_DESCRIPTORS)
        # One read of the whole unit descriptor block is much cheaper than
        # twenty separate ReadProcessMemory calls.
        blk = p.read(d, O.UNIT_NPC_FLAGS + 4)

        def g(off, fmt="<I"):
            return struct.unpack_from(fmt, blk, off)[0]

        u.entry = g(O.OBJECT_FIELD_ENTRY)
        u.target = g(O.UNIT_FIELD_TARGET, "<Q")
        u.summoned_by = g(O.UNIT_FIELD_SUMMONEDBY, "<Q")
        u.health = g(O.UNIT_FIELD_HEALTH)
        u.max_health = g(O.UNIT_FIELD_MAXHEALTH)
        b0 = g(O.UNIT_FIELD_BYTES_0)
        u.race, u.class_id, u.power_type = b0 & 0xFF, (b0 >> 8) & 0xFF, (b0 >> 24) & 0xFF
        # "power" is whatever the unit uses: mana, rage (stored x10) or energy
        if u.power_type == 1:
            u.power = g(O.UNIT_FIELD_POWER2) // 10
            u.max_power = g(O.UNIT_FIELD_MAXPOWER2) // 10
        elif u.power_type == 3:
            u.power = g(O.UNIT_FIELD_POWER4)
            u.max_power = g(O.UNIT_FIELD_MAXPOWER4)
        else:
            u.power = g(O.UNIT_FIELD_POWER1)
            u.max_power = g(O.UNIT_FIELD_MAXPOWER1)
        u.level = g(O.UNIT_FIELD_LEVEL)
        u.faction = g(O.UNIT_FIELD_FACTIONTEMPLATE)
        u.flags = g(O.UNIT_FIELD_FLAGS)
        u.dyn_flags = g(O.UNIT_DYNAMIC_FLAGS)
        u.npc_flags = g(O.UNIT_NPC_FLAGS)
        u.stand_state = g(O.UNIT_FIELD_BYTES_1) & 0xFF
        u.auras = [a for a in struct.unpack_from("<48I", blk, O.UNIT_FIELD_AURA) if a]
        return u

    def units(self) -> list[Unit]:
        out = []
        for base, guid, typ in self.iter_objects():
            if typ == O.TYPE_UNIT:
                try:
                    out.append(self._fill_unit(Unit(base, guid, typ)))
                except Exception:
                    pass  # object freed between list walk and read
        return out

    def player(self) -> Player:
        me = self.local_guid()
        for base, guid, typ in self.iter_objects():
            if guid == me:
                pl = self._fill_unit(Player(base, guid, typ))
                d = self.p.u32(base + O.OBJ_DESCRIPTORS)
                pl.xp = self.p.u32(d + O.PLAYER_XP)
                pl.next_xp = self.p.u32(d + O.PLAYER_NEXT_LEVEL_XP)
                pl.money = self.p.u32(d + O.PLAYER_FIELD_COINAGE)
                pl.player_flags = self.p.u32(d + O.PLAYER_FLAGS)
                return pl
        raise RuntimeError("local player not found in object manager")

    def my_corpse(self) -> tuple[float, float, float] | None:
        """Position of our own corpse, if the client has it in view."""
        me = self.local_guid()
        for base, guid, typ in self.iter_objects():
            if typ == O.TYPE_CORPSE:
                d = self.p.u32(base + O.OBJ_DESCRIPTORS)
                if self.p.u64(d + O.CORPSE_FIELD_OWNER) != me:
                    continue
                # bones from an earlier death stay owned by us - not reclaimable
                if self.p.u32(d + O.CORPSE_FIELD_FLAGS) & O.CORPSE_FLAG_BONES:
                    continue
                return struct.unpack("<3f", self.p.read(d + O.CORPSE_FIELD_POS_X, 12))
        return None

    def containers(self) -> list[tuple[int, int, list[int]]]:
        """Equipped bags: (bag guid, slots, [item guid per used slot])."""
        d = self.p.u32(self.player().base + O.OBJ_DESCRIPTORS)
        bag_guids = [g for g in struct.unpack("<4Q", self.p.read(d + O.PLAYER_FIELD_BAG_SLOT_1, 32)) if g]
        out = []
        if not bag_guids:
            return out
        for base, guid, typ in self.iter_objects():
            if typ == O.TYPE_CONTAINER and guid in bag_guids:
                cd = self.p.u32(base + O.OBJ_DESCRIPTORS)
                n = min(self.p.u32(cd + O.CONTAINER_FIELD_NUM_SLOTS), 36)
                slots = struct.unpack(f"<{n}Q", self.p.read(cd + O.CONTAINER_FIELD_SLOT_1, n * 8)) if n else ()
                out.append((guid, n, [g for g in slots if g]))
        return out

    def free_bag_slots(self) -> int:
        """Empty bag *equipment* slots (room for another bag)."""
        d = self.p.u32(self.player().base + O.OBJ_DESCRIPTORS)
        return sum(1 for g in struct.unpack("<4Q", self.p.read(d + O.PLAYER_FIELD_BAG_SLOT_1, 32)) if not g)

    def bag_capacity(self) -> int:
        return 16 + sum(n for _, n, _ in self.containers())

    def _all_item_guids(self) -> set[int]:
        d = self.p.u32(self.player().base + O.OBJ_DESCRIPTORS)
        guids = {g for g in struct.unpack("<16Q", self.p.read(d + O.PLAYER_FIELD_PACK_SLOT_1, 128)) if g}
        for _, _, items in self.containers():
            guids.update(items)
        return guids

    def bag_item_count(self) -> int:
        """Occupied slots in the backpack and every equipped bag."""
        return len(self._all_item_guids())

    def gameobjects(self) -> list[dict]:
        """Chests, plants, crates, quest objects... in view."""
        out = []
        for base, guid, typ in self.iter_objects():
            if typ != O.TYPE_GAMEOBJECT:
                continue
            try:
                d = self.p.u32(base + O.OBJ_DESCRIPTORS)
                entry = self.p.u32(d + O.OBJECT_FIELD_ENTRY)
                x, y, z = struct.unpack("<3f", self.p.read(d + O.GAMEOBJECT_POS_X, 12))
                out.append({"guid": guid, "entry": entry, "x": x, "y": y, "z": z,
                            "type": self.p.u32(d + O.GAMEOBJECT_TYPE_ID),
                            "dyn": self.p.u32(d + O.GAMEOBJECT_DYN_FLAGS)})
            except Exception:
                pass
        return out

    def quest_log(self) -> dict[int, tuple[int, int]]:
        """Quests in the log: {quest id: (state word, counters word)}."""
        d = self.p.u32(self.player().base + O.OBJ_DESCRIPTORS)
        raw = self.p.read(d + O.PLAYER_QUEST_LOG_1_1, O.QUEST_LOG_ENTRIES * O.QUEST_LOG_STRIDE)
        out = {}
        for i in range(O.QUEST_LOG_ENTRIES):
            qid, a, b = struct.unpack_from("<3I", raw, i * O.QUEST_LOG_STRIDE)
            if qid:
                out[qid] = (a, b)
        return out

    def bag_items(self) -> dict[int, int]:
        """Bag contents (backpack + equipped bags) as {item entry: total count}."""
        guids = self._all_item_guids()
        out: dict[int, int] = {}
        for base, guid, typ in self.iter_objects():
            # bags in the backpack are containers, not items - count them too
            if typ in (O.TYPE_ITEM, O.TYPE_CONTAINER) and guid in guids:
                idesc = self.p.u32(base + O.OBJ_DESCRIPTORS)
                entry = self.p.u32(idesc + O.OBJECT_FIELD_ENTRY)
                count = self.p.u32(idesc + O.ITEM_FIELD_STACK_COUNT)
                out[entry] = out.get(entry, 0) + count
        return out

    def unit(self, guid: int) -> Unit | None:
        for base, g, typ in self.iter_objects():
            if g == guid and typ in (O.TYPE_UNIT, O.TYPE_PLAYER):
                return self._fill_unit(Unit(base, g, typ))
        return None
