"""Minimal WDC5 (.db2) reader for the Forever client's tables (D:\\tcforever\\server\\data\\dbc\\enUS).

read(path) -> {id: [field values]}: each field a list of its array elements (ints; floats are returned as raw
uint32 and turned into floats by the caller with as_float). Handles the storage types the small tables use:
plain, bitpacked, common data, pallet and pallet array. No encrypted sections, no string fields.
"""
from __future__ import annotations

import struct


def as_float(u: int) -> float:
    return struct.unpack("<f", struct.pack("<I", u & 0xFFFFFFFF))[0]


def read(path: str) -> dict:
    d = open(path, "rb").read()
    if d[:4] != b"WDC5":
        raise ValueError(f"{path}: not WDC5")
    o = 4 + 4 + 128
    (rec_count, field_count, rec_size, st_size, _th, _lh, min_id, max_id, _loc, flags, id_index, total_fields,
     bp_off, lookup_cols, fsi_size, common_size, pallet_size, n_sec) = struct.unpack_from("<4I2I2IiHH7I", d, o)
    o += struct.calcsize("<4I2I2IiHH7I")
    secs = []
    for _ in range(n_sec):
        secs.append(struct.unpack_from("<Q8I", d, o))
        o += struct.calcsize("<Q8I")
    fields = [struct.unpack_from("<hH", d, o + 4 * i) for i in range(field_count)]
    o += 4 * field_count
    fsi = [struct.unpack_from("<HHIIIII", d, o + 24 * i) for i in range(field_count)]
    o += fsi_size
    pallet = d[o:o + pallet_size]
    o += pallet_size
    common = d[o:o + common_size]
    o += common_size
    # per-field offsets into the pallet / common blocks (additional data is laid out field after field)
    pal_at, com_at, pa, ca = [], [], 0, 0
    for f in fsi:
        pal_at.append(pa)
        com_at.append(ca)
        if f[3] in (3, 4):
            pa += f[2]
        elif f[3] == 2:
            ca += f[2]
    commons = []
    for i, f in enumerate(fsi):
        m = {}
        if f[3] == 2:
            blk = common[com_at[i]:com_at[i] + f[2]]
            for k in range(0, len(blk), 8):
                rid, val = struct.unpack_from("<II", blk, k)
                m[rid] = val
        commons.append(m)
    out = {}
    if flags & 1:
        raise ValueError("offset-map (sparse) tables are not supported")
    for sec in secs:
        _key, foff, n, sst, _oend, idl, rel, _omc, cpy = sec
        recs = d[foff:foff + n * rec_size]
        p = foff + n * rec_size + sst
        ids = list(struct.unpack_from(f"<{idl // 4}I", d, p)) if idl else []
        p += idl
        copies = [struct.unpack_from("<II", d, p + 8 * k) for k in range(cpy)]
        p += 8 * cpy
        relmap = {}
        if rel:                                   # relationship data: the foreign key (TransportAnimation.TransportID)
            num, _rmin, _rmax = struct.unpack_from("<3I", d, p)
            for k in range(num):
                fid, ix = struct.unpack_from("<II", d, p + 12 + 8 * k)
                relmap[ix] = fid
        for r in range(n):
            base = r * rec_size
            raw = int.from_bytes(recs[base:base + rec_size] + b"\0" * 8, "little")
            vals = []
            for i, f in enumerate(fsi):
                bo, bs, add, st, v1, v2, v3 = f
                if st == 0:                       # plain: field_structure gives size (32 - size bits) / offset bytes
                    size = (32 - fields[i][0]) // 8
                    nxt = fields[i + 1][1] if i + 1 < field_count else rec_size
                    cnt = max(1, (nxt - fields[i][1]) // max(1, size))
                    vals.append([int.from_bytes(recs[base + fields[i][1] + size * k:base + fields[i][1] + size * (k + 1)],
                                                "little") for k in range(cnt)])
                elif st in (1, 5):                # bitpacked (signed: 5)
                    v = (raw >> bo) & ((1 << bs) - 1)
                    if st == 5 and v >> (bs - 1):
                        v -= 1 << bs
                    vals.append([v])
                elif st == 2:                     # common data: default v1 unless the id has its own value
                    vals.append([None])           # filled once the id is known
                elif st == 3:                     # pallet
                    ix = (raw >> bo) & ((1 << bs) - 1)
                    vals.append([struct.unpack_from("<I", pallet, pal_at[i] + 4 * ix)[0]])
                elif st == 4:                     # pallet array of v3 elements
                    ix = (raw >> bo) & ((1 << bs) - 1)
                    vals.append(list(struct.unpack_from(f"<{v3}I", pallet, pal_at[i] + 4 * v3 * ix)))
                else:
                    raise ValueError(f"storage type {st}")
            rid = ids[r] if ids else vals[id_index][0]
            for i, f in enumerate(fsi):
                if f[3] == 2:
                    vals[i] = [commons[i].get(rid, f[4])]
            if relmap:
                vals.append([relmap.get(r, 0)])   # last field: the relationship's foreign id
            out[rid] = vals
        for new, old in copies:
            if old in out:
                out[new] = out[old]
    return out
