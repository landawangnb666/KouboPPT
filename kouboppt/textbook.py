"""扫描版 PDF 教材 → 带 LaTeX 公式的 Markdown 文本 + 章节切分。

识别策略：有文字层的页直接抽取（快、免流量）；扫描页交给多模态大模型逐页识别。
切分策略：① 按每节页数平均切（余数独立成最后一节）；② 按教材章节切
（优先读输出目录里的 章节结构.json，可手工编辑；其次 PDF 书签；其次 AI 看目录），
节边界对齐目录小节（二级书签 / AI 识别的 sections；无小节的章退回按每节页数均分），
绝不跨章；低于「最短节」页数的节自动并入相邻节。
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf as fitz  # PyMuPDF

from .llm import LLMError

MIN_CHAPTER_PAGES = 2
CHAPTERS_FILE = "章节结构.json"
DEFAULT_PAGES_PER_LESSON = 15


@dataclass
class Chapter:
    """章（或"按每节页数"模式下的一节）；sections 是目录里的二级小节（可选）。

    sections 只在"按教材章节"模式下用来聚合出节边界，节定完即弃，不参与存盘。
    """

    title: str
    start: int          # 1-based，含
    end: int            # 1-based，含
    sections: list["Chapter"] = field(default_factory=list)


def open_pdf(path: Path) -> fitz.Document:
    doc = fitz.open(str(path))
    if doc.page_count == 0:
        raise ValueError("PDF 没有任何页面")
    return doc


def page_count(doc: fitz.Document) -> int:
    return doc.page_count


def render_page(doc: fitz.Document, page_no: int, out_dir: Path, dpi: int = 170,
                quality: int = 88) -> Path:
    """渲染第 page_no 页（1-based）为 JPEG。同分辨率下比 PNG 小一个数量级，上传更快。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    pix = doc[page_no - 1].get_pixmap(dpi=dpi)
    p = out_dir / f"page_{page_no:04d}.jpg"
    p.write_bytes(pix.tobytes("jpeg", jpg_quality=quality))
    return p


