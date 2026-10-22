"""特色2：中英文自动切换规则引擎（分层）。

层级（自上而下，首个命中的层级生效）：
  L1 进程/窗口类/标题规则 —— switch_rules.json 里的显式规则（无 url 字段的规则）
  L2 网页 URL 规则        —— 浏览器扩展经本地 WS 上报 URL（后续实现；
                             decide(url=...) / set_url() 已留好挂接点）
  L3 光标上下文启发式     —— UIA 读光标前后文猜中英（后续实现，默认关闭）
  L4 手动记忆             —— 用户 Shift 手动切过的模式按进程记住（自动写入）
  默认                    —— default_mode（默认中文）

切换时机：前台窗口变化（SetWinEventHook EVENT_SYSTEM_FOREGROUND）
          -> 防抖 debounce_ms（默认 150ms）-> 决策 -> Frontend.set_ascii。
Shift 手动覆盖优先级最高（取消未决自动决策），并写入 L4 记忆。

线程模型：WinEvent 回调只往队列塞 hwnd；查进程名/标题放在主线程
          drain() 里做，ctypes 调用全部串行，避免跨线程竞争。
"""
import ctypes
import json
import queue
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

kernel32 = ctypes.windll.kernel32
user32 = ctypes.windll.user32

from .paths import PROJECT_ROOT
DEFAULT_RULES_PATH = PROJECT_ROOT / "runtime" / "switch_rules.json"
DEFAULT_MEMORY_PATH = PROJECT_ROOT / "runtime" / "switch_memory.json"
DEFAULT_STATE_PATH = PROJECT_ROOT / "runtime" / "switch_state.json"

# SetWinEventHook 常量
EVENT_SYSTEM_FOREGROUND = 0x0003
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002

# WinEventProc 原型 7 参数：hHook, event, hwnd, idObject, idChild,
# dwEventThread, dwmsEventTime（少声明一个 -> ctypes 调用直接 TypeError）
WinEventProcType = ctypes.WINFUNCTYPE(
    None, ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD, ctypes.wintypes.HWND,
    ctypes.wintypes.LONG, ctypes.wintypes.LONG, ctypes.wintypes.DWORD,
    ctypes.wintypes.DWORD)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


@dataclass
class WindowInfo:
    hwnd: int
    process: str | None   # 不含 .exe，小写比较；提权窗口取不到时为 None
    cls: str
    title: str


@dataclass
class Decision:
    mode: str    # "zh" | "en"
    source: str  # 状态栏展示，如 "规则:VS Code" / "记忆" / "默认"


@dataclass
class Rule:
    name: str
    mode: str
    process: list | None = None  # 进程名列表（不含 .exe）
    cls: str | None = None
    title: str | None = None      # 正则，re.search 大小写不敏感
    url: str | None = None        # 有 url 字段 => 归属 L2 层
    enabled: bool = True

    @classmethod
    def from_json(cls, raw):
        return cls(
            name=str(raw.get("name", "未命名规则")),
            mode=str(raw.get("mode", "zh")).lower(),
            process=[str(p) for p in raw["process"]] if raw.get("process") else None,
            cls=raw.get("cls"),
            title=raw.get("title"),
            url=raw.get("url"),
            enabled=bool(raw.get("enabled", True)),
        )

    def match(self, info, url=None):
        if not self.enabled:
            return False
        if self.process:
            if not info.process:
                return False
            if info.process.lower() not in [p.lower() for p in self.process]:
                return False
        if self.cls and info.cls != self.cls:
            return False
        if self.title and not re.search(self.title, info.title or "", re.I):
            return False
        if self.url:
            if not url or not re.search(self.url, url, re.I):
                return False
        return True


