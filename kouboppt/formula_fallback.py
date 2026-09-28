"""公式兜底：本地渲不出来时的两级救援（都离线，只有层①要调一次 AI）。

① AI 改写重渲：请文本模型"只改写法、不改语义"地把这条公式改写成 matplotlib mathtext
   能吃的形式，再本地重渲——不少"渲染不了"其实是模型自己把 LaTeX 写坏了（用了 mathtext
   不支持的环境/命令），改写法就能救回来。
② 扫图裁图：拿教材原页图（叠了百分比坐标网格）让视觉模型定位这条公式，按 300 DPI 裁下来
   直接用——书上印着的就是最忠实的版本，多冷门的记号都不怕。

两级都失败才返回 None，由上层（slidegen）降级成可读文字。
结果按"行文本 + 颜色 + 是否独立公式"哈希落盘（公式兜底.json + 图片），**成功与失败都记**，
所以重跑不会为同一条公式反复花钱。

扩展点：联网渲染服务（把 LaTeX 发给第三方换一张 PNG）接起来很容易——实现一个
``render_remote(latex, out_path, endpoint) -> Path | None``，在 ``_rescue`` 里插一层即可；
届时按 LLM 的做法在配置里加一个"公式渲染服务地址"（可指向内网自建服务），默认留空不启用。
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

from . import formula

CACHE_VERSION = "fb1"        # 兜底规则升级后旧缓存自动失效
CACHE_FILE = "公式兜底.json"
CROP_DPI = 300               # 裁图与公式图同尺度，slidegen 的图片缩放不用换算
LOCATE_PER_CALL = 4          # 一次视觉请求发几页
LOCATE_MAX_PAGES = 8         # 一节课最多看几页（再往后不划算，放弃裁图）
GRID = 10                    # 页面网格等分数（10 → 刻度 10/20/…/90）


# ---------------------------------------------------------------- 对外
def make_fallback(llm, doc, page_range: tuple[int, int], cache_dir: Path,
                  page_dir: Path, log=print):
    """造一个给 BuiltinEngine 用的兜底回调：``(text, color, display) -> Path | None``。

    page_range：该节课的页码范围（1-based，裁图只在这些页里找）
    page_dir ：教材页面图目录（textbook.render_page 的产物 `page_XXXX.jpg`）
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    page_dir = Path(page_dir)
    state = _load(cache_dir)

    def fallback(text: str, color: str = "#1a1a1a", display: bool = False) -> Path | None:
        key = _key(text, color, display)
        rec = state.get(key)
        if rec is not None:                     # 记过账：成功的给图，失败的直接放弃（别再花钱）
            if rec.get("kind") in ("latex", "crop"):
                img = cache_dir / str(rec.get("image", ""))
                if img.exists() and img.stat().st_size > 0:
                    return img
            return None
        img = _rescue(llm, doc, page_range, text, color, display, cache_dir, page_dir, log)
        state[key] = ({"kind": "none"} if img is None else
                      {"kind": "latex" if img.name.startswith("fb_") else "crop",
                       "image": img.name})
        _save(cache_dir, state)
        return img

    return fallback


# ---------------------------------------------------------------- 两级救援
def _rescue(llm, doc, page_range, text, color, display, cache_dir, page_dir, log):
    img = _rewrite_and_render(llm, text, color, display, cache_dir, log)
    if img is not None:
        return img
    img = _crop_from_page(llm, doc, page_range, text, cache_dir, page_dir, log)
    if img is None:
        log(f"    ⚠ 公式兜底也救不回来（将降级为可读文字）：{text[:60]}")
    return img


_REWRITE_SYS = ("你是 LaTeX 修正助手。把用户给的公式改写成 matplotlib mathtext 能渲染的写法："
                "数学含义、符号、上下标、数值一个都不能变；中文字面原样保留；不写任何解释。")


