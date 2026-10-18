"""python -m nb.evi <inst> "<lua expr>"  - /be eval in one instance's client."""
import sys
from nb.ev import ev
from nb.eye import Eye
from nb.win import Keys
from nb.multi import find_instance_window

h = find_instance_window(int(sys.argv[1]))
e, k = Eye(h), Keys(h)
for expr in sys.argv[2:]:
    print(expr, "=>", ev(expr, e, k, wait=3))
