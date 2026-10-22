"""词库导入：txt / csv / RIME dict -> shangkey.dict.yaml -> 重新部署。

用法：
    python -m rimeime.importer 词库.txt [更多文件...]
    python -m rimeime.importer --dry-run 词库.txt      # 只解析，不写盘
    python -m rimeime.importer --no-deploy 词库.txt    # 写盘，不重新部署
    python -m rimeime.importer --no-stop-ime 词库.txt  # IME 运行时只提示，不自动退出

支持的行格式（词语/拼音顺序自动识别，注释行 # 或 ; 开头）：
    你好<TAB>nihao<TAB>5000     词语在前
    nihao<TAB>你好<TAB>5000     拼音在前
    你好,nihao,5000             逗号分隔
    你好 nihao 5000             空白分隔
    你好                        （仅词语，拼音由 pypinyin 生成）
RIME dict.yaml 文件整体导入（自动识别 --- / ... 头部结构）。

拼音规范化（对齐 luna_pinyin 基础词库表示）：小写、去声调数字（ni3->ni）、
ü->v、nue/lue->nve/lve。无声调分隔的拼音串（如 nihao）按词语用 pypinyin 切分。

文件编码自动探测：utf-8(sig) -> gbk -> gb18030。
"""

import argparse
import ctypes
import os
import re
import sys
import time
from pathlib import Path

from .repl import DEFAULT_SHARED, make_rime, notification_cb

from .paths import PROJECT_ROOT
USER_DIR = PROJECT_ROOT / "runtime" / "rime_user"
MANAGED_DICT = USER_DIR / "shangkey.dict.yaml"
SCHEMA = "luna_pinyin_simp"
SCHEMA_PATCH = USER_DIR / f"{SCHEMA}.custom.yaml"
DICT_NAME = "shangkey"
MUTEX_NAME = "ShangKeyIME_Singleton"

#: 无来源词频时的默认权重。基础词库条目默认权重为 0.0，
#: 1000 可保证导入词排在基础词库之前（导入词优先）。
DEFAULT_WEIGHT = 1000

SYLLABLE = re.compile(r"^[a-zA-Z]+[1-5]?$")
COMMENT = re.compile(r"^\s*[#;]")
CJK = re.compile(r"[\u3400-\u9fff]")

try:
    from pypinyin import lazy_pinyin
except ImportError:  # pragma: no cover
    lazy_pinyin = None


def has_cjk(s):
    return bool(CJK.search(s))


# ---------------------------------------------------------------- 解析

def read_text(path):
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "gbk", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"无法识别文件编码（试过 utf-8/gbk/gb18030）: {path}")


def looks_like_pinyin(s):
    s = s.strip()
    if not s:
        return False
    return all(SYLLABLE.match(tok) for tok in s.split())


def _clean_syllable(tok):
    """单音节清理：去声调数字、ü->v、nue/lue->nve/lve。"""
    tok = re.sub(r"[1-5]$", "", tok)
    tok = tok.replace("ü", "v")
    tok = re.sub(r"^([nl])ue$", r"\1ve", tok)
    return tok


_SYLLABLES = None


def _valid_syllables():
    """引擎合法音节集 = luna_pinyin 基础词库出现过的全部音节。

    表编译时不在音节表里的音节会被丢弃，导入前先自行校验，
    避免静默丢词。取不到基础词库时返回空集（跳过校验）。
    """
    global _SYLLABLES
    if _SYLLABLES is None:
        shared = Path(os.environ.get("RIME_SHARED_DATA", DEFAULT_SHARED))
        path = shared / "luna_pinyin.dict.yaml"
        syls = set()
        try:
            for _, py, _ in parse_rime_dict(read_text(path)):
                syls.update(py.split())
        except Exception:
            pass
        _SYLLABLES = syls
    return _SYLLABLES


def _segment_syllables(s, valid):
    """DP 把拼音串切成合法音节序列；失败返回 None。"""
    n = len(s)
    dp = [None] * (n + 1)
    dp[0] = []
    for i in range(1, n + 1):
        for j in range(max(0, i - 8), i):
            if dp[j] is not None and s[j:i] in valid:
                dp[i] = dp[j] + [s[j:i]]
                break
    return dp[n]


