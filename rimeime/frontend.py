"""前端主逻辑：全局钩子 -> rime 引擎 -> 悬浮 UI / SendInput 上屏。

按键流：
  钩子线程收到键 -> 英文模式或引擎不消费 -> 放行（返回 True）
                 -> 引擎消费           -> 抑制（返回 False）+ 刷新 UI
  引擎产出 commit -> 经 UI 队列交主线程 SendInput 上屏 + 隐藏候选窗
  （上屏不能在钩子线程做：LL 钩子回调内嵌套 SendInput 不可靠）

快捷键：Ctrl+Alt+K 开关拦截；Shift 单击切换中英（手动覆盖，优先级最高）；
        Ctrl+Alt+Q 退出。

特色2（自动切换）：FocusWatcher 监视前台窗口变化 -> 防抖 -> Switcher.decide()
        -> set_ascii；Shift 手动覆盖写 L4 记忆。见 switcher.py。
"""
import ctypes
import os
import time

import keyboard

from . import keys
from .caret import get_foreground_rect, get_input_pos, get_window_fallback_pos
from .mousehook import ClickTracker
from .switcher import FocusWatcher, Switcher, get_foreground_info

user32 = ctypes.windll.user32

# 候选窗定位诊断：SHANGKEY_DEBUG_CARET=1 时每次组词都打印来源与坐标
_DEBUG_CARET = os.environ.get("SHANGKEY_DEBUG_CARET", "") not in ("", "0")


