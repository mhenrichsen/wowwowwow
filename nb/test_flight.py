"""nb/flight.py: known nodes from the taximask, and when a leg is flown."""

from nb import flight

FMS = {6: (1573, "Gryth Thurden", -4821.1, -1152.4, 502.3), 2: (352, "Dungar Longdrink", -8835.8, 490.1, 109.7),
       8: (1572, "Thorgrum Borrelson", -5424.9, -2929.9, 347.6)}
AIR = {(6, 2): 197.0, (2, 6): 197.0, (8, 2): 302.0, (6, 8): 120.0, (8, 6): 120.0, (2, 8): 302.0}


def air(a, b):
    return AIR.get((a, b))


def test_mask_nodes():
    assert flight.mask_nodes("250 168 4 0") >= {2, 4, 5, 6, 7, 8} and 1 not in flight.mask_nodes("250 0")
    assert flight.mask_nodes("0 1") == {9}


def test_ironforge_to_stormwind_flies():
    p = flight.plan((-4794.6, -1250.7), (-8867.8, 673.6), {2, 6, 8}, FMS, air)
    assert p and p[:2] == (6, 2) and p[2] > 600, p


def test_short_or_unknown_walks():
    assert flight.plan((-5080, -3098), (-5400, -2900), {2, 6, 8}, FMS, air) is None        # under FLY_MIN
    assert flight.plan((-4794.6, -1250.7), (-8867.8, 673.6), {6, 8}, FMS, air) is None    # Stormwind not known
    # a flight master farther than the walk is worth: walk
    assert flight.plan((-10500, -3000), (-10500, -3900), {2, 6, 8}, FMS, air) is None


if __name__ == "__main__":
    n = 0
    for k, fn in list(globals().items()):
        if k.startswith("test_") and callable(fn):
            fn()
            print("ok", k)
            n += 1
    print(n, "tests passed")
