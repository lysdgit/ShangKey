"""无控制台启动入口（开机自启 / 托盘启动用）。

pythonw launch.pyw  ->  启动 ShangKey 输入法（pythonw 下 print 需兜底，
由 rimeime.main 内部处理 sys.stdout 为 None 的情况）。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rimeime.main import main

if __name__ == "__main__":
    main()