def normalize_pinyin(s, word=None):
    """规范化拼音；无声调分隔/切分错误的拼音串自动重新切分。"""
    s = s.strip().lower()
    if not s:
        return ""
    toks = [t for t in (_clean_syllable(t) for t in s.split()) if t]
    if not toks:
        return ""
    if len(toks) == 1 and word and has_cjk(word):
        # 单 token 拼音串（nihao）：按词语用 pypinyin 切分
        if lazy_pinyin:
            toks = [t for t in (_clean_syllable(x) for x in lazy_pinyin(word))
                    if t]
    valid = _valid_syllables()
    if valid and not all(t in valid for t in toks):
        # 带空格但切分错误（shanghai jian）或单 token 未切开：
        # 拼接后用合法音节集 DP 重切
        seg = _segment_syllables("".join(toks), valid)
        if seg:
            toks = seg
        else:
            return ""
    return " ".join(toks)


def _split_fields(line):
    if "\t" in line:
        return [f.strip() for f in line.split("\t")]
    if "," in line or "，" in line:
        return [f.strip() for f in re.split("[,，]", line)]
    parts = line.split()
    if len(parts) > 3:
        # 空白分隔且超过 3 段：拼音被拆成多段（词 + 拼音段们 + 词频）
        if parts[-1].replace(".", "", 1).isdigit():
            parts = [parts[0], " ".join(parts[1:-1]), parts[-1]]
        else:
            parts = [parts[0], " ".join(parts[1:])]
    return parts


def _parse_weight(s):
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
    if s.endswith("%"):
        # 百分比是多音字概率而非词频；preset 词表关闭时百分比会变 0，
        # 这里转成绝对值（x10）保持相对排序。
        try:
            return max(1, round(float(s[:-1]) * 10))
        except ValueError:
            return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_line(line):
    """解析一行，返回 (word, pinyin, weight|None)；无法解析返回 None。"""
    line = line.strip()
    if not line or COMMENT.match(line):
        return None
    fields = [f for f in _split_fields(line) if f]
    if not fields:
        return None
    if len(fields) == 1:
        # 纯词表行：仅中文词语，拼音由 pypinyin 生成
        word = fields[0]
        if has_cjk(word) and lazy_pinyin:
            return (word, " ".join(lazy_pinyin(word)), None)
        return None
    if len(fields) > 3:
        return None
    if looks_like_pinyin(fields[0]) and not looks_like_pinyin(fields[1]):
        pinyin, word = fields[0], fields[1]
    else:
        word, pinyin = fields[0], fields[1]
    weight = _parse_weight(fields[2]) if len(fields) == 3 else None
    return (word, pinyin, weight)


def parse_text(text):
    """解析普通文本（逐行），返回 [(word, pinyin, weight)]。"""
    entries = []
    for ln in text.splitlines():
        e = parse_line(ln)
        if e:
            entries.append(e)
    return entries


def parse_rime_dict(text):
    """解析 RIME dict.yaml：跳过 --- ... 头部，正文同样逐行解析。"""
    lines = text.splitlines()
    body_start = 0
    for i, ln in enumerate(lines):
        if ln.strip() == "...":
            body_start = i + 1
            break
    return parse_text("\n".join(lines[body_start:]))


