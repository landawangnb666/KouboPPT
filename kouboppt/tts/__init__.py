from .base import TTSError, TTSProvider, chunk_text, get_provider, provider_names, register
from . import edge_tts_provider  # noqa: F401  import 即完成内置供应商注册

__all__ = [
    "TTSError",
    "TTSProvider",
    "chunk_text",
    "get_provider",
    "provider_names",
    "register",
]
