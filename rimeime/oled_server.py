"""OLED 小屏同步（第 2 部分）：局域网 HTTP 取帧服务 + UDP 自动发现。

为什么单独开一个服务（而不是复用 urlserver.py）
------------------------------------------------
urlserver 绑 127.0.0.1，只给本机浏览器扩展用；小屏走 WiFi，必须监听
0.0.0.0 才有局域网设备能连上。两者协议、监听地址、生命周期都不同，
分开更清晰，也让「要不要开小屏服务」可以独立开关。

接口
----
  GET /frame.bin          -> 1024 字节原始位图（SSD1306 页序）
  GET /frame.png          -> PNG 预览图（浏览器/手机直接看，方便调试）
  GET /state.json         -> 状态 JSON（自检、脚本用）
  GET /                   -> HTML 调试页（列出上面三个链接）

安全
----
局域网内任何设备都能取到你的输入内容。默认监听 0.0.0.0，但：
  * 可用环境变量 SHANGKEY_OLED_HOST 改监听地址（设为 127.0.0.1 即关闭外网访问）
  * 可用 SHANGKEY_OLED_PORT 改端口
  * 可用 SHANGKEY_OLED_TOKEN 加一个简单 token（?token=xxx），防止同网段蹭用
注意：Windows 防火墙首次会拦截，需要放行该端口（见 README）。

UDP 自动发现
------------
ESP8266 不知道电脑的 IP 怎么办？开机后向局域网广播 255.255.255.255:61825
发一个 4 字节探测包，本机收到后回一条「IP:端口」，小屏就自己接上了。
电脑 IP 变了（DHCP 续租/换网段）也不需要重新烧录固件。
"""
import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import oled
from .oled import FRAME_BYTES, WIDTH, HEIGHT

DEFAULT_PORT = 61824
DISCOVERY_PORT = 61825
DISCOVERY_MAGIC = b"SKY1"          # 小屏发来的探测包
_DISCOVERY_REPLY = b"SKY1"         # 本机回给它的应答前缀

_TOKEN = os.environ.get("SHANGKEY_OLED_TOKEN", "").strip()


class _Renderer(threading.Thread):
    """后台渲染线程：状态变了就重画一帧，HTTP 线程直接取缓存。

    打字时每按一键都会改状态，同步渲染要 10ms 左右，会拖慢候选窗；
    放到后台后主线程只做「置脏标记」，帧渲染与编码工作错开。
    """

    def __init__(self, state):
        super().__init__(daemon=True, name="shangkey-oled-render")
        self.state = state
        self._frame = b"\x00" * FRAME_BYTES
        self._ver = -1
        self._lock = threading.Lock()
        self._dirty = threading.Event()
        self._dirty.set()

    def mark_dirty(self):
        self._dirty.set()

    def get_frame(self):
        """取当前帧；发现状态已更新时先同步渲染一帧（保证不落后一拍）。"""
        with self._lock:
            ver = self.state.version
            if ver != self._ver:
                need = True
            else:
                need = False
        if need:
            frame = oled.render_frame(self.state)
            with self._lock:
                self._frame = frame
                self._ver = self.state.version
        with self._lock:
            return self._frame

    def run(self):
        while True:
            self._dirty.wait(timeout=1.0)
            self._dirty.clear()
            try:
                frame = oled.render_frame(self.state)
            except Exception as ex:
                print("[oled] 渲染失败:", ex, flush=True)
                continue
            with self._lock:
                self._frame = frame
                self._ver = self.state.version


