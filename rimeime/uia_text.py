"""L3：UIA 读光标前文本，启发式判定中英。

实现：comtypes + UIAutomationCore。取焦点元素的 TextPattern，
把文档范围末端对齐到选区起点 -> 拿到光标前文本，取末尾 ~60 字符判定。

判定策略（保守，宁缺勿滥——返回 None 表示"无意见"，交给下层）：
  命中英文强特征（URL/邮箱/盘符路径/代码文件名/camelCase）-> en
  末尾是中文（汉字/中文标点）                          -> zh
  其他（纯英文散文、空文本、取不到）                    -> None

线程约束：全部 UIA 调用必须在主线程（Switcher.decide 由
Frontend._tick 在 tkinter 主线程驱动），COM 单元模型一致。
UAC 提权窗口 UIA 读不到焦点元素 -> 异常/None -> 自动降级下一层。
"""
import ctypes
import queue
import re
import threading
import time

_ZH_TAIL = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef"
                      r"「」『』【】、。，：；？！·—…][^A-Za-z0-9]*$")

# 英文强特征：命中即可信
_EN_HINTS = [
    re.compile(r"(https?://|www\.)\S*$", re.I),                     # URL
    re.compile(r"[\w.\-+]+@[\w\-]+(?:\.[\w\-]+)+\s*$", re.I),       # 邮箱
    re.compile(r"[A-Za-z]:[\\/][^\u4e00-\u9fff]*$"),                # 盘符路径
    re.compile(r"\w+\.(?:py|js|ts|cpp|c|h|hpp|java|go|rs|md|json|yaml|yml"
               r"|xml|html|css|sql|sh|bat|ps1|toml|ini|log)\s*$", re.I),  # 文件名
    re.compile(r"\b[a-z]+(?:[A-Z][a-z0-9]+)+$"),                    # camelCase
    re.compile(r"[-\w]+(?:\s+[-\w]+)*::?\s*$"),                     # 命名空间 / 冒号
]

_UIAC = None
_init_failed = False


def _ensure_module():
    """惰性初始化 UIAutomationClient 包装（主线程一次性）。失败则永久禁用。"""
    global _UIAC, _init_failed
    if _UIAC is not None or _init_failed:
        return _UIAC
    try:
        import comtypes.client
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as UIAC
        _UIAC = UIAC
    except Exception:
        _init_failed = True
    return _UIAC


def get_text_before_caret(max_chars=60):
    """光标前文本（最长 max_chars）；取不到返回 None。"""
    UIAC = _ensure_module()
    if UIAC is None:
        return None
    uia = UIAC.CUIAutomation().QueryInterface(UIAC.IUIAutomation)
    el = uia.GetFocusedElement()
    if el is None:
        return None
    raw = el.GetCurrentPattern(UIAC.UIA_TextPatternId)
    if not raw:
        return None
    pat = raw.QueryInterface(UIAC.IUIAutomationTextPattern)
    sel = pat.GetSelection()
    if not sel or sel.Length < 1:
        return None
    caret = sel.GetElement(0)
    pre = pat.DocumentRange.Clone()
    pre.MoveEndpointByRange(UIAC.UIA_TextPatternRangeEndpoint_End,
                            caret, UIAC.UIA_TextPatternRangeEndpoint_Start)
    text = pre.GetText(-1) or ""
    return text[-max_chars:] if text else None