def render_page_small(doc: fitz.Document, page_no: int, out_dir: Path,
                      dpi: int = 150, quality: int = 78) -> Path:
    """渲染小尺寸 JPEG（目录识别专用）：同样一页比 170dpi PNG 小 10~50 倍，
    控制请求体大小，避免接口网关因请求太重而超时（HTTP 524）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    pix = doc[page_no - 1].get_pixmap(dpi=dpi)
    p = out_dir / f"toc_{page_no:04d}.jpg"
    p.write_bytes(pix.tobytes("jpeg", jpg_quality=quality))
    return p


def _text_layer(doc: fitz.Document, page_no: int) -> str:
    return (doc[page_no - 1].get_text("text") or "").strip()


def _norm_secs(secs: list[Chapter], a: int, b: int) -> list[Chapter]:
    """小节归一化：夹取进 [a,b]、按起点排序、重叠时后小节起点后移（让不下则丢弃）。

    幂等：normalize_chapters / AI 解析 / 读缓存共用，往返结果一致。
    """
    out: list[Chapter] = []
    for s in sorted(secs, key=lambda s: (s.start, s.end)):
        sa = max(a, min(s.start, b))
        se = max(sa, min(s.end, b))
        if out and sa <= out[-1].end:
            sa = out[-1].end + 1
            if sa > se:
                continue
        out.append(Chapter(s.title, sa, se))
    return out


# 纯页码式标题：'1'、'12'、'第 3 页'、'Page 5'、'p12'、'IV'（罗马数字）。
_BOOKMARK_PAGE_RE = re.compile(
    r"^(?:第\s*[0-9ivxlcdm]+\s*页|p(?:age)?\.?\s*[0-9]+|[0-9]+|[ivxlcdm]+)$",
    re.IGNORECASE)


def _is_meaningful_bookmark_title(title: str) -> bool:
    """书签标题是否有章节含义：含汉字/英文字母，且不是"纯页码式"标题。

    扫描工具（FreePic2Pdf 等）常给每一页自动塞一条标题就是页码的书签
    （'1'、'2'、'第3页'、'Page 5'、'IV'），这些都不是章节名，
    旧实现用 \\w 匹配会连数字一起放行，从而把垃圾书签误判为有效目录。
    """
    s = (title or "").strip()
    if not s:
        return False
    if _BOOKMARK_PAGE_RE.match(s):
        return False
    return bool(re.search(r"[A-Za-z\u4e00-\u9fff]", s))


def _bookmarks_usable(entries, total: int) -> tuple[bool, str]:
    """书签是否像"真章节书签"。返回 (是否可用, 不可用原因)。

    两类垃圾书签会被判死（任一命中）：
    ① 标题大多是页码：有效标题占比 < 40%；
    ② 疑似逐页页码书签：书签数 ≥ 页数、清一色一级、且平均每 1~2 页一条。
    命中即让 chapters_from_bookmarks 返回 []，由上层回退「AI 看目录」。
    判定只针对这本 PDF 自己的书签，阈值保守（正常人一律走原书签路径）。
    """
    n = len(entries)
    if n == 0:
        return False, "没有书签"
    good = sum(1 for _, t, _ in entries if _is_meaningful_bookmark_title(t))
    if good / n < 0.4:
        return False, f"书签标题多为页码（{n - good}/{n} 条不是章节名）"
    if total > 0 and n >= total:                      # 书签不少于页数 = 疑似每页一条
        if not any(lv >= 2 for lv, _, _ in entries):  # 真目录通常有章/节两级
            pages = sorted({p for _, _, p in entries})
            gaps = [pages[i + 1] - pages[i] for i in range(len(pages) - 1)]
            avg = sum(gaps) / len(gaps) if gaps else 0.0
            if avg <= 1.2:
                return False, (f"书签过密（{n} 条 / {total} 页，平均每 {avg:.1f} 页一条），"
                               "疑似逐页页码书签")
    return True, ""


def _looks_degenerate(chapters: list["Chapter"], total: int) -> bool:
    """「书签」来源的章节结构是否明显退化：整本塌成不到 2 章（且书不短）。

    好书签至少能分出 2 章；归一化后只剩 1 章，基本是逐页页码/坏书签被丢弃后
    残留下来的（如《戏剧艺术概论》塌成 1 章"封底"覆盖全书）。对很短的文档不做
    此判定，避免误伤；只用于书签来源，AI/手工来源一律信任。
    """
    return total >= 30 and len(chapters) < 2


def chapters_from_bookmarks(doc: fitz.Document, log=None) -> list[Chapter]:
    """一级书签 → 章节页码范围，二级书签 → 章内小节。

    书签太少、无页码或明显是"逐页页码书签"时返回 []，让上层回退 AI 看目录。
    """
    raw = [(lv, t.strip(), p) for lv, t, p in doc.get_toc(simple=True) if p > 0]
    usable, reason = _bookmarks_usable(raw, doc.page_count)
    if not usable:
        if log is not None:
            log(f"  ⚠ {reason}，跳过书签，改让 AI 看目录推断章节")
        return []
    entries = [(lv, t, p) for lv, t, p in raw
               if lv <= 2 and _is_meaningful_bookmark_title(t)]
    tops = [(t, p) for lv, t, p in entries if lv == 1]
    if len(tops) < 2:
        return []
    total = doc.page_count
    chapters: list[Chapter] = []
    subs: list[Chapter] = []

    def close(end: int) -> None:
        """把攒着的二级书签归入前一章（章尾暂记为 end），起点型页码补成范围。"""
        if chapters:
            ch = chapters[-1]
            end = max(ch.start, end)
            pages = sorted({s.start for s in subs if ch.start <= s.start <= end})
            secs = []
            for i, p in enumerate(pages):
                e = (pages[i + 1] - 1) if i + 1 < len(pages) else end
                title = next(s.title for s in subs if s.start == p)
                secs.append(Chapter(title, p, min(e, end)))
            ch.sections = _norm_secs(secs, ch.start, end)
        subs.clear()

    for lv, t, p in entries:
        if lv == 1:
            if chapters:
                close(p - 1)
            chapters.append(Chapter(_clean_title(t), max(1, min(p, total)), total))
        else:
            subs.append(Chapter(_clean_title(t), max(1, p), max(1, p)))
    close(total)
    for i, ch in enumerate(chapters):          # 章尾 = 下一章起点 - 1，末章到全书末尾
        ch.end = (chapters[i + 1].start - 1) if i + 1 < len(chapters) else total
        ch.end = max(ch.start, min(ch.end, total))
    return chapters


def _clean_title(t: str) -> str:
    return t.strip()[:60]


def normalize_chapters(chapters: list[Chapter], total: int,
                       log=None) -> list[Chapter]:
    """统一归一化章节范围，幂等（写缓存→读缓存往返结果一致）。

    顺序：夹取/校验 → 丢弃页数过少的碎章 → 重叠时后章起点后移 → 章间缝隙并入前一章
    （章起始页视为权威，绝不回拉）→ 末章补到全书末尾。所有来源（书签/AI/手工 JSON）
    共用同一套规则，因此首次识别与重跑读缓存得到的边界完全相同。

    章之外的页（封面/目录/附录）只有当它落在「章与章之间」或「末章之后」才被补进章节；
    完全位于首章之前的页不会被任何章节覆盖。
    """
    total = max(1, int(total))
    items: list[Chapter] = []
    for c in chapters:
        a = max(1, min(int(c.start), total))
        b = max(a, min(int(c.end), total))
        if b >= a:
            title = _clean_title(c.title) or f"第{len(items) + 1}章"
            items.append(Chapter(title, a, b,
                                 _norm_secs(c.sections, c.start, c.end)))
    items.sort(key=lambda c: (c.start, c.end))
    # 丢弃页数过少的碎章（与书签/AI 路径一致），由此产生的缝隙随后并入前一章
    items = [c for c in items if c.end - c.start + 1 >= MIN_CHAPTER_PAGES]

    norm: list[Chapter] = []
    for c in items:
        if norm and c.start <= norm[-1].end:          # 重叠：后章起点后移，让不下则丢弃
            c.start = norm[-1].end + 1
            if c.start > c.end:
                continue
        norm.append(c)
    for i in range(1, len(norm)):                      # 章间缝隙并入前一章（起点权威，不回拉）
        if norm[i].start > norm[i - 1].end + 1:
            norm[i - 1].end = norm[i].start - 1
    if norm:
        old = norm[-1].end
        norm[-1].end = total                          # 末章补到全书末尾
        if log is not None and old < total:
            log(f"  末章「{norm[-1].title}」补到全书末尾（原 {old} 页 → {total} 页）")
    for ch in norm:                                   # 小节随章边界最终裁剪，越界小节丢弃
        ch.sections = _norm_secs(ch.sections, ch.start, ch.end)
    return norm


# ---------------------------------------------------------------------------
_OCR_SYSTEM = """你是理工科教材扫描识别专家。把图片中的页面内容转写成 Markdown：
1. 标题用 #/##/###；正文忠实转写，不翻译、不总结、不评论。
2. 数学公式用 LaTeX：行内 $...$，独立成行的用 $$...$$（单独占一行）。
3. 表格用 Markdown 表格；化学方程式用 $...$ 包裹。
4. 忽略页眉、页脚、页码；图片无法转写的用（图：简短说明）占位。
5. 课后习题、思考题、复习题这一类小节，标题要照原样转写成 ### 标题行，题干、选项、
   小题编号原样照抄（这几节会被单独抽出来，漏抄就没了）。
