"""视频合成：每页 = 幻灯片图片 + 口播音频 → 一段 mp4，最后无损拼接。

编码器：默认 CPU 软编（libx264）。显卡硬编（NVENC/Quick Sync/AMF）是否更快因机而异，
所以靠 self_check() 实测：真编一段测试片段计时，比"问驱动支不支持"可靠。
硬编在真实片段上万一失败，抛 EncoderFallback，调用方须整节改回软编重做——
因为 concat_segments 用 -c copy 无损拼接，混用两种编码器的片段会拼出坏文件。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .subproc import NO_WINDOW


def _ffmpeg_exe() -> str:
    if getattr(sys, "frozen", False):  # PyInstaller 打包后：exe 旁边/解包目录里带了 ffmpeg.exe
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass and (Path(meipass) / "ffmpeg.exe").exists():
            return str(Path(meipass) / "ffmpeg.exe")
        cand = Path(sys.executable).parent / "ffmpeg.exe"
        if cand.exists():
            return str(cand)
        raise RuntimeError("打包环境中缺少 ffmpeg.exe")
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


FFMPEG = _ffmpeg_exe()

# ------------------------------------------------------------------ 编码器
SW_CODEC = "libx264"
HW_CODECS = ("h264_nvenc", "h264_qsv", "h264_amf")
ENCODER_LABELS = {
    SW_CODEC: "CPU 软件编码",
    "h264_nvenc": "NVIDIA 显卡硬编（NVENC）",
    "h264_qsv": "Intel 核显硬编（Quick Sync）",
    "h264_amf": "AMD 显卡硬编（AMF）",
}


class EncoderFallback(RuntimeError):
    """硬编运行期失败：调用方应把整节改回软编重做。"""


_hw_disabled = False        # 真实片段上硬编失败过 → 本进程内一律软编
_selfcheck: dict | None = None
_announced: set[str] = set()


def is_hw(codec: str) -> bool:
    return codec in HW_CODECS


def codec_pix_fmt(codec: str) -> str:
    return "nv12" if codec in ("h264_qsv", "h264_amf") else "yuv420p"


def codec_args(codec: str, crf: int = 22, preset: str = "veryfast") -> list[str]:
    """各编码器的画质参数。硬编没有 -crf/-tune，用各自的等效质量选项。"""
    if codec == "h264_nvenc":
        return ["-c:v", codec, "-rc", "vbr", "-cq", str(crf + 2), "-b:v", "0"]
    if codec == "h264_qsv":
        return ["-c:v", codec, "-preset", preset, "-global_quality", str(crf)]
    if codec == "h264_amf":
        return ["-c:v", codec, "-quality", "balanced", "-rc", "cqp",
                "-qp_i", str(crf), "-qp_p", str(crf + 2)]
    return ["-c:v", SW_CODEC, "-preset", preset, "-tune", "stillimage", "-crf", str(crf)]


def set_selfcheck(result: dict | None) -> None:
    global _selfcheck
    _selfcheck = result


def selfcheck_result() -> dict | None:
    return _selfcheck


def disable_hardware() -> None:
    global _hw_disabled
    _hw_disabled = True


def resolve_codec(mode: str = "auto") -> str:
    """界面选项（auto/hw/sw）或具体编码器名 → 实际使用的编码器。"""
    if mode in ENCODER_LABELS:                       # 已经是具体编码器名，直接用
        codec = mode
    elif mode == "sw":
        codec = SW_CODEC
    elif mode == "hw":
        got = (_selfcheck or {}).get("hw_available") or []
        codec = got[0] if got else SW_CODEC
    else:                                            # auto：谁快用谁，没自检过就软编
        codec = (_selfcheck or {}).get("recommended") or SW_CODEC
    if _hw_disabled and is_hw(codec):
        codec = SW_CODEC
    return codec


def announce(codec: str, log) -> None:
    """每种编码器在日志里只报一次，避免一本书十几节刷同样的行。"""
    if codec not in _announced:
        _announced.add(codec)
        log(f"视频编码：{ENCODER_LABELS.get(codec, codec)}")


# ------------------------------------------------------------------ 自检
def _probe_image(path: Path) -> None:
    """生成一张有文字细节的 1080p 静帧。不能用纯色帧——那压缩得太快，测不出差别。"""
    from PIL import Image, ImageDraw
    w, h = 1920, 1080
    im = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, w, 110], fill=(31, 78, 158))
    d.rectangle([80, 900, 900, 990], outline=(120, 120, 120), width=3)
    line = "x_(k+1) = x_k - f(x_k)/f'(x_k)   0123456789 abcdefg 定义与性质"
    for i in range(26):
        d.text((80, 150 + i * 28), f"{i + 1}. {line}", fill=(20, 20, 20))
    im.save(path)


def _first_err(stderr: str) -> str:
    for ln in (stderr or "").splitlines():
        if ln.strip():
            return ln.strip()[:160]
    return "ffmpeg 未给出错误信息"


def _run_probe(codec: str, img: Path, seconds: float, fps: int, timeout: float,
               tmp: Path) -> tuple[bool, float, str]:
    """真编一段测试片段，返回 (成功, 耗时秒, 错误摘要)。"""
    out = tmp / f"probe_{codec}.mp4"
    out.unlink(missing_ok=True)
    fmt = codec_pix_fmt(codec)
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
           "-loop", "1", "-framerate", str(fps), "-i", str(img),
           "-t", f"{seconds:.3f}",
           "-vf", f"scale=1920:1080,format={fmt}",
           *codec_args(codec), "-pix_fmt", fmt, "-an", str(out)]
    t0 = time.perf_counter()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                           timeout=timeout, **NO_WINDOW)
    except subprocess.TimeoutExpired:
        return False, timeout, f"超过 {timeout:g} 秒没返回（驱动无响应？）"
    dt = time.perf_counter() - t0
    if p.returncode == 0 and out.exists() and out.stat().st_size > 0:
        return True, dt, ""
    return False, dt, _first_err(p.stderr)


def self_check(seconds: float = 30.0, fps: int = 10, timeout: float = 15.0,
               deadline: float = 45.0, progress=None, cancel=None) -> dict:
    """首次启动的本机自检：软编与各家硬编各编同一段测试片段，比谁快。

    测试片段必须够长（默认 30 秒）：硬编每次起 ffmpeg 都要付约 0.7 秒的硬件会话
    初始化开销，片段太短会把硬编冤枉死，而真实的每页口播是 60~120 秒。
    timeout/deadline 是"驱动卡死"的兜底，不是速度门槛：30 秒画面编 15 秒以上
    （慢于 2 倍实时）的编码器本来也没法用。cancel() 返回 True 时提前收工。
    """
    tmp = Path(tempfile.mkdtemp(prefix="kouboppt_enc_"))
    res: dict = {"seconds": seconds, "fps": fps, "timings": {}, "errors": {},
                 "hw_available": [], "recommended": SW_CODEC, "elapsed": 0.0,
                 "cancelled": False}
    t_start = time.perf_counter()
    try:
        img = tmp / "slide.png"
        _probe_image(img)
        _run_probe(SW_CODEC, img, 0.3, fps, timeout, tmp)   # 预热：让 ffmpeg 进系统缓存
        for codec in (SW_CODEC, *HW_CODECS):
            if cancel and cancel():
                res["cancelled"] = True
                res["errors"][codec] = "用户跳过了自检"
                continue
            if time.perf_counter() - t_start > deadline:
                res["errors"][codec] = "自检超过总时限，已跳过"
                continue
            if progress:
                progress(f"正在测试 {ENCODER_LABELS[codec]} …")
            ok, dt, err = _run_probe(codec, img, seconds, fps, timeout, tmp)
            if ok:
                res["timings"][codec] = round(dt, 3)
                if is_hw(codec):
                    res["hw_available"].append(codec)
            else:
                res["errors"][codec] = err
        if res["timings"]:
            res["recommended"] = min(res["timings"], key=lambda k: res["timings"][k])
        res["elapsed"] = round(time.perf_counter() - t_start, 2)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return res


def selfcheck_summary(res: dict | None) -> str:
    """把自检结果写成人话，给界面状态行用。"""
    if not res or res.get("skipped") or (res.get("cancelled") and not res.get("timings")):
        return "未自检：当前用 CPU 软件编码（点「重新自检」可测本机哪种更快）"
    t = res.get("timings") or {}
    if not t:
        return "自检没测出可用编码器，回退 CPU 软件编码"
    secs = res.get("seconds", 30)
    parts = [f"{ENCODER_LABELS.get(k, k)} {v:.2f}s" for k, v in t.items()]
    rec = ENCODER_LABELS.get(res.get("recommended") or SW_CODEC, "")
    hw = [k for k in t if is_hw(k)]
    if not hw:
        return ("本机没有可用的显卡硬编，使用 CPU 软件编码"
                f"（{secs:g} 秒测试片段耗时 {t.get(SW_CODEC, 0):.2f}s）")
    return (f"本机自检（{secs:g} 秒测试片段）：" + " / ".join(parts) + f" → 推荐 {rec}")


def audio_duration(path: Path) -> float:
    """读取音频时长（秒）：先试 mutagen（纯本地、毫秒级），失败再退到 ffmpeg 探测。"""
    try:
        from mutagen.mp3 import MP3
        return float(MP3(str(path)).info.length)
    except Exception:
        pass
    try:
        proc = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)],
                              capture_output=True, text=True, errors="replace",
                              **NO_WINDOW)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr)
        if m:
            h, mi, s = int(m[1]), int(m[2]), float(m[3])
            return h * 3600 + mi * 60 + s
    except Exception:
        pass
    raise RuntimeError(f"无法读取音频时长：{path}")


def make_segment(image: Path, audio: Path | None, duration: float, out_path: Path,
                 width: int, height: int, fps: int = 10, crf: int = 22,
                 preset: str = "veryfast", codec: str = SW_CODEC) -> None:
    """一页幻灯片 → 一段视频。无音频时用静音，长度 = duration。"""
    codec = SW_CODEC if (_hw_disabled and is_hw(codec)) else (codec or SW_CODEC)
    fmt = codec_pix_fmt(codec)
    vf = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
          f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=white,setsar=1,format={fmt}")
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
           "-loop", "1", "-framerate", str(fps), "-i", str(image)]
    filters = [f"[0:v]{vf}[v]"]
    if audio is not None:
        cmd += ["-i", str(audio)]
        filters.append("[1:a]apad[a]")
        amap = "[a]"
    else:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
        amap = "1:a"
    cmd += ["-filter_complex", ";".join(filters),
            "-map", "[v]", "-map", amap,
            "-t", f"{duration:.3f}",
            *codec_args(codec, crf, preset), "-pix_fmt", fmt,
            "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
            str(out_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                          **NO_WINDOW)
    if proc.returncode != 0 or not out_path.exists():
        msg = (f"生成视频片段失败（第 {out_path.stem} 段，编码器 {codec}）："
               f"\n{proc.stderr[-800:]}")
        if is_hw(codec):
            raise EncoderFallback(msg)
        raise RuntimeError(msg)


def concat_segments(segments: list[Path], out_path: Path) -> None:
    """无损拼接所有片段（编码参数一致，-c copy 秒拼）。"""
    if not segments:
        raise RuntimeError("没有任何视频片段")
    if len(segments) == 1:
        shutil.copyfile(segments[0], out_path)
        return
    list_file = out_path.with_name(out_path.stem + "_concat.txt")
    list_file.write_text(
        "".join("file '{}'\n".format(p.resolve().as_posix()) for p in segments),
        encoding="utf-8")
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
           "-f", "concat", "-safe", "0", "-i", str(list_file),
           "-c", "copy", "-movflags", "+faststart", str(out_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                          **NO_WINDOW)
    list_file.unlink(missing_ok=True)
    if proc.returncode != 0 or not out_path.exists():
        raise RuntimeError(f"拼接视频失败：\n{proc.stderr[-800:]}")
