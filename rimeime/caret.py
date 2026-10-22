"""光标/输入位置获取。

定位链（自上而下，先精确后兜底）：
  1. GetGUIThreadInfo —— 标准 Win32 控件（记事本、Office、部分登录框）
  2. 浏览器扩展上报   —— Chromium 系应用内的网页输入框（精确）
  3. 最后鼠标点击位置 —— 无法注入脚本的 Electron 应用兜底
  4. 前台窗口顶部中央 —— 最后手段

为什么需要 2/3：Chrome / Edge / Electron（WorkBuddy、VS Code 等）
不向 Windows 暴露插入符——GetGUIThreadInfo 返回空，UIA 在这些应用上
直接 E_NOTIMPL（实测 Chromium 只在检测到读屏软件时才建可访问性树，
且 MSAA 树里只有浏览器 UI，没有网页内容）。

所有 Win32 调用都声明原型：x64 下不声明会按 32 位截断 HWND 等句柄，
曾导致 access violation（详见 README 的 Win32 ctypes 一节）。
"""
import ctypes
from ctypes import wintypes

from . import caret_feed

user32 = ctypes.windll.user32

# ---- 原型声明（x64 安全）
user32.GetForegroundWindow.argtypes = ()
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND,
                                            ctypes.POINTER(wintypes.DWORD))
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetGUIThreadInfo.argtypes = (wintypes.DWORD, ctypes.c_void_p)
user32.GetGUIThreadInfo.restype = wintypes.BOOL
user32.ClientToScreen.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.POINT))
user32.ClientToScreen.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
user32.GetClassNameW.restype = ctypes.c_int
user32.MonitorFromWindow.argtypes = (wintypes.HWND, wintypes.DWORD)
user32.MonitorFromWindow.restype = ctypes.c_void_p
user32.GetMonitorInfoW.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
user32.GetMonitorInfoW.restype = wintypes.BOOL
user32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
user32.GetCursorPos.restype = wintypes.BOOL

# Chromium 系窗口（Chrome / Edge / WorkBuddy / VS Code / 微信新版等共用
# Chrome_WidgetWin_1）。这类窗口的 GetGUIThreadInfo 不可信：要么返回空，
# 要么返回浏览器自身 UI（地址栏、工具栏按钮）的插入符——用它定位会让
# 候选窗飞到标签栏或窗口中央，离真实光标很远。所以对它们优先采用
# 扩展上报 / 点击位置，native 降为末位。
CHROMIUM_CLASSES = ("Chrome_WidgetWin_1",)
# native 结果的合理范围（像素）：插入符高度、宽度
CARET_MIN_H, CARET_MAX_H = 6, 160
CARET_MAX_W = 40


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


def get_foreground_title():
    """前台窗口标题；失败返回空串。"""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def get_foreground_class():
    """前台窗口类名；失败返回空串。"""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def is_chromium_window():
    """前台是否为 Chromium 系窗口（Chrome/Edge/Electron 应用）。"""
    return get_foreground_class() in CHROMIUM_CLASSES


def get_caret_screen_pos(debug=False):
    """返回光标（插入符）的屏幕坐标 (x, y)；失败或不可信返回 None。"""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = wintypes.DWORD()
    tid = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    info = GUITHREADINFO()
    info.cbSize = ctypes.sizeof(GUITHREADINFO)
    if not user32.GetGUIThreadInfo(tid, ctypes.byref(info)):
        return None
    rc = info.rcCaret
    if rc.left == 0 and rc.top == 0 and rc.right == 0 and rc.bottom == 0:
        return None
    # 合理性校验：插入符是个细长条。矩形过宽/过高说明拿到的其实是某个
    # 控件（工具栏按钮、地址栏）的矩形，不是文本插入符，宁可丢弃。
    w = rc.right - rc.left
    h = rc.bottom - rc.top
    if not (CARET_MIN_H <= h <= CARET_MAX_H) or w > CARET_MAX_W:
        if debug:
            print(f"[caret] native 丢弃：rcCaret={w}x{h}（不像插入符）")
        return None
    pt = wintypes.POINT(rc.left, rc.bottom)
    target = info.hwndCaret or info.hwndFocus or hwnd
    user32.ClientToScreen(target, ctypes.byref(pt))
    if debug:
        print(f"[caret] native ok: rcCaret=({rc.left},{rc.top},{rc.right},{rc.bottom})"
              f" -> screen=({pt.x},{pt.y})")
    return (pt.x, pt.y)