def parse_scel(path):
    """解析搜狗 scel 细胞词库，返回 (info, entries)。

    结构（真实文件实测，并与 scel2txt 等开源实现交叉验证）：
      0x0000  12 字节魔数 40 15 00 00 44 43 53 01 01 00 00 00
      0x130   词库名（UTF-16LE）
      0x1540  新版：u32 拼音条目数，条目从 0x1544 起（idx 依次 0..n-1）；
              旧版：无计数头，直接是条目 (u16 idx, u16 len, UTF-16LE 拼音)
      词表（起始偏移随版本浮动，结构行走判定）：
        组 (u16 same, u16 py_len字节, u16 py_idx[py_len/2])，
        组内 same 次 (u16 word_len, UTF-16LE 词, u16 ext_len, ext)，
        ext 前 2 字节 u16 词频。
        注意字段顺序是 词 -> ext_len（此前实现误为 ext_len -> 词，
        合成测试文件按同样错误顺序生成所以从未暴露，真实文件回归时修复）。
        文件尾部可能附有一段非词表数据（联想/示例），结构行走遇之即停，
        已收集词数足够即视为成功。
    """
    data = Path(path).read_bytes()
    if len(data) < 0x1540 or data[:4] != b"\x40\x15\x00\x00":
        raise ValueError(f"不是有效的搜狗 scel 文件: {path}")

    info = _scel_name(data)
    py_table, chinese_start = _scel_locate(data)
    if py_table is None:
        raise ValueError(f"无法定位 scel 中文词表（格式不兼容？）: {path}")
    words, _eof = _scel_walk_chinese(data, chinese_start)
    if not words:
        raise ValueError(f"scel 中文词表结构损坏: {path}")

    entries = []
    for word, py_idx, freq in words:
        syls = [py_table.get(i) for i in py_idx]
        if not word or not all(syls):
            continue
        entries.append((word, " ".join(syls),
                        freq if freq else None))
    return info, entries


def _scel_locate(data):
    """定位拼音表 + 中文词表起始偏移：返回 (py_table, start) 或 (None, None)。

    拼音表：新版 0x1540 是 u32 条目数、条目自 0x1544 起（用计数严格截断，
    避免行走越过表尾把词组头误吃成拼音条目）；旧版无计数头，0x1540 起行走。
    词表：先试常见偏移，再试拼音表结尾，再向后扫描；接受标准见 _scel_walk_chinese。
    """
    py_table, py_end = None, None
    count = int.from_bytes(data[0x1540:0x1544], "little")
    if 1 <= count <= 100000:
        table, pos, ok = {}, 0x1544, True
        for i in range(count):
            if pos + 4 > len(data):
                ok = False
                break
            idx = _u16(data, pos)
            ln = _u16(data, pos + 2)
            if idx != i or ln == 0 or ln % 2 or pos + 4 + ln > len(data):
                ok = False
                break
            table[idx] = data[pos + 4:pos + 4 + ln].decode(
                "utf-16-le", "replace")
            pos += 4 + ln
        if ok and len(table) == count:
            py_table, py_end = table, pos
    if py_table is None:
        # 旧版：无计数头，直接行走；idx 必须严格递增，
        # 越过表尾进入词表区会立刻出现回退（词组头 same 远小于当前 idx），
        # 借此截断，避免把词表字节误吃成拼音条目
        table, pos, last = {}, 0x1540, -1
        while pos + 4 <= len(data):
            idx = _u16(data, pos)
            ln = _u16(data, pos + 2)
            if (ln == 0 or ln % 2 or pos + 4 + ln > len(data)
                    or idx <= last):
                break
            table[idx] = data[pos + 4:pos + 4 + ln].decode(
                "utf-16-le", "replace")
            last = idx
            pos += 4 + ln
        if len(table) >= 100:
            py_table, py_end = table, pos
    if py_table is None:
        return None, None

    def acceptable(words, eof):
        # 精确走到 EOF 的完整词表（合成/小文件），或收集了大量有效词
        # 后遇到尾部非词表数据停下（真实文件尾部附有联想/示例段）
        return (eof and words) or len(words) >= 200

    candidates = [0x2628, 0x26C4, py_end]
    candidates += list(range(0x1560, min(len(data), 0x8000), 4))
    for cand in candidates:
        if cand >= len(data):
            continue
        words, eof = _scel_walk_chinese(data, cand)
        if acceptable(words, eof):
            return py_table, cand
    return None, None


def _scel_name(data):
    """读取 0x130 起的词库名（UTF-16LE，遇 0 结束）。"""
    try:
        raw = data[0x130:0x338]
        end = raw.find(b"\x00\x00")
        if end > 0:
            return raw[:end].decode("utf-16-le", "replace")
    except Exception:
        pass
    return ""


