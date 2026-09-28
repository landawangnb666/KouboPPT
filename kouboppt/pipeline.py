"""流水线编排：渲染幻灯片 → 提取文稿 → TTS → 合成视频。

进度模型：渲染每页 1 个单位，每个输出片段 3 个单位（TTS + 编码）。
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import ppt_render, ppt_text, video
from .tts import get_provider


class Cancelled(Exception):
    pass


def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise Cancelled()


@dataclass
class Settings:
    text_source: str = "正文"                      # 正文 / 备注 / 备注优先
    tts_provider: str = "Edge TTS（内置·免费）"
    tts_voice: str = "zh-CN-XiaoxiaoNeural"
    tts_rate: str = "+0%"
    target_height: int = 1080                      # 导出图片的高（宽按比例）
    fps: int = 10                                  # 画面是静止幻灯片：实测 10fps 比 30fps 快约 5.9 倍，画质(SSIM)更高、文件更小
    crf: int = 22
    preset: str = "veryfast"
    codec: str = "libx264"                         # 由 GUI 按首启自检结论解析好再传进来
    min_slide_seconds: float = 3.0                 # 无文字页的停留时长
    tail_pad_seconds: float = 0.4                  # 每页话音后的停顿
    workers: int = 4                               # 同时处理的页数（1 = 完全串行）


def parse_ranges(spec: str, total: int) -> list[tuple[int, int]] | None:
    """'1-18, 19-36, 5' → [(1,18),(19,36),(5,5)]；空串 → None 表示整个 PPT。"""
    spec = (spec or "").strip()
    if not spec:
        return None
    out: list[tuple[int, int]] = []
    for part in re.split(r"[,，;；\s]+", spec):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)\s*[-~—至]\s*(\d+)", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
        elif re.fullmatch(r"\d+", part):
            a = b = int(part)
        else:
            raise ValueError(f"页码格式不认识：{part}（示例：1-18,19-36）")
        if a > b:
            a, b = b, a
        if a < 1 or b > total:
            raise ValueError(f"页码超出范围：{part}（本文档共 {total} 页）")
        out.append((a, b))
    if not out:
        raise ValueError("页码范围没有解析出有效内容")
    return out


def output_name(stem: str, rng: tuple[int, int] | None) -> str:
    if rng is None:
        return f"{stem}.mp4"
    a, b = rng
    if a == b:
        return f"{stem}_p{a}.mp4"
    return f"{stem}_p{a}-{b}.mp4"


def compute_export_size(w_emu: int, h_emu: int, target_h: int = 1080,
                        max_w: int = 1920) -> tuple[int, int]:
    """按幻灯片宽高比算导出像素尺寸（偶数，视频编码友好）。"""
    if w_emu <= 0 or h_emu <= 0:
        return 1920, 1080
    aspect = w_emu / h_emu
    h = target_h
    w = round(h * aspect)
    if w > max_w:
        w = max_w
        h = round(w / aspect)
    return max(w - w % 2, 2), max(h - h % 2, 2)


@dataclass
class Pipeline:
    settings: Settings
    log: object = print                          # log(str)
    progress: object = None                      # progress(fraction 0~1, msg)

    _done_units: float = field(default=0.0, init=False)
    _total_units: float = field(default=0.0, init=False)
    _lock: object = field(default=None, init=False, repr=False)

    def __post_init__(self):
        self._lock = threading.Lock()

    def _tick(self, units: float, msg: str):
        with self._lock:
            self._done_units += units
            frac = self._done_units / self._total_units if self._total_units else 0
        if self.progress:
            self.progress(min(frac, 1.0), msg)

    def _plan_units(self, render_pages: int, encode_pages: int):
        self._done_units = 0.0
        self._total_units = render_pages + encode_pages * 3

    # ------------------------------------------------------------------
    def process_file(self, ppt_path: Path, ranges_spec: str, out_dir: Path,
                     cancel=None, out_base: str | None = None) -> list[Path]:
        """处理一个 PPT（含页码范围拆分），返回生成的视频路径列表。

        out_base 指定输出视频的文件名（不含扩展名，默认沿用 PPT 文件名）：
        教材→课程按"课时标题+视频"命名输出。
        """
        ppt_path = Path(ppt_path)
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = out_base or ppt_path.stem
        workdir = out_dir / f".work_{ppt_path.stem}"
        cache_dir = out_dir / ".tts_cache"
        workdir.mkdir(parents=True, exist_ok=True)
        cache_dir.mkdir(parents=True, exist_ok=True)

        try:
            # 老 .ppt 先转 .pptx
            if ppt_path.suffix.lower() == ".ppt":
                self.log("检测到老版 .ppt，先转换为 .pptx …")
                converted = workdir / "_converted.pptx"
                ppt_render.convert_to_pptx(ppt_path, converted, workdir)
                ppt_path = converted

            prs = ppt_text.load(ppt_path)
            total = ppt_text.slide_count(prs)
            w_emu, h_emu = ppt_text.slide_size_emu(prs)
            width, height = compute_export_size(w_emu, h_emu, self.settings.target_height)
            self.log(f"共 {total} 页，导出分辨率 {width}x{height}")

            ranges = parse_ranges(ranges_spec, total)
            encode_pages = sum(b - a + 1 for a, b in (ranges or [(1, total)]))
            self._plan_units(total, encode_pages)

            # 1) 渲染全部幻灯片（多个范围共用一次渲染）
            slides_dir = workdir / "slides"
            try:
                engine = ppt_render.export_slides(
                    ppt_path, slides_dir, width, height, 1, total,
                    on_slide=lambda i: self._tick(1, f"渲染幻灯片 {i}/{total}"),
                    workdir=workdir,
                    should_cancel=lambda: cancel is not None and cancel.is_set(),
                )
            except ppt_render.Cancelled:
                raise Cancelled() from None
            self.log(f"幻灯片渲染完成（引擎：{engine}）")

            # 2) 提取文稿 + 准备 TTS
            texts = ppt_text.extract_all(prs, self.settings.text_source)
            provider = get_provider(self.settings.tts_provider)

            # 3) 逐范围生成视频
            outputs: list[Path] = []
            for rng in (ranges if ranges else [None]):
                _check_cancel(cancel)
                a, b = rng if rng else (1, total)
                out_path = out_dir / output_name(stem, rng)
                self.log(f"生成 {out_path.name}（第 {a}-{b} 页）…")
                self._encode_range(slides_dir, texts, a, b, width, height,
                                   cache_dir, provider, workdir, out_path, total, cancel)
                outputs.append(out_path)
                self.log(f"✔ 已生成：{out_path.name}")
            return outputs
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    # ------------------------------------------------------------------
    def _encode_range(self, slides_dir: Path, texts: list[str], a: int, b: int,
                      width: int, height: int, cache_dir: Path, provider,
                      workdir: Path, out_path: Path, total: int, cancel) -> None:
        s = self.settings
        pages = list(range(a, b + 1))
        segs: dict[int, Path] = {}

        def one(page: int, codec: str) -> None:
            _check_cancel(cancel)
            image = slides_dir / f"slide_{page:04d}.png"
            if not image.exists():
                raise RuntimeError(f"幻灯片图片缺失：{image.name}")
            text = ppt_text.clean_for_tts(texts[page - 1]) if page <= len(texts) else ""

            audio: Path | None = None
            if text:
                audio = self._synth_cached(provider, text, cache_dir)
                dur = video.audio_duration(audio) + s.tail_pad_seconds
            else:
                self.log(f"  第 {page} 页没有可朗读文字，停留 {s.min_slide_seconds:g} 秒")
                dur = s.min_slide_seconds

            seg = workdir / f"seg_{page:04d}.mp4"
            video.make_segment(image, audio, dur, seg, width, height,
                               fps=s.fps, crf=s.crf, preset=s.preset, codec=codec)
            segs[page] = seg
            self._tick(3, f"合成第 {page}/{total} 页视频")

        def encode_all(codec: str, n: int) -> None:
            if n <= 1:
                for page in pages:
                    one(page, codec)
                return
            with ThreadPoolExecutor(n) as pool:
                futs = [pool.submit(one, page, codec) for page in pages]
                try:
                    for f in futs:
                        f.result()
                except BaseException:
                    for f in futs:
                        f.cancel()
                    raise

        # 页级并发：网络（TTS）与 ffmpeg 子进程都不吃 GIL；编码并发别超过核数一半，免得越并越慢
        n = max(1, min(int(s.workers or 1),
                       max(2, (os.cpu_count() or 4) // 2),
                       len(pages)))
        codec = video.resolve_codec(s.codec)
        video.announce(codec, self.log)
        try:
            encode_all(codec, n)
        except video.EncoderFallback as exc:
            # 硬编在真实片段上失败：整节改回软编重做。不能只补失败那几页——
            # concat 用 -c copy，混两种编码器的片段会拼出坏文件。配音走 .tts_cache 不重复合成。
            video.disable_hardware()
            self.log("  ⚠ 显卡硬编失败，本节改回 CPU 软编重做"
                     f"（配音已缓存，不重复合成）：{str(exc).splitlines()[0][:120]}")
            segs.clear()
            encode_all(video.SW_CODEC, n)
        video.concat_segments([segs[p] for p in sorted(segs)], out_path)

    # ------------------------------------------------------------------
    def _synth_cached(self, provider, text: str, cache_dir: Path) -> Path:
        key = hashlib.sha1(
            f"{provider.name}|{self.settings.tts_voice}|{self.settings.tts_rate}|{text}".encode()
        ).hexdigest()[:24]
        mp3 = cache_dir / f"{key}.mp3"
        if mp3.exists() and mp3.stat().st_size > 0:
            return mp3
        tmp = cache_dir / f"{key}.{os.getpid()}_{threading.get_ident()}.part.mp3"
        last_exc: BaseException | None = None
        for attempt in (1, 2):                        # 并发下 TTS 偶发失败，自动重试一次
            try:
                provider.synthesize(text, self.settings.tts_voice, self.settings.tts_rate, tmp)
                tmp.replace(mp3)
                return mp3
            except BaseException as exc:
                last_exc = exc
                tmp.unlink(missing_ok=True)
                if attempt == 1:
                    self.log(f"  ⚠ 配音失败，重试 1 次（{exc}）")
        raise last_exc
