"""校准 content.js 的屏幕坐标换算（换了显示器/DPI 缩放后可复跑）。

用法：python tools/coord_check/coord_check.py
它会打开同目录的 coord_check.html 并点击几个已知点，
对比页面算出的屏幕坐标与真实鼠标坐标，误差应在 2px 内。
"""
import ctypes
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

PAGE_FILE = Path(__file__).with_name("coord_check.html")

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass
u = ctypes.windll.user32


def title(h):
    b = ctypes.create_unicode_buffer(512)
    u.GetWindowTextW(h, b, 512)
    return b.value


def find():
    hits = []

    @ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
    def cb(h, _):
        t = title(h)
        if not u.IsWindowVisible(h):
            return 1
        # 排除加载失败的错误页（标题就是 URL）
        if t.startswith("file://"):
            return 1
        if ("坐标换算验证页" in t or t.startswith("CAL")
                or t.startswith("MOV")):
            hits.append(h)
        return 1

    u.EnumWindows(cb, 0)
    return hits


def activate(hwnd):
    for _ in range(12):
        u.keybd_event(0x12, 0, 0, 0)
        u.keybd_event(0x12, 0, 2, 0)
        u.SwitchToThisWindow(hwnd, True)
        u.SetForegroundWindow(hwnd)
        time.sleep(0.25)
        if u.GetForegroundWindow() == hwnd:
            return True
    return False


hits = find()
if not hits:
    subprocess.Popen([r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                      "--new-window", PAGE_FILE.as_uri()])
    for _ in range(24):
        time.sleep(0.5)
        hits = find()
        if hits:
            break
if not hits:
    print("FAIL: 没有测试页窗口")
    sys.exit(1)

hwnd = hits[0]
print("窗口:", hex(hwnd), "激活:", activate(hwnd))
# 刷新页面，确保用的是最新 JS
u.keybd_event(0x74, 0, 0, 0)      # F5
u.keybd_event(0x74, 0, 2, 0)
time.sleep(2.0)
activate(hwnd)
time.sleep(0.5)

r = wintypes.RECT()
u.GetWindowRect(hwnd, ctypes.byref(r))
cx = (r.left + r.right) // 2
cy = (r.top + r.bottom) // 2
print(f"窗口矩形: ({r.left},{r.top})-({r.right},{r.bottom})")

targets = [(cx - 120, cy - 80), (cx + 40, cy + 40), (cx + 200, cy - 30)]
ok = 0
for tx, ty in targets:
    u.SetCursorPos(tx, ty)
    time.sleep(0.2)
    u.mouse_event(0x0002, 0, 0, 0, 0)
    u.mouse_event(0x0004, 0, 0, 0, 0)
    time.sleep(0.35)
    parts = title(hwnd).split()
    if len(parts) >= 3 and parts[0] in ("CAL", "MOV"):
        px, py = int(parts[1]), int(parts[2])
        dx, dy = px - tx, py - ty
        good = abs(dx) <= 2 and abs(dy) <= 2
        ok += good
        print(f"鼠标=({tx},{ty}) 页面算=({px},{py}) 误差=({dx:+d},{dy:+d}) "
              f"{'OK' if good else '偏差'}")
    else:
        print(f"鼠标=({tx},{ty}) 标题未更新: {title(hwnd)[:60]!r}")
print(f"\n{ok}/{len(targets)} 个点误差 <= 2px")