def _u16(buf, pos):
    return buf[pos] | (buf[pos + 1] << 8)


def _scel_pinyin_table(data, start, end):
    """解析 [start, end) 区的拼音表；遇填充/异常停止。"""
    table = {}
    pos = start
    while pos + 4 <= end:
        idx = _u16(data, pos)
        ln = _u16(data, pos + 2)
        if ln == 0 or ln % 2 or pos + 4 + ln > end:
            break
        table[idx] = data[pos + 4:pos + 4 + ln].decode("utf-16-le", "replace")
        pos += 4 + ln
    return table


def _scel_walk_chinese(data, start):
    """行走中文词表：组 (u16 same, u16 py_len, u16 py_idx[py_len/2])，
    组内 same 次 (u16 word_len, UTF-16LE 词, u16 ext_len, ext[ext_len])；
    ext 前 2 字节为词频。允许 0000 0000 分隔（跳过）。

    返回 (words, reached_eof)：words 为 [(word, py_idx, freq)]；
    结构断裂即停（真实文件尾部附有非词表数据，停在那里属正常）。
    是否可信由调用方按词数/EOF 判定。
    """
    pos = start
    n = len(data)
    out = []
    while pos < n:
        if pos + 4 > n:
            return out, False
        same = _u16(data, pos)
        py_len = _u16(data, pos + 2)
        if same == 0 and py_len == 0:  # 段间分隔
            pos += 4
            continue
        if same == 0 or py_len == 0 or py_len % 2 or pos + 4 + py_len > n:
            return out, False
        py_idx = [_u16(data, pos + 4 + 2 * i) for i in range(py_len // 2)]
        pos += 4 + py_len
        for _ in range(same):
            if pos + 2 > n:
                return out, False
            wl = _u16(data, pos)
            pos += 2
            if wl == 0 or wl % 2 or pos + wl + 2 > n:
                return out, False
            word = data[pos:pos + wl].decode("utf-16-le", "replace")
            pos += wl
            ext_len = _u16(data, pos)
            pos += 2
            if pos + ext_len > n:
                return out, False
            freq = _u16(data, pos) if ext_len >= 2 else 0
            pos += ext_len
            out.append((word, py_idx, freq))
    return out, True


# ---------------------------------------------------------------- 合并与生成

def load_managed_entries():
    """读取现有 shangkey.dict.yaml，返回 {(word, pinyin): weight}。

    加载时重新规范化拼音：清理历史脏数据（如切分错误的
    "shanghai jian"），同键合并取最大权重。
    """
    if not MANAGED_DICT.exists():
        return {}
    out = {}
    dropped = 0
    for word, pinyin, weight in parse_rime_dict(read_text(MANAGED_DICT)):
        np = normalize_pinyin(pinyin, word)
        if not np:
            dropped += 1
            continue
        key = (word, np)
        w = weight if weight is not None else DEFAULT_WEIGHT
        out[key] = max(out.get(key, w), w)
    if dropped:
        print(f"[清理] 现有词库中 {dropped} 条拼音无效，已移除")
    return out


def merge_entries(old, new):
    """new 合并进 old（原地）。返回 (added, updated)。"""
    added = updated = 0
    for word, pinyin, weight in new:
        key = (word, pinyin)
        w = weight if weight is not None else DEFAULT_WEIGHT
        if key not in old:
            old[key] = w
            added += 1
        elif w != old[key]:
            old[key] = w
            updated += 1
    return added, updated


def write_managed_dict(entries):
    """写 shangkey.dict.yaml（含 import_tables 继承基础词库）。"""
    lines = [
        "# Rime dictionary",
        "# encoding: utf-8",
        "#",
        "# ShangKey 导入词库（rimeime.importer 自动管理，请勿手工编辑）",
        "---",
        f"name: {DICT_NAME}",
        f'version: "{time.strftime("%Y.%m.%d", time.localtime())}"',
        "sort: by_weight",
        # 必须为 true：import_tables 导入的 luna_pinyin 词条本身不带权重列，
        # 靠 essay（八股文）词频排序；若为 false 导入词条权重全 0，候选乱序
        # （生僻字排最前，如 ke yi -> 㕉ㄧ）。自定义词条自带权重列不受影响。
        "use_preset_vocabulary: true",
        "import_tables:",
        "  - luna_pinyin",
        "...",
        "",
    ]
    for (word, pinyin), weight in sorted(
            entries.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1])):
        w = int(weight) if float(weight).is_integer() else weight
        lines.append(f"{word}\t{pinyin}\t{w}")
    MANAGED_DICT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ensure_schema_patch(fuzzy=False):
    """（兼容旧调用）写 luna_pinyin_simp.custom.yaml，见 write_schema_patch。"""
    return write_schema_patch(fuzzy=fuzzy)