def get_foreground_rect():
    """前台窗口屏幕矩形 (left, top, right, bottom)；失败返回 None。"""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return (rect.left, rect.top, rect.right, rect.bottom)


def get_window_fallback_pos():
    """兜底锚点（无精确来源时）。

    顺序：
      1. 该窗口上次的鼠标点击位置——用户几乎总是先点输入框再打字，
         哪怕切走再切回来，这个位置仍是最接近光标的猜测；
      2. 当前鼠标位置（仅当鼠标停在前台窗口内）；
      3. 窗口中心 + 顶部偏移——最后手段。
    早前这里直接用窗口中心，导致宽窗口里候选窗横向跑到屏幕中间，
    与左侧输入框相距很远（用户反馈"偏右很远"）。
    """
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    hist = caret_feed.get_click(hwnd=hwnd)
    if hist:
        return hist
    fg = get_foreground_rect()
    pt = wintypes.POINT()
    if user32.GetCursorPos(ctypes.byref(pt)) and fg:
        if fg[0] <= pt.x <= fg[2] and fg[1] <= pt.y <= fg[3]:
            return (pt.x, pt.y)
    if not fg:
        return None
    return ((fg[0] + fg[2]) // 2, fg[1] + 48)


def get_input_pos(debug=False):
    """输入位置（候选窗锚点）：返回 (x, y) 或 None。

    y 为光标底边/点击位置，候选窗在 y 下方展开。
    debug=True 时打印选用来源与坐标（SHANGKEY_DEBUG_CARET=1 可常开）。

    来源优先级按窗口类型分两套：
      Chromium 系（网页/Electron）：ext -> native -> click
        网页输入框没有 Win32 插入符，靠扩展上报；而地址栏/查找框这类
        浏览器自带输入框恰恰只有 native 准（扩展管不到浏览器 UI），
        所以 native 排在 click 之前。
      Win32 原生应用：native -> ext -> click
        原生控件的 rcCaret 最准，优先用。
    """
    title = get_foreground_title()
    native = get_caret_screen_pos(debug=debug)
    ext = caret_feed.get_reported(title)
    # 点击位置只在"点击的窗口 == 当前前台窗口"时才算数
    click = caret_feed.get_click(hwnd=user32.GetForegroundWindow() or 0)

    if is_chromium_window():
        order = (("ext", ext), ("native", native), ("click", click))
    else:
        order = (("native", native), ("ext", ext), ("click", click))

    pos, src = None, ""
    for name, cand in order:
        if cand:
            pos, src = cand, name
            break
    if not pos:
        pos = get_window_fallback_pos()
        src = "window"
    if debug:
        print(f"[caret] source={src} pos={pos} | native={native} "
              f"ext={ext} click={click}")
    return pos


def get_work_area(hwnd=None):
    """（指定/前台）窗口所在显示器的工作区 (left, top, right, bottom)。

    多显示器：MonitorFromWindow(TONEAREST) 定位到目标屏幕，
    任务栏等不算在工作区内。失败返回 None。
    """
    hwnd = hwnd or user32.GetForegroundWindow()
    if not hwnd:
        return None
    hmon = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
    if not hmon:
        return None
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if not user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
        return None
    rc = mi.rcWork
    return (rc.left, rc.top, rc.right, rc.bottom)
