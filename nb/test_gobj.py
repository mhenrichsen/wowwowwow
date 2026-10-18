"""gobj.py boxes and navmesh._avoid_doodads round them (python -m nb.test_gobj)."""

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gobj      # noqa: E402
import navmesh   # noqa: E402


def test_world_box_yaw():
    # a 4 x 1 yd model turned 90 degrees stands 1 x 4 in the world
    q = gobj.quat((0, 0, 0, 0), math.pi / 2)
    b = gobj.world_box((-2, -0.5, 0, 2, 0.5, 2), 10, 20, 5, q, 1.0)
    assert abs(b[0] - 9.5) < 1e-6 and abs(b[2] - 10.5) < 1e-6, b
    assert abs(b[1] - 18) < 1e-6 and abs(b[3] - 22) < 1e-6, b
    assert abs(b[4] - 5) < 1e-6 and abs(b[5] - 7) < 1e-6, b


def test_world_box_scale_and_quat():
    # the quaternion wins over the yaw; size scales the box
    b = gobj.world_box((-1, -1, 0, 1, 1, 1), 0, 0, 0, gobj.quat((0, 0, 0, 1), 1.0), 2.0)
    assert b[:4] == (-2, -2, 2, 2) and b[5] == 2, b


def test_solid():
    assert gobj.solid((0, 0, 2, 2, 0, 1.5))
    assert not gobj.solid((0, 0, 2, 2, 0, 0.3))        # stepped over
    assert not gobj.solid((0, 0, 50, 50, 0, 10))       # a dock we walk on


def test_blocks_at():
    e = (0, 0, 1, 1, 100, 103, "x")
    assert gobj.blocks_at(e, 100.2)
    assert not gobj.blocks_at(e, 104)                  # we walk over it (a bridge deck above)
    assert not gobj.blocks_at(e, 97)                   # we walk under it


def test_two_corner_detour():
    # a leg straight through the middle of a box: no single corner clears it
    real = navmesh._doodad_boxes
    navmesh._doodad_boxes = lambda *a: []
    try:
        box = (-1.0, -1.0, 1.0, 1.0, 0.0)
        out = navmesh._avoid_doodads((-10, 0, 0), [(10, 0, 0)], extra_boxes=[box])
        pts = [(-10, 0, 0)] + out
        assert len(out) >= 3, out
        assert not any(navmesh._seg_hits_rect(a, b, (-0.95, -0.95, 0.95, 0.95)) for a, b in zip(pts, pts[1:])), out
        assert out[-1] == (10, 0, 0)
    finally:
        navmesh._doodad_boxes = real


def test_goal_inside_box_left_alone():
    # walking to a chest: the box round the goal is not a detour
    real_d, real_b = navmesh._doodad_boxes, gobj.boxes_near
    navmesh._doodad_boxes = lambda *a: []
    gobj.boxes_near = lambda *a: [(9.5, -0.5, 10.5, 0.5, 0.0, 1.5, "chest")]
    try:
        out = navmesh._avoid_doodads((0, 0, 0), [(10, 0, 0)])
        assert out == [(10, 0, 0)], out
        out = navmesh._avoid_doodads((0, 0, 0), [(20, 0, 0)])
        assert len(out) > 1, out                      # walking past it: round
    finally:
        navmesh._doodad_boxes, gobj.boxes_near = real_d, real_b


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
