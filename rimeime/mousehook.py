"""低级鼠标钩子：记录最后一次左键点击的屏幕坐标。

用途见 caret_feed：Chromium 系应用（Chrome / Edge / Electron）
不向 Windows 暴露插入符，拿不到光标时用"用户点击输入框的位置"兜底。

钩子只读坐标，不抑制任何消息（一律 CallNextHookEx 放行），
对系统输入零影响。所有 Win32 调用都声明了原型——x64 下不声明会
按 32 位截断句柄/指针，曾导致 access violation（tray.py 同款坑）。
"""
import ctypes
import threading
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WH_MOUSE_LL = 14
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONDBLCLK = 0x0203

ULONG_PTR = ctypes.c_size_t
LRESULT = ctypes.c_ssize_t


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int,
                              wintypes.WPARAM, wintypes.LPARAM)


def _declare():
    user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC,
                                         ctypes.c_void_p, wintypes.DWORD)
    user32.SetWindowsHookExW.restype = ctypes.c_void_p
    user32.CallNextHookEx.argtypes = (ctypes.c_void_p, ctypes.c_int,
                                      wintypes.WPARAM, wintypes.LPARAM)
    user32.CallNextHookEx.restype = LRESULT
    user32.UnhookWindowsHookEx.argtypes = (ctypes.c_void_p,)
    user32.UnhookWindowsHookEx.restype = wintypes.BOOL
    user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),
                                   ctypes.c_void_p, wintypes.UINT, wintypes.UINT)
    user32.GetMessageW.restype = wintypes.BOOL
    user32.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
    user32.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
    user32.DispatchMessageW.restype = LRESULT
    kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
    kernel32.GetModuleHandleW.restype = ctypes.c_void_p
    # 钩子回调里只能调极快的函数：GetForegroundWindow 不跨进程发消息，
    # 安全。WindowFromPoint 会向目标窗口发 WM_NCHITTEST，可能卡住输入，不用。
    user32.GetForegroundWindow.argtypes = ()
    user32.GetForegroundWindow.restype = wintypes.HWND


class ClickTracker:
    """在独立线程里跑 LL 鼠标钩子 + 消息循环，记录点击坐标。"""

    def __init__(self):
        self._hook = None
        self._proc = None      # 必须持引用，否则回调对象被 GC -> 崩溃
        self._thread = None
        self._started = False

    def start(self):
        if self._started:
            return True
        self._started = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="shangkey-mouse-hook")
        self._thread.start()
        return True

    def _run(self):
        try:
            _declare()
            from .caret_feed import set_click

            def _proc(n_code, w_param, l_param):
                if n_code == 0 and w_param in (WM_LBUTTONDOWN, WM_LBUTTONDBLCLK):
                    info = ctypes.cast(
                        l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                    try:
                        set_click(info.pt.x, info.pt.y, user32.GetForegroundWindow() or 0)
                    except Exception:
                        pass
                return user32.CallNextHookEx(None, n_code, w_param, l_param)

            self._proc = HOOKPROC(_proc)
            self._hook = user32.SetWindowsHookExW(
                WH_MOUSE_LL, self._proc, None, 0)
            if not self._hook:
                return
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            pass

    def stop(self):
        try:
            if self._hook:
                user32.UnhookWindowsHookEx(self._hook)
                self._hook = None
        except Exception:
            pass