def write_schema_patch(fuzzy=False):
    """整体托管 luna_pinyin_simp.custom.yaml（内容确定性，按需重写）。

    补丁内容：
      translator/dictionary  挂载 shangkey 用户词库
      key_binder/bindings/+  编码中翻页键：-,= 与 ,/.
      speller/algebra/+      模糊音（fuzzy=True 时）
    """
    lines = [
        "# ShangKey schema 补丁（importer 自动管理，请勿手工编辑）",
        "patch:",
        f"  translator/dictionary: {DICT_NAME}",
        "  # 编码中的翻页键（不编码时 , . 输出标点，不受影响）",
        "  key_binder/bindings/+:",
        "    - {when: composing, accept: comma, send: Page_Up}",
        "    - {when: composing, accept: period, send: Page_Down}",
        "    - {when: composing, accept: minus, send: Page_Up}",
        "    - {when: composing, accept: equal, send: Page_Down}",
    ]
    if fuzzy:
        # 注意：import_tables 导入的 luna 词条须 use_preset_vocabulary: true
        # 才有权重；模糊音规则追加在 schema 原有 algebra 之后。
        lines += [
            "  # 模糊音（switch_rules.json 的 fuzzy 开关控制）",
            "  speller/algebra/+:",
            "    - derive/^([zcs])h/$1/",
            "    - derive/^([zcs])([^h])/$1h$2/",
            "    - derive/([aeiou])ng$/$1n/",
            "    - derive/([aeiou])n$/$1ng/",
            "    - derive/^n([aeiou])/l$1/",
            "    - derive/^l([aeiou])/n$1/",
        ]
    return _write_if_changed(SCHEMA_PATCH, "\n".join(lines) + "\n")


# 双拼方案：官方 rime-double-pinyin 仓库 schema（已下载到用户目录），
# 挂上 shangkey 词库 + 默认简体输出 + 翻页键。Ctrl+` / F4 打开方案菜单切换。
DOUBLE_PINYIN_SCHEMAS = ("double_pinyin", "double_pinyin_flypy")

_SWITCHES_BLOCK = """  switches:
    - name: ascii_mode
      state: 0
    - name: full_shape
      state: 0
    - name: simplification
      state: 1
    - name: ascii_punct
      state: 0
"""


