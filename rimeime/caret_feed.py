"""光标位置的外部来源（Chromium 系应用专用）。

背景：Chrome / Edge / Electron（WorkBuddy、VS Code 等）**不向 Windows
暴露插入符**——GetGUIThreadInfo 返回空，UIA 在这些应用上直接 E_NOTIMPL
（实测：Chromium 只在检测到读屏软件时才建可访问性树，且 MSAA 树里
也只有浏览器 UI，没有网页内容）。所以需要两条替代通路：

1. `reported`：浏览器扩展用 JS 精确算出输入框光标屏幕坐标上报。
   这是浏览器内唯一的精确来源（textarea 用镜像测量，contenteditable
   用 Selection Range 的包围盒）。
2. `click`：最后鼠标点击位置。给无法注入脚本的应用（Electron）
   兜底——用户点击输入框的位置就是他要输入的位置，比"窗口顶部中央"
   准确得多。

两者都在本模块做线程安全缓存，供 caret 定位链读取。
"""
import threading
import time

REPORT_TTL = 15.0   # 扩展上报有效期（秒）：光标没动过就一直有效，
                    # 扩展在换输入框/点击/滚动时会立即重报
CLICK_TTL = 1800.0  # 点击位置有效期（秒，30 分钟）。按窗口分别记忆：
                    # 用户在输入框点一下、切走查资料、再切回来继续打，
                    # 仍然沿用那个位置——比"窗口中心"准得多。

_lock = threading.Lock()
_reported = None    # (x, y, height, title, monotonic)
_click = {}         # hwnd -> (x, y, monotonic)
_click_latest = None  # (hwnd, x, y, monotonic)：hwnd 未知时的兜底


def set_reported(x, y, height=0, title=""):
    """扩展上报光标位置（屏幕物理坐标，y 为光标底边）。"""
    global _reported
    with _lock:
        _reported = (int(x), int(y), int(height or 0),
                     str(title or ""), time.monotonic())


def _title_match(page_title, window_title):
    """页面标题是否属于该窗口标题（浏览器标题 = 页面标题 + " - 浏览器名"）。

    取多个长度档位依次尝试：标题很长时浏览器可能截断显示，但
    GetWindowTextW 通常返回完整标题；仍可能因全角/省略号对不上，
    所以退到更短的前缀匹配，宁可命中也不要误丢精确坐标。
    """
    page_title = (page_title or "").strip()
    if not page_title or not window_title:
        return True
    for n in (24, 16, 12, 8):
        core = page_title[:n]
        if len(core) >= 8 and core in window_title:
            return True
    return False


def get_reported(window_title=""):
    """取有效上报坐标；过期/标题不匹配 -> None。

    标题校验的原因：上报来自某个浏览器标签页，若前台已经切到别的
    窗口（比如 WorkBuddy），就不能再用那个标签页的光标坐标。
    浏览器窗口标题 = "页面标题 - 浏览器名"，用页面标题做子串匹配。
    """
    with _lock:
        item = _reported
    if not item:
        return None
    x, y, _h, title, ts = item
    if time.monotonic() - ts > REPORT_TTL:
        return None
    if window_title and title and not _title_match(title, window_title):
        return None
    return (x, y)


def peek_reported():
    """原始上报（不做标题校验），排查用。"""
    with _lock:
        item = _reported
    if not item:
        return None
    x, y, h, title, ts = item
    age = time.monotonic() - ts
    return {"x": x, "y": y, "h": h, "title": title,
            "age": round(age, 1), "fresh": age <= REPORT_TTL}


def set_click(x, y, hwnd=0):
    """记录鼠标点击位置（屏幕坐标），按窗口分别记忆。

    记 hwnd 有两个作用：① 避免"点了聊天记录区再回输入框打字"沿用
    无关坐标；② 用户切走再切回来时，仍能取回该窗口上次的点击位置——
    这是 Chromium 系应用（无插入符、扩展又管不到 Electron）唯一
    稳定的横向锚点，比窗口中心准得多。
    """
    global _click_latest
    key = int(hwnd or 0)
    now = time.monotonic()
    with _lock:
        _click[key] = (int(x), int(y), now)
        _click_latest = (key, int(x), int(y), now)
        if len(_click) > 64:      # 控制体积：清掉最旧的一半
            old = sorted(_click.items(), key=lambda kv: kv[1][2])[:32]
            for k, _v in old:
                _click.pop(k, None)


def get_click(max_age=CLICK_TTL, hwnd=0):
    """取点击位置；过期 -> None。

    hwnd 给定时只取该窗口的记录（跨窗口切换不会串用）；hwnd=0 时取
    最近一次点击，供调试用。
    """
    now = time.monotonic()
    with _lock:
        if hwnd:
            item = _click.get(int(hwnd))
            if not item:
                return None
            x, y, ts = item
        else:
            if not _click_latest:
                return None
            _k, x, y, ts = _click_latest
    if now - ts > max_age:
        return None
    return (x, y)


def peek_click(hwnd=0):
    """原始点击记录（不做过期判断），排查用。"""
    with _lock:
        if hwnd:
            item = _click.get(int(hwnd))
            if not item:
                return None
            x, y, ts = item
        else:
            if not _click_latest:
                return None
            _k, x, y, ts = _click_latest
    return {"x": x, "y": y, "age": round(time.monotonic() - ts, 1)}


def clear():
    """测试用：清空缓存。"""
    global _reported, _click_latest
    with _lock:
        _reported = None
        _click.clear()
        _click_latest = None
