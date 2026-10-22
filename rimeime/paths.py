"""项目根目录定位（全模块共用）。

开发模式：rimeime 包的上级目录（含 runtime/）。
PyInstaller 打包后：__file__ 在解包临时目录里，必须改用 exe 所在目录
——runtime/ 配置（词库、规则、UI 配置）放在 exe 旁边随包分发。
"""

import sys
from pathlib import Path

if getattr(sys, "frozen", False):  # PyInstaller 打包
    PROJECT_ROOT = Path(sys.executable).resolve().parent
else:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