def _rewrite_and_render(llm, text, color, display, cache_dir, log) -> Path | None:
    """层①：AI 改写 LaTeX → 本地重渲。"""
    core = text.strip().strip("$").strip()
    out = cache_dir / f"fb_{_key(core, color, display)[:16]}.png"
    if out.exists() and out.stat().st_size > 0:
        return out                              # 改写结果自己也缓存，省一次请求
    try:
        data = llm.chat_json(
            _REWRITE_SYS,
            "这条公式本地渲染失败了：\n" + core + "\n\n"
            "请只改写法（mathtext 不支持的环境/命令换成等价写法），数学含义保持不变。\n"
            '输出 JSON：{"latex":"改写后的公式，不要 $ 定界符","note":"改了什么"}',
            max_tokens=1000, retries=1, tag="公式改写")
    except Exception as exc:                    # noqa: BLE001 兜底失败不能往上抛
        log(f"    ⚠ 公式改写请求失败（{exc}），改试裁书页原图")
        return None
    new = str((data or {}).get("latex", "")).strip().strip("$").strip()
    if not new or new == core:
        return None
    got = formula.render(new, out, fontsize=20, color=color, display=display)
    if got is None:
        log(f"    ⚠ AI 改写后本地仍渲不出：{new[:60]}")
        return None
    log(f"    ↳ 公式兜底：AI 改写后重渲成功「{core[:40]}」")
    return got


_LOCATE_SYS = ("你是版面定位助手。用户会给几张教材书页照片，每张都叠了百分比坐标网格"
               "（竖线 10/20/…/90，横线同理，刻度数字标在网格线旁）。"
               "请找出指定公式出现在哪张图上，并给出它的矩形范围（百分比）。")


def _crop_from_page(llm, doc, page_range, text, cache_dir, page_dir, log) -> Path | None:
    """层②：在教材原页图上定位这条公式并裁图。"""
    first, last = int(page_range[0]), int(page_range[1])
    pages = list(range(first, last + 1))
    if not pages:
        return None
    if len(pages) > LOCATE_MAX_PAGES:
        log(f"    · 该节课有 {len(pages)} 页，只在前 {LOCATE_MAX_PAGES} 页里找公式原图")
        pages = pages[:LOCATE_MAX_PAGES]
    target = text.strip().strip("$").strip()
    for i in range(0, len(pages), LOCATE_PER_CALL):
        group = pages[i:i + LOCATE_PER_CALL]
        picked = _locate(llm, group, target, cache_dir, page_dir)
        if picked is None:
            continue
        page_no, box = picked
        img = _crop(doc, page_no, box, cache_dir)
        if img is not None:
            log(f"    ↳ 公式兜底：从第 {page_no} 页裁到原图「{target[:40]}」")
            return img
    return None


def _locate(llm, pages, target, cache_dir, page_dir):
    """让视觉模型在这几页里找公式，返回 (页码, [左,上,右,下] 百分比) 或 None。"""
    imgs = []
    for p in pages:
        raw = page_dir / f"page_{p:04d}.jpg"
        if not (raw.exists() and raw.stat().st_size):
            return None                         # 页图缺失（没走过识别？）就别勉强裁了
        imgs.append(_gridded(raw, cache_dir / f"grid_{p:04d}.jpg"))
    try:
        data = llm.chat_json(
            _LOCATE_SYS,
            f"要找的公式（LaTeX）：\n{target}\n\n"
            f"上面 {len(imgs)} 张图依次是第 {'、'.join(str(p) for p in pages)} 页。\n"
            "请给出它所在那张图的序号（从 1 开始；都没找到就填 0），以及矩形范围"
            "（百分比 0~100，原点在左上角）。\n"
            '输出 JSON：{"index":1,"box":[左,上,右,下],"note":"看到的内容（便于核对）"}',
            images=imgs, max_tokens=600, retries=1, tag="公式定位")
        idx = int((data or {}).get("index", 0))
        box = [float(v) for v in ((data or {}).get("box") or [])][:4]
    except Exception:                           # noqa: BLE001 定位失败就换下一组
        return None
    if not (1 <= idx <= len(pages)) or len(box) != 4 or not _box_ok(box):
        return None
    return pages[idx - 1], box


