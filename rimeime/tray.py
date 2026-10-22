"""系统托盘图标：纯 ctypes 实现（Shell_NotifyIconW），无第三方依赖。

独立线程里跑隐藏窗口 + 消息循环；菜单命令通过 on_menu 回调交出。
注意 on_menu 在托盘线程执行——涉及 tkinter 的操作必须经 UI 队列
投递回主线程，本模块不做任何 marshalling。

用法：
    tray = TrayIcon(tip="ShangKey 输入法",
                    items=[(101, "词库管理…", None),
                           (102, "暂停键盘拦截", "check"),
                           (104, "退出", None)])
    tray.start(on_menu=handle)   # handle(menu_id) 在托盘线程被调用
    tray.set_checked(102, True)  # 更新勾选态
    tray.stop()                  # 退出前移除图标
"""

import ctypes
import ctypes.wintypes as wt
import threading
from pathlib import Path

# Shell_NotifyIconW
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x01, 0x02, 0x80

# 窗口消息
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_APP_TRAY = 0x8000 + 1  # WM_APP+1：托盘回调消息

# 菜单
MF_STRING, MF_SEPARATOR, MF_CHECKED = 0x0000, 0x0800, 0x0008
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100
TPM_NONOTIFY = 0x0080

IDI_APPLICATION = 32512
HWND_MESSAGE = -3  # 消息专用窗口父句柄常量（不是 user32 导出）
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32
kernel32 = ctypes.windll.kernel32

WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_longlong, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

