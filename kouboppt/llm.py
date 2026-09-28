"""OpenAI 兼容大模型客户端（文本 + 多模态视觉）。

配置 base_url / api_key / model 三项即可对接任意兼容厂商
（DeepSeek、通义、Kimi、智谱、OpenRouter、本地 Ollama 等）。
"""
from __future__ import annotations

import base64
import json
import mimetypes
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

_thread_local = threading.local()


def _session():
    """每线程一个 Session：复用 TCP/TLS 连接（每次请求省一次握手），并发时互不共享。"""
    import requests
    from requests.adapters import HTTPAdapter

    sess = getattr(_thread_local, "session", None)
    if sess is None:
        sess = requests.Session()
        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=16)
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
        _thread_local.session = sess
    return sess


class LLMError(RuntimeError):
    raw_text: str = ""      # 解析失败时附上模型的原始回复，便于抢救/留档排查


@dataclass
class LLMConfig:
    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = ""
    model: str = "deepseek-chat"
    temperature: float = 0.3
    timeout: int = 300
    max_retries: int = 3


def _b64_image(path: Path) -> str:
    """图片 → data URL。按后缀给 MIME，章节识别用的 JPEG 缩图比 PNG 小一个数量级。"""
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(Path(path).read_bytes()).decode()}"


def _friendly_http_error(status: int, text: str) -> str:
    body = (text or "").strip()
    looks_html = body[:200].lstrip().lower().startswith(("<!doctype", "<html"))
    if status == 524 or (status in (502, 503, 504) and looks_html):
        return (f"HTTP {status}：接口网关超时（服务器 100 秒内没有返回）——通常是这次请求太重"
                "或模型响应太慢。可稍后重试、换更快的模型/线路；若反复出现，教材章节识别"
                "可改用「按每节页数」或手工编写 章节结构.json")
    if looks_html:
        return f"HTTP {status}：服务器返回的是网页而不是接口数据，请检查接口地址是否填写正确"
    return f"HTTP {status}: {body[:300]}"


_DUMP_LOCK = threading.Lock()


def dump_raw(raw_dir: Path | None, tag: str, text: str) -> None:
    """把模型的原始回复留档到 ``AI原始回复.log``，排查"JSON 坏了 / 被截断"用。

    逐页识别、按小节并发生成时会同时写，所以加锁；任何写入失败都静默忽略，
    留档只是辅助手段，绝不能影响主流程。
    """
    if raw_dir is None or not text:
        return
    try:
        d = Path(raw_dir)
        d.mkdir(parents=True, exist_ok=True)
        with _DUMP_LOCK:
            with (d / "AI原始回复.log").open("a", encoding="utf-8") as f:
                f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} {tag} =====\n{text}\n")
    except OSError:
        pass


