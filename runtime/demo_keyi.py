"""演示：自动打开记事本，注入 keyi，让 ShangKey IME 组词并上屏「可以」。

用法：python runtime/demo_keyi.py
前提：IME 已在运行（默认中文模式，记事本命中的是 default=zh）。
"""
import ctypes
import ctypes.wintypes as wt
import subprocess
import time

import keyboard

user32 = ctypes.windll.user32
WM_GETTEXT = 0x000D


def find_notepads():
    """找标题含 记事本/Notepad 的可见顶层窗口。"""
    result = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            t = buf.value
            if "记事本" in t or "Notepad" in t:
                result.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return set(result)


def read_text(hwnd):
    """读记事本编辑区文本（EnumChildWindows + WM_GETTEXT）。"""
    texts = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(child, _):
        buf = ctypes.create_unicode_buffer(4096)
        n = user32.SendMessageW(child, WM_GETTEXT, 4096, buf)
        if n:
            texts.append(buf.value)
        return True

    user32.EnumChildWindows(hwnd, cb, 0)
    return "\n".join(texts)


def fg_title():
    """当前前台窗口标题（诊断焦点归属）。"""
    h = user32.GetForegroundWindow()
    if not h:
        return "<none>"
    n = user32.GetWindowTextLengthW(h)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(h, buf, n + 1)
    return f"{buf.value!r}@{h:#x}"


def activate(hwnd, tries=8):
    """强制激活窗口（后台进程直接 SetForegroundWindow 会被前台锁拒绝）。"""
    for _ in range(tries):
        # ALT 键技巧：按下并释放 ALT 会解除前台锁，允许本轮激活
        user32.keybd_event(0x12, 0, 0, 0)        # ALT down
        user32.keybd_event(0x12, 0, 2, 0)        # ALT up
        user32.SwitchToThisWindow(hwnd, True)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.4)
        if user32.GetForegroundWindow() == hwnd:
            return True
    return False


def main():
    before = find_notepads()
    subprocess.Popen(["notepad.exe"])
    time.sleep(1.5)
    new = find_notepads() - before
    if not new:
        print("FAIL: 没找到新开的记事本窗口")
        return 1
    hwnd = new.pop()
    print(f"记事本: hwnd={hwnd:#x}")
    if not activate(hwnd):
        print(f"FAIL: 记事本激活失败，前台是 {fg_title()}")
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        subprocess.run(["taskkill", "/PID", str(pid.value), "/F"],
                       capture_output=True)
        return 1
    print(f"[fg 打字前] {fg_title()}")

    print("注入 keyi ...")
    for ch in "keyi":
        keyboard.send(ch)
        time.sleep(0.12)
    time.sleep(0.8)          # 等 IME 组词 + 候选窗刷新
    print("注入 1 选词 ...")
    keyboard.send("1")

    # 轮询读文本：SendInput 落到编辑区有延迟，读到「可以」或超时为止
    text = ""
    for _ in range(15):      # 最多 ~6s
        time.sleep(0.4)
        text = read_text(hwnd)
        if "可以" in text:
            break
    print(f"[fg 读时] {fg_title()}")
    print(f"记事本内容: {text!r}")
    ok = "可以" in text
    print("PASS: 上屏了「可以」" if ok else "FAIL: 没看到「可以」")

    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    subprocess.run(["taskkill", "/PID", str(pid.value), "/F"],
                   capture_output=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
