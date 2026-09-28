"""把幻灯片导出为 PNG：通过子进程调用 PowerShell 驱动 PowerPoint/WPS 的 COM 接口。

好处：
- 不依赖 pywin32/comtypes，打包简单、不会污染主进程
- COM 假死可以超时强杀（PowerPoint 与 WPS 演示都支持同一套 COM 接口）
"""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

from .subproc import NO_WINDOW


class RenderError(RuntimeError):
    pass


class Cancelled(Exception):
    """用户取消（渲染中途）。"""


_PS = r"""
param(
    [string]$PptPath = "",
    [string]$OutDir = "",
    [string]$SaveAsPath = "",
    [int]$Width = 1920,
    [int]$Height = 1080,
    [int]$First = 1,
    [int]$Last = 0,
    [int]$DoExport = 1
)
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$script:Engine = ""
function New-PpApp {
    foreach ($progid in @('PowerPoint.Application', 'KWPP.Application')) {
        try {
            $app = New-Object -ComObject $progid
            $script:Engine = $progid
            return $app        # 注意：函数内不能再 Write-Output，否则返回值会被污染
        } catch { }
    }
    throw "本机没有检测到 Microsoft PowerPoint 或 WPS 演示，无法渲染幻灯片，请先安装其中之一。"
}

function Open-Presentation($app, $path) {
    # ReadOnly=-1 Untitled=0 WithWindow=-1
    # WPS/部分版本在无窗口(0)打开时 Slide.Export 会报 E_FAIL，因此带窗口打开（窗口会在完成后自动关闭）
    return $app.Presentations.Open($path, -1, 0, -1)
}

$app = New-PpApp
Write-Output ("APP {0}" -f $script:Engine)
try {
    $pres = Open-Presentation $app $PptPath
    try {
        if ($SaveAsPath -ne "") {
            $pres.SaveAs($SaveAsPath, 24)   # ppSaveAsOpenXMLPresentation
            Write-Output "SAVED"
        }
        if ($DoExport -eq 1) {
            if ($Last -le 0) { $Last = $pres.Slides.Count }
            for ($i = $First; $i -le $Last; $i++) {
                $out = Join-Path $OutDir ("slide_{0:d4}.png" -f $i)
                $pres.Slides.Item($i).Export($out, "PNG", $Width, $Height)
                Write-Output ("OK {0}" -f $i)
                [Console]::Out.Flush()
            }
        }
    } finally {
        $pres.Close()
    }
} finally {
    try { $app.Quit() } catch {}
    try { [System.Runtime.InteropServices.Marshal]::ReleaseComObject($app) | Out-Null } catch {}
}
Write-Output "DONE"
"""


def _run_ps(ps1: Path, args: list[str], on_slide, idle_timeout: float,
            should_cancel=None) -> tuple[list[int], str, str]:
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
           "-File", str(ps1)] + args
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", **NO_WINDOW)

    done: list[int] = []
    engine: str = ""
    tail: list[str] = []
    state = {"last": time.time(), "reason": ""}

    def watchdog():
        while proc.poll() is None:
            try:
                if should_cancel and should_cancel():
                    state["reason"] = "cancelled"
                    proc.kill()
                    return
            except Exception:
                pass
            if time.time() - state["last"] > idle_timeout:
                state["reason"] = "hung"
                proc.kill()
                return
            time.sleep(0.5)

    def read_err():
        for line in proc.stderr:
            tail.append(line)

    threading.Thread(target=watchdog, daemon=True).start()
    threading.Thread(target=read_err, daemon=True).start()

    for line in proc.stdout:
        state["last"] = time.time()
        line = line.strip()
        if not line:
            continue
        if line.startswith("OK "):
            try:
                page = int(line.split()[1])
            except ValueError:
                continue
            done.append(page)
            if on_slide:
                on_slide(page)
        elif line.startswith("APP "):
            engine = line[4:].strip()
        elif line in ("SAVED", "DONE"):
            pass
        else:
            tail.append(line + "\n")

    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
    if state["reason"] == "cancelled":
        raise Cancelled("用户取消")
    if state["reason"] == "hung":
        raise RenderError(
            f"渲染幻灯片卡住了（{int(idle_timeout)} 秒没有响应），已中止。\n"
            "如果屏幕上还有 PowerPoint/WPS 窗口卡住，请手动关闭后重试。")
    return done, engine, "".join(tail)


def _prepare(workdir: Path) -> Path:
    workdir.mkdir(parents=True, exist_ok=True)
    ps1 = workdir / "_render.ps1"
    ps1.write_text(_PS, encoding="utf-8-sig")  # 带 BOM，PowerShell 5.1 才按 UTF-8 解析
    return ps1


def convert_to_pptx(ppt_path: Path, save_as: Path, workdir: Path) -> None:
    """把老版 .ppt 转成 .pptx（不做导出）。"""
    ps1 = _prepare(workdir)
    args = ["-PptPath", str(Path(ppt_path).resolve()), "-OutDir", str(Path(workdir).resolve()),
            "-SaveAsPath", str(Path(save_as).resolve()), "-DoExport", "0"]
    _, _, tail = _run_ps(ps1, args, None, idle_timeout=120)
    if not save_as.exists():
        raise RenderError(f"转换 .ppt 失败：\n{tail[-800:]}")


def export_slides(ppt_path: Path, out_dir: Path, width: int, height: int,
                  first: int, last: int, on_slide=None,
                  workdir: Path | None = None,
                  idle_timeout: float = 120.0,
                  should_cancel=None) -> str:
    """导出 [first, last] 页为 PNG。返回使用的渲染引擎（PowerPoint/WPS）。"""
    out_dir.mkdir(parents=True, exist_ok=True)  # 目标目录必须已存在，否则 WPS 报 E_FAIL
    ps1 = _prepare(workdir or out_dir)
    args = ["-PptPath", str(Path(ppt_path).resolve()), "-OutDir", str(Path(out_dir).resolve()),
            "-Width", str(width), "-Height", str(height),
            "-First", str(first), "-Last", str(last)]
    done, engine, tail = _run_ps(ps1, args, on_slide, idle_timeout, should_cancel)

    missing = [i for i in range(first, last + 1) if i not in done]
    if missing:
        raise RenderError(
            f"有 {len(missing)} 页幻灯片导出失败（第 {missing[:10]} 页...）。\n"
            f"详细信息：{tail[-800:] or 'PowerPoint 未返回错误信息'}")
    return engine or "COM"
