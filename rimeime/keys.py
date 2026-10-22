"""Windows 虚拟键码 -> rime(X11 keysym) 键码映射，以及修饰键掩码。

rime 内部一律使用 X11 keysym 作为 keycode（见 librime key_table.h），
Windows 前端需要自己做 VK -> keysym 的翻译（Weasel 也是这么做的）。
"""

# rime 修饰键掩码（key_table.h 中的 RimeModifier）
SHIFT = 1 << 0
LOCK = 1 << 1
CONTROL = 1 << 2
ALT = 1 << 3  # kMod1Mask
SUPER = 1 << 26
RELEASE = 1 << 30

# X11 keysym 常量（常用子集）
XK_SPACE = 0x020
XK_BACKSPACE = 0xFF08
XK_TAB = 0xFF09
XK_RETURN = 0xFF0D
XK_ESCAPE = 0xFF1B
XK_DELETE = 0xFFFF
XK_HOME = 0xFF50
XK_LEFT = 0xFF51
XK_UP = 0xFF52
XK_RIGHT = 0xFF53
XK_DOWN = 0xFF54
XK_PAGE_UP = 0xFF55
XK_PAGE_DOWN = 0xFF56
XK_END = 0xFF57
XK_INSERT = 0xFF63
XK_MENU = 0xFF67
XK_NUM_LOCK = 0xFF7F
XK_KP_ENTER = 0xFF8D
XK_KP_MULTIPLY = 0xFFAA
XK_KP_ADD = 0xFFAB
XK_KP_SUBTRACT = 0xFFAD
XK_KP_DECIMAL = 0xFFAE
XK_KP_DIVIDE = 0xFFAF
XK_KP_0 = 0xFFB0  # .. 0xFFB9
XK_F1 = 0xFFBE  # .. 0xFFE0

# VK -> keysym 特殊键表
_VK_SPECIAL = {
    0x08: XK_BACKSPACE,   # Back
    0x09: XK_TAB,         # Tab
    0x0D: XK_RETURN,      # Return
    0x13: 0xFF13,         # Pause
    0x1B: XK_ESCAPE,      # Escape
    0x20: XK_SPACE,       # Space
    0x21: XK_PAGE_UP,
    0x22: XK_PAGE_DOWN,
    0x23: XK_END,
    0x24: XK_HOME,
    0x25: XK_LEFT,
    0x26: XK_UP,
    0x27: XK_RIGHT,
    0x28: XK_DOWN,
    0x2C: 0xFF61,         # Print
    0x2D: XK_INSERT,
    0x2E: XK_DELETE,
    0x5B: 0xFFEB,         # Left Win -> Super_L
    0x5C: 0xFFEC,         # Right Win
    0x5D: XK_MENU,        # Apps
    0x6A: XK_KP_MULTIPLY,
    0x6B: XK_KP_ADD,
    0x6D: XK_KP_SUBTRACT,
    0x6E: XK_KP_DECIMAL,
    0x6F: XK_KP_DIVIDE,
    0x90: XK_NUM_LOCK,
    # OEM 键：美式键盘布局
    0xBA: 0x03B,  # ; :
    0xBB: 0x03D,  # = +
    0xBC: 0x02C,  # ,
    0xBD: 0x02D,  # - _
    0xBE: 0x02E,  # .
    0xBF: 0x02F,  # / ?
    0xC0: 0x060,  # ` ~
    0xDB: 0x05B,  # [ {
    0xDC: 0x05C,  # \ |
    0xDD: 0x05D,  # ] }
    0xDE: 0x027,  # ' "
}

# F1-F24
for _i in range(24):
    _VK_SPECIAL[0x70 + _i] = XK_F1 + _i
# 小键盘 0-9
for _i in range(10):
    _VK_SPECIAL[0x60 + _i] = XK_KP_0 + _i


def vk_to_keycode(vk: int) -> int:
    """Windows VK -> rime keysym。字母统一转小键形（大小写由 mask 表达）。"""
    if vk in _VK_SPECIAL:
        return _VK_SPECIAL[vk]
    if 0x30 <= vk <= 0x39:  # 0-9
        return vk
    if 0x41 <= vk <= 0x5A:  # A-Z -> a-z
        return vk + 0x20
    if 0x60 <= vk <= 0x69:  # numpad 0-9
        return 0x30 + (vk - 0x60)
    return 0  # XK_VoidSymbol


def modifier_mask(shift=False, ctrl=False, alt=False, caps=False,
                  release=False) -> int:
    m = 0
    if shift:
        m |= SHIFT
    if ctrl:
        m |= CONTROL
    if alt:
        m |= ALT
    if caps:
        m |= LOCK
    if release:
        m |= RELEASE
    return m
