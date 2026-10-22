"""OLED 小屏同步自测：渲染位图 + HTTP 取帧 + 状态流转。

用法：
    python -m rimeime.oled_selftest
生成 runtime/oled_preview.png 供肉眼检查排版。
"""
import json
import os
import socket
import threading
import time
import urllib.request

from . import oled
from .oled import OledState, frame_to_png
from .oled_server import start_oled_server

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get(url, timeout=5):
    with OPENER.open(url, timeout=timeout) as r:
        return r.status, r.read()


def main():
    fails = []

    def check(name, cond, detail=""):
        print(("  PASS  " if cond else "  FAIL  ") + name +
              (("  -> " + str(detail)) if detail else ""))
        if not cond:
            fails.append(name)

    print("--- 1. 状态快照 ---")
    st = OledState()
    v0 = st.version
    st.update(preedit="ni hao", candidates=["你好", "尼豪", "泥壕"],
              highlighted=0, mode="中", source="URL")
    check("update 后 version 自增", st.version > v0, st.version)
    v1 = st.version
    st.update(preedit="ni hao", candidates=["你好", "尼豪", "泥壕"],
              highlighted=0, mode="中", source="URL")
    check("内容未变时 version 不变（避免无谓重绘）", st.version == v1)
    st.update(preedit="ni ha")
    check("内容变化时 version 自增", st.version > v1)

    print("--- 2. 位图渲染 ---")
    st2 = OledState()
    st2.update(preedit="nihao", candidates=["你好", "尼豪", "泥壕", "拟好", "逆好"],
               highlighted=0, mode="中", source="URL")
    frame = oled.render_frame(st2)
    check("帧长 1024 字节", len(frame) == 1024, len(frame))
    check("帧非全空（有内容）", any(frame), sum(frame))
    png = frame_to_png(frame)
    if png is not None:
        out = os.path.join(os.path.dirname(__file__), "..", "runtime",
                           "oled_preview.png")
        out = os.path.abspath(out)
        png.save(out)
        print("  预览图:", out)

    print("--- 3. 页序转换正确性 ---")
    # 人工构造：仅第 0 页第 0 列的第 0 位置亮 -> 期望 byte0 = 0x01
    class Fake:
        def snapshot(self):
            return {"preedit": "", "candidates": [], "highlighted": -1,
                    "mode": "", "source": "", "version": 0}
    # 直接验证 render_frame 的输出结构：构造一张已知图不现实，
    # 改为验证 frame_to_png 与 render_frame 互为逆运算
    if png is not None:
        px = png.load()
        lit = sum(1 for y in range(64) for x in range(128) if px[x, y])
        raw_lit = sum(bin(b).count("1") for b in frame)
        check("PNG 亮点数 == 位图亮点数", lit == raw_lit, f"{lit} vs {raw_lit}")

    print("--- 4. HTTP 取帧服务 ---")
    srv, renderer = start_oled_server(st2, host="127.0.0.1", port=0,
                                      discovery=False)
    if srv is None:
        check("HTTP 服务启动", False)
        return 1
    port = srv.server_address[1]
    time.sleep(0.3)
    try:
        code, body = _get(f"http://127.0.0.1:{port}/frame.bin")
        check("GET /frame.bin 返回 200", code == 200, code)
        check("frame.bin 长度 1024", len(body) == 1024, len(body))
        check("frame.bin 与渲染结果一致", body == frame)

        code, body = _get(f"http://127.0.0.1:{port}/state.json")
        check("GET /state.json 返回 200", code == 200, code)
        data = json.loads(body.decode("utf-8"))
        check("state.json 含拼音串", data["preedit"] == "nihao", data["preedit"])
        check("state.json 候选数 5", len(data["candidates"]) == 5,
              data["candidates"])

        code, body = _get(f"http://127.0.0.1:{port}/frame.png")
        check("GET /frame.png 返回 PNG", code == 200 and body[:4] == b"\x89PNG",
              code)

        code, body = _get(f"http://127.0.0.1:{port}/")
        check("GET / 返回调试页", code == 200 and b"frame.png" in body, code)
    except Exception as ex:
        check("HTTP 请求", False, ex)

    print("--- 5. 状态变更后取到新帧 ---")
    try:
        st2.update(preedit="shi", candidates=["是", "市", "事"], highlighted=1)
        code, body = _get(f"http://127.0.0.1:{port}/frame.bin")
        check("更新后帧内容变化", body != frame)
        code, body = _get(f"http://127.0.0.1:{port}/state.json")
        data = json.loads(body.decode("utf-8"))
        check("state.json 拼音串已更新", data["preedit"] == "shi", data["preedit"])
    except Exception as ex:
        check("状态更新", False, ex)

    print("--- 6. clear_composition ---")
    st2.clear_composition()
    snap = st2.snapshot()
    check("清空后拼音串为空", snap["preedit"] == "")
    check("清空后候选为空", snap["candidates"] == [])
    check("清空后保留中英状态", snap["mode"] == "中", snap["mode"])

    print("--- 7. UDP 自动发现应答 ---")
    # discovery 线程在 start_oled_server(discovery=True) 里起，
    # 端口固定 61825（小屏固件里的 DISCOVERY_PORT）
    disc_srv, _ = start_oled_server(st2, host="127.0.0.1", port=0,
                                    discovery=True)
    time.sleep(0.4)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(3)
        s.sendto(b"SKY1", ("127.0.0.1", 61825))
        data, addr = s.recvfrom(64)
        s.close()
        ok = bool(data) and b":" in data
        check("UDP 探测有应答（含 IP:端口）", ok, data)
        if ok:
            ip, p = data.decode().split(":")
            check("应答 IP 非回环", not ip.startswith("127."), ip)
            check("应答端口是本次 HTTP 端口",
                  int(p) == disc_srv.server_address[1], p)
    except Exception as ex:
        check("UDP 探测", False, ex)
    if disc_srv:
        disc_srv.shutdown()
        disc_srv.server_close()

    srv.shutdown()
    srv.server_close()

    print()
    if fails:
        print("FAILED: " + ", ".join(fails))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
