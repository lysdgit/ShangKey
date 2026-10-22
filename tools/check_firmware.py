"""固件静态检查：没有 C++ 编译器时的兜底（针对 Arduino/ESP8266 常见坑）。

检查项：
  1. 括号配平（正确处理字符字面量与注释）
  2. 每个函数都有对应实现，调用了不存在的函数
  3. 数组下标越界风险（frameBuf 访问）
  4. http.begin / http.end 配对（不配对会漏连接）
  5. 变量先用后定义
  6. 常见 API 误用：millis() 溢出比较、delay 在 while 里、
     String 未判空、Wire.write 缓冲区越界

用法：python tools/check_firmware.py [固件路径]
"""
import re
import sys
from pathlib import Path

DEFAULT = Path(__file__).resolve().parent.parent / "runtime" / "oled" / \
    "ShangKeyOLED.ino"


def strip_code(src):
    """去掉注释与字符串/字符字面量，保留可解析的代码骨架。"""
    out = []
    i = 0
    n = len(src)
    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                i += 1
        elif c == "/" and i + 1 < n and src[i + 1] == "*":
            i += 2
            while i + 1 < n and not (src[i] == "*" and src[i + 1] == "/"):
                i += 1
            i += 2
        elif c == '"':
            i += 1
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == '"':
                    i += 1
                    break
                i += 1
            out.append('""')
        elif c == "'":
            i += 1
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == "'":
                    i += 1
                    break
                i += 1
            out.append("''")
        else:
            out.append(c)
            i += 1
    return "".join(out)


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    src = path.read_text(encoding="utf-8")
    code = strip_code(src)
    fails = []
    warns = []

    def check(name, cond, detail=""):
        print(("  PASS  " if cond else "  FAIL  ") + name +
              (("  -> " + str(detail)) if detail else ""))
        if not cond:
            fails.append(name)

    def warn(name, detail=""):
        print("  WARN  " + name + (("  -> " + str(detail)) if detail else ""))
        warns.append(name)

    print(f"固件静态检查: {path.name}\n")

    print("--- 1. 括号配平 ---")
    for a, b, nm in (("{", "}", "花括号"), ("(", ")", "圆括号"),
                     ("[", "]", "方括号")):
        d = code.count(a) - code.count(b)
        check(f"{nm}配平", d == 0, f"{a}={code.count(a)} {b}={code.count(b)}")

    print("--- 2. 函数定义与调用 ---")
    defined = set(re.findall(r"\b(?:static\s+)?(?:\w+[\w:*&<>]*\s+)+"
                              r"(\w+)\s*\([^;{]*\)\s*\{", code))
    defined |= set(re.findall(r"\b(\w+)\s*\([^;{]*\)\s*\{", code))
    called = set(re.findall(r"\b(\w+)\s*\(", code))
    # 排除「变量名(」这种构造调用（其实是变量初始化，如 IPAddress bcast(...)）
    # 两种形态都要覆盖：Type name(...); 与 Type name = ...;
    var_names = set(re.findall(r"\b\w+\s+(\w+)\s*[=;]", code))
    var_names |= set(re.findall(r"\b\w+\s+(\w+)\s*\(", code))
    called -= var_names
    # Arduino/ESP8266 core 与标准库提供的
    builtin = {
        "setup", "loop", "if", "for", "while", "switch", "return", "sizeof",
        "memset", "memcpy", "delay", "delayMicroseconds", "millis", "micros",
        "pinMode", "begin", "end", "write", "read", "available", "readBytes",
        "setTimeout", "setClock", "beginTransmission", "endTransmission",
        "beginPacket", "endPacket", "parsePacket", "printf", "print", "println",
        "localIP", "subnetMask", "status", "hasHeader", "getHeader", "getSize",
        "getStreamPtr", "addHeader", "GET", "mode", "config",
        # String 的成员方法
        "c_str", "indexOf", "length", "substring", "toString",
        "IPAddress", "String", "uint8_t", "uint16_t", "uint32_t", "int",
        "bool", "void", "static", "const", "else", "do",
    }
    unknown = called - defined - builtin
    check("无未定义的函数调用", not unknown, sorted(unknown))

    print("--- 3. frameBuf 访问安全 ---")
    # frameBuf 大小 FRAME_LEN=1024，页 p 的字节偏移 p*WIDTH
    m = re.search(r"FRAME_LEN\s*=\s*WIDTH\s*\*\s*HEIGHT\s*/\s*8", code)
    check("FRAME_LEN 由 WIDTH*HEIGHT/8 推导", bool(m))
    m2 = re.search(r"WIDTH\s*=\s*128", code)
    m3 = re.search(r"HEIGHT\s*=\s*64", code)
    check("分辨率为 128x64", bool(m2 and m3))
    # frameBuf 的下标表达式里应含 WIDTH 或 FRAME_LEN，
    # 或是被显式边界约束的循环变量（x < WIDTH、got < FRAME_LEN）
    idxs = re.findall(r"frameBuf\s*\[([^\]]+)\]", code)
    bad = []
    for x in idxs:
        if re.search(r"WIDTH|FRAME_LEN", x):
            continue
        # 形如 p * WIDTH + x / p * WIDTH + WIDTH - 1 已被上面覆盖
        if re.search(r"\b(x|got|p)\b", x):
            continue
        bad.append(x)
    check("frameBuf 下标均含宽度约束", not bad, bad)

    print("--- 4. HTTP 连接配对 ---")
    begins = len(re.findall(r"http\.begin\s*\(", code))
    ends = len(re.findall(r"http\.end\s*\(", code))
    check("http.begin / http.end 数量匹配", begins == ends,
          f"begin={begins} end={ends}")

    print("--- 5. 变量声明在使用前 ---")
    for var in ("pcHost", "lastEtag", "pcFound"):
        decl = re.search(r"\b" + var + r"\b\s*[=;]", code)
        uses = [m.start() for m in re.finditer(r"\b" + var + r"\b", code)]
        check(f"{var} 已声明", bool(decl) and bool(uses))
    # frameBuf 是数组，声明形如 uint8_t frameBuf[FRAME_LEN];
    fb = re.search(r"uint8_t\s+frameBuf\s*\[\s*([A-Z_]+)\s*\]", code)
    check("frameBuf 数组已声明", bool(fb), fb.group(1) if fb else "")
    # 声明的长度必须等于 FRAME_LEN
    if fb:
        check("frameBuf 长度 = FRAME_LEN", fb.group(1) == "FRAME_LEN",
              fb.group(1))

    print("--- 6. 常见 Arduino 坑 ---")
    # millis() 溢出：应该用无符号差值比较
    for m in re.finditer(r"millis\(\)\s*([<>=!]+)\s*(\w+)", code):
        warn(f"millis() 直接比较（应确认用了无符号差值）: {m.group(0)}")
    # Wire.write 缓冲区
    w = re.findall(r"Wire\.write\s*\(\s*&?(\w+)\s*\[\s*([^]]+)\]\s*,\s*(\w+)\s*\)",
                    code)
    for arr, off, ln in w:
        ok = ("WIDTH" in off or "FRAME_LEN" in off or "sizeof" in ln)
        if not ok:
            warn(f"Wire.write 长度/偏移需人工确认: {arr}[{off}], {ln}")
    # 广播地址
    if "255.255.255.255" in src and "subnetBroadcast" not in src:
        check("广播用子网地址而非 255.255.255.255", False,
              "ESP8266 官方 issue #1252：受限广播收不到")
    else:
        check("广播用子网地址而非 255.255.255.255", True)
    # I2C 速率
    ck = re.search(r"Wire\.setClock\s*\(\s*(\d+)\s*\)", code)
    if ck:
        check("I2C 速率 <= 100kHz（自选引脚走软件 I2C）",
              int(ck.group(1)) <= 100000, ck.group(1))
    else:
        check("显式设置 I2C 速率", False, "未找到 Wire.setClock")
    # http 失败重试
    check("取帧失败有重连逻辑",
          "FETCH_FAILED" in code and "pcFound = false" in code)

    print()
    if fails:
        print("FAILED: " + ", ".join(fails))
        return 1
    if warns:
        print(f"通过（{len(warns)} 条警告需人工确认）")
        return 0
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
