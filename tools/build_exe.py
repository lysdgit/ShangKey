"""构建发布包：PyInstaller 打包 ShangKeyIME.exe + 随包 runtime 配置 + 发布 zip。

用法：
    python tools/build_exe.py            # 构建（exe + runtime/ + zip）
    dist/ShangKeyIME.exe --version       # 验证
产物：
    dist/ShangKeyIME.exe                    # 单文件 exe（无控制台窗口）
    dist/runtime/                           # 词库配置/规则/外观/托盘图标/浏览器扩展
    dist/ShangKeyIME-v<版本>-win64.zip      # 发布包（exe + runtime/ + README + 启动说明）
说明：
    exe 依赖本机已安装 Weasel（rime.dll 与共享方案数据来自其安装目录）；
    运行时数据（用户库/学习库）在 exe 旁 runtime/ 下自动生成。
    GitHub Actions（.github/workflows/release.yml）打 tag 时跑同一脚本，产物直接挂 Release。
"""

import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
ENTRY = Path(__file__).with_name("ime_entry.py")
DIST = ROOT / "dist"
RUNTIME = ROOT / "runtime"

# 这两个 yaml 含本机 installation_id / 上次构建时间，不能进发布包
SKIP_RIME_YAML = {"installation.yaml", "user.yaml"}

START_TXT = """ShangKey 输入法 v{ver}（Windows x64 原型）
========================================
1. 先安装小狼毫 Weasel 0.17.4（https://rime.im/download/），本程序借用它的 rime.dll；
2. 双击 ShangKeyIME.exe，托盘出现蓝色「中」图标即启动成功；
3. Ctrl+Alt+K 开关输入法，Shift 单击切中英，Ctrl+Alt+Q 退出；
4. 语言图标（中/英）按住左键可拖到任意位置，托盘菜单可一键复位；
5. Chrome/Edge 里候选窗跟随光标，需加载 runtime/browser-extension 扩展（开发者模式加载已解压的扩展）；
6. 详细说明见 README.md，问题反馈见 https://github.com/lysdgit/ShangKey/issues
"""


def read_version():
    """从 rimeime/__init__.py 读 __version__（不 import，避免拖进运行时依赖）。"""
    txt = (ROOT / "rimeime" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', txt)
    return m.group(1) if m else "0.0.0"


def reset_dir(dst):
    """清空目录（不存在则创建）。

    shutil.rmtree 在受限环境（沙箱/杀软）可能被拦，此时退化为逐个删除；
    删不掉的旧文件也不影响正确性——后续白名单覆盖拷贝，zip 另用全新 staging。
    """
    if dst.exists():
        try:
            shutil.rmtree(dst)
        except OSError as ex:
            print(f"[warn] 清理 {dst} 失败（{ex}），改为逐个删除")
            for p in sorted(dst.rglob("*"), key=lambda x: len(x.parts), reverse=True):
                try:
                    p.unlink() if p.is_file() else p.rmdir()
                except OSError:
                    pass
    dst.mkdir(parents=True, exist_ok=True)


def stage_runtime(dst):
    """按白名单把 runtime/ 配置拷到 dst（不含用户库/部署缓存/本机 ID）。"""
    reset_dir(dst)
    (dst / "rime_user").mkdir(parents=True, exist_ok=True)
    for f in (RUNTIME / "rime_user").glob("*.yaml"):
        if f.name in SKIP_RIME_YAML:
            continue
        shutil.copy2(f, dst / "rime_user" / f.name)
    for name in ("switch_rules.json", "ui.json", "shangkey.ico"):
        src = RUNTIME / name
        if src.exists():
            shutil.copy2(src, dst / name)
    ext_src = RUNTIME / "browser-extension"
    if ext_src.is_dir():
        shutil.copytree(ext_src, dst / "browser-extension")
    return dst


def make_zip(ver):
    """组装发布包：解压即用的扁平结构，打成 dist/ShangKeyIME-v<ver>-win64.zip。

    runtime 直接用源配置重新 staging（全新目录），不依赖 dist/runtime，
    避免旧运行数据（installation.yaml / user.yaml / userdb）混进发布包。
    """
    stage = DIST / "_stage"
    reset_dir(stage)
    shutil.copy2(DIST / "ShangKeyIME.exe", stage / "ShangKeyIME.exe")
    stage_runtime(stage / "runtime")
    readme = ROOT / "README.md"
    if readme.exists():
        shutil.copy2(readme, stage / "README.md")
    (stage / "启动说明.txt").write_text(START_TXT.format(ver=ver), encoding="utf-8")

    zip_path = DIST / f"ShangKeyIME-v{ver}-win64.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(stage.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(stage).as_posix())
    reset_dir(stage)
    return zip_path


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

    ver = read_version()
    rt_dst = stage_runtime(DIST / "runtime")
    zip_path = make_zip(ver)

    exe = DIST / "ShangKeyIME.exe"
    print("\n构建完成:")
    print(f"  版本   v{ver}")
    print(f"  exe    {exe}  ({exe.stat().st_size / 1048576:.2f} MB)")
    print(f"  runtime {rt_dst}")
    print(f"  发布包 {zip_path}  ({zip_path.stat().st_size / 1048576:.2f} MB)")


if __name__ == "__main__":
    main()
