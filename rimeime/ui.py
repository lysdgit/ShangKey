"""tkinter 悬浮 UI：候选窗 + 中英状态指示 + 上屏。

所有跨线程更新都走 queue，由主线程的 poll() 消费，避免 tkinter 线程安全问题。
上屏（SendInput）也在主线程执行——LL 键盘钩子线程内嵌套注入不可靠。

外观可配置：runtime/ui.json（缺省项自动兜底）：
  font_family        字体（默认 Microsoft YaHei）
  preedit_font_size  拼音串字号（默认 11）
  candidate_font_size候选字号（默认 13）
  status_font_size   状态窗字号（默认 10）
  bg / fg / border   候选窗背景/前景/边框色
  highlight_color    高亮候选（第一候选）颜色
  orientation        候选排布 horizontal（一行）| vertical（一列）
  opacity            候选窗不透明度 0.0~1.0
  status_bg / status_fg  状态窗配色

语言图标（状态小窗）可拖动，位置另存 runtime/status_pos.json。
"""
import ctypes
import json
import queue
import tkinter as tk
from pathlib import Path

from .caret import get_work_area
from .type_text import type_text

from .paths import PROJECT_ROOT
UI_CONFIG_PATH = PROJECT_ROOT / "runtime" / "ui.json"
# 语言图标（中英状态小窗）被拖动后的位置记忆
STATUS_POS_PATH = PROJECT_ROOT / "runtime" / "status_pos.json"
STATUS_MIN_VISIBLE = 24  # 拖到屏幕边缘时至少保留这么多像素可见，避免"拖丢"

# 悬浮窗防抢焦点：WS_EX_NOACTIVATE 让 Windows 永不激活该窗口，
# 键盘焦点始终留在用户正在输入的目标应用（候选窗标准做法）
_GWL_EXSTYLE = -20
_WS_EX_NOACTIVATE = 0x08000000
_WS_EX_TOOLWINDOW = 0x00000080


def _set_noactivate(win):
    """给 tkinter 顶层窗设置 WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW。"""
    try:
        user32 = ctypes.windll.user32
        # winfo_id 返回的是 Tk 内部子窗，真正的顶层 HWND 要取 GA_ROOT
        GetAncestor = user32.GetAncestor
        GetAncestor.argtypes = (ctypes.c_void_p, ctypes.c_uint)
        GetAncestor.restype = ctypes.c_void_p
        hwnd = GetAncestor(win.winfo_id(), 2)  # GA_ROOT
        if not hwnd:
            return
        ex = user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, _GWL_EXSTYLE,
                              ex | _WS_EX_NOACTIVATE | _WS_EX_TOOLWINDOW)
    except Exception:
        pass  # 拿不到句柄就退化为旧行为，不影响功能


def _virtual_screen():
    """虚拟屏幕边界（含多显示器）: (left, top, right, bottom)。

    tkinter 的 winfo_screenwidth/height 只报主屏尺寸，状态窗允许拖到副屏，
    所以这里用 GetSystemMetrics(SM_*VIRTUALSCREEN) 拿整个桌面范围。
    """
    try:
        u = ctypes.windll.user32
        u.GetSystemMetrics.argtypes = (ctypes.c_int,)
        u.GetSystemMetrics.restype = ctypes.c_int
        x, y = u.GetSystemMetrics(76), u.GetSystemMetrics(77)
        w, h = u.GetSystemMetrics(78), u.GetSystemMetrics(79)
        if w > 0 and h > 0:
            return x, y, x + w, y + h
    except Exception:
        pass
    return None


DEFAULT_STYLE = {
    "font_family": "Microsoft YaHei",
    "preedit_font_size": 11,
    "candidate_font_size": 13,
    "status_font_size": 10,
    "bg": "#ffffff",
    "fg": "#1f2328",
    "border": "#c8cdd4",
    "highlight_color": "#c7391f",
    "orientation": "horizontal",
    "opacity": 0.96,
    "status_bg": "#1f2328",
    "status_fg": "#ffffff",
}


def load_style():
    """读 runtime/ui.json 与默认值合并；坏文件/缺文件都用默认。"""
    style = dict(DEFAULT_STYLE)
    try:
        raw = json.loads(UI_CONFIG_PATH.read_text(encoding="utf-8"))
        for k in DEFAULT_STYLE:
            if k in raw:
                style[k] = raw[k]
    except (FileNotFoundError, ValueError, OSError):
        pass
    return style