def get_caret_rect_uia():
    """UIA 取光标（选区）包围盒 -> 屏幕坐标 (x, y=光标底)；失败返回 None。

    GetGUIThreadInfo 对 Electron/Chrome 应用通常拿不到光标，
    UIA TextPattern 的选区包围盒是更通用的来源。
    折叠光标宽度为 0，部分 provider 不给包围盒 -> 向后借位 1 字符再取。
    """
    UIAC = _ensure_module()
    if UIAC is None:
        return None
    try:
        uia = UIAC.CUIAutomation().QueryInterface(UIAC.IUIAutomation)
        el = uia.GetFocusedElement()
        if el is None:
            return None
        raw = el.GetCurrentPattern(UIAC.UIA_TextPatternId)
        if not raw:
            return None
        pat = raw.QueryInterface(UIAC.IUIAutomationTextPattern)
        sel = pat.GetSelection()
        if not sel or sel.Length < 1:
            return None
        rng = sel.GetElement(0)
        rects = rng.GetBoundingRectangles()
        if not rects:
            probe_rng = rng.Clone()
            probe_rng.Move(UIAC.UIA_TextUnit_Character, 1)
            rects = probe_rng.GetBoundingRectangles()
        if not rects:
            return None
        x, y, _w, h = rects[0][:4]
        return (int(x), int(y + h))     # 光标底部（下一行文字的顶）
    except Exception:
        return None


def classify_tail(tail):
    """按上下文特征判定 -> "zh" / "en" / None。"""
    if not tail or not tail.strip():
        return None
    for p in _EN_HINTS:
        if p.search(tail):
            return "en"
    if _ZH_TAIL.search(tail):
        return "zh"
    return None


def probe(info=None):
    """同步探测（仅供测试/手动调用）：返回 "zh" / "en" / None。

    IME 运行时不要在主线程用这个——UIA COM 调用可能阻塞
    （窗口忙/模块首次生成），会卡死 UI 轮询。用 AsyncContextProbe。
    """
    try:
        return classify_tail(get_text_before_caret())
    except Exception:
        return None


class AsyncContextProbe:
    """后台 STA 线程异步探测光标上下文，主线程只读缓存，零阻塞。

    用法：焦点变化时 request(hwnd)（非阻塞，投递请求）；
    防抖结束后 latest(hwnd) 取结果——同 hwnd 且未过 TTL 才有效。
    组词期间 frontend 另调 request_caret()/latest_caret() 跟踪光标
    位置（GetGUIThreadInfo 失败的应用用 UIA 兜底，结果有 1 键延迟）。
    """
    TTL = 3.0        # 上下文结果有效期（秒）
    CARET_TTL = 0.8  # 光标位置有效期（秒）：光标随打字移动，必须短

    def __init__(self):
        self._queue = queue.Queue(maxsize=1)
        self._lock = threading.Lock()
        self._latest = None  # (hwnd, mode, monotonic)
        self._caret = None   # ((x, y), monotonic)
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="shangkey-uia-probe")
        self._started = False

    def start(self):
        if not self._started:
            self._thread.start()
            self._started = True

    def request(self, hwnd):
        """非阻塞投递探测请求（重复请求自然被队列去重）。"""
        try:
            self._queue.put_nowait(int(hwnd))
        except Exception:
            pass

    def request_caret(self):
        """非阻塞请求刷新光标位置（组词期间每次刷新都调）。"""
        self.request(-1)    # 哨兵 hwnd：只关心 caret 结果

    def latest(self, hwnd):
        """取该 hwnd 的有效探测结果；无/过期/不同窗口 -> None。"""
        with self._lock:
            item = self._latest
        if item and item[0] == int(hwnd) and time.monotonic() - item[2] <= self.TTL:
            return item[1]
        return None

    def latest_caret(self):
        """取最近一次 UIA 光标屏幕坐标；无/过期 -> None。"""
        with self._lock:
            item = self._caret
        if item and time.monotonic() - item[1] <= self.CARET_TTL:
            return item[0]
        return None

    def _loop(self):
        # COM 单元初始化必须在本线程内完成（STA）
        try:
            ctypes.windll.ole32.CoInitialize(None)
        except Exception:
            pass
        while True:
            hwnd = self._queue.get()
            try:
                mode = probe()
            except Exception:
                mode = None
            try:
                caret = get_caret_rect_uia()
            except Exception:
                caret = None
            with self._lock:
                self._latest = (hwnd, mode, time.monotonic())
                if caret:
                    self._caret = (caret, time.monotonic())