def _local_lan_ip():
    """取本机在局域网中的真实 IP。

    不能用 socket.gethostbyname(gethostname())：多网卡（有虚拟网卡、
    VPN、WSL）时它会返回 127.0.0.1 或某个虚拟网卡地址，小屏就连不上。
    可靠做法是建一个 UDP socket 并 connect 到一个公网地址——connect
    只做路由选择不发包，内核会据此选定出口网卡，于是 getsockname()
    就是真实的局域网 IP。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    finally:
        s.close()
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    return "127.0.0.1"


class _Discovery(threading.Thread):
    """UDP 广播应答：告诉小屏「本机在哪」。"""

    def __init__(self, port, http_port):
        super().__init__(daemon=True, name="shangkey-oled-discovery")
        self.port = port
        self.http_port = http_port
        self._stop = False

    def run(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except OSError as ex:
            print(f"[oled] UDP 套接字创建失败: {ex}", flush=True)
            return
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("", self.port))
        except OSError as ex:
            print(f"[oled] UDP {self.port} 绑定失败（自动发现不可用）: {ex}",
                  flush=True)
            sock.close()
            return
        sock.settimeout(0.5)
        print(f"[oled] 自动发现监听 UDP {self.port}", flush=True)
        while not self._stop:
            try:
                data, addr = sock.recvfrom(64)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data.startswith(DISCOVERY_MAGIC):
                continue
            host = _local_lan_ip()
            reply = ("%s:%d" % (host, self.http_port)).encode("ascii")
            try:
                sock.sendto(reply, addr)
                print(f"[oled] 小屏 {addr[0]} 请求发现 -> 回复 {reply.decode()}",
                      flush=True)
            except OSError:
                pass
        sock.close()


def _authorized(handler):
    """token 校验：设置了 SHANGKEY_OLED_TOKEN 就必须带对。"""
    if not _TOKEN:
        return True
    from urllib.parse import urlparse, parse_qs
    q = parse_qs(urlparse(handler.path).query)
    return q.get("token", [""])[0] == _TOKEN


def start_oled_server(state, host=None, port=None, discovery=True):
    """启动取帧 HTTP 服务 + UDP 自动发现。返回 (http_server, renderer)。"""
    host = host or os.environ.get("SHANGKEY_OLED_HOST", "0.0.0.0")
    port = int(port or os.environ.get("SHANGKEY_OLED_PORT", DEFAULT_PORT))
    renderer = _Renderer(state)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, code, body, ctype, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not _authorized(self):
                self._send(403, b"forbidden", "text/plain")
                return
            path = self.path.split("?")[0].rstrip("/") or "/"
            if path == "/frame.bin":
                # ETag = 状态版本号。画面没变就回 304 空体，
                # 小屏空闲时不必每 200ms 白传 1KB（省电也省 WiFi 带宽）。
                etag = '"%d"' % state.version
                if self.headers.get("If-None-Match") == etag:
                    self.send_response(304)
                    self.send_header("ETag", etag)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self._send(200, renderer.get_frame(), "application/octet-stream",
                           {"ETag": etag})
            elif path == "/frame.png":
                png = oled.frame_to_png(renderer.get_frame())
                if png is None:
                    self._send(500, b"no pillow", "text/plain")
                else:
                    import io
                    buf = io.BytesIO()
                    png.save(buf, format="PNG")
                    self._send(200, buf.getvalue(), "image/png")
            elif path == "/state.json":
                body = json.dumps(state.snapshot(), ensure_ascii=False)
                self._send(200, body.encode("utf-8"),
                           "application/json; charset=utf-8")
            elif path == "/":
                html = (
                    "<html><head><meta charset='utf-8'>"
                    "<title>ShangKey OLED</title>"
                    "<style>body{font-family:sans-serif;background:#111;color:#eee;"
                    "padding:24px}img{image-rendering:pixelated;"
                    "border:4px solid #444;width:512px;height:256px}"
                    "a{color:#6cf}</style></head><body>"
                    "<h3>ShangKey 小屏调试</h3>"
                    "<p><img src='/frame.png?t=%d'></p>"
                    "<p><a href='/frame.bin'>frame.bin</a> (1024B) | "
                    "<a href='/state.json'>state.json</a></p>"
                    "</body></html>" % int(state.version))
                self._send(200, html.encode("utf-8"),
                           "text/html; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain")

        def log_message(self, *args):
            pass

    try:
        srv = ThreadingHTTPServer((host, port), Handler)
    except OSError as ex:
        print(f"[oled] HTTP 服务启动失败 {host}:{port} -> {ex}", flush=True)
        return None, None
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True,
                     name="shangkey-oled-http").start()
    print(f"[oled] 取帧服务 http://{host}:{port}/frame.bin "
          f"({WIDTH}x{HEIGHT}, {FRAME_BYTES}B)", flush=True)
    if _TOKEN:
        print("[oled] 已启用 token 校验（?token=xxx）", flush=True)

    if discovery:
        _Discovery(DISCOVERY_PORT, port).start()
    return srv, renderer