class Frontend:
    def __init__(self, rime, ui, switcher=None, oled_state=None):
        self.rime = rime
        self.ui = ui
        self.enabled = True
        self.sid = rime.create_session()
        self._composing = False
        self._shift_down = False
        self._shift_used = False
        self._ctrl_down = False
        self._alt_down = False
        # 特色2：规则引擎（selftest 可传 None 关闭）
        self.switcher = switcher
        # OLED 小屏同步：状态快照（可为 None = 不启用）
        self.oled = oled_state
        self._focus = None
        self._pending = None  # (WindowInfo, 生效截止时间 monotonic)
        self._reapply_flag = False  # 标签页上报 -> 重决策标记（HTTP 线程置位）

    # ---- 对外操作（特色2 的规则引擎后续就调用 set_ascii）

    def set_ascii(self, on, source=""):
        self.rime.set_ascii_mode(on, self.sid)
        if on:
            self.rime.clear_composition(self.sid)
            self._clear()
        self._push_status(source)

    def _push_status(self, source=""):
        """状态窗标签：英 / 中（简）/ 繁（中文简繁引擎选项）。

        简繁开关名不统一：luna_pinyin_simp 用 zh_simp（reset:1 默认简），
        双拼等标准方案用 simplification——两个都查，任一开启视为简体。
        """
        if self.rime.get_option("ascii_mode", self.sid):
            label = "英"
        elif (self.rime.get_option("zh_simp", self.sid)
              or self.rime.get_option("simplification", self.sid)):
            label = "中"
        else:
            label = "繁"
        self.ui.queue.put(("status", label, source))
        # 小屏同步：中英状态（拼音串/候选由 _refresh 更新）
        if self.oled is not None:
            self.oled.update(mode=label, source=source)

    def toggle_ascii(self, source="手动"):
        """切换中英。注意：只能在主线程调用（含写盘/跨线程消息），
        钩子线程请投递 ("toggle",) 事件经 ui 主线程执行。"""
        self._pending = None  # 手动覆盖优先：取消未决的自动决策
        on = not self.rime.get_option("ascii_mode", self.sid)
        self.set_ascii(on, source)
        if source == "手动" and self.switcher:
            # L4 手动记忆：按进程记住本次选择（等于默认值则清除记忆）
            info = get_foreground_info()
            if info and info.process:
                self.switcher.remember(info.process, "en" if on else "zh")
                self.switcher.save_state(info.process, "en" if on else "zh")

    # ---- 特色2：焦点监视 + 防抖决策（主线程，由 ui.poll 的 on_tick 驱动）

    # ---- 特色2：焦点监视 + 防抖决策（主线程，由 ui.poll 的 on_tick 驱动）

    def _apply(self, info):
        """对某个前台窗口做决策并应用。焦点变化即丢弃进行中的编码。"""
        dec = self.switcher.decide(info)
        print(f"[switch] {info.process} / {info.cls} / {info.title!r} "
              f"-> {dec.mode} ({dec.source})", flush=True)
        self._clear()  # 光标换窗口，旧编码作废（IME 标准行为）
        want = dec.mode == "en"
        if want != bool(self.rime.get_option("ascii_mode", self.sid)):
            self.set_ascii(want, dec.source)
        else:
            self._push_status(dec.source)
        # 会话状态持久化：记录前台进程 + 最终生效模式，供重启恢复
        if info.process:
            self.switcher.save_state(info.process, dec.mode)

    def _tick(self):
        if not self._focus:
            return
        if self._reapply_flag:
            self._reapply_flag = False
            # 标签页 URL 上报：窗口焦点没变，主动重决策当前前台
            # （HTTP 线程只置标记，查询/决策都在本主线程做）
            info = get_foreground_info()
            if info:
                self.switcher.request_context(info)
                self._pending = (
                    info, time.monotonic() + self.switcher.debounce_ms / 1000.0)
        for info in self._focus.drain():
            # 焦点变化即刻异步请求 L3 上下文探测：防抖 150ms 期间后台线程
            # 正好完成 UIA 调用，_apply 时结果已就绪（绝不阻塞主线程）。
            self.switcher.request_context(info)
            self._pending = (
                info, time.monotonic() + self.switcher.debounce_ms / 1000.0)
        if self._pending and time.monotonic() >= self._pending[1]:
            info, _ = self._pending
            self._pending = None
            self._apply(info)

    # ---- 按键处理（selftest 也走这里）

    def feed_key(self, vk, mask):
        """喂一个键给引擎；True = 引擎已消费（调用方应抑制该键）。"""
        if not self.enabled:
            return False
        if not self._composing and self.rime.get_option("ascii_mode", self.sid):
            return False  # 英文模式未编码：直通应用
        consumed = self.rime.process_key(vk, mask, self.sid)
        if not consumed:
            if self._composing:
                self._clear()
            return False
        self._refresh()
        return True

    def _feed_raw(self, name, shift, ctrl, alt):
        """带修饰键的按键直接喂引擎（Ctrl+` 方案菜单 / Ctrl+Shift+4 简繁）。

        返回钩子返回值：False=抑制（引擎已消费），True=放行。
        """
        keycode = {"grave": 0x060, "`": 0x060, "4": 0x34}.get(name)
        if not keycode:
            return True
        mask = keys.modifier_mask(shift=shift, ctrl=ctrl, alt=alt)
        consumed = self.rime.process_key(keycode, mask, self.sid)
        if consumed:
            self._refresh()
            self._push_status("")  # 方案/简繁状态可能变了，刷新状态窗
            return False
        return True

    def _refresh(self):
        ctx = self.rime.get_context(self.sid)
        commit = self.rime.get_commit(self.sid)
        if commit:
            # 上屏必须放到 UI 主线程执行：在 LL 键盘钩子回调里
            # 同步调用 SendInput（嵌套注入）不可靠，文本会丢失。
            self.ui.queue.put(("commit", commit))
            self._clear()
            return
        if ctx and (ctx.preedit or ctx.candidates):
            self._composing = True
            # 小屏同步：拼音串 + 候选列表 + 高亮位
            if self.oled is not None:
                self.oled.update(
                    preedit=ctx.preedit or "",
                    candidates=[c.text for c in ctx.candidates],
                    highlighted=ctx.highlighted,
                )
            # 定位链（见 caret.get_input_pos）：
            #   GetGUIThreadInfo（原生控件）
            #   -> 浏览器扩展上报（Chromium 网页输入框，精确）
            #   -> 最后鼠标点击位置（无法注入脚本的 Electron 应用）
            #   -> 窗口顶部中央（最后手段）
            pos = get_input_pos(debug=_DEBUG_CARET)
            # 校验：坐标必须落在前台窗口矩形内——扩展上报可能来自
            # 别的标签页/窗口（如后台 Chrome 页面），越界直接丢弃
            if pos:
                fr = get_foreground_rect()
                if fr and not (fr[0] <= pos[0] <= fr[2]
                               and fr[1] <= pos[1] <= fr[3]):
                    pos = None
            if not pos:
                pos = get_window_fallback_pos()
            self.ui.queue.put(("candidates", ctx.preedit, ctx.candidates,
                               ctx.highlighted, pos))
        else:
            self._clear()

    def _clear(self):
        self._composing = False
        self.ui.queue.put(("hide",))
        # 小屏同步：清掉拼音串与候选（保留中英状态行）
        if self.oled is not None:
            self.oled.clear_composition()

    # ---- 钩子安装

    def start(self):
        keyboard.add_hotkey("ctrl+alt+q", self.quit, suppress=True)
        keyboard.add_hotkey("ctrl+alt+k", self.toggle_enabled, suppress=True)
        keyboard.hook(self._on_event, suppress=True)
        # 记录最后鼠标点击位置：Chromium 系应用（Chrome/Electron）
        # 拿不到插入符时用它兜底定位候选窗
        try:
            self._clicks = ClickTracker()
            self._clicks.start()
        except Exception:
            self._clicks = None
        self.ui.on_toggle = self.toggle_ascii  # 主线程执行手动切换
        self.ui.on_pause = self.toggle_enabled  # 托盘菜单：暂停/恢复拦截
        if self.switcher:
            self._focus = FocusWatcher()
            self._focus.start()
            self.ui.on_tick = self._tick
            # 标签页上报 -> 主动重决策（只置标记，主线程消费）
            self.switcher.on_tab_reported = self._on_tab_reported
            # 启动恢复：上次退出时的前台进程 + 模式一致则直接恢复，
            # 否则按当前前台窗口定模式（不防抖，避免首窗延迟感）
            info = get_foreground_info()
            if info:
                saved = self.switcher.load_state()
                if (saved and info.process
                        and saved["process"] == info.process.lower()):
                    self._clear()
                    self.set_ascii(saved["mode"] == "en", "恢复")
                else:
                    self._apply(info)

    def _on_tab_reported(self):
        """HTTP 线程调用：只置标记，重决策留给主线程 _tick。"""
        self._reapply_flag = True

    def toggle_enabled(self):
        self.enabled = not self.enabled
        if not self.enabled:
            self._clear()
            self.ui.queue.put(("status", "关", ""))
        else:
            self._push_status("")

    def quit(self):
        self.ui.queue.put(("quit",))

    # ---- 钩子回调（在 keyboard 监听线程执行；返回 True 放行，False 阻断）

    def _on_event(self, e):
        try:
            name = e.name
            if e.event_type == "up":
                if name == "shift" and self._shift_down:
                    self._shift_down = False
                    if not self._shift_used:
                        # Shift 单击：手动切换中英。
                        # 必须交主线程执行：钩子回调里做写盘/跨线程
                        # GetWindowTextW 会毒化钩子，系统静默丢弃
                        # 该会话后续放行的按键（实测 abc 全丢）。
                        self.ui.queue.put(("toggle",))
                elif name == "ctrl":
                    self._ctrl_down = False
                elif name == "alt":
                    self._alt_down = False
                return True
            if name == "shift":
                self._shift_down = True
                self._shift_used = False
                return True
            if name == "ctrl":
                self._ctrl_down = True
                return True
            if name == "alt":
                self._alt_down = True
                return True
            if name == "caps lock":
                return True  # 锁定态反映在 mask 里，本身不进引擎
            self._shift_used = True
            if not self.enabled:
                return True
            # 修饰组合键（ctrl/alt + 任意键）一律直通：
            # ① IME 标准行为——组合键是应用/系统快捷键，引擎不消费；
            # ② 关键：blocking hook 返回 False 会让 keyboard 库跳过
            #    _pressed_events 更新，全局热键（Ctrl+Alt+Q/K）将失效。
            if self._ctrl_down or self._alt_down:
                # 例外：引擎级热键透传给 rime——
                #   Ctrl+` 打开方案菜单（含双拼方案）、Ctrl+Shift+4 简繁切换。
                # rime 不消费则原样放行，不影响其他组合键。
                if (self._ctrl_down and not self._alt_down
                        and name in ("grave", "`") ):
                    return self._feed_raw(name, self._shift_down, True, False)
                if (self._ctrl_down and self._shift_down and not self._alt_down
                        and name == "4"):
                    return self._feed_raw(name, True, True, False)
                return True
            vk = user32.MapVirtualKeyW(e.scan_code & 0xFF, 1)
            if not vk:
                return True
            keycode = keys.vk_to_keycode(vk)  # VK 'N' -> keysym 'n'
            # 修饰键状态用钩子事件流自维护：钩子线程不泵消息，
            # GetKeyState 读到陈旧状态；GetAsyncKeyState 在注入/
            # 快速连击时序下也不可靠，事件流顺序才是权威来源。
            mask = keys.modifier_mask(
                shift=self._shift_down,
                ctrl=self._ctrl_down,
                alt=self._alt_down,
                caps=bool(user32.GetAsyncKeyState(0x14) & 1),
            )
            return not self.feed_key(keycode, mask)
        except Exception as ex:  # 钩子线程异常绝不能吞掉按键
            print("hook error:", ex, flush=True)
            return True
