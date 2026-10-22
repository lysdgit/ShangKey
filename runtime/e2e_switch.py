"""M4 e2e：双记事本焦点自动切换 + Shift 手动覆盖。

用法（IME 需已用本脚本生成的临时规则启动）：
    pythonw runtime/e2e_switch.py
结果写 runtime/e2e_switch/e2e_result.txt。
"""
import ctypes
import json
import subprocess
import time
from pathlib import Path

import keyboard

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "runtime" / "e2e_switch"
RULES = WORK / "rules.json"
MEMORY = WORK / "memory.json"
RESULT = WORK / "e2e_result.txt"

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
user32.GetClipboardData.restype = ctypes.c_void_p
user32.GetClipboardData.argtypes = [ctypes.c_uint]

EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p,
                                     ctypes.c_void_p)


def log(msg):
    print(msg, flush=True)


def find_window(substr):
    found = []

    def cb(hwnd, _):
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if user32.IsWindowVisible(hwnd) and substr in buf.value:
            found.append(hwnd)
        return True

    user32.EnumWindows(EnumWindowsProc(cb), None)
    return found[0] if found else None


def activate(hwnd):
    cur = kernel32.GetCurrentThreadId()
    fg_thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
    user32.AttachThreadInput(cur, fg_thread, True)
    user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)
    user32.AttachThreadInput(cur, fg_thread, False)
    time.sleep(0.3)
    if user32.GetForegroundWindow() != hwnd:
        # 前台权限受限时用真实鼠标点击兜底
        rect = ctypes.wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        user32.SetCursorPos((rect.left + rect.right) // 2,
                            (rect.top + rect.bottom) // 2)
        user32.mouse_event(2, 0, 0, 0, 0)
        user32.mouse_event(4, 0, 0, 0, 0)
        time.sleep(0.3)
    return user32.GetForegroundWindow() == hwnd


def send_str(s):
    for ch in s:
        keyboard.send(ch)
        time.sleep(0.05)


def clear_clipboard():
    user32.OpenClipboard(None)
    user32.EmptyClipboard()
    user32.CloseClipboard()


def read_clipboard():
    if not user32.OpenClipboard(None):
        return None
    try:
        h = user32.GetClipboardData(13)  # CF_UNICODE
        return ctypes.c_wchar_p(h).value if h else None
    finally:
        user32.CloseClipboard()


def read_notepad(hwnd):
    """直接读记事本文本（WM_GETTEXT 发给 Edit 子窗口）。

    比剪贴板方案可靠：不依赖 Ctrl+A/C，不受全选选区、
    空文本不写剪贴板等边界影响。Win11 记事本子窗口类名是
    RichEditD2DPT，Win10 是 Edit，这里枚举子窗口取第一个。
    """
    result = []

    def cb(child, _):
        buf = ctypes.create_unicode_buffer(4096)
        user32.SendMessageW(child, 0x000D, 4095, buf)  # WM_GETTEXT
        result.append(buf.value)
        return True

    user32.EnumChildWindows(hwnd, EnumWindowsProc(cb), None)
    return result[0] if result else None


def main():
    WORK.mkdir(exist_ok=True)
    RULES.write_text(json.dumps({
        "default_mode": "zh",
        "debounce_ms": 150,
        "rules": [{"name": "英文窗口", "title": "英文窗口", "mode": "en"}],
    }, ensure_ascii=False), encoding="utf-8")
    if MEMORY.exists():
        MEMORY.unlink()
    en_file = WORK / "英文窗口.txt"
    zh_file = WORK / "中文窗口.txt"
    en_file.write_text("", encoding="utf-8")
    zh_file.write_text("", encoding="utf-8")

    p_en = subprocess.Popen(["notepad", str(en_file)])
    time.sleep(0.8)
    p_zh = subprocess.Popen(["notepad", str(zh_file)])
    time.sleep(1.0)
    hwnd_en = find_window("英文窗口.txt")
    hwnd_zh = find_window("中文窗口.txt")
    checks = []
    if not hwnd_en or not hwnd_zh:
        RESULT.write_text(f"FATAL: windows not found "
                          f"en={hwnd_en} zh={hwnd_zh}\n", encoding="utf-8")
        return

    # 1) 中文窗：abc 被拼音消费，记事本保持空
    activate(hwnd_zh)
    time.sleep(0.6)
    send_str("abc")
    time.sleep(0.5)
    t = read_notepad(hwnd_zh)
    checks.append(("中文窗 abc 被消费", t in (None, ""), repr(t)))

    # 2) 切到英文窗（标题规则 -> en）：abc 直通
    activate(hwnd_en)
    time.sleep(0.6)
    send_str("abc")
    time.sleep(0.5)
    t = read_notepad(hwnd_en)
    checks.append(("英文窗 abc 直通", t == "abc", repr(t)))

    # 3) 切回中文窗（默认 zh）：xyz 仍被消费
    activate(hwnd_zh)
    time.sleep(0.6)
    send_str("xyz")
    time.sleep(0.5)
    t = read_notepad(hwnd_zh)
    checks.append(("切回中文窗仍消费", t in (None, ""), repr(t)))

    # 4) Shift 单击手动覆盖 -> 英。已知注入伪影：keyboard 库注入 shift
    #    后续 send 的 VK 事件会被系统拒绝派发（无 IME 时也复现，见
    #    diag8），故本项不再验证 abc 落地，只验证 toggle 链路生效——
    #    IME 收到 shift 单击并切换（由第 5 项 L4 记忆写入证明）。
    fg = user32.GetForegroundWindow()
    n = user32.GetWindowTextLengthW(fg)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(fg, buf, n + 1)
    log(f"step4 before: fg={fg:#x} title={buf.value!r} "
        f"zh={hwnd_zh:#x} en={hwnd_en:#x}")
    keyboard.send("shift")
    time.sleep(0.8)

    # 5) L4 记忆文件应写入 notepad -> en（证明 Shift toggle 全链路生效）
    mem = {}
    if MEMORY.exists():
        try:
            mem = json.loads(MEMORY.read_text(encoding="utf-8"))
        except ValueError:
            pass
    checks.append(("Shift toggle 生效（L4 记忆）", mem.get("notepad") == "en",
                   repr(mem)))

    lines = []
    ok = True
    for name, passed, detail in checks:
        ok = ok and passed
        lines.append(f"[{'PASS' if passed else 'FAIL'}] {name}  {detail}")
    lines.append("RESULT: " + ("ALL PASS" if ok else "HAS FAILURES"))
    RESULT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log("\n".join(lines))

    for p in (p_en, p_zh):
        try:
            p.kill()
        except OSError:
            pass


if __name__ == "__main__":
    main()
