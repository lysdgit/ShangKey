"""入口：

    python -m rimeime.main             # 正常运行（全局钩子 + 悬浮窗）
    python -m rimeime.main --selftest  # 自测：不走钩子，直接喂键
    python -m rimeime.main --rules <switch_rules.json> [--memory <mem.json>]

运行时快捷键：Ctrl+Alt+K 开关拦截 / Shift 单击切中英 / Ctrl+Alt+Q 退出
特色2：前台窗口变化自动切中英（规则见 runtime/switch_rules.json）
        Ctrl+` 或 F4 打开方案菜单（含双拼）；Ctrl+Shift+4 简繁切换
        浏览器扩展把 URL POST 到 http://127.0.0.1:<url_server.port>/url
"""
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import keyboard

from .frontend import Frontend
from .importer import PROJECT_ROOT, ensure_double_pinyin, ensure_schema_patch
from .repl import make_rime, notification_cb
from .switcher import Switcher, WindowInfo
from .tray import TrayIcon
from .ui import UI
from . import autostart
from . import keys
from . import uia_text
from .urlserver import start_url_server

# 托盘菜单项 ID
TRAY_DICT = 101
TRAY_PAUSE = 102
TRAY_AUTORUN = 103
TRAY_QUIT = 104
TRAY_RESET_STATUS = 105
DETACHED_PROCESS = 0x00000008


def _pop_arg(argv, name):
    """取出 --name value 并原地删除，返回 value（无则 None）。"""
    if name in argv:
        i = argv.index(name)
        val = argv[i + 1] if i + 1 < len(argv) else None
        del argv[i:i + 2]
        return val
    return None