# x64 下 WPARAM/LPARAM 是 64 位，不声明原型会按 32 位截断，
# 窗口创建期间（WM_NCCREATE）回调抛 OverflowError -> CreateWindowExW 失败
user32.DefWindowProcW.argtypes = (wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
user32.DefWindowProcW.restype = ctypes.c_longlong

# ---- 全量声明原型：x64 下所有句柄/指针参数不声明会按 32 位截断 ----
# （此前 GetModuleHandleW 返回的 HMODULE 被截断，坏句柄传给
#   RegisterClassW/CreateWindowExW 造成偶发 access violation）
kernel32.GetModuleHandleW.argtypes = (wt.LPCWSTR,)
kernel32.GetModuleHandleW.restype = wt.HMODULE

user32.RegisterClassW.argtypes = (ctypes.c_void_p,)
user32.RegisterClassW.restype = wt.ATOM
user32.CreateWindowExW.argtypes = (
    wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID)
user32.CreateWindowExW.restype = wt.HWND
user32.DestroyWindow.argtypes = (wt.HWND,)
user32.PostMessageW.argtypes = (wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
user32.SetForegroundWindow.argtypes = (wt.HWND,)

shell32.Shell_NotifyIconW.argtypes = (wt.DWORD, ctypes.c_void_p)
shell32.Shell_NotifyIconW.restype = wt.BOOL

user32.LoadImageW.argtypes = (
    wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ctypes.c_int, ctypes.c_int, wt.UINT)
user32.LoadImageW.restype = wt.HANDLE
user32.LoadIconW.argtypes = (wt.HINSTANCE, ctypes.c_void_p)  # 第二参为 MAKEINTRESOURCE
user32.LoadIconW.restype = wt.HICON

user32.CreatePopupMenu.restype = wt.HMENU
user32.CreatePopupMenu.argtypes = []
user32.DestroyMenu.argtypes = (wt.HMENU,)
user32.AppendMenuW.argtypes = (wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR)
user32.CheckMenuItem.argtypes = (wt.HMENU, wt.UINT, wt.UINT)
user32.CheckMenuItem.restype = wt.BOOL
user32.TrackPopupMenu.argtypes = (
    wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, wt.HWND, ctypes.c_void_p)
user32.TrackPopupMenu.restype = wt.BOOL
user32.GetCursorPos.argtypes = (ctypes.c_void_p,)
user32.GetMessageW.argtypes = (ctypes.c_void_p, wt.HWND, wt.UINT, wt.UINT)
user32.GetMessageW.restype = ctypes.c_longlong
user32.TranslateMessage.argtypes = (ctypes.c_void_p,)
user32.DispatchMessageW.argtypes = (ctypes.c_void_p,)
user32.PostQuitMessage.argtypes = (ctypes.c_int,)


class NOTIFYICONDATAW(ctypes.Structure):
    """经典布局（不含 guidItem），cbSize 用旧尺寸以获得最大兼容。"""
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("hWnd", wt.HWND),
        ("uID", wt.UINT),
        ("uFlags", wt.UINT),
        ("uCallbackMessage", wt.UINT),
        ("hIcon", wt.HICON),
        ("szTip", wt.WCHAR * 128),
        ("dwState", wt.DWORD),
        ("dwStateMask", wt.DWORD),
        ("szInfo", wt.WCHAR * 256),
        ("uVersion", wt.UINT),
        ("szInfoTitle", wt.WCHAR * 64),
        ("dwInfoFlags", wt.DWORD),
    ]


class TrayIcon:
    """托盘图标 + 右键菜单。items: [(id, 标签, None|'check')]。"""

    def __init__(self, tip, items, icon_path=None):
        self.tip = tip
        self.items = items
        self.icon_path = str(Path(icon_path)) if icon_path else None
        self.checked = set()
        self._thread = None
        self._hwnd = None
        self._hicon = None
        self._nid = None
        self._proc = None       # WNDPROC 回调必须保活
        self._menu = None
        self._ready = threading.Event()
        self._stop = False
        self.on_menu = None     # (menu_id) -> None，托盘线程调用

    # ---- 对外 ----

    def start(self, on_menu):
        self.on_menu = on_menu
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="shangkey-tray")
        self._thread.start()
        self._ready.wait(3.0)
        return self._ready.is_set()

    def set_checked(self, menu_id, on):
        """更新勾选态（线程安全：交给托盘线程重画图标态即可）。"""
        if on:
            self.checked.add(menu_id)
        else:
            self.checked.discard(menu_id)

    def stop(self):
        self._stop = True
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
        if self._thread:
            self._thread.join(2.0)

    # ---- 内部（托盘线程） ----

    def _load_icon(self):
        if self.icon_path and Path(self.icon_path).exists():
            h = user32.LoadImageW(
                None, self.icon_path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE)
            if h:
                return h
        return user32.LoadIconW(None, IDI_APPLICATION)

    def _loop(self):
        hinst = kernel32.GetModuleHandleW(None)
        wndclass = "ShangKeyTrayWnd"

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wt.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", ctypes.c_void_p), ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR),
            ]

        self._proc = WNDPROC(self._wndproc)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = wndclass
        if not user32.RegisterClassW(ctypes.byref(wc)):
            return

        # 消息专用窗口（HWND_MESSAGE）：托盘回调消息照样送达
        self._hwnd = user32.CreateWindowExW(
            0, wndclass, "ShangKey tray", 0, 0, 0, 0, 0,
            ctypes.c_void_p(HWND_MESSAGE), None, hinst, None)
        if not self._hwnd:
            return

        self._hicon = self._load_icon()
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_APP_TRAY
        nid.hIcon = self._hicon
        nid.szTip = self.tip
        self._nid = nid
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            return

        self._menu = user32.CreatePopupMenu()
        for mid, label, kind in self.items:
            flags = MF_STRING | (MF_CHECKED if mid in self.checked else 0)
            ctypes.windll.user32.AppendMenuW(
                self._menu, flags, mid, label)
        self._ready.set()

        msg = wt.MSG()
        while not self._stop:
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r <= 0:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        if self._menu:
            user32.DestroyMenu(self._menu)
        user32.DestroyWindow(self._hwnd)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_APP_TRAY:
            if lparam in (WM_LBUTTONUP, WM_RBUTTONUP):
                self._popup()
            return 0
        if msg == WM_CLOSE:
            user32.PostQuitMessage(0)
            return 0
        if msg == WM_DESTROY:
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _popup(self):
        """左键/右键单击：同步刷新勾选态后在光标处弹出菜单。"""
        for mid, _label, kind in self.items:
            if kind == "check":
                flags = MF_STRING | (MF_CHECKED if mid in self.checked else 0)
                user32.CheckMenuItem(self._menu, mid, flags)
        # TrackPopupMenu 前必须前置窗口，否则点击菜单外区域菜单不消失
        user32.SetForegroundWindow(self._hwnd)
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        chosen = user32.TrackPopupMenu(
            self._menu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
            pt.x, pt.y, 0, self._hwnd, None)
        user32.PostMessageW(self._hwnd, 0x0000, 0, 0)  # WM_NULL 收尾
        if chosen and self.on_menu:
            try:
                self.on_menu(chosen)
            except Exception as ex:  # 托盘线程兜底，不让异常杀死循环
                print(f"[tray] 菜单处理异常: {ex}")