def ensure_double_pinyin():
    """为双拼方案写 custom 补丁并登记进 default.custom.yaml 的 schema_list。

    首次调用会创建 {schema}.custom.yaml（shangkey 词库 + 简体 + 翻页键）；
    default.custom.yaml 的 schema_list 里追加两个双拼方案。
    返回 True 表示部署配置有变化（需重新部署）。
    """
    changed = False
    for sid in DOUBLE_PINYIN_SCHEMAS:
        schema_file = USER_DIR / f"{sid}.schema.yaml"
        if not schema_file.exists():
            print(f"[importer] 跳过 {sid}：schema 文件不存在（{schema_file}）")
            continue
        text = (
            f"# ShangKey {sid} 补丁（importer 自动管理，请勿手工编辑）\n"
            "patch:\n"
            f"  translator/dictionary: {DICT_NAME}\n"
            + _SWITCHES_BLOCK +
            "  key_binder/bindings/+:  # 编码中的翻页键\n"
            "    - {when: composing, accept: comma, send: Page_Up}\n"
            "    - {when: composing, accept: period, send: Page_Down}\n"
            "    - {when: composing, accept: minus, send: Page_Up}\n"
            "    - {when: composing, accept: equal, send: Page_Down}\n"
        )
        changed |= _write_if_changed(USER_DIR / f"{sid}.custom.yaml", text)

    # default.custom.yaml：schema_list 覆盖写（本文件也是托管内容）
    entries = ["  schema_list:", "    - schema: luna_pinyin_simp"]
    for sid in DOUBLE_PINYIN_SCHEMAS:
        if (USER_DIR / f"{sid}.schema.yaml").exists():
            entries.append(f"    - schema: {sid}")
    text = (
        "# ShangKey 用户补丁：合并在共享 default.yaml 之上\n"
        "# （用补丁而非整体替换，保留 key_binder / ascii_composer 等被 schema 引用的段落）\n"
        "patch:\n" + "\n".join(entries) + "\n"
    )
    changed |= _write_if_changed(USER_DIR / "default.custom.yaml", text)
    return changed


def _write_if_changed(path, text):
    """内容不同才写（避免无谓触发重新部署）。返回是否写入。"""
    path = Path(path)
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except (FileNotFoundError, OSError):
        pass
    path.write_text(text, encoding="utf-8")
    return True


# ---------------------------------------------------------------- IME 进程管理

def ime_running():
    """探测 IME 是否在运行（与 main.py 共用同一个命名互斥体）。

    注意：探测后必须 CloseHandle，否则自己会占住互斥体，
    导致之后 IME 主程序误判“已在运行”而拒绝启动。
    """
    k32 = ctypes.windll.kernel32
    h = k32.CreateMutexW(None, False, MUTEX_NAME)
    if not h:
        return False
    err = ctypes.GetLastError()
    k32.CloseHandle(h)
    return err == 183  # ERROR_ALREADY_EXISTS


def stop_ime(timeout=6.0):
    """发 Ctrl+Alt+Q 让 IME 自行退出，等待互斥体释放。"""
    import keyboard
    keyboard.send("ctrl+alt+q")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not ime_running():
            return True
        time.sleep(0.3)
    return False


# ---------------------------------------------------------------- 部署与验证

def redeploy():
    """独立加载 rime 完成部署（initialize 内部等待部署结束）。"""
    r = make_rime()
    t0 = time.time()
    r.initialize(on_notification=notification_cb)
    elapsed = time.time() - t0
    return r, elapsed


def recent_dropped_entries(seconds=120):
    """扫描部署日志里被丢弃的条目（拼音音节不在 prism 音节表中）。"""
    logs = PROJECT_ROOT / "runtime" / "logs"
    if not logs.is_dir():
        return []
    cutoff = time.time() - seconds
    hits = []
    for f in sorted(logs.glob("*.log"), key=lambda p: p.stat().st_mtime,
                    reverse=True)[:3]:
        if f.stat().st_mtime < cutoff:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for ln in text.splitlines():
            if "dropping entry" in ln:
                hits.append(ln.strip())
    return hits