class CandidateWindow:
    """无边框置顶候选窗，跟随光标位置，按所在显示器工作区防出屏。"""

    def __init__(self, root, style):
        self.style = style
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        fam = style["font_family"]
        self.win.configure(bg=style["bg"])
        try:
            self.win.attributes("-alpha", float(style["opacity"]))
        except tk.TclError:
            pass
        outer = tk.Frame(self.win, bg=style["bg"],
                         highlightbackground=style["border"],
                         highlightthickness=1)
        outer.pack()
        self.preedit = tk.Label(outer, text="", font=(fam, style["preedit_font_size"]),
                                bg=style["bg"], fg=style["fg"], anchor="w")
        self.preedit.pack(fill="x", padx=10, pady=(8, 2))
        self.cands = tk.Label(outer, text="",
                              font=(fam, style["candidate_font_size"]),
                              bg=style["bg"], fg=style["fg"], anchor="w",
                              justify="left")
        self.cands.pack(fill="x", padx=10, pady=(0, 8))
        self.win.withdraw()
        _set_noactivate(self.win)  # 防候选窗抢走目标应用焦点

    def show(self, preedit, candidates, highlighted, pos):
        self.preedit.configure(text=preedit or "")
        hi = self.style["highlight_color"]
        n = len(candidates)
        if self.style["orientation"] == "vertical":
            parts = []
            for i, c in enumerate(candidates):
                mark = "*" if i == highlighted else " "
                parts.append(f"{mark}{i + 1}.{c.text}")
            self.cands.configure(text="\n".join(parts), justify="left")
            self.cands.configure(fg=self.style["fg"])
            # 竖排时用富文本高亮第一候选代价高，保持统一前景色即可
        else:
            parts = []
            for i, c in enumerate(candidates):
                mark = "*" if i == highlighted else " "
                parts.append(f"{mark}{i + 1}.{c.text}")
            self.cands.configure(text="    ".join(parts), justify="left")
            self.cands.configure(fg=hi if highlighted == 0 else self.style["fg"])
        self.win.update_idletasks()
        if pos:
            w = self.win.winfo_reqwidth()
            h = self.win.winfo_reqheight()
            # 前台窗口所在显示器工作区内 clamp（多显示器/任务栏避让）
            wa = get_work_area() or (0, 0, self.win.winfo_screenwidth(),
                                     self.win.winfo_screenheight())
            x = max(wa[0], min(pos[0], wa[2] - w - 8))
            y = pos[1] + 6             # 默认在光标下方（主流输入法习惯）
            if y + h > wa[3]:
                y = pos[1] - h - 6     # 底部放不下 -> 悬到光标上方
            y = max(wa[1], min(y, wa[3] - h - 8))
            self.win.geometry(f"+{x}+{y}")
        self.win.deiconify()
        self.win.lift()

    def hide(self):
        self.win.withdraw()


