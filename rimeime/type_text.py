"""用 SendInput 以 Unicode 方式把文本输入到当前焦点窗口。

KEYEVENTF_UNICODE 注入的是 VK_PACKET 事件，keyboard 库的钩子会忽略它，
不会造成递归触发。
"""
import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32

INPUT_KEYBOARD = 1
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_KEYUP = 0x0002


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUTUNION(ctypes.Union):
    # 注意：Windows INPUT 在 x64 下共 40 字节，union 部分 32 字节
    # （最大成员 MOUSEINPUT 为 32 字节）。只放 KEYBDINPUT(24) 会让
    # ctypes 算出 32 字节的 INPUT，SendInput 因 cbSize 不匹配直接失败。
    _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_ubyte * 32)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _send_char_unit(unit, flags):
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.ki = KEYBDINPUT(0, unit, flags, 0, None)
    n = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    if n != 1:
        err = ctypes.windll.kernel32.GetLastError()
        raise OSError(f"SendInput failed (sent {n}, err={err})")


def type_text(text):
    """逐字符输入文本（支持 BMP 外字符，自动拆代理对）。"""
    for ch in text:
        code = ord(ch)
        if code > 0xFFFF:
            code -= 0x10000
            for unit in (0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF)):
                _send_char_unit(unit, KEYEVENTF_UNICODE)
                _send_char_unit(unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)
        else:
            _send_char_unit(code, KEYEVENTF_UNICODE)
            _send_char_unit(code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)
