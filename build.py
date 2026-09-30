"""PyInstaller 打包 + Inno Setup 生成安装包。

产物：
- dist/KouboPPT/                   目录版程序（启动快；安装包装的就是这个文件夹）
- dist/KouboPPT_Setup_v<版本>.exe  安装包（双击安装：开始菜单/桌面快捷方式 + 卸载）

用法: .venv/Scripts/python.exe build.py

注意：`--collect-all tkinterdnd2` 是必须的——拖拽导入依赖它自带的 tkdnd
二进制（win-x64/libtkdnd*.dll）与配套 .tcl 脚本，缺了打包后拖拽会静默失效。
"""
import atexit
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
ASSETS = ROOT / "build_assets"
APP_NAME = "KouboPPT"
LOCK_FILE = ROOT / ".build.lock"        # 单实例锁：并发构建会互相删除 build/ 与 dist/
_LOCK_STALE_SECONDS = 1800              # 上次异常退出留下的锁超过 30 分钟视为陈旧、可接管
ISCC_CANDS = (r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
              r"C:\Program Files\Inno Setup 6\ISCC.exe",
              str(Path.home() / r"AppData\Local\Programs\Inno Setup 6\ISCC.exe"))


def version() -> str:
    text = (ROOT / "kouboppt" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    return m.group(1) if m else "0.0.0"


def find_iscc() -> str | None:
    for p in ISCC_CANDS:
        if Path(p).exists():
            return p
    return shutil.which("iscc")


def _lock_is_stale() -> bool:
    try:
        return time.time() - LOCK_FILE.stat().st_mtime > _LOCK_STALE_SECONDS
    except OSError:
        return True


def _release_lock() -> None:
    LOCK_FILE.unlink(missing_ok=True)


def _acquire_lock() -> bool:
    """单实例锁：两个 build.py 同时跑会互相删掉对方的 build/ 与 dist/，必须挡住。

    用 O_EXCL 原子创建，所以同时启动的多个实例里只有一个能拿到锁；
    陈旧锁（上次异常退出留下、超过 30 分钟）会被接管。
    """
    for attempt in (0, 1):
        try:
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if attempt or not _lock_is_stale():
                return False
            LOCK_FILE.unlink(missing_ok=True)
            continue
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    return False


def main():
    if not _acquire_lock():
        print("已有另一个 build.py 在运行，本次退出（避免两者互相删除 build/ 与 dist/）。")
        return
    atexit.register(_release_lock)
    import imageio_ffmpeg
    ASSETS.mkdir(exist_ok=True)
    # 以固定名字 ffmpeg.exe 打进包里，运行时按这个路径找
    ffmpeg_exe = ASSETS / "ffmpeg.exe"
    shutil.copyfile(imageio_ffmpeg.get_ffmpeg_exe(), ffmpeg_exe)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onedir", "--windowed",
        "--name", APP_NAME,
        "--icon", str(ASSETS / "icon.ico"),
        "--collect-all", "customtkinter",
        "--collect-all", "edge_tts",
        "--collect-all", "tkinterdnd2",
        "--collect-data", "matplotlib",
        "--collect-data", "docx",
        "--collect-submodules", "pymupdf",
        "--add-binary", f"{ffmpeg_exe};.",
        "--add-data", f"{ASSETS / 'icon.ico'};.",
        "--add-data", f"{ASSETS / 'icon_ui.png'};.",
        str(ROOT / "run.py"),
    ]
    print(">>", " ".join(cmd))
    subprocess.run(cmd, cwd=ROOT, check=True)

    app_dir = ROOT / "dist" / APP_NAME
    if not (app_dir / f"{APP_NAME}.exe").exists():
        sys.exit(f"打包失败：未找到 {app_dir / (APP_NAME + '.exe')}")
    print(f"\n目录版完成：{app_dir}")

    iscc = find_iscc()
    if not iscc:
        print("未找到 Inno Setup（ISCC.exe），跳过安装包；装好后重跑本脚本即可生成。")
        return
    ver = version()
    subprocess.run([iscc, f"/DAppVersion={ver}", str(ROOT / "installer.iss")],
                   cwd=ROOT, check=True)
    setup = ROOT / "dist" / f"{APP_NAME}_Setup_v{ver}.exe"
    if setup.exists():
        print(f"\n安装包完成：{setup}  ({setup.stat().st_size / 1024 / 1024:.1f} MB)")
    else:
        sys.exit("安装包编译失败：未找到输出文件")


if __name__ == "__main__":
    main()