def selftest(rime=None):
    """无头自测：feed_key 直接驱动，验证 拼音->候选->上屏->中英切换 全链路。"""
    if rime is None:
        rime = make_rime()
        rime.initialize(on_notification=notification_cb)
    ui = UI()
    fe = Frontend(rime, ui)

    def drain():
        ui.root.update()
        while not ui.queue.empty():
            kind, *args = ui.queue.get()
            if kind == "candidates":
                print(f"  [ui] preedit={args[0]!r} cands={[c.text for c in args[1]]}")
            elif kind == "hide":
                print("  [ui] hide")
            elif kind == "commit":
                print(f"  [ui] commit={args[0]!r} (SendInput 上屏)")
            elif kind == "status":
                print(f"  [ui] status={args[0]} {args[1]}")

    print("--- 中文输入 nihao ---")
    for ch in "nihao":
        vk = ord(ch.upper())
        blocked = fe.feed_key(keys.vk_to_keycode(vk), 0)
        print(f"key {ch}: consumed={blocked}")
        drain()
        time.sleep(0.05)

    print("--- 选词 1 号位 ---")
    blocked = fe.feed_key(keys.vk_to_keycode(ord("1")), 0)
    print(f"key 1: consumed={blocked}  (commit 已通过 SendInput 上屏)")
    drain()

    print("--- Shift 单击 -> 切英文 ---")
    fe.toggle_ascii("手动")
    drain()

    print("--- 英文模式直通 abc ---")
    for ch in "abc":
        blocked = fe.feed_key(keys.vk_to_keycode(ord(ch.upper())), 0)
        print(f"key {ch}: consumed={blocked} (False=放行给应用)")
    drain()

    print("--- 切回中文 ---")
    fe.toggle_ascii("手动")
    drain()

    print("--- 规则引擎 decide()（L1/默认/L4 记忆）---")
    with tempfile.TemporaryDirectory() as td:
        rules_p = Path(td) / "rules.json"
        mem_p = Path(td) / "mem.json"
        rules_p.write_text(json.dumps({
            "default_mode": "zh", "debounce_ms": 100,
            "rules": [{"name": "测试规则", "process": ["testapp"], "mode": "en"}],
        }), encoding="utf-8")
        sw = Switcher(rules_path=rules_p, memory_path=mem_p)
        app = WindowInfo(1, "testapp", "Cls", "title")
        other = WindowInfo(2, "notepad", "Notepad", "无标题 - 记事本")
        d = sw.decide(app)
        assert d.mode == "en" and d.source == "规则:测试规则", d
        d = sw.decide(other)
        assert d.mode == "zh" and d.source == "默认", d
        sw.remember("notepad", "en")
        d = sw.decide(other)
        assert d.mode == "en" and d.source == "记忆", d
        sw.remember("notepad", "zh")  # 等于默认值 -> 记忆被清除
        d = sw.decide(other)
        assert d.source == "默认", d
        print("  [switcher] L1 规则 / 默认 / L4 记忆读写 断言通过")

        # L2：浏览器扩展通道（标题匹配）+ url 规则
        sw2 = Switcher(rules_path=Path(td) / "rules2.json",
                       memory_path=Path(td) / "mem2.json",
                       state_path=Path(td) / "state2.json")
        (Path(td) / "rules2.json").write_text(json.dumps({
            "default_mode": "zh",
            "rules": [{"name": "GitHub", "url": "github\\.com", "mode": "en"}],
        }), encoding="utf-8")
        sw2.reload()
        sw2.set_tab("https://github.com/rime/home", "GitHub - 浏览器")
        browser = WindowInfo(3, "chrome", "Chrome_WidgetWin_1",
                             "GitHub - 浏览器")
        d = sw2.decide(browser)
        assert d.mode == "en" and d.source == "网页:GitHub", d
        # 标题不匹配 -> 不套用该 URL（防误伤其他窗口）
        sw2.set_tab("https://github.com/rime/home", "GitHub - 浏览器")
        d = sw2.decide(other)
        assert d.source != "网页:GitHub", d
        print("  [switcher] L2 网页规则（标题匹配 + 防误伤）断言通过")

        # L3：异步探针挂接（mock，真实 UIA 探测在实机验证）
        sw2.layers["l3"] = True
        sw2.context_probe = type("P", (), {
            "latest": staticmethod(lambda h: "en" if h == 4 else "zh"),
        })()
        probe_win = WindowInfo(4, "someapp", "ProbeEN", "t")
        d = sw2.decide(probe_win)
        assert d.mode == "en" and d.source == "上下文", d
        d = sw2.decide(other)  # notepad 无规则无记忆 -> probe 返回 zh
        assert d.mode == "zh" and d.source == "上下文", d
        print("  [switcher] L3 上下文探针挂接断言通过")

        # 会话状态持久化
        sw2.save_state("notepad", "en")
        loaded = sw2.load_state()
        assert loaded == {"process": "notepad", "mode": "en"}, loaded
        print("  [switcher] 状态持久化读写断言通过")

        # UI 外观配置加载（无 ui.json 时用默认，不炸即可）
        from .ui import load_style
        st = load_style()
        assert st["orientation"] in ("horizontal", "vertical"), st
        print("  [ui] 外观配置加载断言通过")

        # L3 文本分类器（纯函数，无 COM）
        from .uia_text import classify_tail
        assert classify_tail("你好，请看 https://github.com/rime") == "en"
        assert classify_tail("帮我写一下这个函数") == "zh"
        assert classify_tail("see config.yaml in") is None
        assert classify_tail("打开 config.yaml 看看") == "zh"  # 中文语境优先
        assert classify_tail("please open config.yaml") == "en"
        assert classify_tail("hello world") is None
        print("  [uia_text] 上下文分类器断言通过")

    fe.quit()
    time.sleep(0.2)
    rime.finalize()
    print("selftest done")


