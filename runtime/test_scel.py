"""合成 scel 测试：按真实布局构造 -> parse_scel 解析 -> 校验 round-trip。

真实布局：魔数 12B | 词库名@0x130 | 拼音表@0x1540（恰好填满到 0x2628）
| 中文词表@0x2628。
"""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rimeime.importer import parse_scel

MAGIC = b"\x40\x15\x00\x00\x44\x43\x53\x01\x01\x00\x00\x00"
PY_START = 0x1540
CN_START = 0x2628
NAME = "ShangKey合成测试词库"

# (idx, pinyin)
PY_REAL = [(1, "shang"), (2, "hai"), (3, "jian"), (4, "ce"),
           (5, "shi"), (6, "fan"), (7, "yi")]

# (word, py_idx列表, freq)
WORDS = [
    ("上海键", [1, 2, 3], 2000),
    ("上海间", [1, 2, 3], 500),
    ("测试", [4, 5], 800),
    ("翻译", [6, 7], 9999),
    ("反译", [6, 7], 100),
]


def build(path):
    buf = bytearray(CN_START)
    buf[0:12] = MAGIC
    name_bytes = NAME.encode("utf-16-le")
    buf[0x130:0x130 + len(name_bytes)] = name_bytes

    # 拼音表：真实条目 + 1 字音填充，恰好填满到 CN_START
    pos = PY_START
    for idx, py in PY_REAL:
        pb = py.encode("utf-16-le")
        buf[pos:pos + 4] = struct.pack("<HH", idx, len(pb))
        buf[pos + 4:pos + 4 + len(pb)] = pb
        pos += 4 + len(pb)
    filler_idx = 1000
    while pos < CN_START:
        rem = CN_START - pos
        # 余数 >=12 用 6 字节条目；尾部 rem∈{6,8,10} 用精确长度收尾
        ln = 2 if rem >= 12 else rem - 4
        pb = ("a" * (ln // 2)).encode("utf-16-le")
        buf[pos:pos + 4 + ln] = struct.pack("<HH", filler_idx, ln) + pb
        pos += 4 + ln
        filler_idx += 1
    assert pos == CN_START, f"拼音表未对齐: {pos:#x}"

    # 中文词表
    out = bytearray()
    groups = {}
    for word, py_idx, freq in WORDS:
        groups.setdefault(tuple(py_idx), []).append((word, freq))
    for py_idx, items in groups.items():
        out += struct.pack("<HH", len(items), len(py_idx) * 2)
        for i in py_idx:
            out += struct.pack("<H", i)
        for word, freq in items:
            wb = word.encode("utf-16-le")
            ext = struct.pack("<H", freq) + b"\x00" * 8
            # 真实字段顺序：word_len, 词, ext_len, ext（此前误为 ext_len 在词前）
            out += struct.pack("<H", len(wb)) + wb + struct.pack(
                "<H", len(ext)) + ext
    Path(path).write_bytes(bytes(buf) + bytes(out))


def main():
    p = Path(__file__).with_name("test_synth.scel")
    build(p)
    print(f"built {p} ({p.stat().st_size} bytes)")

    info, entries = parse_scel(p)
    print(f"info: {info!r}")
    for w, py, wt in entries:
        print(f"  {w!r:10} {py!r:20} freq={wt}")

    py_map = dict(PY_REAL)
    expect = sorted((w, " ".join(py_map[i] for i in idx), f)
                    for w, idx, f in WORDS)
    got = sorted((w, py, wt if wt else None) for w, py, wt in entries)
    assert info == NAME, f"name mismatch: {info!r}"
    assert got == expect, f"entries mismatch:\n got={got}\n exp={expect}"
    print("ROUND-TRIP PASS")


if __name__ == "__main__":
    main()
