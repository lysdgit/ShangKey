"""OLED 小屏同步（第 1 部分）：输入法状态快照 + 128x64 位图渲染。

设计取舍
--------
SSD1306 是纯位图屏，一帧 = 128*64/8 = 1024 字节，天然按「页」组织
（8 页 x 128 列，每字节 8 个垂直像素）。所以本模块在 PC 侧把界面渲染好，
ESP8266 只要把收到的 1024 字节按页刷进 GDDRAM 即可——固件端因此
不需要任何中文字库或图形库，只用 ESP8266 core 自带的 WiFi/HTTP/Wire。

布局（128x64，1bpp）
--------------------
    y=0   拼音串（preedit）
    y=13  候选 1  候选 2      每行两个，共 3 行
    y=26  候选 3  候选 4
    y=39  候选 5  候选 6
    y=52  状态行：中/英/繁 + 切换来源

高亮候选用反白（白底黑字），与电脑端候选窗的观感一致。

渲染放在后台线程（见 oled_server.py）：打字时每按一键都会更新状态，
同步渲染会阻塞主线程约 10ms，足以让候选窗掉帧，因此改为「脏标记 +
后台渲染 + 缓存帧」。
"""
import threading

try:
    from PIL import Image, ImageDraw, ImageFont
    _HAVE_PIL = True
except Exception:  # 没装 Pillow 时降级：小屏不可用，输入法本体不受影响
    _HAVE_PIL = False

WIDTH = 128
HEIGHT = 64
FRAME_BYTES = WIDTH * HEIGHT // 8   # 1024

# 候选字 12px、状态行字 11px：
# 12px 黑体是单色屏上中文可读的下限，10px 中文会糊成一团认不出。
_FONT_SIZE = 12
_FONT_SIZE_SMALL = 11
# 候选列的 x 起点（两列）
_COL_X = (2, 66)
_ROW_H = 12
_ROW_Y0 = 13
# 状态行与候选同用 12px（11px 中文在 64px 高的屏上偏糊），
# 单独用一个 10px 的 ASCII 小字只画英文来源，中文状态用大号字。
# y=50：12px 字的墨迹盒约 13px 高，50+13=63 刚好不越界。
_STATUS_Y = 50

# 中文字体候选（按优先级）。index 用于 ttc 字体集内选字体。
# 首选黑体：微软雅黑在 12px 下 antialiasing 会糊成双影，
# 128x64 单色屏放不下这种抗锯齿，黑体笔画实、12px 就能认清。
_FONT_FILES = (
    ("C:/Windows/Fonts/simhei.ttf", 0),    # 黑体（首选）
    ("C:/Windows/Fonts/msyh.ttc", 0),      # 微软雅黑
    ("C:/Windows/Fonts/simsun.ttc", 0),    # 宋体
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 0),
    ("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", 0),
)

_font_cache = {}
_font_lock = threading.Lock()


def _load_font(size):
    """按 size 取 truetype 字体（带缓存）；找不到任何字体时返回 None。"""
    key = size
    with _font_lock:
        if key in _font_cache:
            return _font_cache[key]
    font = None
    if _HAVE_PIL:
        for path, index in _FONT_FILES:
            try:
                font = ImageFont.truetype(path, size, index=index)
                break
            except OSError:
                continue
    with _font_lock:
        _font_cache[key] = font
    return font