6. 只输出转写结果本身。"""


_HEAD_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S")
_EX_KEYWORDS = ("课后习题", "课后练习", "课后思考", "本章习题", "本章练习", "本节习题",
                "习题", "练习与思考", "思考与练习", "思考题", "复习题", "讨论题",
                "问题与思考", "习题解答", "参考答案", "作业")


def _is_exercise_line(line: str) -> bool:
    """这一行是不是"课后习题/思考题"这类小节的标题行（短、含关键词）。"""
    s = re.sub(r"\s+", "", line.strip().strip("#*_>` "))
    if not s or len(s) > 24:
        return False
    return any(k in s for k in _EX_KEYWORDS)


def split_exercises(md: str) -> tuple[str, str]:
    """把识别稿拆成（正文, 课后习题）。

    遇到含关键词的标题行进入习题区，直到下一个标题行结束；习题区总长度太短时
    视为误判，原样返回（返回的习题为空）。只按行切，不改动任何文字。
    """
    body: list[str] = []
    ex: list[str] = []
    in_ex = False
    for line in md.splitlines():
        if _is_exercise_line(line):
            in_ex = True
        elif in_ex and _HEAD_RE.match(line):
            in_ex = False
        (ex if in_ex else body).append(line)
    ex_text = "\n".join(ex).strip()
    if len(ex_text) < 30:
        return md, ""
    return "\n".join(body).strip(), ex_text


class Cancelled(Exception):
    """识别被用户取消。"""


def ocr_chapter(llm, doc: fitz.Document, a: int, b: int, tmpdir: Path,
                log=print, workers: int = 4, cancel=None,
                raw_dir: Path | None = None) -> str:
    """识别 [a,b] 页为 Markdown。文字层够用则直取，扫描页多模态并发识别。

    渲染留在主线程（PyMuPDF 非线程安全），只有网络请求并发；
    结果按页序拼接，输出与串行完全一致。"""
    parts: dict[int, str] = {}
    todo: list[tuple[int, Path]] = []
    for page in range(a, b + 1):
        text = _text_layer(doc, page)
        if len(text) >= 80 and "$" not in text and "\\frac" not in text:
            parts[page] = f"\n<!-- 原书第 {page} 页 -->\n{text}\n"
        else:
            todo.append((page, render_page(doc, page, tmpdir)))

    def one(page: int, img: Path) -> tuple[int, str]:
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        try:
            md = llm.chat(_OCR_SYSTEM, f"这是教材的第 {page} 页，转写为 Markdown。",
                          images=[img], max_tokens=6000,
                          raw_dir=raw_dir, tag=f"识别第{page}页")
        finally:
            img.unlink(missing_ok=True)
        md = re.sub(r"^```(?:markdown)?\s*|\s*```$", "", md.strip())
        log(f"  识别第 {page}/{b} 页（{len(md)} 字符）")
        return page, f"\n<!-- 原书第 {page} 页 -->\n{md}\n"

    n = max(1, min(int(workers or 1), len(todo)))
    if n <= 1:
        for page, img in todo:
            p, md = one(page, img)
            parts[p] = md
    elif todo:
        with ThreadPoolExecutor(n) as pool:
            futs = [pool.submit(one, page, img) for page, img in todo]
            try:
                for f in as_completed(futs):
                    p, md = f.result()
                    parts[p] = md
            except BaseException:
                for f in futs:
                    f.cancel()
                raise
    return "".join(parts[p] for p in sorted(parts)).strip()


# 目录识别请求的降级序列：(看前几页, 渲染 DPI, JPEG 质量)
# 之前一次性发前 12 页 PNG，请求体可达几十 MB、模型读图也慢，容易撞上接口网关
# 100 秒超时（HTTP 524），所以改成低清 JPEG 且页数由多到少逐级重试。
_TOC_ATTEMPTS = ((20, 150, 78), (10, 120, 75), (5, 100, 72), (2, 100, 70))


def _parse_toc_chapters(data, total: int) -> list[Chapter]:
    """把 AI 返回的章结构解析成 Chapter 列表（只做夹取/校验，不做缝隙/碎章处理——
    那些交给 normalize_chapters 统一处理，避免不同来源规则不一致）。"""
    raw = data.get("chapters") if isinstance(data, dict) else data
    chapters: list[Chapter] = []
    for item in raw or []:
        try:
            a = max(1, int(item["start"]))
            b = min(total, int(item["end"]))
        except (KeyError, TypeError, ValueError):
            continue
        if b >= a:
            title = str(item.get("title", f"第{len(chapters) + 1}章")).strip()[:60]
            secs: list[Chapter] = []
            for s in item.get("sections") or []:
                try:
                    sa = max(1, int(s["start"]))
                    sb = min(total, int(s["end"]))
                except (KeyError, TypeError, ValueError):
                    continue
                if sb >= sa:
                    secs.append(Chapter(str(s.get("title", "")).strip()[:60], sa, sb))
            chapters.append(Chapter(title, a, b, secs))
    return chapters


def detect_chapters_llm(llm, doc: fitz.Document, tmpdir: Path, log=print,
                        raw_dir: Path | None = None) -> list[Chapter]:
    """让 AI 看前若干页（含目录）推断各章 PDF 页码范围。"""
    total = doc.page_count
    prompt = ("这些是一本教材 PDF 的前若干页（通常含目录）。目录页码是'印刷页码'，"
              "与 PDF 页码可能有固定偏移。请根据目录推断每一章及章内小节（1.1、1.2…"
              "或一、二…）对应的 PDF 页码范围（1 到 " + str(total) + "）。"
              "小节范围必须落在所属章内。输出 JSON：{\"chapters\": "
              "[{\"title\": \"第1章 ...\", \"start\": 页码, \"end\": 页码, "
              "\"sections\": [{\"title\": \"1.1 ...\", \"start\": 页码, \"end\": 页码}]}]}\n"
              "如果目录里没有小节页码，sections 给空数组即可，不要编造。")
    tried: set[int] = set()
    for n_pages, dpi, quality in _TOC_ATTEMPTS:
        head = min(n_pages, total)
        if head in tried:
            continue
        tried.add(head)
        imgs = [render_page_small(doc, p, tmpdir, dpi=dpi, quality=quality)
                for p in range(1, head + 1)]
        try:
            data = llm.chat_json("你是教材结构分析助手。", prompt,
                                 images=imgs, max_tokens=4000, retries=1,
                                 raw_dir=raw_dir, tag="章节结构")
        except LLMError as exc:
            log(f"  ⚠ 看前 {head} 页识别失败（{exc}）"
                + ("；换更小的请求重试…" if head > min(_TOC_ATTEMPTS[-1][0], total) else ""))
            continue
        finally:
            for im in imgs:
                im.unlink(missing_ok=True)
        chapters = _parse_toc_chapters(data, total)
        if chapters and head < _TOC_ATTEMPTS[0][0]:
            log(f"  ⚠ 章节结构只根据前 {head} 页目录推断，可能不全——请检查 "
                f"{CHAPTERS_FILE}，缺的章可手工补上")
        return chapters
    return []


def _find_chapter(chapters, page: int) -> Chapter | None:
    for ch in chapters:
        if ch.start <= page <= ch.end:
            return ch
    return None


def split_by_pages(total: int, start_page: int = 1,
                   per: int = DEFAULT_PAGES_PER_LESSON,
                   chapters=(), log=None) -> list[Chapter]:
    """从 start_page 起每 per 页一节课，余数独立成最后一节。

    chapters 只用于给节标题附上章名（可为空）。
    """
    a = int(start_page or 1)
    per = max(1, int(per or DEFAULT_PAGES_PER_LESSON))
    if a < 1:
        raise ValueError("起始页请从 1 开始")
    if a > total:
        raise ValueError(f"起始页 {a} 超出 PDF 总页数 {total}")
    out: list[Chapter] = []
    while a <= total:
        b = min(a + per - 1, total)
        ch = _find_chapter(chapters, a)
        title = f"第{len(out)+1}课 p{a}-p{b}"
        if ch is not None:
            title += f"（{ch.title}）"
        out.append(Chapter(title, a, b))
        if log is not None:
            crossed = _find_chapter(chapters, b)
            if ch is not None and crossed is not None and crossed is not ch:
                log(f"  ⚠ {title} 跨过了章边界（{ch.title} → {crossed.title}），"
                    "想不跨章请改用「按教材章节」")
        a = b + 1
    return out


def split_chapters(chapters: list[Chapter], per: int = DEFAULT_PAGES_PER_LESSON,
                   min_pages: int = 0) -> list[Chapter]:
    """章节内部切分成节，绝不跨章。

    有小节目录的章：沿小节边界切小块，相邻小块凑成每节 ≤ per 页；
    章首没被小节覆盖的页并入第一小块；单小块超过 per 页时独占一节
    （一个知识点不被拦腰切断），标题加（i/n）后缀。
    没有小节信息的章：退回按 per 页均分（旧规则）。
    min_pages > 0 时，节聚合完再做一轮短节合并：
    第一节并入第二节、最后一节并入上一节；整章只剩一节仍短时，
    短章并入下一章（末章并入上一章）；标题跟页数多的那节走。
    """
    per = max(1, int(per or DEFAULT_PAGES_PER_LESSON))
    min_pages = max(0, int(min_pages or 0))
    out: list[Chapter] = []
    for ch in chapters:
        blocks = _lesson_blocks(ch, per)
        lessons = _aggregate_blocks(blocks, per)
        if min_pages and len(lessons) > 1:
            lessons = _merge_short(lessons, min_pages)
        if not ch.sections:                     # 均分章：合并后按序重新编号
            for k, les in enumerate(lessons, 1):
                les.title = ch.title if len(lessons) == 1 else f"{ch.title} 第{k}节"
        out.extend(lessons)
    if min_pages and len(out) > 1:              # 跨章：整章只有一节且短 → 并邻章
        out = _merge_short(out, min_pages)
    return out


def _lesson_blocks(ch: Chapter, per: int) -> list[tuple[str, int, int]]:
    """章内切成最小可分块：有小节沿小节边界切，无小节按 per 页均分。"""
    if ch.sections:
        cuts = sorted({s.start for s in ch.sections if ch.start < s.start <= ch.end})
        starts = [ch.start] + cuts
        by_start = {s.start: s for s in ch.sections}
        blocks = []
        for i, a in enumerate(starts):
            b = (starts[i + 1] - 1) if i + 1 < len(starts) else ch.end
            blocks.append(((by_start.get(a) or ch.sections[0]).title, a, b))
        if not by_start.get(ch.start) and len(blocks) > 1:
            t, _, b1 = blocks[1]                # 章首未被小节覆盖的页并入第一小节
            blocks[1] = (t, ch.start, b1)
            blocks = blocks[1:]
        out = []
        for t, a, b in blocks:
            out.extend(_oversize(t, a, b, per))
        return out
    n_parts = max(1, -(-(ch.end - ch.start + 1) // per))
    return [(f"{ch.title} 第{k + 1}节" if n_parts > 1 else ch.title,
             ch.start + k * per, min(ch.start + k * per + per - 1, ch.end))
            for k in range(n_parts)]


def _oversize(title: str, a: int, b: int, per: int) -> list[tuple[str, int, int]]:
    """超过 per 页的块均分成多份，标题加（i/n）后缀。"""
    if b - a + 1 <= per:
        return [(title, a, b)]
    parts = -(-(b - a + 1) // per)
    step = -(-(b - a + 1) // parts)
    out = []
    k = a
    for i in range(parts):
        e = min(k + step - 1, b)
        out.append((f"{title}（{i + 1}/{parts}）" if parts > 1 else title, k, e))
        k = e + 1
    return out


def _aggregate_blocks(blocks: list[tuple[str, int, int]], per: int) -> list[Chapter]:
    """相邻小块凑成每节 ≤ per 页；节边界=块边界=小节边界，标题取首块。"""
    lessons: list[Chapter] = []
    cur: tuple[str, int, int] | None = None
    for t, a, b in blocks:
        if cur is not None and b - cur[1] + 1 <= per:
            cur = (cur[0], cur[1], b)
        else:
            if cur is not None:
                lessons.append(Chapter(cur[0], cur[1], cur[2]))
            cur = (t, a, b)
    if cur is not None:
        lessons.append(Chapter(cur[0], cur[1], cur[2]))
    return lessons


def _merge_short(lessons: list[Chapter], min_pages: int) -> list[Chapter]:
    """不足 min_pages 的短节就地合并：第一节向后并，其余（含末节）向前并；
    标题跟页数多的那节走（平手取前者）。反复扫描直到没有短节。
    兼用于章内合并与跨章合并（整章一节且短 → 并入邻章）。"""

    def pg(c: Chapter) -> int:
        return c.end - c.start + 1

    ls = list(lessons)
    changed = True
    while changed and len(ls) > 1:
        changed = False
        for i, c in enumerate(ls):
            if pg(c) >= min_pages:
                continue
            if i == 0:
                o = ls[1]
                keep = c if pg(c) >= pg(o) else o
                ls[1] = Chapter(keep.title, c.start, o.end)
                del ls[0]
            else:
                o = ls[i - 1]
                keep = o if pg(o) >= pg(c) else c
                ls[i - 1] = Chapter(keep.title, o.start, c.end)
                del ls[i]
            changed = True
            break
    return ls


# ------------------------------------------------- 章节结构缓存（章节结构.json）
def chapter_structure_path(book_dir: Path) -> Path:
    return Path(book_dir) / CHAPTERS_FILE


def _save_secs(ch: Chapter) -> dict:
    return {"title": ch.title, "start": ch.start, "end": ch.end}


def save_chapter_structure(book_dir: Path, chapters: list[Chapter], source: str,
                           total: int, log=None) -> Path:
    p = chapter_structure_path(book_dir)
    data = {
        "version": 2,
        "source": source,                       # bookmarks / ai / manual
        "total_pages": int(total),
        "chapters": [{**_save_secs(c), "sections": [_save_secs(s) for s in c.sections]}
                     for c in chapters],
    }
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    if log is not None:
        n_secs = sum(len(c.sections) for c in chapters)
        log(f"  章节结构已保存：{CHAPTERS_FILE}（{len(chapters)} 章"
            + (f"、{n_secs} 小节" if n_secs else "") + f"，来源 {source}；"
            "可手工编辑，删掉它则重新识别）")
    return p


def load_chapter_structure(book_dir: Path, total: int, log=None
                           ) -> tuple[list[Chapter], str] | None:
    """读 章节结构.json；不存在/读不出/没有可用章节时返回 None（不抛异常）。"""
    p = chapter_structure_path(book_dir)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        raw = data.get("chapters") if isinstance(data, dict) else None
        if not isinstance(raw, list) or not raw:
            raise ValueError("chapters 为空")
        items: list[Chapter] = []
        for it in raw:
            a, b = int(it["start"]), int(it["end"])
            if a < 1 or b < a or a > total:
                continue
            secs: list[Chapter] = []
            for s in it.get("sections") or []:
                try:
                    sa, sb = int(s["start"]), int(s["end"])
                except (KeyError, TypeError, ValueError):
                    continue
                if 1 <= sa <= sb <= total:
                    secs.append(Chapter(str(s.get("title") or "").strip()[:60],
                                        sa, min(sb, total)))
            items.append(Chapter(str(it.get("title") or "").strip()[:60] or f"第{len(items)+1}章",
                                 a, min(b, total), secs))
        if not items:
            raise ValueError("没有可用的章节条目")
    except Exception as e:
        if log is not None:
            log(f"  ⚠ {CHAPTERS_FILE} 读不出来（{e}），已忽略并重新识别")
        return None
    items.sort(key=lambda c: (c.start, c.end))
    norm = normalize_chapters(items, total, log)
    if not norm:
        if log is not None:
            log(f"  ⚠ 归一化后没有任何可用章节（各章都不足 {MIN_CHAPTER_PAGES} 页？）")
        return None
    src = str((data.get("source") if isinstance(data, dict) else None) or "manual")
    # 缓存自愈：早期"逐页页码书签"被误用后落盘的退化结构，不该一直被沿用，
    # 否则用户永远走不到重新识别。只否决 bookmarks 来源；AI/手工一律信任。
    if src == "bookmarks" and _looks_degenerate(norm, total):
        if log is not None:
            log(f"  ⚠ {CHAPTERS_FILE} 来源为书签且结果退化（归一化后只剩 {len(norm)} 章），"
                "已忽略并重新识别")
        return None
    if log is not None:
        if norm[0].start > 1:
            log(f"  第 1-{norm[0].start - 1} 页（封面/目录等）不在任何章节内，跳过")
        log(f"  沿用 {CHAPTERS_FILE}（{len(norm)} 章，来源 {src}；删掉它可重新识别）")
    return norm, src


def ensure_chapter_structure(llm, doc: fitz.Document, book_dir: Path, tmpdir: Path,
                             log=print) -> tuple[list[Chapter], str]:
    """拿每章页码范围：缓存文件 → PDF 书签 → AI 看目录；命中后落盘缓存。"""
    total = page_count(doc)
    loaded = load_chapter_structure(book_dir, total, log)
    if loaded is not None:
        return loaded
    chapters = chapters_from_bookmarks(doc, log)
    source = "bookmarks"
    if chapters:
        # 书签质量校验之后再加一道"结果合理性"校验：好书签不会整本塌成 1 章。
        probe = normalize_chapters(
            [Chapter(c.title, c.start, c.end, list(c.sections)) for c in chapters],
            total, log=None)
        if _looks_degenerate(probe, total):
            log(f"  ⚠ 书签识别结果退化（归一化后只剩 {len(probe)} 章覆盖全书），"
                "视为无效书签，改让 AI 看目录…")
            chapters = []
        else:
            log(f"  从 PDF 书签识别出 {len(chapters)} 章")
    if not chapters:
        log("  让 AI 看前 20 页目录推断章节（低清缩图，超时会自动换更小的请求）…")
        chapters = detect_chapters_llm(llm, doc, tmpdir, log, raw_dir=book_dir)
        source = "ai"
    if not chapters:
        raise ValueError(
            "没能识别出章节结构。可改用「按每节页数」切分，或在输出目录手工编写 "
            f'{CHAPTERS_FILE}（{{"chapters":[{{"title":"第1章","start":1,"end":30}}]}}）后重跑')
    chapters = normalize_chapters(chapters, total, log)
    if not chapters:
        raise ValueError(
            "识别出的章节都太短（每章至少需 2 页）。可改用「按每节页数」切分，或在输出目录"
            f"手工编写 {CHAPTERS_FILE} 后重跑")
    save_chapter_structure(book_dir, chapters, source, total, log)
    return chapters, source