def spot_check(r, samples, limit=3):
    """抽查：模拟输入导入词的拼音，看候选里是否出现该词。"""
    results = []
    for word, pinyin, _ in samples[:limit]:
        sid = r.create_session()
        try:
            r.simulate_key_sequence(pinyin.replace(" ", ""), sid)
            ctx = r.get_context(sid)
            cands = [c.text for c in (ctx.candidates if ctx else [])]
            results.append((word, word in cands))
        finally:
            r.destroy_session(sid)
    return results


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="python -m rimeime.importer",
        description="导入词库（txt/csv/RIME dict）到 ShangKey 输入法")
    ap.add_argument("files", nargs="*", help="词库文件路径")
    ap.add_argument("--dry-run", action="store_true",
                    help="只解析和统计，不写盘不部署")
    ap.add_argument("--no-deploy", action="store_true",
                    help="写入词库文件和补丁，但不重新部署")
    ap.add_argument("--no-stop-ime", action="store_true",
                    help="IME 在运行时只提示，不自动退出")
    args = ap.parse_args(argv)

    if not args.files:
        ap.print_help()
        return 2

    # 1) 解析所有文件
    new_entries = []
    for f in args.files:
        p = Path(f)
        if not p.exists():
            print(f"[跳过] 文件不存在: {f}")
            continue
        if p.suffix.lower() == ".scel":
            try:
                info, entries = parse_scel(p)
            except ValueError as ex:
                print(f"[跳过] {ex}")
                continue
            print(f"[解析] {p.name}: {len(entries)} 条有效"
                  + (f"（词库：{info}）" if info else ""))
            new_entries.extend(entries)
            continue
        try:
            text = read_text(p)
            entries = (parse_rime_dict(text)
                       if re.search(r"^---\s*$", text, re.M) and
                       re.search(r"^\.\.\.\s*$", text, re.M)
                       else parse_text(text))
        except ValueError as ex:
            print(f"[跳过] {ex}")
            continue
        valid = []
        for word, pinyin, weight in entries:
            np = normalize_pinyin(pinyin, word)
            if word and np and all(SYLLABLE.match(t) for t in np.split()):
                valid.append((word, np, weight))
        print(f"[解析] {p.name}: {len(valid)} 条有效"
              + (f"（跳过 {len(entries) - len(valid)} 条无效行）"
                 if len(entries) != len(valid) else ""))
        new_entries.extend(valid)

    if not new_entries:
        print("没有可导入的有效词条。")
        return 1

    # 2) 合并
    old = load_managed_entries()
    added, updated = merge_entries(old, new_entries)
    print(f"[合并] 新增 {added} 条，更新 {updated} 条，"
          f"词库总计 {len(old)} 条")

    if args.dry_run:
        print("[dry-run] 未写入任何文件。")
        return 0

    # 3) 写盘
    write_managed_dict(old)
    patched = ensure_schema_patch()
    print(f"[写入] {MANAGED_DICT}")
    if patched:
        print(f"[写入] {SCHEMA_PATCH}（挂载 translator/dictionary: {DICT_NAME}）")

    if args.no_deploy:
        print("[提示] 已写盘但未部署；重启 IME 时生效。")
        return 0

    # 4) 部署（需要 IME 退出以释放 LevelDB 锁）
    if ime_running():
        if args.no_stop_ime:
            print("[提示] IME 正在运行且未自动退出；词库已写盘，"
                  "重启 IME 后生效。")
            return 0
        print("[提示] IME 正在运行，发送 Ctrl+Alt+Q 退出以便重新部署...")
        if not stop_ime():
            print("[错误] IME 未能退出，无法安全部署。"
                  "请手动退出后重跑：--no-stop-ime（写盘已生效，重启即可）")
            return 1

    print("[部署] 正在重建词库...")
    try:
        r, elapsed = redeploy()
    except Exception as ex:
        print(f"[错误] 部署失败: {ex}")
        print("        IME 可能仍在运行（LevelDB 锁冲突），请退出后重试。")
        return 1

    dropped = recent_dropped_entries()
    if dropped:
        print(f"[警告] {len(dropped)} 条因拼音音节无效被丢弃，例如:")
        for ln in dropped[:5]:
            print(f"        {ln}")

    print(f"[部署] 完成，耗时 {elapsed:.1f}s")
    samples = [(w, p, wt) for (w, p), wt in
               sorted(old.items(), key=lambda kv: -kv[1])][:50]
    ok = spot_check(r, samples)
    r.finalize()
    for word, found in ok:
        print(f"  [抽查] {word}: {'✓ 出现在候选' if found else '✗ 未出现（可能排名靠后）'}")

    print("\n完成。启动 IME 即可使用：python -m rimeime.main")
    return 0


if __name__ == "__main__":
    sys.exit(main())
