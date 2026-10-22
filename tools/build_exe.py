"""构建发布包：PyInstaller 打包 ShangKeyIME.exe + 随包 runtime 配置。

用法：
    python tools/build_exe.py            # 构建
    dist/ShangKeyIME.exe --version       # 验证
产物：
    dist/ShangKeyIME.exe                 # 单文件 exe（无控制台窗口）
    dist/runtime/                        # 词库配置/规则/外观/托盘图标/浏览器扩展
说明：
    exe 依赖本机已安装 Weasel（rime.dll 与共享方案数据来自其安装目录）；
    运行时数据（用户库/学习库）在 exe 旁 runtime/ 下自动生成。
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
ENTRY = Path(__file__).with_name("ime_entry.py")
DIST = ROOT / "dist"
RUNTIME = ROOT / "runtime"


def main():
    subprocess.check_call([
        PY, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile", "--noconsole",
        "--name", "ShangKeyIME",
        "--icon", str(RUNTIME / "shangkey.ico"),
        "--paths", str(ROOT),
        str(ENTRY),
    ], cwd=ROOT)

    # runtime 配置随包分发（不含用户库/部署缓存，首次运行自动生成）
    rt_dst = DIST / "runtime"
    if rt_dst.exists():
        shutil.rmtree(rt_dst)
    (rt_dst / "rime_user").mkdir(parents=True)
    for f in (RUNTIME / "rime_user").glob("*.yaml"):
        shutil.copy2(f, rt_dst / "rime_user" / f.name)
    for name in ("switch_rules.json", "ui.json", "shangkey.ico"):
        src = RUNTIME / name
        if src.exists():
            shutil.copy2(src, rt_dst / name)
    ext_src = RUNTIME / "browser-extension"
    if ext_src.is_dir():
        shutil.copytree(ext_src, rt_dst / "browser-extension")

    print("\n构建完成:")
    print(f"  {DIST / 'ShangKeyIME.exe'}")
    print(f"  {rt_dst}")


if __name__ == "__main__":
    main()