class OledState:
    """输入法当前状态的线程安全快照。

    字段：
        preedit     拼音串（编码中）
        candidates  候选词列表（按 rime 返回顺序）
        highlighted 高亮候选下标，-1 表示无
        mode        "中" / "英" / "繁"
        source      切换来源（URL / 手动 / 规则名 …）
        version     每次内容变化自增，HTTP 层用它判断是否需要 304
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.preedit = ""
        self.candidates = []
        self.highlighted = -1
        self.mode = "中"
        self.source = ""
        self.version = 0

    def update(self, preedit=None, candidates=None, highlighted=None,
               mode=None, source=None):
        """更新状态；只有关键字段真的变了才自增 version。"""
        with self._lock:
            changed = False
            if preedit is not None and preedit != self.preedit:
                self.preedit = preedit
                changed = True
            if candidates is not None and list(candidates) != self.candidates:
                self.candidates = list(candidates)
                changed = True
            if highlighted is not None and highlighted != self.highlighted:
                self.highlighted = highlighted
                changed = True
            if mode is not None and mode != self.mode:
                self.mode = mode
                changed = True
            if source is not None and source != self.source:
                self.source = source
                changed = True
            if changed:
                self.version += 1
            return self.version

    def clear_composition(self):
        """编码结束（选中/取消）：清空拼音串与候选，保留中英状态。"""
        with self._lock:
            changed = False
            if self.preedit:
                self.preedit = ""
                changed = True
            if self.candidates:
                self.candidates = []
                changed = True
            if self.highlighted != -1:
                self.highlighted = -1
                changed = True
            if changed:
                self.version += 1
            return self.version

    def snapshot(self):
        with self._lock:
            return {
                "preedit": self.preedit,
                "candidates": list(self.candidates),
                "highlighted": self.highlighted,
                "mode": self.mode,
                "source": self.source,
                "version": self.version,
            }


def _truncate(draw, text, font, max_w):
    """按像素宽度裁剪文本，超出部分用省略号。"""
    if not text:
        return ""
    if draw.textlength(text, font=font) <= max_w:
        return text
    ell = "…"
    out = text
    while out and draw.textlength(out + ell, font=font) > max_w:
        out = out[:-1]
    return out + ell


def render_frame(state):
    """把状态渲染成 SSD1306 原生页序位图（1024 字节）。

    页序：out[page*128 + x] 的 bit b（第 b 行，b=0 在最上）对应像素 (x, page*8+b)。
    """
    if not _HAVE_PIL:
        return b"\x00" * FRAME_BYTES

    snap = state.snapshot()
    img = Image.new("L", (WIDTH, HEIGHT), 0)
    d = ImageDraw.Draw(img)
    f = _load_font(_FONT_SIZE)
    fs = _load_font(_FONT_SIZE_SMALL)

    if f is not None:
        # 拼音串
        pre = _truncate(d, snap["preedit"] or "", f, WIDTH - 4)
        d.text((2, 0), pre, font=f, fill=255)

        # 候选：两列三行，第 i 个候选用「序号 词」形式
        cands = snap["candidates"][:6]
        hi = snap["highlighted"]
        for i, cand in enumerate(cands):
            col, row = i % 2, i // 2
            x = _COL_X[col]
            y = _ROW_Y0 + row * _ROW_H
            label = "%d %s" % (i + 1, cand)
            label = _truncate(d, label, f, _COL_X[1] - _COL_X[0] - 4)
            if i == hi:
                w = d.textlength(label, font=f)
                d.rectangle([x - 1, y - 1, x + w, y + _FONT_SIZE], fill=255)
                d.text((x, y), label, font=f, fill=0)
            else:
                d.text((x, y), label, font=f, fill=255)

    # 状态行：左边大号「中/英/繁」，右边小号 ASCII 显示切换来源
    mode = snap["mode"] or ""
    if f is not None and mode:
        d.text((2, _STATUS_Y), mode, font=f, fill=255)
    src = (snap["source"] or "").strip()
    if src and fs is not None:
        # 来源可能含中文（规则名），小号字体画不下就只画 ASCII 部分
        ascii_src = "".join(ch for ch in src if ord(ch) < 128)[:16]
        if ascii_src:
            w = d.textlength(ascii_src, font=fs)
            d.text((WIDTH - w - 2, _STATUS_Y + 2), ascii_src, font=fs, fill=255)

    # L -> 1bpp，行优先 packed；再转成 SSD1306 的页优先
    raw = img.convert("1").tobytes()
    out = bytearray(FRAME_BYTES)
    for y in range(HEIGHT):
        page, bit = y >> 3, y & 7
        row = raw[y * (WIDTH // 8):(y + 1) * (WIDTH // 8)]
        base = page * WIDTH
        for x in range(WIDTH):
            if row[x >> 3] & (0x80 >> (x & 7)):
                out[base + x] |= (1 << bit)
    return bytes(out)


def frame_to_png(frame_bytes):
    """把页序位图转成 PNG（调试预览用）。"""
    if not _HAVE_PIL:
        return None
    img = Image.new("1", (WIDTH, HEIGHT), 0)
    px = img.load()
    for y in range(HEIGHT):
        page, bit = y >> 3, y & 7
        base = page * WIDTH
        for x in range(WIDTH):
            if frame_bytes[base + x] & (1 << bit):
                px[x, y] = 255
    return img