def _box_ok(box) -> bool:
    """框必须落在页内且大小合理：太小像没找准，太大说明把整页圈进去了。"""
    x0, y0, x1, y1 = box
    if not (0 <= x0 < x1 <= 100 and 0 <= y0 < y1 <= 100):
        return False
    return 3 <= (x1 - x0) <= 90 and 1 <= (y1 - y0) <= 60


def _gridded(page_jpg: Path, out: Path, max_w: int = 1000) -> Path:
    """给页面图叠百分比网格与刻度：视觉模型报坐标的准确度会明显好一截。"""
    from PIL import Image, ImageDraw

    if out.exists() and out.stat().st_size > 0:
        return out
    with Image.open(page_jpg) as im:
        im = im.convert("RGB")
        if im.width > max_w:
            im = im.resize((max_w, max(1, int(im.height * max_w / im.width))))
        d = ImageDraw.Draw(im)
        for i in range(1, GRID):
            x, y = im.width * i // GRID, im.height * i // GRID
            d.line((x, 0, x, im.height), fill=(220, 30, 30), width=1)
            d.line((0, y, im.width, y), fill=(220, 30, 30), width=1)
            d.text((x + 2, 2), str(i * 10), fill=(220, 30, 30))
            d.text((2, y + 2), str(i * 10), fill=(220, 30, 30))
        im.save(out, quality=85)
    return out


def _crop(doc, page_no: int, box, cache_dir: Path) -> Path | None:
    """按百分比框重渲原页（300 DPI）并裁下来，再削掉四周白边。"""
    from PIL import Image

    try:
        pix = doc[page_no - 1].get_pixmap(dpi=CROP_DPI)
        im = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    except Exception:                           # noqa: BLE001
        return None
    width, height = im.size
    x0, y0, x1, y1 = box
    pad_x, pad_y = int(width * 0.012) + 4, int(height * 0.006) + 4
    left = max(0, int(width * x0 / 100) - pad_x)
    top = max(0, int(height * y0 / 100) - pad_y)
    right = min(width, int(width * x1 / 100) + pad_x)
    bottom = min(height, int(height * y1 / 100) + pad_y)
    if right - left < width * 0.02 or bottom - top < height * 0.004:
        return None                             # 框退化（模型给错坐标）
    crop = _trim_white(im.crop((left, top, right, bottom)))
    out = cache_dir / ("crop_" + hashlib.sha1(
        f"{CACHE_VERSION}|{page_no}|{box}".encode()).hexdigest()[:16] + ".png")
    crop.save(out)
    return out


def _trim_white(img, margin: int = 6):
    """削掉四周白边：模型给的框通常偏松，紧一下才像一张"公式图"。"""
    gray = img.convert("L").point(lambda v: 255 if v < 235 else 0)
    box = gray.getbbox()
    if not box:
        return img
    left, top, right, bottom = box
    return img.crop((max(0, left - margin), max(0, top - margin),
                     min(img.width, right + margin), min(img.height, bottom + margin)))


# ---------------------------------------------------------------- 缓存
def _key(text: str, color: str, display: bool) -> str:
    return hashlib.sha1(f"{CACHE_VERSION}|{text}|{color}|{display}".encode()).hexdigest()[:24]


def _load(cache_dir: Path) -> dict:
    try:
        data = json.loads((cache_dir / CACHE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("_version") != CACHE_VERSION:
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def _save(cache_dir: Path, state: dict) -> None:
    try:
        (cache_dir / CACHE_FILE).write_text(
            json.dumps({"_version": CACHE_VERSION, **state}, ensure_ascii=False, indent=2),
            encoding="utf-8")
    except OSError:
        pass