def get_foreground_info(hwnd=None):
    """取（指定/前台）窗口的进程名、窗口类、标题。提权窗口进程名取不到时降级。"""
    if hwnd is None:
        hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    cls = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, cls, 256)
    n = user32.GetWindowTextLengthW(hwnd)
    title = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, title, n + 1)
    pid = ctypes.c_ulong(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    process = None
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if h:
        try:
            buf = ctypes.create_unicode_buffer(260)
            size = ctypes.c_ulong(260)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                name = buf.value.replace("\\", "/").rsplit("/", 1)[-1]
                process = name[:-4] if name.lower().endswith(".exe") else name
        finally:
            kernel32.CloseHandle(h)
    return WindowInfo(hwnd=hwnd, process=process, cls=cls.value, title=title.value)


class FocusWatcher:
    """前台窗口变化监视。

    注意：WINEVENT_OUTOFCONTEXT 的回调由安装线程的消息循环派发
    （我们在 tkinter 主线程安装，故回调跑在主线程、mainloop 必须存活）。
    回调只投递 hwnd；查进程名/标题放在主线程 drain() 里做，
    ctypes 调用全部串行，避免跨线程竞争。
    """

    def __init__(self):
        self._events = queue.Queue()
        self._proc = None   # 必须持有引用，否则 CFUNCTYPE 被回收后回调崩
        self._hook = None

    def start(self):
        self._proc = WinEventProcType(self._callback)
        self._hook = user32.SetWinEventHook(
            EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND,
            None, self._proc, 0, 0,
            WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS)
        print(f"[focus] win event hook={self._hook and hex(self._hook)}",
              flush=True)
        if not self._hook:
            raise RuntimeError("SetWinEventHook failed")

    def _callback(self, hHook, event, hwnd, idObject, idChild, tid, event_time):
        print(f"[focus] event hwnd={hwnd:#x}", flush=True)
        self._events.put(hwnd)

    def drain(self):
        """返回自上次 drain 以来的前台窗口信息列表（主线程调用）。"""
        out = []
        while True:
            try:
                hwnd = self._events.get_nowait()
            except queue.Empty:
                break
            info = get_foreground_info(hwnd)
            if info:
                out.append(info)
        return out

    def stop(self):
        if self._hook:
            user32.UnhookWinEvent(self._hook)
            self._hook = None


class Switcher:
    """分层决策引擎。decide() 为纯函数，便于单测；焦点/防抖在 Frontend 侧。"""

    MAX_URL_CACHE = 64

    def __init__(self, rules_path=None, memory_path=None, state_path=None):
        self.rules_path = Path(rules_path) if rules_path else DEFAULT_RULES_PATH
        self.memory_path = Path(memory_path) if memory_path else DEFAULT_MEMORY_PATH
        self.state_path = Path(state_path) if state_path else DEFAULT_STATE_PATH
        self.rules = []
        self.default_mode = "zh"
        self.debounce_ms = 150
        self.layers = {"l1": True, "l2": True, "l3": False, "l4": True}
        self.url_port = 61823   # L2 HTTP 端点端口（url_server.port）
        self.fuzzy = False      # 模糊音开关（main.py 读出后写 schema 补丁）
        self.memory = {}
        self.urls = {}   # hwnd -> url（显式按窗口上报；扩展通道见 tab_url）
        self.tab_url = ""    # 浏览器扩展上报的活动标签页 URL
        self.tab_title = ""  # 对应标签页标题（用于和前台窗口标题匹配）
        # L3 异步上下文探针（uia_text.AsyncContextProbe），main.py 注入并 start；
        # request_context 在焦点变化时投递请求，decide 只读缓存不阻塞。
        self.context_probe = None
        # 标签页上报回调（frontend 注入）：URL 变了但窗口没变焦点，
        # 需要主动触发当前前台窗口重新决策，否则切标签页永远不生效。
        self.on_tab_reported = None
        self.reload()

    # ---- 配置加载 ----

    def reload(self):
        try:
            raw = json.loads(self.rules_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            print(f"[switcher] 规则文件不存在，使用内置默认: {self.rules_path}")
            raw = {}
        except (ValueError, OSError) as ex:
            print(f"[switcher] 规则文件解析失败（{ex}），使用内置默认")
            raw = {}
        self.rules = [Rule.from_json(r) for r in raw.get("rules", [])]
        self.default_mode = str(raw.get("default_mode", "zh")).lower()
        try:
            self.debounce_ms = max(0, int(raw.get("debounce_ms", 150)))
        except (TypeError, ValueError):
            self.debounce_ms = 150
        layers = raw.get("layers", {})
        self.layers = {
            "l1": bool(layers.get("l1", True)),
            "l2": bool(layers.get("l2", True)),
            "l3": bool(layers.get("l3", False)),
            "l4": bool(layers.get("l4", True)),
        }
        try:
            self.url_port = int(raw.get("url_server", {}).get("port", 61823))
        except (TypeError, ValueError, AttributeError):
            self.url_port = 61823
        self.fuzzy = bool(raw.get("fuzzy", False))
        try:
            mem = json.loads(self.memory_path.read_text(encoding="utf-8"))
            self.memory = {str(k): str(v).lower() for k, v in mem.items()}
        except FileNotFoundError:
            self.memory = {}
        except (ValueError, OSError) as ex:
            print(f"[switcher] 记忆文件解析失败（{ex}），忽略")
            self.memory = {}

    # ---- L2 URL 注册表 ----

    def set_url(self, hwnd, url):
        """按窗口句柄显式登记 URL（预留通道）。"""
        if len(self.urls) >= self.MAX_URL_CACHE:
            self.urls.pop(next(iter(self.urls)))
        self.urls[int(hwnd)] = url or None

    def set_tab(self, url, title):
        """浏览器扩展通道：登记活动标签页 URL + 标题。"""
        self.tab_url = (url or "").strip()
        self.tab_title = (title or "").strip()
        if self.on_tab_reported:
            try:
                self.on_tab_reported()
            except Exception as ex:
                print(f"[switcher] on_tab_reported 回调异常: {ex}")

    def request_context(self, info):
        """焦点变化时调用：异步请求 L3 上下文探测（绝不阻塞）。"""
        if self.layers["l3"] and self.context_probe:
            try:
                self.context_probe.request(info.hwnd)
            except Exception:
                pass

    def url_for(self, info):
        """取前台窗口对应的 URL。

        扩展通道：浏览器窗口标题 = 标签页标题 + " - 浏览器名"，
        用 tab_title 做子串匹配确认"前台就是这个标签页"，
        不匹配（非浏览器/别的标签页/别的窗口）则返回 None，L2 失效。
        """
        if info and self.tab_url and self.tab_title:
            if self.tab_title in (info.title or ""):
                return self.tab_url
        if info:
            return self.urls.get(info.hwnd)
        return None

    # ---- 决策 ----

    def decide(self, info, url=None):
        """返回 Decision。层级顺序：L1 -> L2 -> L3 -> L4 -> 默认。"""
        if url is None:
            url = self.url_for(info)
        if self.layers["l1"]:
            for r in self.rules:
                if r.url is None and r.match(info, url):
                    return Decision(r.mode, f"规则:{r.name}")
        if self.layers["l2"] and url:
            for r in self.rules:
                if r.url is not None and r.match(info, url):
                    return Decision(r.mode, f"网页:{r.name}")
        if self.layers["l3"] and self.context_probe:
            try:
                m = self.context_probe.latest(info.hwnd)
            except Exception:
                m = None
            if m in ("zh", "en"):
                return Decision(m, "上下文")
        if self.layers["l4"] and info.process:
            mode = self.memory.get(info.process.lower())
            if mode in ("zh", "en"):
                return Decision(mode, "记忆")
        return Decision(self.default_mode, "默认")

    # ---- L4 记忆读写 ----

    def remember(self, process, mode):
        """手动切换时调用。mode 等于默认值时删除记忆（保持文件干净）。"""
        if not process:
            return
        key = process.lower()
        if mode == self.default_mode:
            self.memory.pop(key, None)
        else:
            self.memory[key] = mode
        self._save_memory()

    def forget(self, process=None):
        if process is None:
            self.memory.clear()
        else:
            self.memory.pop(process.lower(), None)
        self._save_memory()

    def _save_memory(self):
        try:
            self.memory_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.memory_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.memory, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(self.memory_path)
        except OSError as ex:
            print(f"[switcher] 记忆写入失败: {ex}")

    # ---- 会话状态持久化（重启恢复上次中英状态） ----

    def load_state(self):
        """返回 {"process": "...", "mode": "zh|en"} 或 None。"""
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            mode = str(raw.get("mode", "")).lower()
            proc = str(raw.get("process", "")).lower()
            if mode in ("zh", "en") and proc:
                return {"process": proc, "mode": mode}
        except FileNotFoundError:
            pass
        except (ValueError, OSError) as ex:
            print(f"[switcher] 状态文件解析失败（{ex}），忽略")
        return None

    def save_state(self, process, mode):
        """记录当前前台进程 + 生效模式，供下次启动恢复。"""
        if not process or mode not in ("zh", "en"):
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(
                {"process": process.lower(), "mode": mode},
                ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError as ex:
            print(f"[switcher] 状态写入失败: {ex}")
