"""开机自启管理：HKCU\\...\\Run 注册表值（无需管理员权限）。

    python -m rimeime.autostart on|off|status

开发模式：注册命令为 <pythonw.exe> launch.pyw（无控制台窗口）；
打包模式（sys.frozen）：注册命令为 exe 自身。
"""

import sys
from pathlib import Path

try:
    import winreg
except ImportError:  # pragma: no cover - 仅 Windows
    winreg = None

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "ShangKeyIME"


def launch_command():
    """写进 Run 键的启动命令（自动决定 pythonw/exe 形态）。"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    project = Path(__file__).resolve().parent.parent
    launcher = project / "launch.pyw"
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    py = pythonw if pythonw.exists() else Path(sys.executable)
    return f'"{py}" "{launcher}"'


def is_enabled():
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_READ) as k:
            winreg.QueryValueEx(k, VALUE_NAME)
        return True
    except OSError:
        return False


def enable():
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, VALUE_NAME, 0, winreg.REG_SZ,
                              launch_command())
        return True
    except OSError as ex:
        print(f"[autostart] 写注册表失败: {ex}")
        return False


def disable():
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, VALUE_NAME)
        return True
    except FileNotFoundError:
        return True
    except OSError as ex:
        print(f"[autostart] 删注册表失败: {ex}")
        return False


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else "status"
    if cmd == "on":
        print("开机自启已开启" if enable() else "开机自启开启失败")
        return 0
    if cmd == "off":
        print("开机自启已关闭" if disable() else "开机自启关闭失败")
        return 0
    print(f"开机自启: {'开启' if is_enabled() else '关闭'}\n命令: {launch_command()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
