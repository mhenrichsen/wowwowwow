"""Offline tests for vmap.py on the TrinityCore forever vmaps (D:\\tcforever) and the old fallback.

Run: python -m nb.test_vmap   (from the wowbot folder). Reads vmap files only; skips a data set that is missing.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import vmap  # noqa: E402

NEW = vmap.NEW_VMAP_DIR if vmap.is_new_dir(vmap.NEW_VMAP_DIR) else None
OLD = vmap.OLD_VMAP_DIR if os.path.isdir(vmap.OLD_VMAP_DIR) else None


def _wmo(x, y, name, d):
    hits = [s for s in vmap.spawns_near(x, y, 2.0, vmap_dir=d) if not s.m2 and s.name.lower() == name.lower()]
    assert len(hits) == 1, (name, hits)
    return hits[0]


def _near(a, b, tol=0.5):
    return all(abs(p - q) <= tol for p, q in zip(a, b))


def _check_known(d):
    t = _wmo(-4825, -2660, "Dwarven_Guardtower.wmo", d)          # Algaz bunker
    assert _near(t.lo, (-4863.2, -2696.1, 325.7)) and _near(t.hi, (-4788.4, -2626.1, 352.4)), t
    inn = _wmo(-5390, -2960, "Wet_Inn.wmo", d)                    # Thelsamar inn
    assert _near(inn.lo, (-5426.9, -2999.8, 311.4)) and _near(inn.hi, (-5353.3, -2920.8, 348.8)), inn
    near = vmap.spawns_near(-5407, -2960, 3.0, vmap_dir=d)
    names = {s.name for s in near}
    assert "Powderkeg01.m2" in names and "Gunshopammoboxredblock.m2" in names, names
    assert all(s.m2 for s in near if s.name.endswith(".m2")), near
    keg = [s for s in near if s.name == "Powderkeg01.m2"][0]
    assert 0.3 <= keg.height <= 2.0 and keg.width < 2.0 and 312 < keg.z < 320, keg
    assert vmap.inside_building(-5407, -2960, 316, vmap_dir=d)
    assert vmap.inside_building(-4825, -2660, 330, vmap_dir=d)
    assert not vmap.inside_building(-5480, -2900, 340, vmap_dir=d)      # open hillside west of the inn
    assert not vmap.inside_building(-4825, -2660, 380, vmap_dir=d)      # far above the bunker roof


def test_new_known_spawns():
    if NEW is None:
        print("skip: no", vmap.NEW_VMAP_DIR)
        return
    assert vmap.VMAP_DIR == vmap.NEW_VMAP_DIR
    assert vmap.tile_name(0, -5407, -2960, NEW) == "0000/0000_42_37.vmtile"
    _check_known(NEW)
    near = vmap.spawns_near(-5407, -2960, 3.0, vmap_dir=NEW)
    assert len({s.uid for s in near}) == len(near), "a spawn listed twice"
    assert not any(s.name.startswith("FILE") for s in near), "vmap_names.tsv missing? run: python vmap.py --build-names"


def test_old_fallback_known_spawns():
    if OLD is None:
        print("skip: no", vmap.OLD_VMAP_DIR)
        return
    assert vmap.tile_name(0, -5407, -2960, OLD) == "000_37_42.vmtile"
    _check_known(OLD)


def test_unknown_fid_falls_back_to_vmo_kind():
    if NEW is None:
        return
    raw = next(f[:-4] for f in os.listdir(NEW) if f.startswith("FILE") and f.endswith(".xxx.vmo"))
    saved = vmap._names
    try:
        vmap._names = {}
        name, m2 = vmap._resolve(raw, NEW)
        assert name == raw and isinstance(m2, bool)
    finally:
        vmap._names = saved


def test_fast_warm_queries():
    if NEW is None:
        return
    vmap.inside_building(-5400, -2950, 320)
    t = time.perf_counter()
    for i in range(2000):
        vmap.inside_building(-5400 + i * 0.05, -2950, 320)
    dt = (time.perf_counter() - t) / 2000
    assert dt < 0.0005, f"{dt * 1e6:.0f} us per inside_building"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