class StatusWindow:
    """中英状态小窗（语言图标）。

    默认停在右上角；按住左键可拖到任意位置（含副屏），松手后坐标写进
    runtime/status_pos.json，下次启动沿用。托盘菜单「语言图标复位」可回默认位。
    """

    def __init__(self, root, style):
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        fam = style["font_family"]
        self.win.configure(bg=style["status_bg"])
        self.label = tk.Label(self.win, text="中",
                              font=(fam, style["status_font_size"], "bold"),
                              bg=style["status_bg"], fg=style["status_fg"],
                              padx=10, pady=2,
                              cursor="fleur")  # 四向箭头：提示可拖
        self.label.pack()
        self.custom = self._load_pos()  # 用户拖过的位置；None = 默认右上角
        self._drag_off = None           # 按下时鼠标相对窗口左上角的偏移
        self._dragging = False
        # 整窗可拖：bindtags 默认含 toplevel，label 上的事件也会冒泡到 win
        self.win.bind("<Button-1>", self._on_press)
        self.win.bind("<B1-Motion>", self._on_drag)
        self.win.bind("<ButtonRelease-1>", self._on_release)
        self._place()
        _set_noactivate(self.win)  # 状态小窗不抢焦点（也因此只能靠鼠标消息拖动）

    # ---- 拖动 ----

    def _on_press(self, e):
        self._drag_off = (e.x_root - self.win.winfo_x(),
                          e.y_root - self.win.winfo_y())
        self._dragging = False

    def _on_drag(self, e):
        if not self._drag_off:
            return
        self._dragging = True
        dx, dy = self._drag_off
        self.win.geometry(f"+{e.x_root - dx}+{e.y_root - dy}")

    def _on_release(self, e):
        if not self._drag_off:
            return
        self._drag_off = None
        if not self._dragging:   # 只是点了一下，不动位置也不写盘
            return
        self._dragging = False
        x, y = self._clamp(self.win.winfo_x(), self.win.winfo_y(),
                           self.win.winfo_width())
        self.custom = (x, y)
        self.win.geometry(f"+{x}+{y}")
        self._save_pos(x, y)

    def _clamp(self, x, y, w):
        """夹到虚拟屏幕内，至少留 STATUS_MIN_VISIBLE 像素可见。"""
        vs = _virtual_screen()
        if not vs:
            return int(x), int(y)
        x = max(vs[0], min(int(x), vs[2] - STATUS_MIN_VISIBLE))
        y = max(vs[1], min(int(y), vs[3] - STATUS_MIN_VISIBLE))
        return x, y

    # ---- 位置持久化 ----

    def _load_pos(self):
        try:
            raw = json.loads(STATUS_POS_PATH.read_text(encoding="utf-8"))
            x, y = int(raw["x"]), int(raw["y"])
        except (FileNotFoundError, OSError, ValueError, KeyError, TypeError):
            return None
        vs = _virtual_screen()
        # 换过显示器/分辨率导致旧坐标落在屏外 -> 回默认位，别让图标消失
        if vs and not (vs[0] <= x <= vs[2] - STATUS_MIN_VISIBLE
                       and vs[1] <= y <= vs[3] - STATUS_MIN_VISIBLE):
            return None
        return (x, y)

    def _save_pos(self, x, y):
        try:
            STATUS_POS_PATH.parent.mkdir(parents=True, exist_ok=True)
            STATUS_POS_PATH.write_text(
                json.dumps({"x": int(x), "y": int(y)}), encoding="utf-8")
            print(f"[status] 位置已记忆 {x},{y}", flush=True)
        except OSError as ex:
            print(f"[status] 位置保存失败: {ex}", flush=True)

    def reset_pos(self):
        """回默认右上角，并删掉记忆文件。"""
        self.custom = None
        try:
            STATUS_POS_PATH.unlink()
        except OSError:
            pass
        self._place()

    # ---- 摆放/文字 ----

    def _place(self):
        if self._dragging:
            return  # 拖动途中文字若变化，别把窗口拽回旧位置（会抖）
        self.win.update_idletasks()
        w = self.win.winfo_reqwidth()
        if self.custom:
            x, y = self._clamp(self.custom[0], self.custom[1], w)
            self.custom = (x, y)  # 文字变宽后重新夹紧
        else:
            x = max(0, self.win.winfo_screenwidth() - w - 8)
            y = 36
        self.win.geometry(f"+{int(x)}+{int(y)}")

    def set(self, text, source=""):
        self.label.configure(text=text + (f"  {source}" if source else ""))
        # 拖过的位置钉左边缘（宽度变化时向右长），默认位置保持右上角右对齐
        self._place()


class UI:
    """队列 + 轮询：钩子线程投递事件，主线程安全更新。"""

    def __init__(self, style=None):
        self.style = style or load_style()
        self.root = tk.Tk()
        self.root.withdraw()
        self.candidates = CandidateWindow(self.root, self.style)
        self.status = StatusWindow(self.root, self.style)
        self.queue = queue.Queue()
        self.on_tick = None   # 主线程 30ms 节拍（焦点防抖等用），由 Frontend 注入
        self.on_toggle = None  # 手动切换中英（钩子线程投递，主线程执行）
        self.on_pause = None  # 暂停/恢复键盘拦截（托盘投递，主线程执行）
        self.on_toggle_oled = None  # 开关 OLED 小屏服务（托盘投递）

    def poll(self):
        try:
            # 退出时 root 已销毁，排队中的 after 回调会拿到死应用 -> 直接收摊
            if not self.root.winfo_exists():
                return
        except tk.TclError:
            return
        try:
            while True:
                kind, *args = self.queue.get_nowait()
                if kind == "candidates":
                    self.candidates.show(*args)
                elif kind == "hide":
                    self.candidates.hide()
                elif kind == "commit":
                    print(f"[commit] {args[0]!r}", flush=True)
                    type_text(args[0])
                    print("[commit] typed ok", flush=True)
                elif kind == "status":
                    self.status.set(*args)
                elif kind == "reset_status":
                    self.status.reset_pos()
                elif kind == "toggle":
                    if self.on_toggle:
                        self.on_toggle()
                elif kind == "pause":
                    if self.on_pause:
                        self.on_pause()
                elif kind == "toggle_oled":
                    if self.on_toggle_oled:
                        self.on_toggle_oled()
                elif kind == "quit":
                    self.root.destroy()
                    return
        except queue.Empty:
            pass
        if self.on_tick:
            self.on_tick()
        self.root.after(30, self.poll)

    def mainloop(self):
        self.root.mainloop()
