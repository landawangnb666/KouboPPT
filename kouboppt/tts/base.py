"""TTS 供应商抽象层。

后续接入新的 TTS（Azure、本地模型、第三方 API 等）只需要：
1. 写一个 TTSProvider 子类，实现 synthesize()
2. 在本包 __init__.py 里 import 触发 @register 注册

GUI 和流水线完全不感知具体实现。
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from pathlib import Path


class TTSError(RuntimeError):
    """TTS 合成失败。"""


class TTSProvider(ABC):
    #: 显示名称（GUI 供应商下拉框用的）
    name: str = "base"

    @abstractmethod
    def list_voices(self) -> list[str]:
        """返回推荐音色列表（用户也可以手输其他音色名）。"""

    @abstractmethod
    def synthesize(self, text: str, voice: str, rate: str, out_path: Path) -> None:
        """把 text 合成语音写入 out_path（mp3）。rate 形如 '+0%'、'-10%'。"""

    def validate(self) -> str | None:
        """合成前的自检，返回错误信息或 None。"""
        return None


_registry: dict[str, type[TTSProvider]] = {}


def register(cls: type[TTSProvider]) -> type[TTSProvider]:
    _registry[cls.name] = cls
    return cls


def provider_names() -> list[str]:
    return list(_registry)


def get_provider(name: str) -> TTSProvider:
    try:
        return _registry[name]()
    except KeyError:
        raise TTSError(f"未知的 TTS 供应商：{name}（可用：{provider_names()}）") from None


def chunk_text(text: str, limit: int = 1500) -> list[str]:
    """按句子边界把长文本切块，避免单次合成请求过长。"""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    parts = re.split(r"(?<=[。！？；!?;])", text)
    chunks: list[str] = []
    buf = ""
    for part in parts:
        if not part:
            continue
        if buf and len(buf) + len(part) > limit:
            chunks.append(buf)
            buf = part
        else:
            buf += part
    if buf:
        chunks.append(buf)
    final: list[str] = []
    for c in chunks:  # 无标点超长的兜底硬切
        while len(c) > limit:
            final.append(c[:limit])
            c = c[limit:]
        if c:
            final.append(c)
    return final
