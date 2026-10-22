"""L2 全链路 e2e：HTTP 上报 URL -> 焦点窗口标题匹配 -> 自动切中英。

模拟浏览器：开两个记事本，把窗口标题改成页面标题样式，
POST 对应 URL 后激活窗口，注入字母观察是否被消费：
  英文模式 -> 字母直通上屏；中文模式 -> 字母被引擎消费（不上屏）。
"""
import ctypes
import json
import subprocess
import time
import urllib.request

import keyboard

user32 = ctypes.windll.user32
WM_SETTEXT = 0x000C
URL_EP = "http://127.0.0.1:61823/url"


def report(url, title):
    req = urllib.request.Request(
        URL_EP, data=json.dumps({"url": url, "title": title}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=3) as r:
        assert r.status == 204, r.status


def open_notepad():
    before = set()
    out = []

    def cb(h, _):
        if user32.IsWindowVisible(h):
            n = user32.GetWindowTextLengthW(h)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(h, buf, n + 1)
            if "记事本" in buf.value or "Notepad" in buf.value:
                out.append(h)
        return True

    CB = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
    user32.EnumWindows(CB(cb), 0)
    before |= set(out)
    subprocess.Popen(["notepad.exe"])
    time.sleep(1.5)
    user32.EnumWindows(CB(cb), 0)
    new = set(out) - before
    return new.pop() if new else None


def set_title(hwnd, title):
    user32.SendMessageW(hwnd, WM_SETTEXT, 0,
                        ctypes.c_wchar_p(title))


def read_text(hwnd):
    texts = []

    def cb(child, _):
        buf = ctypes.create_unicode_buffer(4096)
        if user32.SendMessageW(child, 0x000D, 4096, buf):
            texts.append(buf.value)
        return True

    CB = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
    user32.EnumChildWindows(hwnd, CB(cb), 0)
    return "\n".join(texts)


def type_one(ch):
    keyboard.send(ch)
    time.sleep(0.4)


def main():
    hwnd = open_notepad()
    assert hwnd, "没开起记事本"
    print(f"记事本 hwnd={hwnd:#x}")

    # 1) 模拟 GitHub 标签页 -> 应自动切英文，字母直通
    report("https://github.com/rime/home", "GitHub - where world builds")
    set_title(hwnd, "GitHub - where world builds - Google Chrome")
    user32.SetForegroundWindow(hwnd)
    time.sleep(1.5)  # 防抖 + 决策
    type_one("a")
    text1 = read_text(hwnd)
    print(f"[GitHub 页] 注入 a -> 记事本内容 {text1!r}")
    ok1 = text1 == "a"
    print("PASS: 英文模式（字母直通）" if ok1 else "FAIL: 未切英文")

    # 2) 模拟百度标签页 -> 应切回中文，字母被消费
    report("https://www.baidu.com/s?wd=test", "百度一下，你就知道")
    set_title(hwnd, "百度一下，你就知道 - Google Chrome")
    user32.SetForegroundWindow(hwnd)
    time.sleep(1.5)
    type_one("b")
    text2 = read_text(hwnd)
    print(f"[百度 页] 注入 b -> 记事本内容 {text2!r}")
    ok2 = text2 == "a"  # b 被中文引擎消费，内容不变
    print("PASS: 中文模式（字母被消费）" if ok2 else "FAIL: 未切中文")

    pid = ctypes.c_ulong(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    subprocess.run(["taskkill", "/PID", str(pid.value), "/F"],
                   capture_output=True)

    print("ALL PASS" if (ok1 and ok2) else "E2E FAIL")
    return 0 if (ok1 and ok2) else 1


if __name__ == "__main__":
    raise SystemExit(main())