class LLMClient:
    def __init__(self, cfg: LLMConfig, log=print):
        self.cfg = cfg
        self.log = log

    # ------------------------------------------------------------------
    def _post(self, messages: list, max_tokens: int, retries: int | None = None,
              raw_dir: Path | None = None, tag: str = "") -> str:
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        payload = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature,
            "max_tokens": max_tokens,
        }
        tries = self.cfg.max_retries if retries is None else max(1, int(retries))
        last_err: Exception | None = None
        for attempt in range(1, tries + 1):
            try:
                resp = _session().post(url, headers=headers, json=payload,
                                       timeout=self.cfg.timeout)
                if resp.status_code != 200:
                    raise LLMError(_friendly_http_error(resp.status_code, resp.text))
                data = resp.json()
                choice = data["choices"][0]
                content = choice["message"]["content"] or ""
                if choice.get("finish_reason") == "length":
                    # 接口/模型把回复截断了，JSON 大概率是坏的；后续会走抢救路径
                    self.log("  ⚠ 模型回复撞到输出长度上限（finish_reason=length），可能被截断")
                    dump_raw(raw_dir, f"{tag or '回复'}（长度截断）", content)
                return content
            except Exception as exc:              # noqa: BLE001
                last_err = exc
                if attempt < tries:
                    wait = 3 * attempt
                    self.log(f"  AI 请求失败({exc})，{wait}s 后第 {attempt + 1} 次重试…")
                    time.sleep(wait)
        raise LLMError(f"AI 请求最终失败：{last_err}")

    # ------------------------------------------------------------------
    def chat(self, system: str, user: str, images: list[Path] | None = None,
             max_tokens: int = 8192, retries: int | None = None,
             raw_dir: Path | None = None, tag: str = "") -> str:
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        if images:
            content = [{"type": "text", "text": user}]
            for img in images:
                content.append({"type": "image_url",
                                "image_url": {"url": _b64_image(img)}})
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": "user", "content": user})
        return self._post(messages, max_tokens, retries, raw_dir, tag)

    def chat_json(self, system: str, user: str, images: list[Path] | None = None,
                  max_tokens: int = 8192, retries: int | None = None,
                  raw_dir: Path | None = None, tag: str = ""):
        """要求模型输出 JSON；容忍 ```json 围栏和前后杂文字，解析失败会请模型修一次。"""
        text = self.chat(system + "\n只输出 JSON，不要任何其他文字。", user, images,
                         max_tokens, retries, raw_dir, tag)
        try:
            return extract_json(text)
        except LLMError as exc:
            self.log(f"  ⚠ 模型输出不是合法 JSON（{str(exc)[:100]}），请它修正后重发一次…")
            fix_prompt = (f"下面这段文本本该是 JSON，但无法解析（{str(exc)[:120]}）。"
                          "请修正为合法 JSON 后原样输出：内容不要改动，"
                          "字符串里的反斜杠要写成两个（\\\\）。\n\n" + text[:6000])
            fixed = self.chat("你是 JSON 修复助手，只输出修正后的 JSON。", fix_prompt,
                              None, max_tokens, 1, raw_dir, f"{tag}（自修）")
            try:
                return extract_json(fixed)
            except LLMError as exc2:
                exc2.raw_text = max((t for t in (text, fixed) if t), key=len, default="")
                raise


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
# 要么整体吃掉一个合法转义（\n、\\、\uXXXX…），要么把非法转义的单个反斜杠补成两个。
# 必须整体消费合法转义，否则 "\\frac" 里的第二个反斜杠会被误判成非法转义。
_JSON_ESC = re.compile(r'\\(u[0-9a-fA-F]{4}|["\\/bfnrt])|\\(.)', re.S)


def _repair_escape(m: re.Match) -> str:
    return m.group(0) if m.group(1) else "\\\\" + m.group(2)


def _loose_load(text: str):
    """宽容解析：模型常把 LaTeX 反斜杠直接写进 JSON 字符串（\\(x\\)、\\frac），
    而 \\( 不是合法转义。先把非法转义补一个反斜杠，再允许字符串内裸换行。"""
    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError:
        pass
    return json.loads(_JSON_ESC.sub(_repair_escape, text), strict=False)


def extract_json(text: str):
    text = text.strip()
    m = _FENCE.search(text)
    if m:
        text = m.group(1).strip()
    try:
        return _loose_load(text)
    except json.JSONDecodeError:
        pass
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        raise LLMError(f"模型没有返回可解析的 JSON：{text[:200]}")
    s = min(starts)
    end = max(text.rfind("}"), text.rfind("]"))
    try:
        return _loose_load(text[s:end + 1])
    except json.JSONDecodeError as exc:
        raise LLMError(f"JSON 解析失败：{exc}；片段：{text[s:s+200]}") from None


def _scan_objects(text: str, pos: int):
    """从数组的 [ 之后，逐个扫出括号配平的 {...} 片段（跳过字符串里的括号）。"""
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i in range(pos, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            if depth:
                depth -= 1
                if depth == 0:
                    yield text[start:i + 1]
        elif c == "]" and depth == 0:
            return


def salvage_objects(text: str, array_key: str) -> list[dict]:
    """从写坏/被截断的 JSON 里抢救 array_key 数组里完整的对象。

    截断只会毁掉最后一个对象，前面的都还完好——能救多少是多少。
    """
    m = re.search(r'"' + re.escape(array_key) + r'"\s*:\s*\[', text or "")
    if not m:
        return []
    out: list[dict] = []
    for chunk in _scan_objects(text, m.end()):
        try:
            obj = _loose_load(chunk)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out
