"""内置 Edge TTS 供应商（免费、微软神经网络语音、需联网）。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import edge_tts

from .base import TTSError, TTSProvider, chunk_text, register

# 推荐音色（用户也可在界面里手输任意 Edge 官方音色名）
POPULAR_VOICES = [
    "zh-CN-XiaoxiaoNeural",          # 晓晓 女声·自然
    "zh-CN-YunxiNeural",             # 云希 男声·阳光
    "zh-CN-YunyangNeural",           # 云扬 男声·播音
    "zh-CN-YunjianNeural",           # 云健 男声·浑厚
    "zh-CN-XiaoyiNeural",            # 晓伊 女声·温柔
    "zh-CN-liaoning-XiaobeiNeural",  # 晓贝 东北话
    "zh-CN-shaanxi-XiaoniNeural",    # 晓妮 陕西话
    "zh-TW-HsiaoChenNeural",         # 曉臻 台湾腔
    "zh-HK-HiuMaanNeural",           # 曉曼 粤语
    "en-US-AriaNeural",
    "en-US-GuyNeural",
]


@register
class EdgeTTSProvider(TTSProvider):
    name = "Edge TTS（内置·免费）"

    def list_voices(self) -> list[str]:
        return list(POPULAR_VOICES)

    def synthesize(self, text: str, voice: str, rate: str, out_path: Path) -> None:
        try:
            asyncio.run(self._synth(text, voice, rate, out_path))
        except Exception as exc:  # 网络/配额等统一包装
            raise TTSError(f"Edge TTS 合成失败：{exc}（请检查网络连接）") from exc

    async def _synth(self, text: str, voice: str, rate: str, out_path: Path) -> None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        parts: list[Path] = []
        for i, chunk in enumerate(chunk_text(text)):
            tmp = out_path.with_name(f"{out_path.stem}.part{i}.mp3")
            await edge_tts.Communicate(chunk, voice=voice, rate=rate).save(str(tmp))
            parts.append(tmp)
        if not parts:
            raise TTSError("没有可合成的文本")
        if len(parts) == 1:
            parts[0].replace(out_path)
        else:
            with open(out_path, "wb") as w:  # mp3 帧可直接字节拼接
                for p in parts:
                    w.write(p.read_bytes())
            for p in parts:
                p.unlink(missing_ok=True)