def _read_rules_config(rules_path):
    """读规则文件顶层配置（供 rime 初始化前决定 schema 补丁内容）。"""
    path = Path(rules_path) if rules_path else Switcher().rules_path
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def main():
    # 无控制台形态（pythonw / PyInstaller --noconsole）下 stdout 是 None，
    # 任何 print 都会抛 AttributeError——重定向到 runtime/ime.log，
    # 既是兜底也是打包版唯一的日志出口
    if sys.stdout is None or sys.stderr is None:
        log_f = open(PROJECT_ROOT / "runtime" / "ime.log", "a",
                     buffering=1, encoding="utf-8")
        if sys.stdout is None:
            sys.stdout = log_f
        if sys.stderr is None:
            sys.stderr = log_f
    # 无头形态的退出诊断：faulthandler 抓原生崩溃（segfault 等），
    # atexit 记录解释器正常退出路径
    try:
        import faulthandler
        faulthandler.enable(open(PROJECT_ROOT / "runtime" / "ime_fault.txt",
                                 "w", buffering=1, encoding="utf-8"))
        import atexit
        atexit.register(
            lambda: open(PROJECT_ROOT / "runtime" / "ime_fault.txt", "a",
                         encoding="utf-8").write("[atexit] exited\n"))
    except Exception:
        pass

    if "--version" in sys.argv:
        from . import __version__
        print(f"ShangKey IME v{__version__}")
        return

    # 单实例保护：rime 用户库（LevelDB）不支持多进程并发，
    # 第二个实例会因 LOCK 冲突行为异常，直接用命名互斥体挡掉。
    mutex = ctypes.windll.kernel32.CreateMutexW(None, True, "ShangKeyIME_Singleton")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        print("ShangKey IME 已在运行（单实例限制）。")
        return

    # DPI 感知：候选窗/光标坐标用物理像素，多显示器缩放场景不飘
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    rules_path = _pop_arg(sys.argv, "--rules")
    memory_path = _pop_arg(sys.argv, "--memory")
    cfg = _read_rules_config(rules_path)

    # schema 补丁（须在 rime.initialize 部署前写好）：模糊音开关 + 双拼方案
    if ensure_schema_patch(fuzzy=bool(cfg.get("fuzzy", False))):
        print("schema patch updated (fuzzy=%s)" % bool(cfg.get("fuzzy", False)))
    if ensure_double_pinyin():
        print("double pinyin schemas registered")

    print("initializing rime ...")
    rime = make_rime()
    rime.initialize(on_notification=notification_cb)
    print("rime ready, version:", rime.version())

    if "--selftest" in sys.argv:
        selftest(rime)
        return

    # 组件禁用开关（调试用）：SHANGKEY_DISABLE=urlserver,tray,probe,...
    DISABLE = {s.strip() for s in
               os.environ.get("SHANGKEY_DISABLE", "").split(",") if s.strip()}
    switcher = Switcher(rules_path=rules_path, memory_path=memory_path)
    # L3 异步上下文探针（后台 STA 线程，绝不阻塞主线程）
    # SHANGKEY_DISABLE=probe 可禁用（崩溃二分调试用）
    if "probe" not in DISABLE:
        switcher.context_probe = uia_text.AsyncContextProbe()
        switcher.context_probe.start()
    print(f"switcher: {len(switcher.rules)} rules, "
          f"default={switcher.default_mode}, debounce={switcher.debounce_ms}ms, "
          f"layers={switcher.layers}, fuzzy={switcher.fuzzy}")

    if "urlserver" not in DISABLE:
        # L2：本地 HTTP 端点收浏览器扩展上报的 URL
        srv = start_url_server(switcher, port=switcher.url_port)
        print(f"url endpoint: http://127.0.0.1:{srv.server_address[1]}/url "
              f"(browser extension -> POST)")

    ui = UI()
    ui.poll()
    fe = Frontend(rime, ui, switcher=switcher)
    fe.start()
    threading.Thread(target=keyboard.wait, daemon=True).start()

    # 系统托盘：词库管理 / 暂停拦截 / 开机自启 / 退出
    tray_icon = None
    if "tray" not in DISABLE:
        tray_icon = TrayIcon(
            tip="ShangKey 输入法（左键菜单）",
            items=[(TRAY_DICT, "词库管理…", None),
                   (TRAY_PAUSE, "暂停键盘拦截", "check"),
                   (TRAY_AUTORUN, "开机自启", "check"),
                   (TRAY_RESET_STATUS, "语言图标复位到右上角", None),
                   (TRAY_QUIT, "退出", None)],
            icon_path=PROJECT_ROOT / "runtime" / "shangkey.ico")
        if autostart.is_enabled():
            tray_icon.checked.add(TRAY_AUTORUN)

        def _on_tray(mid):
            # 托盘线程：涉及 UI/引擎的都投队列回主线程
            if mid == TRAY_DICT:
                if getattr(sys, "frozen", False):
                    cmd = [sys.executable]
                    cwd = PROJECT_ROOT
                else:
                    cmd = [sys.executable, "-m", "rimeime.dictmgr"]
                    cwd = PROJECT_ROOT
                subprocess.Popen(cmd, cwd=str(cwd),
                                 creationflags=DETACHED_PROCESS)
            elif mid == TRAY_PAUSE:
                ui.queue.put(("pause",))
            elif mid == TRAY_AUTORUN:
                if autostart.is_enabled():
                    autostart.disable()
                else:
                    autostart.enable()
                tray_icon.set_checked(TRAY_AUTORUN, autostart.is_enabled())
            elif mid == TRAY_RESET_STATUS:
                ui.queue.put(("reset_status",))
            elif mid == TRAY_QUIT:
                fe.quit()

        if tray_icon.start(on_menu=_on_tray):
            print("tray icon running")

    print("ShangKey IME running. Ctrl+Alt+K 开关 / Shift 切中英 / Ctrl+Alt+Q 退出")
    ui.mainloop()
    print("[main] mainloop exited", flush=True)
    if tray_icon:
        tray_icon.stop()
    os._exit(0)


if __name__ == "__main__":
    main()
