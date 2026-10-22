"""命令行验证：拼音 -> 候选 -> 选词上屏。

用法：
    python -m rimeime.repl nihao
    python -m rimeime.repl --status
"""

import os
import sys
import time
from pathlib import Path

from .api import Rime
from . import keys

from .paths import PROJECT_ROOT
DEFAULT_DLL = r"C:\Program Files\Rime\weasel-0.17.4\rime.dll"
DEFAULT_SHARED = r"C:\Program Files\Rime\weasel-0.17.4\data"


def notification_cb(context, session_id, msg_type, msg_value):
    """部署进度回调（rime 在后台线程调用）。"""
    t = (msg_type or b"").decode("utf-8", "replace")
    v = (msg_value or b"").decode("utf-8", "replace")
    if t in ("deploy", "schema"):
        print(f"  [rime] {t}: {v}")


def make_rime(log=True):
    dll = os.environ.get("RIME_DLL", DEFAULT_DLL)
    shared = os.environ.get("RIME_SHARED_DATA", DEFAULT_SHARED)
    user = os.environ.get("RIME_USER_DATA", str(PROJECT_ROOT / "runtime" / "rime_user"))
    log_dir = PROJECT_ROOT / "runtime" / "logs"
    Path(user).mkdir(parents=True, exist_ok=True)
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    r = Rime(dll, shared, user,
             log_dir=str(log_dir),
             min_log_level=2)
    return r


def main(argv):
    if "--status" in argv:
        r = make_rime()
        t0 = time.time()
        r.initialize(on_notification=notification_cb)
        print(f"initialized in {time.time()-t0:.1f}s, version={r.version()}")
        sid = r.create_session()
        print("schemas:", r.get_schema_list())
        print("status:", r.get_status(sid))
        r.finalize()
        return 0

    seq = argv[1] if len(argv) > 1 and not argv[1].startswith("-") else "nihao"
    r = make_rime()
    t0 = time.time()
    r.initialize(on_notification=notification_cb)
    print(f"initialized in {time.time()-t0:.1f}s, version={r.version()}")

    sid = r.create_session()
    print("status:", r.get_status(sid))

    print(f"\nsimulate_key_sequence({seq!r})")
    r.simulate_key_sequence(seq, sid)
    ctx = r.get_context(sid)
    if ctx is None:
        print("no context!")
        r.finalize()
        return 1
    print(f"preedit: {ctx.preedit!r}  caret={ctx.caret_pos}")
    print(f"page {ctx.page_no}, size {ctx.page_size}, last={ctx.is_last_page}, "
          f"highlighted={ctx.highlighted}")
    for c in ctx.candidates:
        mark = "*" if c.index == ctx.highlighted else " "
        print(f"  {mark} {c.index + 1}. {c.text}" + (f"  ({c.comment})" if c.comment else ""))

    if ctx.candidates:
        idx = ctx.highlighted if ctx.highlighted >= 0 else 0
        print(f"\nselect_candidate({idx}) -> {ctx.candidates[idx].text}")
        r.select_candidate(idx, sid)
        commit = r.get_commit(sid)
        print(f"commit: {commit!r}")
        ctx2 = r.get_context(sid)
        print(f"context after commit: preedit={ctx2.preedit!r}")

    # 演示中英切换挂接点
    print("\n--- ascii_mode toggle demo ---")
    r.set_ascii_mode(True, sid)
    r.simulate_key_sequence("abc", sid)
    commit = r.get_commit(sid)
    print(f"ascii_mode=True, typed 'abc' -> commit: {commit!r}")
    r.set_ascii_mode(False, sid)
    print("ascii_mode restored to False")

    r.finalize()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
