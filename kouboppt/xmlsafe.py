"""把文本清洗成 OpenXML（docx / pptx）可安全写入的形式。

python-docx / python-pptx 底层是 lxml，一旦写入含 XML 1.0 非法字符的文本就会直接抛：

    ValueError: All strings must be XML compatible: Unicode or ASCII,
                no NULL bytes or control characters

模型输出（以及 OCR 稿）偶尔会在题干、选项、公式里混进这类字符——它们肉眼不可见，
却会让整个文档生成失败、整轮任务中止。所以**所有写进文档的文本都先过这里**。

XML 1.0 合法的字符是 ``\\t``(09)、``\\n``(0A)、``\\r``(0D) 以及 >=0x20 的码位；
本模块剔除其余 C0 控制字符、XML 非字符（U+FFFE/U+FFFF）和落单的代理码位
（lone surrogate，会让 lxml 编码时报同样的错）。
"""
from __future__ import annotations

import re

# 注意：这是普通字符串（非 raw），\x00 / \ud800 等都按转义解析
_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def clean(text: str) -> str:
    """剔除 XML 非法字符（保留 \\t \\n \\r）；空值原样返回。"""
    if not text or not isinstance(text, str):
        return text
    return _ILLEGAL.sub("", text)


def illegal(text: str) -> list[str]:
    """列出文本里出现的 XML 非法字符码位（去重、升序），给日志定位用。"""
    if not text or not isinstance(text, str):
        return []
    return [f"U+{ord(c):04X}" for c in sorted({c for c in _ILLEGAL.findall(text)})]
