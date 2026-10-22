"""OLED 端到端集成测试：真实 rime 引擎 + 前端 -> 小屏取帧。

验证链路：
    feed_key("nihao") -> rime 引擎 -> Frontend._refresh
      -> OledState.update(preedit/candidates)
      -> HTTP /frame.bin 拿到含"你好"字样的帧

用法：
    python -m rimeime.oled_e2e
"""
import os
import sys
import time
import urllib.request

from . import keys
from .frontend import Frontend
from .oled import OledState
from .oled_server import start_oled_server
from .repl import make_rime, notification_cb
from .ui import UI

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get(url, timeout=5):
    with OPENER.open(url, timeout=timeout) as r:
        return r.read()


def main():
    fails = []

    def check(name, cond, detail=""):
        print(("  PASS  " if cond else "  FAIL  ") + name +
              (("  -> " + str(detail)) if detail else ""))
        if not cond:
            fails.append(name)

    print("--- 准备 rime 引擎 ---")
    rime = make_rime()
    rime.initialize(on_notification=notification_cb)

    ui = UI()
    state = OledState()
    fe = Frontend(rime, ui, oled_state=state)

    srv, renderer = start_oled_server(state, host="127.0.0.1", port=0,
                                      discovery=False)
    if srv is None:
        print("HTTP 服务启动失败")
        return 1
    port = srv.server_address[1]
    url = f"http://127.0.0.1:{port}/frame.bin"
    time.sleep(0.3)

    def drain():
        ui.root.update()
        while not ui.queue.empty():
            kind, *args = ui.queue.get()
            if kind == "candidates":
                print(f"  [ui] preedit={args[0]!r} "
                      f"cands={[c.text for c in args[1]]}")
            elif kind == "commit":
                print(f"  [ui] commit={args[0]!r}")
            elif kind == "status":
                print(f"  [ui] status={args[0]} {args[1]}")

    print("--- 1. 逐键输入 nihao ---")
    for ch in "nihao":
        fe.feed_key(keys.vk_to_keycode(ord(ch.upper())), 0)
        drain()
        time.sleep(0.03)

    snap = state.snapshot()
    # rime 的 preedit 会自动插空格（ni hao），这里只校验它确实带上了输入
    check("拼音串已同步到小屏状态",
          "ni" in snap["preedit"] and "hao" in snap["preedit"], snap["preedit"])
    check("候选列表已同步", len(snap["candidates"]) > 0, snap["candidates"])
    check("高亮位已同步", snap["highlighted"] >= 0, snap["highlighted"])

    print("--- 2. 取到的帧里确实画上了候选 ---")
    frame = _get(url)
    check("帧长 1024", len(frame) == 1024, len(frame))
    check("帧非空", any(frame))
    # 把帧反查成像素，检查是否包含候选词的笔画
    from .oled import frame_to_png
    img = frame_to_png(frame)
    px = img.load()
    # 候选区（第 13~25 行）应该有亮点
    lit_in_cand = sum(1 for y in range(13, 26)
                      for x in range(0, 64) if px[x, y])
    check("候选区有像素绘制", lit_in_cand > 50, lit_in_cand)
    # 拼音区（第 0~12 行）也应有亮点
    lit_in_pre = sum(1 for y in range(0, 12) for x in range(0, 128) if px[x, y])
    check("拼音区有像素绘制", lit_in_pre > 20, lit_in_pre)
    img.save(os.path.join(os.path.dirname(__file__), "..", "runtime",
                          "oled_e2e.png"))
    print("  帧预览: runtime/oled_e2e.png")

    print("--- 3. 选词上屏后清空 ---")
    fe.feed_key(keys.vk_to_keycode(ord("1")), 0)
    drain()
    snap = state.snapshot()
    check("选词后拼音串清空", snap["preedit"] == "", snap["preedit"])
    check("选词后候选清空", snap["candidates"] == [], snap["candidates"])
    check("选词后中英状态保留", snap["mode"] in ("中", "英", "繁"), snap["mode"])

    print("--- 4. 切英文后状态更新 ---")
    fe.toggle_ascii("手动")
    drain()
    snap = state.snapshot()
    check("切换后 mode=英", snap["mode"] == "英", snap["mode"])
    check("切换来源已记录", snap["source"] == "手动", snap["source"])

    print("--- 5. 切回中文 ---")
    fe.toggle_ascii("手动")
    drain()
    snap = state.snapshot()
    check("切回后 mode=中", snap["mode"] == "中", snap["mode"])

    srv.shutdown()
    srv.server_close()
    try:
        ui.root.destroy()
    except Exception:
        pass
    rime.finalize()

    print()
    if fails:
        print("FAILED: " + ", ".join(fails))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
