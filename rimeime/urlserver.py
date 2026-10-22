"""L2：本地 HTTP 端点，接收浏览器扩展上报的活动标签页 URL 与光标位置。

协议（无外部依赖，纯 stdlib http.server）：
  POST http://127.0.0.1:<port>/url      body: {"url": "...", "title": "..."}
  POST http://127.0.0.1:<port>/caret    body: {"x": 0, "y": 0, "h": 0, "title": "..."}
  GET  http://127.0.0.1:port/           -> 状态（探活/排查用）

标题匹配机制：浏览器窗口标题 = 活动标签页标题 + " - 浏览器名"，
所以 IME 收到 {url, title} 后，在前台窗口标题里找 title 子串，
命中则认为该前台窗口正在浏览此 URL -> L2 规则生效。
跨窗口/非浏览器窗口标题不匹配，L2 自动失效，不会误伤。

/caret 同理：光标坐标只有在「前台窗口标题包含上报的页面标题」时
才会被采用，避免把别的标签页/别的应用的光标位置套过来。
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import caret_feed

DEFAULT_PORT = 61823
_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


def start_url_server(switcher, port=None):
    """在 daemon 线程起 HTTP 服务，返回 server 实例。"""
    port = port or getattr(switcher, "url_port", None) or DEFAULT_PORT

    class Handler(BaseHTTPRequestHandler):
        def _read_json(self):
            try:
                n = int(self.headers.get("Content-Length", 0) or 0)
                return json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, OSError):
                return {}

        def _empty(self, code=204):
            self.send_response(code)
            for k, v in _CORS.items():
                self.send_header(k, v)
            self.end_headers()

        def do_OPTIONS(self):
            # 扩展的 content script 若直连（不走 background 转发）
            # 会触发预检，这里统一放行
            self._empty()

        def do_POST(self):
            path = self.path.rstrip("/")
            if path in ("/caret", "/shangkey/caret"):
                data = self._read_json()
                try:
                    caret_feed.set_reported(
                        x=int(data.get("x") or 0),
                        y=int(data.get("y") or 0),
                        height=int(data.get("h") or 0),
                        title=str(data.get("title") or "")[:512],
                    )
                except (TypeError, ValueError):
                    pass
                self._empty()
                return
            if path not in ("/url", "/shangkey/url"):
                self.send_error(404)
                return
            data = self._read_json()
            switcher.set_tab(
                url=str(data.get("url") or "")[:2048],
                title=str(data.get("title") or "")[:512],
            )
            self._empty()

        def do_GET(self):
            # 调试视图：当前 tab 上报状态 + 光标来源状态
            win_title = ""
            try:
                from .caret import get_foreground_title
                win_title = get_foreground_title()
            except Exception:
                pass
            body = json.dumps({
                "ok": True, "name": "ShangKey",
                "tab_url": getattr(switcher, "tab_url", ""),
                "tab_title": getattr(switcher, "tab_title", ""),
                "caret_reported": caret_feed.get_reported(win_title),
                "caret_reported_raw": caret_feed.peek_reported(),
                "caret_click": caret_feed.get_click(),
            }, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for k, v in _CORS.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # 静默：IME 控制台不刷屏
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True,
                     name="shangkey-url-server").start()
    return srv
