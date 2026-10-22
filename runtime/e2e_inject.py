"""端到端注入测试：自启记事本 -> 激活 -> 拼音 -> 选词 -> 剪贴板读回。

用 pythonw 运行（无控制台窗口，不抢焦点）。结果写入 runtime/e2e_result.txt。
"""
import ctypes
import subprocess
import time
from ctypes import wintypes

import keyboard

u = ctypes.windll.user32
u.GetClipboardData.restype = wintypes.HANDLE
u.GetClipboardData.argtypes = [wintypes.UINT]

RESULT = r"C:\Users\Administrator\Documents\py_code\ShangKey\runtime\e2e_result.txt"


def log(msg):
    with open(RESULT, "a", encoding="utf-8") as f:
        f.write(msg + "\n")


def find_notepad_hwnd():
    """枚举窗口，找记事本主窗口（类名 Notepad 或标题含 无标题）。"""
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if not u.IsWindowVisible(hwnd):
            return True
        cls = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(hwnd, cls, 256)
        n = u.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, title, n + 1)
        if cls.value == "Notepad" or "记事本" in title.value or "无标题" in title.value:
            found.append(hwnd)
        return True

    u.EnumWindows(cb, 0)
    return found[0] if found else None


def activate(hwnd):
    """激活窗口：AttachThreadInput 优先，失败则真实鼠标点击兜底
    （注入的鼠标输入也算输入事件，可换取 SetForegroundWindow 权限）。"""
    cur = ctypes.windll.kernel32.GetCurrentThreadId()
    fg = u.GetForegroundWindow()
    fg_thread = u.GetWindowThreadProcessId(fg, None) if fg else 0
    if fg_thread:
        u.AttachThreadInput(cur, fg_thread, True)
    u.SetForegroundWindow(hwnd)
    u.BringWindowToTop(hwnd)
    if fg_thread:
        u.AttachThreadInput(cur, fg_thread, False)
    time.sleep(0.3)
    if u.GetForegroundWindow() == hwnd:
        return True
    rect = wintypes.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(rect))
    x = (rect.left + rect.right) // 2
    y = (rect.top + rect.bottom) // 2
    u.SetCursorPos(x, y)
    u.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
    u.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP
    time.sleep(0.4)
    return u.GetForegroundWindow() == hwnd


def type_pinyin(s):
    """逐字符 send：必须产生真实 VK 键盘事件才能被 IME 钩子看到。
    注意不能用 keyboard.write——Windows 上它默认走 Unicode 注入
    （VK_PACKET），绕过键盘钩子，IME 完全看不到。"""
    for ch in s:
        keyboard.send(ch)


def get_clipboard():
    for _ in range(20):
        if u.OpenClipboard(None):
            try:
                h = u.GetClipboardData(13)  # CF_UNICODETEXT
                if h:
                    return ctypes.wstring_at(h)
                return None
            finally:
                u.CloseClipboard()
        time.sleep(0.05)
    return None


def main():
    open(RESULT, "w").close()
    log("=== e2e start ===")
    subprocess.Popen("notepad")
    time.sleep(1.5)
    hwnd = find_notepad_hwnd()
    if not hwnd:
        log("FAIL: notepad window not found")
        return
    ok = activate(hwnd)
    time.sleep(0.5)
    fg = u.GetForegroundWindow()
    log(f"foreground hwnd={fg:#x} target={hwnd:#x} match={fg == hwnd} "
        f"activate_ok={ok}")
    if not ok:
        log("FAIL: cannot activate notepad")
        return

    # 导入词 1：上海键（权重 12000，应为 1 号候选）
    type_pinyin("shanghaijian")
    time.sleep(0.6)
    keyboard.send("1")
    time.sleep(0.6)

    # 导入词 2：翻译（权重 9999，应压过基础词库的翻译）
    type_pinyin("fanyi")
    time.sleep(0.6)
    keyboard.send("1")
    time.sleep(0.8)

    # 全选复制，读回记事本内容
    keyboard.send("ctrl+a")
    time.sleep(0.2)
    keyboard.send("ctrl+c")
    time.sleep(0.4)
    text = get_clipboard()
    log(f"CLIPBOARD: [{text}]")
    log("EXPECT   : [上海键翻译]")
    log("RESULT   : " + ("PASS" if text == "上海键翻译" else "FAIL"))

    # 清理：关掉记事本
    u.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
    time.sleep(0.3)


if __name__ == "__main__":
    main()
