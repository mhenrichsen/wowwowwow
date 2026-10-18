import ctypes, sys, time
from ctypes import wintypes as wt
from nb.win import find_window, Capture
from PIL import Image
u=ctypes.windll.user32
def click(x, y, shot=None, wait=1.5):
    h=find_window()
    u.SetForegroundWindow(h); time.sleep(0.4)
    pt=wt.POINT(int(x), int(y)); u.ClientToScreen(h, ctypes.byref(pt))
    u.SetCursorPos(pt.x, pt.y); time.sleep(0.25)
    u.mouse_event(2,0,0,0,0); time.sleep(0.08); u.mouse_event(4,0,0,0,0)
    time.sleep(wait)
    if shot:
        a=Capture(h).grab(); Image.fromarray(a[:,:,[2,1,0]]).save(shot)
if __name__=="__main__":
    click(float(sys.argv[1]), float(sys.argv[2]), sys.argv[3])
